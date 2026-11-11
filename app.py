"""
ORA Glass ERP - quoting, customers, and accounts.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import click
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from quoting import (
    allowed_tier_ids,
    calculate_price_sqf,
    classify,
    default_quote_tier,
    load_pricing,
    resolve_effective_tier,
    sealant_match_map,
    tier_for_request,
)
from seed_data import apply_seed, new_user
from security import (
    attach_invite,
    hash_password,
    invite_matches,
    is_development,
    login_limiter,
    normalize_email,
    password_is_strong,
    resolve_secret_key,
    session_cookie_secure,
    trust_proxy,
    valid_email,
    verify_password,
)
from storage import (
    append_quote,
    data_dir,
    load_customers,
    load_quotes,
    load_tiers,
    load_users,
    ordered_tiers,
    save_customers,
    save_quotes,
    save_tiers,
    save_users,
)

logger = logging.getLogger('ora_glass')

ROLES = {
    'admin': {
        'name': 'Administrator',
        'modules': ['quotes', 'quote-history', 'orders', 'customers', 'invoices', 'financial', 'inventory', 'pricing', 'users'],
    },
    'sales': {
        'name': 'Sales',
        'modules': ['quotes', 'quote-history', 'orders', 'customers', 'invoices'],
    },
    'production': {
        'name': 'Production',
        'modules': ['orders', 'inventory'],
    },
    'viewer': {
        'name': 'Viewer',
        'modules': ['quotes', 'quote-history', 'orders'],
    },
    'customer': {
        'name': 'Customer',
        'modules': ['quotes', 'quote-history', 'orders', 'invoices', 'financial'],
    },
}

PASSWORD_CHANGE_ENDPOINTS = {'change_password', 'logout'}
ID_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62})$')

app = Flask(__name__)
app.secret_key = resolve_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=session_cookie_secure(),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    WTF_CSRF_TIME_LIMIT=None,
)
csrf = CSRFProtect(app)
if trust_proxy():
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

PRICING, PRICING_SOURCE = load_pricing()
GLASS, SPACERS, SEALANTS = classify(PRICING)
logger.info('Loaded %s pricing rows from %s', len(PRICING), PRICING_SOURCE or 'built-in fallback')

_state = {'bootstrapped': False}


def reset_runtime_state():
    _state['bootstrapped'] = False
    login_limiter.reset()


def json_error(message, status):
    return jsonify({'error': message}), status


def wants_json():
    return request.path.startswith('/api/') or request.is_json


def unauthenticated():
    if wants_json():
        return json_error('Authentication required', 401)
    target = request.path if request.path.startswith('/') and not request.path.startswith('//') else None
    return redirect(url_for('login', next=target))


def forbidden():
    if wants_json():
        return json_error('Access denied', 403)
    flash('Access denied.', 'error')
    return redirect(url_for('dashboard'))


def current_user():
    email = session.get('user')
    if not email:
        return None
    user = load_users().get(email)
    if not user or not user.get('active', True):
        return None
    return user


def user_has_module(user, module):
    role = ROLES.get((user or {}).get('role') or '')
    return bool(role) and module in role['modules']


def _enforce(module=None, admin=False):
    user = current_user()
    if not user:
        return None, unauthenticated()
    if user.get('must_change_password') and request.endpoint not in PASSWORD_CHANGE_ENDPOINTS:
        if wants_json():
            return None, json_error('Password change required', 403)
        return None, redirect(url_for('change_password'))
    if admin and user.get('role') != 'admin':
        return None, forbidden()
    if module and not user_has_module(user, module):
        return None, forbidden()
    return user, None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        _user, response = _enforce()
        if response is not None:
            return response
        return view(*args, **kwargs)
    return wrapped


def module_required(module):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            _user, response = _enforce(module=module)
            if response is not None:
                return response
            return view(*args, **kwargs)
        return wrapped
    return decorator


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        _user, response = _enforce(admin=True)
        if response is not None:
            return response
        return view(*args, **kwargs)
    return wrapped


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


def clean_text(value, limit):
    return str(value or '').replace('\x00', '').strip()[:limit]


def slugify(value):
    text = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().lower()).strip('-')
    return text[:64]


def new_customer_id(name, customers):
    base = slugify(name) or 'customer'
    candidate = base
    suffix = 2
    while candidate in customers:
        candidate = f'{base[:50]}-{suffix}'
        suffix += 1
    return candidate


def safe_next(url):
    if not url or not isinstance(url, str):
        return None
    if not url.startswith('/') or url.startswith('//') or '\\' in url:
        return None
    return url


def external_base():
    configured = os.environ.get('BASE_URL', '').strip().rstrip('/')
    if configured:
        return configured
    return request.host_url.rstrip('/')


def setup_url_for(token):
    return external_base() + url_for('setup_password', token=token)


def scope_note(user=None):
    user = current_user() if user is None else user
    if not user or user.get('role') != 'customer':
        return ''
    customer = load_customers().get(user.get('customer_id') or '')
    if not customer:
        return ''
    return f"Showing records for {customer['name']}."


def effective_tier_for(user, customers=None, tiers=None):
    customers = load_customers() if customers is None else customers
    tiers = load_tiers() if tiers is None else tiers
    return resolve_effective_tier(user, customers, tiers)


def tier_label(tier, tiers=None):
    tiers = load_tiers() if tiers is None else tiers
    return (tiers.get(tier) or {}).get('label') or tier


def public_customer(customer_id, customer, tiers, user_count):
    return {
        'id': customer_id,
        'name': customer.get('name') or '',
        'domain': customer.get('domain') or '',
        'address': customer.get('address') or '',
        'city': customer.get('city') or '',
        'state': customer.get('state') or '',
        'zip': customer.get('zip') or '',
        'phone': customer.get('phone') or '',
        'email': customer.get('email') or '',
        'credit_approved': bool(customer.get('credit_approved')),
        'payment_terms': customer.get('payment_terms') or 'Net 30',
        'tier': customer.get('tier') or '',
        'tier_label': tier_label(customer.get('tier'), tiers),
        'status': customer.get('status') or 'active',
        'user_count': user_count,
    }


def public_user(user, customers, tiers):
    customer_id = user.get('customer_id')
    customer = customers.get(customer_id) if customer_id else None
    effective = effective_tier_for(user, customers, tiers)
    allowed = allowed_tier_ids(user, customers, tiers)
    return {
        'email': user.get('email'),
        'name': user.get('name') or '',
        'role': user.get('role'),
        'customer_id': customer_id,
        'company': customer.get('name') if customer else None,
        'company_tier': customer.get('tier') if customer else None,
        'company_tier_label': tier_label(customer.get('tier'), tiers) if customer else None,
        'tier_override': user.get('tier_override'),
        'effective_tier': effective,
        'effective_tier_label': tier_label(effective, tiers),
        'allowed_tiers': allowed,
        'allowed_tier_labels': [tier_label(tid, tiers) for tid in allowed],
        'active': bool(user.get('active', True)),
        'must_change_password': bool(user.get('must_change_password')),
        'has_password': bool(user.get('password_hash')),
        'invite_pending': bool(user.get('invite_token_hash')),
    }


def apply_allowed_tiers(record, data, customers, tiers):
    """Store an explicit allowed-tier list. The company tier is included only when it is sent."""
    if 'allowed_tiers' not in data:
        return None
    raw = data.get('allowed_tiers')
    if not isinstance(raw, list):
        return 'Allowed tiers must be a list.'
    cleaned = []
    for item in raw:
        tid = clean_text(item, 20)
        if not tid:
            continue
        if tid not in tiers:
            return 'Unknown pricing tier.'
        if tid not in cleaned:
            cleaned.append(tid)
    if not cleaned:
        return 'Choose at least one allowed tier.'
    override = record.get('tier_override') or ''
    customer = (customers or {}).get(record.get('customer_id') or '') or {}
    company_tier = customer.get('tier') or ''
    if override:
        if override not in cleaned:
            return "This user's tier must be one of the allowed tiers."
    elif company_tier:
        if company_tier not in cleaned:
            return 'The company tier is not allowed. Choose one of the allowed tiers for this user.'
    else:
        fallback = 'T2' if 'T2' in tiers else next(iter(tiers), '')
        if fallback not in cleaned:
            return "This user's tier must be one of the allowed tiers."
    draft = dict(record)
    draft['allowed_tiers'] = cleaned
    record['allowed_tiers'] = allowed_tier_ids(draft, customers, tiers)
    return None


def customer_user_counts(users=None):
    users = load_users() if users is None else users
    counts = {}
    for user in users.values():
        customer_id = user.get('customer_id')
        if customer_id:
            counts[customer_id] = counts.get(customer_id, 0) + 1
    return counts


def quotes_visible_to(user, quotes):
    role = user.get('role')
    if role == 'customer':
        customer_id = user.get('customer_id')
        visible = []
        for quote in quotes:
            if quote.get('customer_id') == customer_id:
                visible.append(quote)
            elif not quote.get('customer_id') and quote.get('user') == user.get('email'):
                visible.append(quote)
        return visible
    if role in {'admin', 'sales', 'viewer'}:
        return list(quotes)
    return [quote for quote in quotes if quote.get('user') == user.get('email')]


def blank_customer(customer_id, name, tier):
    return {
        'id': customer_id,
        'name': name,
        'domain': '',
        'address': '',
        'city': '',
        'state': '',
        'zip': '',
        'phone': '',
        'email': '',
        'credit_approved': False,
        'payment_terms': 'Net 30',
        'tier': tier,
        'status': 'active',
    }


def apply_customer_fields(customer, data, tiers, creating):
    if creating or 'name' in data:
        name = clean_text(data.get('name'), 200)
        if not name:
            return 'Company name is required.'
        customer['name'] = name
    if creating or 'domain' in data:
        domain = clean_text(data.get('domain'), 253).lower()
        domain = domain.replace('https://', '').replace('http://', '').strip().strip('/')
        customer['domain'] = domain
    for field, limit in (
        ('address', 300),
        ('city', 80),
        ('state', 80),
        ('zip', 20),
        ('phone', 40),
    ):
        if creating or field in data:
            customer[field] = clean_text(data.get(field), limit)
    if creating or 'email' in data:
        email = normalize_email(data.get('email'))
        if email and not valid_email(email):
            return 'Company email is invalid.'
        customer['email'] = email
    if creating or 'payment_terms' in data:
        terms = clean_text(data.get('payment_terms') or 'Net 30', 40)
        customer['payment_terms'] = terms or 'Net 30'
    if creating or 'credit_approved' in data:
        customer['credit_approved'] = bool(data.get('credit_approved'))
    if creating or 'tier' in data:
        tier = clean_text(data.get('tier'), 20)
        if tier not in tiers:
            return 'Unknown pricing tier.'
        customer['tier'] = tier
    if creating or 'status' in data:
        status = clean_text(data.get('status') or 'active', 20)
        if status not in {'active', 'inactive'}:
            return 'Status must be active or inactive.'
        customer['status'] = status
    return None


def active_admin_count(users, ignore_email=None):
    total = 0
    for email, user in users.items():
        if ignore_email and email == ignore_email:
            continue
        if user.get('role') == 'admin' and user.get('active', True):
            total += 1
    return total


def bootstrap_admin():
    """Create the first admin from ADMIN_EMAIL and ADMIN_PASSWORD. Never resets an existing admin."""
    email = normalize_email(os.environ.get('ADMIN_EMAIL'))
    password = os.environ.get('ADMIN_PASSWORD') or ''
    if not email or not password:
        return 'skipped'
    if not valid_email(email):
        logger.error('ADMIN_EMAIL is not a valid email; admin was not created')
        return 'rejected'
    ok, message = password_is_strong(password)
    if not ok:
        logger.error('ADMIN_PASSWORD was rejected (%s). Admin was not created.', message)
        return 'rejected'
    users = load_users()
    if any(user.get('role') == 'admin' and user.get('password_hash') for user in users.values()):
        return 'exists'
    if email in users and users[email].get('role') != 'admin':
        logger.error('ADMIN_EMAIL %s already exists as %s; not overwriting', email, users[email].get('role'))
        return 'rejected'
    name = clean_text(os.environ.get('ADMIN_NAME') or 'Administrator', 120) or 'Administrator'
    record = users.get(email) or new_user(email=email, name=name, role='admin')
    record['name'] = name
    record['role'] = 'admin'
    record['customer_id'] = None
    record['tier_override'] = None
    record['password_hash'] = hash_password(password)
    record['must_change_password'] = False
    record['active'] = True
    record['invite_token_hash'] = None
    record['invite_expires'] = None
    users[email] = record
    save_users(users)
    logger.warning('Bootstrapped admin %s from ADMIN_EMAIL. Unset ADMIN_PASSWORD after confirming login.', email)
    return 'created'


def bootstrap_once():
    if _state['bootstrapped']:
        return
    os.makedirs(data_dir(), exist_ok=True)
    if os.environ.get('AUTO_SEED', '1') != '0':
        apply_seed(issue_links=False)
    bootstrap_admin()
    _state['bootstrapped'] = True


@app.before_request
def _bootstrap_before_request():
    bootstrap_once()


@app.after_request
def _security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    if response.headers.get('Content-Type', '').startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.context_processor
def inject_user():
    return {'current_user': current_user()}


@app.errorhandler(CSRFError)
def handle_csrf(_error):
    if wants_json():
        return json_error('CSRF validation failed', 400)
    flash('Your session expired. Please try again.', 'error')
    if current_user():
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/')
def index():
    if current_user():
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user() and request.method == 'GET':
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password') or ''
        ip = request.remote_addr or ''
        next_url = safe_next(request.form.get('next') or request.args.get('next')) or ''
        if login_limiter.is_limited(email, ip):
            flash('Too many login attempts. Try again in a few minutes.', 'error')
            return render_template('login.html', next_url=next_url), 429
        users = load_users()
        user = users.get(email)
        if not user or not verify_password(user.get('password_hash'), password):
            login_limiter.record_failure(email, ip)
            logger.info('Failed login for %s', email or '(blank)')
            flash('Invalid credentials', 'error')
            return render_template('login.html', next_url=next_url)
        if not user.get('active', True):
            login_limiter.record_failure(email, ip)
            flash('This account is disabled.', 'error')
            return render_template('login.html', next_url=next_url)
        if user.get('role') == 'customer':
            customer = load_customers().get(user.get('customer_id') or '')
            if not customer or customer.get('status') == 'inactive':
                login_limiter.record_failure(email, ip)
                flash('This account is disabled.', 'error')
                return render_template('login.html', next_url=next_url)
        login_limiter.clear(email, ip)
        session.clear()
        session['user'] = email
        session.permanent = True
        if user.get('must_change_password'):
            return redirect(url_for('change_password'))
        return redirect(next_url or url_for('dashboard'))
    return render_template('login.html', next_url=safe_next(request.args.get('next')) or '')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/setup-password/<token>', methods=['GET', 'POST'])
def setup_password(token):
    user = _user_for_token(token)
    if not user:
        return render_template('setup_password.html', invalid=True), 400
    if request.method == 'POST':
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('setup_password.html', invalid=False, email=user['email'])
        ok, message = password_is_strong(password)
        if not ok:
            flash(message, 'error')
            return render_template('setup_password.html', invalid=False, email=user['email'])
        users = load_users()
        current = users.get(user['email'])
        if not current or not invite_matches(current, token):
            return render_template('setup_password.html', invalid=True), 400
        current['password_hash'] = hash_password(password)
        current['must_change_password'] = False
        current['invite_token_hash'] = None
        current['invite_expires'] = None
        current['active'] = True
        users[current['email']] = current
        save_users(users)
        flash('Password saved. You can sign in.', 'success')
        return redirect(url_for('login'))
    return render_template('setup_password.html', invalid=False, email=user['email'])


def _user_for_token(token):
    now = datetime.now(timezone.utc)
    for user in load_users().values():
        if invite_matches(user, token, now=now):
            return user
    return None


@app.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_password():
    user = current_user()
    if request.method == 'POST':
        current_password = request.form.get('current_password') or ''
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        if not verify_password(user.get('password_hash'), current_password):
            flash('Current password is incorrect.', 'error')
            return render_template('change_password.html', must_change=bool(user.get('must_change_password')))
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('change_password.html', must_change=bool(user.get('must_change_password')))
        if password == current_password:
            flash('Choose a different password.', 'error')
            return render_template('change_password.html', must_change=bool(user.get('must_change_password')))
        ok, message = password_is_strong(password)
        if not ok:
            flash(message, 'error')
            return render_template('change_password.html', must_change=bool(user.get('must_change_password')))
        users = load_users()
        users[user['email']]['password_hash'] = hash_password(password)
        users[user['email']]['must_change_password'] = False
        save_users(users)
        flash('Password updated.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('change_password.html', must_change=bool(user.get('must_change_password')))


@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    role = ROLES.get(user.get('role', ''), {})
    return render_template(
        'dashboard.html',
        user=user,
        role_name=role.get('name', ''),
        modules=role.get('modules', []),
    )


def quote_template_context(user):
    tiers = load_tiers()
    customers = load_customers()
    ordered = ordered_tiers(tiers)
    if user.get('role') == 'customer':
        allowed = set(allowed_tier_ids(user, customers, tiers))
        choices = [(tid, cfg) for tid, cfg in ordered if tid in allowed]
        effective = default_quote_tier(user, customers, tiers)
    else:
        choices = ordered
        effective = effective_tier_for(user, customers, tiers)
    locked = len(choices) <= 1
    locked_id = choices[0][0] if locked and choices else effective
    return {
        'glass': GLASS,
        'spacers': SPACERS,
        'sealants': SEALANTS,
        'can_choose_tier': not locked,
        'tiers': choices,
        'default_tier': effective,
        'locked_tier': locked_id,
        'locked_tier_label': tier_label(locked_id, tiers),
        'sealant_matches': sealant_match_map(SPACERS, SEALANTS),
    }


@app.route('/quotes')
@module_required('quotes')
def quotes():
    return render_template('quotes.html', **quote_template_context(current_user()))


@app.route('/api/calculate', methods=['POST'])
@module_required('quotes')
def calculate():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    user = current_user()
    tiers = load_tiers()
    customers = load_customers()
    tier = tier_for_request(user, clean_text(data.get('tier'), 20), customers, tiers)
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    price = calculate_price_sqf(data, tier, PRICING, tiers)
    return jsonify({'price_sqf': price, 'tier': tier})


@app.route('/api/export-quote', methods=['POST'])
@module_required('quotes')
def export_quote():
    from xml.sax.saxutils import escape
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    items = data.get('items', [])
    if not isinstance(items, list):
        return json_error('Items must be a list', 400)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.4*inch, rightMargin=0.4*inch, topMargin=0.4*inch, bottomMargin=0.4*inch)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('Title', parent=styles['Normal'], fontSize=14, fontName='Helvetica-Bold', alignment=1)
    small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8, leading=10)
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=7, leading=9)
    header_cell = ParagraphStyle('HeaderCell', parent=styles['Normal'], fontSize=7, fontName='Helvetica-Bold', textColor=colors.white)

    elements = []

    logo_path = 'logo.png' if os.path.exists('logo.png') else None
    if logo_path:
        logo = Image(logo_path, width=1.2*inch, height=0.8*inch)
    else:
        logo = Paragraph('<font color="#C41E3A" size="18"><b>ORA</b></font><br/><font size="10">GLASS</font>', small_style)

    project = escape(str(data.get('project', '') or ''))
    project_line = f'<br/><b>Project:</b> {project}' if project else ''
    header_data = [[
        logo,
        Paragraph('<b>Quotation</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title_style),
        Paragraph('<font size="8"><b>Date:</b> ' + date.today().strftime("%m/%d/%Y") + '<br/><b>Time:</b> ' + datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M") + ' PST' + project_line + '</font>', small_style)
    ]]
    header_table = Table(header_data, colWidths=[1.5*inch, 3*inch, 2.5*inch])
    header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements.append(header_table)
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.black))
    elements.append(Spacer(1, 6))

    has_size = any(item.get('sqf') for item in items)
    currency = data.get('currency', 'usd')
    total_style = ParagraphStyle('Total', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold')

    def amount(value, fallback=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    if has_size:
        unit = 'CAD' if currency == 'cad' else 'USD'
        table_data = [[
            Paragraph('<b>ID</b>', header_cell),
            Paragraph('<b>ITEM DESCRIPTION</b>', header_cell),
            Paragraph('<b>SQF</b>', header_cell),
            Paragraph(f'<b>{unit}/SQF</b>', header_cell),
            Paragraph(f'<b>{unit}/PC</b>', header_cell),
            Paragraph(f'<b>TOTAL {unit}</b>', header_cell)
        ]]
        total_sqf = 0
        total_pcs = 0
        total_line_sum = 0
        for i, item in enumerate(items, 1):
            price_sqf = amount(item.get('price_sqf', 0))
            price_pc = amount(item.get('price_pc', 0))
            sqf = amount(item.get('sqf', 0))
            try:
                pcs = int(item.get('pcs', 1) or 1)
            except (TypeError, ValueError):
                pcs = 1
            # Stored line prices are already in the quote currency. Sum them as shown.
            if item.get('total') not in (None, ''):
                line_total = amount(item.get('total'))
            else:
                line_total = price_pc * pcs
            total_sqf += sqf * pcs
            total_pcs += pcs
            total_line_sum += line_total
            table_data.append([
                Paragraph(f'({i})', cell_style),
                Paragraph(escape(str(item.get('name', '') or '')), cell_style),
                Paragraph(f"{sqf * pcs:.2f}", cell_style),
                Paragraph(f"${price_sqf:.2f}", cell_style),
                Paragraph(f"${price_pc:.2f}", cell_style),
                Paragraph(f"${line_total:.2f}", cell_style)
            ])
        table_data.append([
            Paragraph('', cell_style),
            Paragraph(f'<b>TOTAL ({total_pcs} pcs)</b>', total_style),
            Paragraph(f'<b>{total_sqf:.2f}</b>', total_style),
            Paragraph('', cell_style),
            Paragraph('', cell_style),
            Paragraph(f'<b>${total_line_sum:.2f}</b>', total_style)
        ])
        col_widths = [0.4*inch, 3.5*inch, 0.7*inch, 0.9*inch, 0.9*inch, 1*inch]
    else:
        curr_label = 'CAD/SQF' if currency == 'cad' else 'USD/SQF'
        table_data = [[
            Paragraph('<b>ID</b>', header_cell),
            Paragraph('<b>ITEM DESCRIPTION</b>', header_cell),
            Paragraph(f'<b>{curr_label}</b>', header_cell)
        ]]
        total_price = 0.0
        for i, item in enumerate(items, 1):
            price_value = amount(item.get('price', 0))
            total_price += price_value
            price = escape(f'{price_value:.2f}')
            table_data.append([
                Paragraph(f'({i})', cell_style),
                Paragraph(escape(str(item.get('name', '') or '')), cell_style),
                Paragraph(f"${price}", cell_style)
            ])
        table_data.append([
            Paragraph('', cell_style),
            Paragraph('<b>TOTAL</b>', total_style),
            Paragraph(f'<b>${total_price:.2f}</b>', total_style),
        ])
        col_widths = [0.4*inch, 5.5*inch, 1.1*inch]

    items_table = Table(table_data, colWidths=col_widths)
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C41E3A')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 20))

    elements.append(Spacer(1, 10))
    elements.append(Paragraph('<font size="7">Ora Glass Inc Terms And Conditions of Sale apply.</font>', small_style))

    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='ORA_Quote.pdf')


@app.route('/orders')
@module_required('orders')
def orders():
    return render_template('module.html', module='Orders', note=scope_note())


@app.route('/customers')
@module_required('customers')
def customers_page():
    user = current_user()
    return render_template(
        'customers.html',
        can_manage=user.get('role') == 'admin',
        tiers=ordered_tiers(),
    )


@app.route('/inventory')
@module_required('inventory')
def inventory():
    return render_template('module.html', module='Inventory', note='')


@app.route('/pricing', methods=['GET', 'POST'])
@module_required('pricing')
def pricing_admin():
    if current_user().get('role') != 'admin':
        return forbidden()
    tiers = load_tiers()
    if request.method == 'POST':
        errors = []
        for key, cfg in list(tiers.items()):
            glass_raw = request.form.get(f'glass_{key}', '')
            spacer_raw = request.form.get(f'spacer_{key}', '')
            label = clean_text(request.form.get(f'label_{key}'), 40)
            try:
                glass = float(glass_raw)
                spacer = float(spacer_raw)
            except (TypeError, ValueError):
                errors.append(key)
                continue
            if not label or glass < 0 or spacer < 0 or glass > 2000 or spacer > 2000:
                errors.append(key)
                continue
            cfg['label'] = label
            cfg['glass_markup'] = round(glass / 100.0, 6)
            cfg['spacer_markup'] = round(spacer / 100.0, 6)
        if errors:
            flash('Check markup values for: ' + ', '.join(errors), 'error')
        else:
            save_tiers(tiers)
            flash('Tier markups saved.', 'success')
            return redirect(url_for('pricing_admin'))
        tiers = load_tiers()
    return render_template('pricing.html', tiers=ordered_tiers(tiers))


@app.route('/users')
@module_required('users')
def users_page():
    if current_user().get('role') != 'admin':
        return forbidden()
    return render_template('users.html', tiers=ordered_tiers(), roles=ROLES)


@app.route('/quote-history')
@module_required('quote-history')
def quote_history():
    return render_template('quote_history.html', note=scope_note())


@app.route('/invoices')
@module_required('invoices')
def invoices():
    return render_template('invoices.html', note=scope_note())


@app.route('/financial')
@module_required('financial')
def financial():
    return render_template('financial.html', note=scope_note())


@app.route('/api/customers', methods=['GET'])
@module_required('customers')
def get_customers():
    user = current_user()
    tiers = load_tiers()
    customers = load_customers()
    counts = customer_user_counts()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id)
        if not customer:
            return jsonify([])
        return jsonify([public_customer(customer_id, customer, tiers, counts.get(customer_id, 0))])
    rows = [
        public_customer(customer_id, customer, tiers, counts.get(customer_id, 0))
        for customer_id, customer in customers.items()
    ]
    rows.sort(key=lambda row: row['name'].lower())
    return jsonify(rows)


@app.route('/api/customers', methods=['POST'])
@admin_required
def add_customer():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    tiers = load_tiers()
    customers = load_customers()
    name = clean_text(data.get('name'), 200)
    if not name:
        return json_error('Company name is required.', 400)
    customer_id = new_customer_id(name, customers)
    customer = blank_customer(customer_id, name, 'T2')
    error = apply_customer_fields(customer, data, tiers, creating=True)
    if error:
        return json_error(error, 400)
    customer['id'] = customer_id
    customers[customer_id] = customer
    save_customers(customers)
    return jsonify({'success': True, 'id': customer_id})


@app.route('/api/customers/<cid>', methods=['PUT'])
@admin_required
def update_customer(cid):
    if not ID_RE.fullmatch(cid or ''):
        return json_error('Not found', 404)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    customers = load_customers()
    customer = customers.get(cid)
    if not customer:
        return json_error('Not found', 404)
    error = apply_customer_fields(customer, data, load_tiers(), creating=False)
    if error:
        return json_error(error, 400)
    customer['id'] = cid
    customers[cid] = customer
    save_customers(customers)
    return jsonify({'success': True})


@app.route('/api/customers/<cid>', methods=['DELETE'])
@admin_required
def delete_customer(cid):
    if not ID_RE.fullmatch(cid or ''):
        return json_error('Not found', 404)
    customers = load_customers()
    if cid not in customers:
        return json_error('Not found', 404)
    if customer_user_counts().get(cid):
        return json_error("Reassign or delete this customer's users first.", 409)
    del customers[cid]
    save_customers(customers)
    return jsonify({'success': True})


@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    customers = load_customers()
    tiers = load_tiers()
    rows = [public_user(user, customers, tiers) for user in load_users().values()]
    rows.sort(key=lambda row: (row.get('company') or '', row.get('email') or ''))
    return jsonify(rows)


@app.route('/api/users', methods=['POST'])
@admin_required
def add_user():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    email = normalize_email(data.get('email'))
    if not valid_email(email):
        return json_error('A valid email is required.', 400)
    users = load_users()
    if email in users:
        return json_error('A user with that email already exists.', 409)
    name = clean_text(data.get('name'), 120)
    if not name:
        return json_error('Name is required.', 400)
    role = clean_text(data.get('role'), 20)
    if role not in ROLES:
        return json_error('Unknown role.', 400)
    customer_id = clean_text(data.get('customer_id'), 64) or None
    if role == 'customer' and not customer_id:
        return json_error('Customer users must be linked to a company.', 400)
    if customer_id:
        if not ID_RE.fullmatch(customer_id) or customer_id not in load_customers():
            return json_error('Unknown customer.', 400)
    tier_override = clean_text(data.get('tier_override'), 20) or None
    tiers = load_tiers()
    if tier_override and tier_override not in tiers:
        return json_error('Unknown pricing tier.', 400)
    customers = load_customers()
    record = new_user(
        email=email,
        name=name,
        role=role,
        customer_id=customer_id,
        tier_override=tier_override,
    )
    allowed_error = apply_allowed_tiers(record, data, customers, tiers)
    if allowed_error:
        return json_error(allowed_error, 400)
    setup_url = None
    password = data.get('password') or ''
    if password:
        ok, message = password_is_strong(password)
        if not ok:
            return json_error(message, 400)
        record['password_hash'] = hash_password(password)
        temporary = data.get('temporary', True)
        record['must_change_password'] = bool(temporary)
    else:
        token = attach_invite(record)
        setup_url = setup_url_for(token)
        logger.info('Issued password setup link for %s', email)
    users[email] = record
    save_users(users)
    payload = {'success': True, 'email': email}
    if setup_url:
        payload['setup_url'] = setup_url
        payload['expires_in_days'] = 7
    return jsonify(payload)


@app.route('/api/users/<path:email>', methods=['PUT'])
@admin_required
def update_user(email):
    email = normalize_email(email)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    users = load_users()
    user = users.get(email)
    if not user:
        return json_error('Not found', 404)
    if 'name' in data:
        name = clean_text(data.get('name'), 120)
        if not name:
            return json_error('Name is required.', 400)
        user['name'] = name
    if 'role' in data:
        role = clean_text(data.get('role'), 20)
        if role not in ROLES:
            return json_error('Unknown role.', 400)
        if user.get('role') == 'admin' and role != 'admin' and active_admin_count(users) <= 1:
            return json_error('The last administrator cannot be demoted.', 400)
        user['role'] = role
    if 'customer_id' in data:
        customer_id = clean_text(data.get('customer_id'), 64) or None
        if customer_id:
            if not ID_RE.fullmatch(customer_id) or customer_id not in load_customers():
                return json_error('Unknown customer.', 400)
        user['customer_id'] = customer_id
    if 'tier_override' in data:
        tier_override = clean_text(data.get('tier_override'), 20) or None
        if tier_override and tier_override not in load_tiers():
            return json_error('Unknown pricing tier.', 400)
        user['tier_override'] = tier_override
    allowed_error = apply_allowed_tiers(user, data, load_customers(), load_tiers())
    if allowed_error:
        return json_error(allowed_error, 400)
    if 'active' in data:
        active = bool(data.get('active'))
        if user.get('role') == 'admin' and not active and active_admin_count(users) <= 1:
            return json_error('The last administrator cannot be disabled.', 400)
        user['active'] = active
    if user.get('role') == 'customer' and not user.get('customer_id'):
        return json_error('Customer users must be linked to a company.', 400)
    users[email] = user
    save_users(users)
    return jsonify({'success': True})


@app.route('/api/users/<path:email>', methods=['DELETE'])
@admin_required
def delete_user(email):
    email = normalize_email(email)
    actor = current_user()
    if email == actor.get('email'):
        return json_error('You cannot delete your own account.', 400)
    users = load_users()
    user = users.get(email)
    if not user:
        return json_error('Not found', 404)
    if user.get('role') == 'admin' and active_admin_count(users) <= 1:
        return json_error('The last administrator cannot be deleted.', 400)
    del users[email]
    save_users(users)
    return jsonify({'success': True})


@app.route('/api/users/<path:email>/invite', methods=['POST'])
@admin_required
def invite_user(email):
    email = normalize_email(email)
    users = load_users()
    user = users.get(email)
    if not user:
        return json_error('Not found', 404)
    token = attach_invite(user)
    users[email] = user
    save_users(users)
    logger.info('Issued password setup link for %s', email)
    return jsonify({
        'success': True,
        'email': email,
        'setup_url': setup_url_for(token),
        'expires_in_days': 7,
    })


@app.route('/api/users/<path:email>/password', methods=['POST'])
@admin_required
def set_temporary_password(email):
    email = normalize_email(email)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    users = load_users()
    user = users.get(email)
    if not user:
        return json_error('Not found', 404)
    password = data.get('password') or ''
    ok, message = password_is_strong(password)
    if not ok:
        return json_error(message, 400)
    user['password_hash'] = hash_password(password)
    user['must_change_password'] = True
    user['invite_token_hash'] = None
    user['invite_expires'] = None
    users[email] = user
    save_users(users)
    return jsonify({'success': True, 'must_change_password': True})


@app.route('/api/quotes', methods=['POST'])
@module_required('quotes')
def save_quote():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    items = data.get('items', [])
    if not isinstance(items, list) or len(items) > 200:
        return json_error('Items must be a list of at most 200 lines.', 400)
    currency = clean_text(data.get('currency') or 'usd', 8).lower()
    if currency not in {'usd', 'cad'}:
        return json_error('Currency must be usd or cad.', 400)
    quote_type = clean_text(data.get('type') or 'sqf', 20)
    if quote_type not in {'sqf', 'size'}:
        quote_type = 'sqf'
    user = current_user()
    customers = load_customers()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id or '')
        if not customer:
            return json_error('Your account is not linked to a customer.', 400)
        company = customer.get('name') or 'Customer'
    else:
        customer_id = None
        company = 'ORA'
    tiers = load_tiers()
    tier = tier_for_request(user, clean_text(data.get('tier'), 20), customers, tiers)
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    quote = {
        'date': date.today().isoformat(),
        'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'),
        'user': user.get('email'),
        'company': company,
        'customer_id': customer_id,
        'project': clean_text(data.get('project'), 200),
        'items': items,
        'currency': currency,
        'type': quote_type,
        'tier': tier,
    }
    stored = append_quote(quote)
    return jsonify({'success': True, 'id': stored['id']})


@app.route('/api/quotes/clear', methods=['POST'])
@admin_required
def clear_quotes():
    save_quotes([])
    return jsonify({'success': True})


@app.route('/api/quotes', methods=['GET'])
@module_required('quote-history')
def get_quotes():
    user = current_user()
    quotes = quotes_visible_to(user, load_quotes())
    if user.get('role') != 'customer':
        return jsonify(quotes)
    customers = load_customers()
    tiers = load_tiers()
    allowed = set(allowed_tier_ids(user, customers, tiers))
    visible = []
    for quote in quotes:
        row = dict(quote)
        if row.get('tier') not in allowed:
            row.pop('tier', None)
        visible.append(row)
    return jsonify(visible)


def _upsert_admin(email, name, password):
    email = normalize_email(email)
    if not valid_email(email):
        raise click.ClickException('A valid email is required.')
    ok, message = password_is_strong(password)
    if not ok:
        raise click.ClickException(message)
    users = load_users()
    record = users.get(email) or new_user(email=email, name=name, role='admin')
    record['name'] = clean_text(name, 120) or 'Administrator'
    record['role'] = 'admin'
    record['customer_id'] = None
    record['tier_override'] = None
    record['password_hash'] = hash_password(password)
    record['must_change_password'] = False
    record['active'] = True
    record['invite_token_hash'] = None
    record['invite_expires'] = None
    users[email] = record
    save_users(users)
    return email


@app.cli.command('create-admin')
@click.option('--email', default='', help='Admin email. Defaults to ADMIN_EMAIL.')
@click.option('--name', default='Administrator')
def create_admin_command(email, name):
    """Create or reset an administrator. Reads the password from ADMIN_PASSWORD or a prompt."""
    os.makedirs(data_dir(), exist_ok=True)
    email = email or os.environ.get('ADMIN_EMAIL') or ''
    if not email:
        email = click.prompt('Email')
    password = os.environ.get('ADMIN_PASSWORD')
    if not password:
        password = click.prompt('Password', hide_input=True, confirmation_prompt=True)
    saved = _upsert_admin(email, name, password)
    click.echo(f'Admin {saved} is ready. If ADMIN_PASSWORD is set in the environment, unset it after you sign in.')


@app.cli.command('seed')
@click.option('--links', is_flag=True, help='Print one-time setup links for seeded users who have no password.')
@click.option('--base-url', default='', help='Public origin used in setup links. Defaults to BASE_URL or http://localhost:5000.')
def seed_command(links, base_url):
    """Create the Open Windows customer and users if they are missing. Does not set passwords."""
    os.makedirs(data_dir(), exist_ok=True)
    tokens = apply_seed(issue_links=links)
    click.echo('Seed complete. Open Windows and its users are present. No passwords were written.')
    if not links:
        click.echo('Run with --links to print one-time setup URLs, or generate them from the Users page.')
        return
    base = (base_url or os.environ.get('BASE_URL') or 'http://localhost:5000').rstrip('/')
    if not tokens:
        click.echo('No setup links issued. Those users already have passwords.')
        return
    click.echo('Give each person their link. It works once, expires in 7 days, and is not stored in the clear.')
    for email, token in tokens.items():
        click.echo(f'{email}  {base}/setup-password/{token}')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=os.environ.get('FLASK_DEBUG') == '1')

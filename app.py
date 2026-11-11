"""
ORA Glass ERP - quoting, customers, and accounts.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import click
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from billing import (
    CURRENCIES,
    NEXT_STATUS,
    PAYMENT_METHODS,
    STATUS_LABELS,
    apply_credit_limit,
    apply_payment_terms,
    available_credit,
    conversion_allowed,
    coverage,
    credit_message,
    diff_changes,
    invoice_balance,
    allocated_to_target,
    invoice_status,
    invoice_status_label,
    money,
    order_status,
    payment_term_days,
    price_summary,
    unallocated_amount,
    unallocated_credit,
)
from quoting import (
    allowed_tier_ids,
    area_sqf,
    calculate_price_sqf,
    classify,
    default_quote_tier,
    load_pricing,
    reprice_size_line,
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
    append_punch,
    append_quote,
    load_kiosks,
    load_time_settings,
    punch_photo_dir,
    save_kiosks,
    save_time_settings,
    data_dir,
    append_invoice,
    append_order,
    append_payment,
    load_customers,
    load_invoices,
    load_orders,
    load_payments,
    load_punches,
    load_quotes,
    load_tiers,
    load_users,
    save_customers,
    save_invoices,
    save_payments,
    update_order,
    ordered_tiers,
    replace_quote,
    save_punches,
    save_quotes,
    save_tiers,
    save_users,
    update_punch,
)
from timeclock import (
    ACTIONS,
    as_utc,
    clock_status,
    live_hours,
    pacific_clock,
    pacific_date,
    pacific_label,
    replay,
    stamp,
    summarize,
)

logger = logging.getLogger('ora_glass')

# Every gated area that exists as a page or API. There is no separate
# Production or Shipping screen; the old production role used Orders and Inventory.
MODULES = (
    {'id': 'dashboard', 'label': 'Dashboard', 'short': 'Dashboard', 'endpoint': 'dashboard'},
    {'id': 'quotes', 'label': 'Quotes', 'short': 'Quotes', 'endpoint': 'quotes'},
    {'id': 'quote-history', 'label': 'Quote History', 'short': 'History', 'endpoint': 'quote_history'},
    {'id': 'orders', 'label': 'Orders', 'short': 'Orders', 'endpoint': 'orders'},
    {'id': 'inventory', 'label': 'Inventory', 'short': 'Inventory', 'endpoint': 'inventory'},
    {'id': 'pricing', 'label': 'Pricing', 'short': 'Pricing', 'endpoint': 'pricing_admin'},
    {'id': 'invoices', 'label': 'Invoices', 'short': 'Invoices', 'endpoint': 'invoices'},
    {'id': 'financial', 'label': 'Financials', 'short': 'Financials', 'endpoint': 'financial'},
    {'id': 'customers', 'label': 'Customers', 'short': 'Customers', 'endpoint': 'customers_page'},
    {'id': 'users', 'label': 'Users', 'short': 'Users', 'endpoint': 'users_page'},
    {'id': 'time-clock', 'label': 'Time Clock', 'short': 'Time Clock', 'endpoint': 'punch'},
)
MODULE_IDS = tuple(item['id'] for item in MODULES)
# Customers may only be given these. Dashboard stays available as their home page.
CUSTOMER_CEILING = ('quotes', 'quote-history', 'orders', 'invoices', 'financial')
# Access that existing roles already had, plus Dashboard, which was not gated.
LEGACY_ROLE_MODULES = {
    'admin': MODULE_IDS,
    'sales': ('dashboard', 'quotes', 'quote-history', 'orders', 'customers', 'invoices'),
    'production': ('dashboard', 'orders', 'inventory'),
    'viewer': ('dashboard', 'quotes', 'quote-history', 'orders'),
    'customer': ('dashboard',) + CUSTOMER_CEILING,
    'employee': ('dashboard',),
}
LEGACY_STAFF_ROLES = ('sales', 'production', 'viewer', 'employee')
ROLES = {
    'admin': {'name': 'Admin'},
    'employee': {'name': 'Ora Glass employee'},
    'shop': {'name': 'Shop worker'},
    'customer': {'name': 'Customer'},
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


class LongPunchSessionInterface(SecureCookieSessionInterface):
    """The punch page keeps a phone signed in for 30 days. Other pages stay on the normal lifetime."""

    def get_expiration_time(self, app, session):
        if session.get('long_session'):
            return datetime.now(timezone.utc) + timedelta(days=30)
        return super().get_expiration_time(app, session)


app.session_interface = LongPunchSessionInterface()

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
    user = current_user()
    if user and user.get('role') == 'shop':
        return redirect(url_for('punch'))
    if user:
        for item in MODULES:
            if item['endpoint'] == request.endpoint:
                continue
            if user_has_module(user, item['id']):
                return redirect(url_for(item['endpoint']))
    return render_template('module.html', module='Access denied', note='No modules are enabled for this account.'), 403


def current_user():
    email = session.get('user')
    if not email:
        return None
    user = load_users().get(email)
    if not user or not user.get('active', True):
        return None
    return user


def effective_module_ids(user):
    """Modules this account may open. Admins get every module. Missing lists keep the old role access."""
    if not user:
        return []
    role = user.get('role') or ''
    if role == 'admin':
        return list(MODULE_IDS)
    if role == 'shop':
        return []
    stored = user.get('modules', None)
    if role == 'customer':
        if stored is None:
            chosen = list(CUSTOMER_CEILING)
        else:
            chosen = [module_id for module_id in stored if module_id in CUSTOMER_CEILING]
        if 'dashboard' not in chosen:
            chosen.insert(0, 'dashboard')
        # Sales force cannot open Orders, even if that checkbox is ticked.
        if customer_subrole(user) != 'management':
            chosen = [module_id for module_id in chosen if module_id != 'orders']
        return chosen
    if stored is None:
        return list(LEGACY_ROLE_MODULES.get(role, ('dashboard',)))
    return [module_id for module_id in MODULE_IDS if module_id in set(stored)]


def user_has_module(user, module):
    return module in effective_module_ids(user)


def can_manage_time(user):
    """Admins manage timesheets. Employees need the Manage time privilege. Customers never can."""
    if not user or user.get('role') == 'customer':
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('manage_time'))


def apply_manage_time(record, data):
    role = record.get('role')
    if role == 'admin' or role == 'customer' or role == 'shop' or role not in LEGACY_STAFF_ROLES:
        record['manage_time'] = False
        return
    if 'manage_time' in data:
        record['manage_time'] = bool(data.get('manage_time'))
    elif 'manage_time' not in record:
        record['manage_time'] = False


def uses_time_clock(user):
    """Shop workers and admins can punch. Other staff need Uses time clock."""
    if not user or user.get('role') == 'customer':
        return False
    if user.get('role') in {'shop', 'admin'}:
        return True
    if 'uses_time_clock' in user:
        return bool(user.get('uses_time_clock'))
    return user_has_module(user, 'time-clock')


def apply_uses_time_clock(record, data):
    """Employees punch only when Uses time clock is on. Shop workers never get modules."""
    role = record.get('role')
    if role == 'shop':
        record['modules'] = []
        record['uses_time_clock'] = False
        record['manage_time'] = False
        return None
    if role not in LEGACY_STAFF_ROLES:
        record['uses_time_clock'] = False
        return None
    modules = list(record.get('modules') or [])
    if 'uses_time_clock' in data:
        enabled = bool(data.get('uses_time_clock'))
    elif 'uses_time_clock' in record:
        enabled = bool(record.get('uses_time_clock'))
    else:
        enabled = 'time-clock' in modules
    modules = [module_id for module_id in modules if module_id != 'time-clock']
    if enabled:
        modules.append('time-clock')
    record['modules'] = modules
    record['uses_time_clock'] = enabled
    return None


def normalize_employee_number(value):
    return re.sub(r'\s+', '', str(value or '')).strip().upper()


def employee_number_ok(number):
    return bool(number) and len(number) <= 20 and re.fullmatch(r'[A-Z0-9-]+', number) is not None


def pin_ok(pin):
    return isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 6


def shop_account_key(number):
    return 'shop:' + normalize_employee_number(number)


def find_shop_by_number(number, users=None):
    number = normalize_employee_number(number)
    users = load_users() if users is None else users
    for key, user in users.items():
        if user.get('role') == 'shop' and normalize_employee_number(user.get('employee_number')) == number:
            return key, user
    return None, None


def modules_label(user):
    if (user or {}).get('role') == 'admin':
        return 'All modules'
    labels = [item['short'] for item in MODULES if item['id'] in effective_module_ids(user) and item['id'] != 'dashboard']
    return ', '.join(labels) if labels else 'Dashboard only'


def normalize_role(role):
    """Map retired staff roles onto Ora Glass employee. Returns (stored role, module preset role)."""
    if role in LEGACY_STAFF_ROLES:
        return 'employee', role
    if role in ROLES:
        return role, role
    return None, None


def apply_modules(record, data, preset_role=None):
    """Persist module access. Customers cannot be given anything outside their ceiling."""
    role = record.get('role')
    if role == 'admin':
        record.pop('modules', None)
        return None
    if role == 'shop':
        if isinstance(data.get('modules'), list) and any(str(item or '').strip() for item in data.get('modules')):
            return 'Shop workers cannot be given modules.'
        record['modules'] = []
        return None
    if 'modules' not in data:
        if preset_role in LEGACY_ROLE_MODULES:
            record['modules'] = list(LEGACY_ROLE_MODULES[preset_role])
        return None
    raw = data.get('modules')
    if not isinstance(raw, list):
        return 'Modules must be a list.'
    cleaned = []
    for item in raw:
        module_id = clean_text(item, 40)
        if not module_id or module_id in cleaned:
            continue
        if module_id not in MODULE_IDS:
            return 'Unknown module.'
        cleaned.append(module_id)
    if role == 'customer':
        extra = [module_id for module_id in cleaned if module_id not in CUSTOMER_CEILING]
        if extra:
            return 'That module is not available to customer users.'
        record['modules'] = [module_id for module_id in CUSTOMER_CEILING if module_id in cleaned]
        return None
    record['modules'] = [module_id for module_id in MODULE_IDS if module_id in cleaned and module_id != 'dashboard']
    if 'dashboard' in cleaned:
        record['modules'].insert(0, 'dashboard')
    return None


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
        'payment_terms_days': payment_term_days(customer),
        'credit_limit': money(customer.get('credit_limit') or 0),
        'credit_currency': str(customer.get('credit_currency') or 'usd').lower(),
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
        'role': 'employee' if user.get('role') in LEGACY_STAFF_ROLES else user.get('role'),
        'role_label': role_display_name(user),
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
        'modules': effective_module_ids(user),
        'modules_label': modules_label(user),
        'customer_subrole': customer_subrole(user) if user.get('role') == 'customer' else None,
        'customer_subrole_label': customer_subrole_label(user) if user.get('role') == 'customer' else None,
        'edit_orders': bool(user.get('edit_orders')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'update_order_status': bool(user.get('update_order_status')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'record_payments': bool(user.get('record_payments')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'manage_time': bool(user.get('manage_time')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'uses_time_clock': uses_time_clock(user) if user.get('role') != 'shop' else False,
        'employee_number': user.get('employee_number') or '',
        'has_pin': bool(user.get('pin_hash')),
        'allow_phone_punch': bool(user.get('allow_phone_punch')),
        'pin_locked': bool(user.get('pin_locked')),
        'account': user.get('account') or user.get('email'),
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


def customer_subrole(user):
    """Customer team role. Missing values stay Sales force so existing accounts do not gain Management."""
    if (user or {}).get('role') != 'customer':
        return None
    value = (user or {}).get('customer_subrole') or 'sales'
    return value if value in {'management', 'sales'} else 'sales'


def customer_subrole_label(user):
    return 'Management' if customer_subrole(user) == 'management' else 'Sales force'


def apply_customer_subrole(record, data):
    if record.get('role') != 'customer':
        record['customer_subrole'] = None
        return None
    if 'customer_subrole' not in data:
        if not record.get('customer_subrole'):
            record['customer_subrole'] = 'sales'
        return None
    value = clean_text(data.get('customer_subrole'), 20).lower()
    if value not in {'management', 'sales'}:
        return 'Customer role must be Management or Sales force.'
    record['customer_subrole'] = value
    if value != 'management' and isinstance(record.get('modules'), list):
        record['modules'] = [module_id for module_id in record['modules'] if module_id != 'orders']
    return None


def quotes_visible_to(user, quotes):
    role = user.get('role')
    if role == 'customer':
        customer_id = user.get('customer_id')
        own_company = [quote for quote in quotes if quote.get('customer_id') == customer_id]
        if customer_subrole(user) == 'management':
            return own_company
        email = user.get('email')
        return [quote for quote in own_company if quote.get('user') == email]
    if role == 'admin' or role in LEGACY_STAFF_ROLES:
        return list(quotes)
    return [quote for quote in quotes if quote.get('user') == user.get('email')]


def is_order_only(customer):
    return (customer or {}).get('kind') == 'order_only'


def staff_can_order(user):
    """Admins and Ora Glass staff who have the Orders module. Customers never qualify."""
    if not user or user.get('role') == 'customer':
        return False
    return user_has_module(user, 'orders')


def _is_ora_staff(user):
    role = (user or {}).get('role') or ''
    return role == 'admin' or role in LEGACY_STAFF_ROLES


def can_edit_orders(user):
    """Admins can edit orders. Employees need the Edit orders privilege. Customers never can."""
    if not _is_ora_staff(user):
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('edit_orders'))


def can_update_order_status(user):
    """Admins can move an order along. Employees need a separate status privilege. Customers never can."""
    if not _is_ora_staff(user):
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('update_order_status'))


def can_record_payments(user):
    """Admins can record payments. Employees need the Record payments privilege. Customers never can."""
    if not _is_ora_staff(user):
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('record_payments')) and (
        user_has_module(user, 'invoices') or user_has_module(user, 'financial')
    )


def apply_order_privileges(record, data):
    """Store staff-only order privileges. Customers and admins never keep them."""
    role = record.get('role')
    if role == 'admin' or role == 'customer' or role not in LEGACY_STAFF_ROLES:
        record['edit_orders'] = False
        record['update_order_status'] = False
        record['record_payments'] = False
        return
    for field in ('edit_orders', 'update_order_status', 'record_payments'):
        if field in data:
            record[field] = bool(data.get(field))
        elif field not in record:
            record[field] = False


def account_customer(customer_id, customers):
    """A full customer account. Order-only records are not companies people can log into."""
    if not customer_id or not ID_RE.fullmatch(customer_id or ''):
        return None
    customer = (customers or {}).get(customer_id)
    if not customer or is_order_only(customer):
        return None
    return customer


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
    if creating or 'payment_terms' in data or 'payment_terms_days' in data:
        terms_error = apply_payment_terms(customer, data)
        if terms_error:
            return terms_error
    if creating or 'credit_limit' in data or 'credit_currency' in data:
        credit_error = apply_credit_limit(customer, data if not creating else {
            'credit_limit': data.get('credit_limit', 0),
            'credit_currency': data.get('credit_currency') or 'usd',
        })
        if credit_error:
            return credit_error
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
    user = current_user()
    allowed = set(effective_module_ids(user)) if user else set()
    return {
        'current_user': user,
        'nav_modules': [item for item in MODULES if item['id'] in allowed],
        'has_dashboard': 'dashboard' in allowed,
    }


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
    user = current_user()
    if user:
        return redirect(url_for('punch') if user.get('role') == 'shop' else url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user() and request.method == 'GET':
        user = current_user()
        return redirect(url_for('punch') if user.get('role') == 'shop' else url_for('dashboard'))
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
        if user.get('role') == 'shop':
            session['long_session'] = True
            return redirect(url_for('punch'))
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


def role_display_name(user):
    role = (user or {}).get('role') or ''
    if role in ROLES:
        return ROLES[role]['name']
    return {'sales': 'Ora Glass employee', 'production': 'Ora Glass employee', 'viewer': 'Ora Glass employee'}.get(role, role)


@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    if not user_has_module(user, 'dashboard'):
        return forbidden()
    return render_template(
        'dashboard.html',
        user=user,
        role_name=role_display_name(user),
        modules=effective_module_ids(user),
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
    user = current_user()
    return render_template(
        'orders.html',
        note=scope_note(),
        prices_visible=_order_prices_visible(user),
        staff_view=staff_can_order(user),
        can_edit=can_edit_orders(user),
        can_status=can_update_order_status(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in GLASS],
    )


@app.route('/orders/<int:order_id>')
@module_required('orders')
def order_detail(order_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        flash('Order not found.', 'error')
        return redirect(url_for('orders'))
    return render_template(
        'orders.html',
        note=scope_note(),
        prices_visible=_order_prices_visible(user),
        staff_view=staff_can_order(user),
        can_edit=can_edit_orders(user),
        can_status=can_update_order_status(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in GLASS],
        focus_id=order_id,
    )


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
    user = current_user()
    return render_template(
        'users.html',
        tiers=ordered_tiers(),
        roles=ROLES,
        can_manage=user.get('role') == 'admin',
        module_catalog=[{'id': item['id'], 'label': item['label']} for item in MODULES if item['id'] != 'time-clock'],
        customer_modules=[{'id': item['id'], 'label': item['label']} for item in MODULES if item['id'] in CUSTOMER_CEILING],
    )


@app.route('/quote-history')
@module_required('quote-history')
def quote_history():
    user = current_user()
    staff = staff_can_order(user)
    return render_template(
        'quote_history.html',
        note=scope_note(),
        can_order=customer_subrole(user) == 'management' or staff,
        staff_order=staff,
        staff_tools=user.get('role') != 'customer',
        order_tiers=[
            {'id': tid, 'label': (cfg or {}).get('label') or tid}
            for tid, cfg in ordered_tiers()
        ] if staff else [],
        is_admin=user.get('role') == 'admin',
    )


@app.route('/invoices')
@module_required('invoices')
def invoices():
    user = current_user()
    return render_template(
        'invoices.html',
        note=scope_note(),
        staff_view=user.get('role') != 'customer',
        can_record=can_record_payments(user),
        focus_id=None,
    )


@app.route('/invoices/<int:invoice_id>')
@module_required('invoices')
def invoice_detail(invoice_id):
    user = current_user()
    return render_template(
        'invoices.html',
        note=scope_note(),
        staff_view=user.get('role') != 'customer',
        can_record=can_record_payments(user),
        focus_id=invoice_id,
    )


@app.route('/financial')
@module_required('financial')
def financial():
    user = current_user()
    return render_template(
        'financial.html',
        note=scope_note(),
        staff_view=user.get('role') != 'customer',
        can_record=can_record_payments(user),
    )


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
        if not is_order_only(customer)
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
    if not customer or is_order_only(customer):
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
    customer = customers.get(cid)
    if not customer or is_order_only(customer):
        return json_error('Not found', 404)
    if customer_user_counts().get(cid):
        return json_error("Reassign or delete this customer's users first.", 409)
    del customers[cid]
    save_customers(customers)
    return jsonify({'success': True})


@app.route('/api/users', methods=['GET'])
@module_required('users')
def get_users():
    customers = load_customers()
    tiers = load_tiers()
    rows = []
    for key, user in load_users().items():
        view = dict(user)
        view['account'] = key
        rows.append(public_user(view, customers, tiers))
    rows.sort(key=lambda row: (row.get('company') or '', row.get('email') or ''))
    return jsonify(rows)


def _apply_shop_fields(user, data, users, current_key=None):
    """Shop workers have a name and either an employee number, an email, or both. No modules."""
    if 'employee_number' in data or current_key is None:
        number = normalize_employee_number(data.get('employee_number'))
        if number and not employee_number_ok(number):
            return 'Employee number must be letters, digits, or dashes.'
        if number:
            other, _found = find_shop_by_number(number, users)
            if other and other != current_key:
                return 'That employee number is already in use.'
        user['employee_number'] = number or None
    if 'pin' in data and str(data.get('pin') or '').strip():
        pin = str(data.get('pin')).strip()
        if not pin_ok(pin):
            return 'PIN must be 4 to 6 digits.'
        user['pin_hash'] = hash_password(pin)
        user['pin_failures'] = 0
        user['pin_locked'] = False
    if 'allow_phone_punch' in data:
        user['allow_phone_punch'] = bool(data.get('allow_phone_punch'))
    elif 'allow_phone_punch' not in user:
        user['allow_phone_punch'] = False
    if 'email' in data or current_key is None:
        email = normalize_email(data.get('email'))
        if email and not valid_email(email):
            return 'Email is not valid.'
        if email and email != current_key and email in users:
            return 'A user with that email already exists.'
        user['email'] = email or None
    user['role'] = 'shop'
    user['customer_id'] = None
    user['tier_override'] = None
    user['modules'] = []
    user['manage_time'] = False
    user['uses_time_clock'] = False
    user['customer_subrole'] = None
    if not user.get('employee_number') and not valid_email(user.get('email')):
        return 'Enter an employee number or an email.'
    return None


def _add_shop_worker(data):
    if isinstance(data.get('modules'), list) and any(str(item or '').strip() for item in data.get('modules')):
        return json_error('Shop workers cannot be given modules.', 400)
    name = clean_text(data.get('name'), 120)
    if not name:
        return json_error('Name is required.', 400)
    users = load_users()
    record = new_user(email='', name=name, role='shop')
    error = _apply_shop_fields(record, data, users, current_key=None)
    if error:
        return json_error(error, 400)
    email = record.get('email') if valid_email(record.get('email')) else ''
    key = email or shop_account_key(record.get('employee_number'))
    if key in users:
        return json_error('A user with that email already exists.', 409)
    record['account'] = key
    setup_url = None
    if email and not record.get('pin_hash') and not data.get('password'):
        token = attach_invite(record)
        setup_url = setup_url_for(token)
    elif data.get('password'):
        ok, message = password_is_strong(data.get('password'))
        if not ok:
            return json_error(message, 400)
        record['password_hash'] = hash_password(data.get('password'))
        record['must_change_password'] = bool(data.get('temporary', True))
    users[key] = record
    save_users(users)
    payload = {'success': True, 'email': email, 'account': key}
    if setup_url:
        payload['setup_url'] = setup_url
        payload['expires_in_days'] = 7
    return jsonify(payload)


@app.route('/api/users', methods=['POST'])
@admin_required
def add_user():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    role, _preset = normalize_role(clean_text(data.get('role'), 20))
    if role == 'shop':
        return _add_shop_worker(data)
    email = normalize_email(data.get('email'))
    if not valid_email(email):
        return json_error('A valid email is required.', 400)
    users = load_users()
    if email in users:
        return json_error('A user with that email already exists.', 409)
    name = clean_text(data.get('name'), 120)
    if not name:
        return json_error('Name is required.', 400)
    role, preset_role = normalize_role(clean_text(data.get('role'), 20))
    if not role:
        return json_error('Unknown role.', 400)
    customer_id = clean_text(data.get('customer_id'), 64) or None
    if role == 'customer' and not customer_id:
        return json_error('Customer users must be linked to a company.', 400)
    if customer_id and not account_customer(customer_id, load_customers()):
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
    module_error = apply_modules(record, data, preset_role=preset_role)
    if module_error:
        return json_error(module_error, 400)
    subrole_error = apply_customer_subrole(record, data)
    if subrole_error:
        return json_error(subrole_error, 400)
    apply_order_privileges(record, data)
    apply_manage_time(record, data)
    apply_uses_time_clock(record, data)
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
        role, _preset = normalize_role(clean_text(data.get('role'), 20))
        if not role:
            return json_error('Unknown role.', 400)
        if user.get('role') == 'admin' and role != 'admin' and active_admin_count(users) <= 1:
            return json_error('The last administrator cannot be demoted.', 400)
        user['role'] = role
    if 'customer_id' in data:
        customer_id = clean_text(data.get('customer_id'), 64) or None
        if customer_id and not account_customer(customer_id, load_customers()):
            return json_error('Unknown customer.', 400)
        user['customer_id'] = customer_id
    if 'tier_override' in data:
        tier_override = clean_text(data.get('tier_override'), 20) or None
        if tier_override and tier_override not in load_tiers():
            return json_error('Unknown pricing tier.', 400)
        user['tier_override'] = tier_override
    if user.get('role') != 'shop':
        allowed_error = apply_allowed_tiers(user, data, load_customers(), load_tiers())
        if allowed_error:
            return json_error(allowed_error, 400)
    module_error = apply_modules(user, data)
    if module_error:
        return json_error(module_error, 400)
    subrole_error = apply_customer_subrole(user, data)
    if subrole_error:
        return json_error(subrole_error, 400)
    apply_order_privileges(user, data)
    apply_manage_time(user, data)
    apply_uses_time_clock(user, data)
    if user.get('role') == 'shop':
        pin_error = _apply_shop_fields(user, data, users, current_key=email)
        if pin_error:
            return json_error(pin_error, 400)
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
    if user.get('role') == 'shop' and not valid_email(user.get('email')):
        return json_error('Add an email before sending a setup link.', 400)
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


def _query_date(name):
    text = clean_text(request.args.get(name), 10)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text
    return ''


def quote_total(quote):
    total = 0.0
    for item in quote.get('items') or []:
        if not isinstance(item, dict):
            continue
        raw = item.get('total')
        if raw in (None, ''):
            raw = item.get('price')
        try:
            total += float(raw)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def order_total_value(order):
    try:
        return round(float(order.get('total') or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def order_day(order):
    text = str(order.get('date') or order.get('approved_at') or '')
    return text[:10]


def filter_quote_rows(quotes):
    customer_id = clean_text(request.args.get('customer_id'), 80)
    user_email = clean_text(request.args.get('user'), 200)
    project = clean_text(request.args.get('project'), 200).lower()
    date_from = _query_date('from')
    date_to = _query_date('to')
    sort = clean_text(request.args.get('sort'), 20)
    rows = []
    for quote in quotes:
        if customer_id == '__none__':
            if quote.get('customer_id'):
                continue
        elif customer_id and quote.get('customer_id') != customer_id:
            continue
        if user_email and (quote.get('user') or '') != user_email:
            continue
        if project and project not in str(quote.get('project') or '').lower():
            continue
        quote_date = str(quote.get('date') or '')
        if date_from and quote_date < date_from:
            continue
        if date_to and quote_date > date_to:
            continue
        rows.append(quote)
    if sort in {'total_asc', 'total_desc'}:
        rows.sort(key=lambda row: (quote_total(row), row.get('id') or 0), reverse=sort == 'total_desc')
    return rows


def filter_order_rows(orders):
    customer_id = clean_text(request.args.get('customer_id'), 80)
    date_from = _query_date('from')
    date_to = _query_date('to')
    sort = clean_text(request.args.get('sort'), 20)
    rows = []
    for order in orders:
        if customer_id and order.get('customer_id') != customer_id:
            continue
        day = order_day(order)
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        status = clean_text(request.args.get('status'), 20).lower()
        if status in STATUS_LABELS and order_status(order) != status:
            continue
        rows.append(order)
    if sort in {'total_asc', 'total_desc'}:
        rows.sort(key=lambda row: (order_total_value(row), row.get('id') or 0), reverse=sort == 'total_desc')
    else:
        rows.sort(key=lambda row: row.get('id') or 0, reverse=True)
    return rows


def _fallback_tier(tiers):
    if 'T2' in tiers:
        return 'T2'
    return next(iter(tiers), '')


def _chosen_tier(raw, tiers):
    text = clean_text(raw, 20)
    if not text:
        return _fallback_tier(tiers), None
    if text not in tiers:
        return None, 'Unknown pricing tier.'
    return text, None


def _company_tier(customer, tiers):
    tier = (customer or {}).get('tier') or ''
    if tier in tiers:
        return tier
    return _fallback_tier(tiers)


@app.route('/api/quotes', methods=['GET'])
@module_required('quote-history')
def get_quotes():
    user = current_user()
    visible = []
    for quote in quotes_visible_to(user, load_quotes()):
        row = dict(quote)
        # The quote keeps the tier and prices it was created with. Do not reprice it.
        if row.get('order_id') and not row.get('order_status'):
            row['order_status'] = 'Approved for order'
        visible.append(row)
    return jsonify(filter_quote_rows(visible))


@app.route('/api/order-customers', methods=['GET'])
@login_required
def order_customer_choices():
    """Customers a staff member can attach to a production order, including order-only records."""
    user = current_user()
    if user.get('role') == 'customer' or not (
        user_has_module(user, 'quote-history') or user_has_module(user, 'orders')
        or user_has_module(user, 'invoices') or user_has_module(user, 'financial')
    ):
        return json_error('Access denied', 403)
    tiers = load_tiers()
    rows = []
    for customer_id, customer in load_customers().items():
        tier = customer.get('tier') or ''
        known = tier if tier in tiers else ''
        rows.append({
            'id': customer_id,
            'name': customer.get('name') or '',
            'tier': known,
            'tier_label': tier_label(known, tiers) if known else '',
            'kind': 'order_only' if is_order_only(customer) else 'account',
        })
    rows.sort(key=lambda row: (row['name'].lower(), row['id']))
    return jsonify(rows)


def _order_prices_visible(user):
    """Management and Ora Glass see production prices. Sales force never sees company-tier amounts."""
    if not user or user.get('role') != 'customer':
        return True
    return customer_subrole(user) == 'management'


def orders_visible_to(user, orders):
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        return [order for order in orders if order.get('customer_id') == customer_id]
    return list(orders)


SPEC_KEYS = (
    'glass1', 'temper1', 'spacer1', 'sealant1',
    'glass2', 'temper2', 'spacer2', 'sealant2',
    'glass3', 'temper3', 'width', 'height', 'unit',
)
MONEY_LOG_FIELDS = {
    'discount', 'discount_amount', 'extra_charge', 'total', 'igu_subtotal', 'igu_total', 'extras_total',
}


def _dimension(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if abs(number - round(number)) < 1e-9:
        return int(round(number))
    return round(number, 4)


def _known_priced_name(name):
    if not name or name == 'n/a':
        return True
    return any(row.get('name') == name for row in PRICING)


def order_for_viewer(order, user, with_coverage=False):
    status = order_status(order)
    summary, _error = price_summary(
        order.get('items') or [],
        order.get('discount') or {'type': 'percent', 'value': 0},
        order.get('extra_charges') or [],
    )
    summary = summary or {}
    base = {
        'id': order.get('id'),
        'quote_id': order.get('quote_id'),
        'customer_id': order.get('customer_id'),
        'company': order.get('company') or '',
        'project': order.get('project') or '',
        'notes': order.get('notes') or '',
        'approved_by': order.get('approved_by'),
        'approved_at': order.get('approved_at'),
        'date': order.get('date'),
        'currency': order.get('currency') or 'usd',
        'tier': order.get('tier'),
        'tier_label': order.get('tier_label'),
        'status': status,
        'status_label': STATUS_LABELS[status],
        'invoice_id': order.get('invoice_id'),
        'invoice_number': order.get('invoice_number'),
    }
    if not _order_prices_visible(user):
        items = []
        for item in order.get('items') or []:
            spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
            items.append({
                'name': item.get('name') or '',
                'sqf': item.get('sqf'),
                'pcs': item.get('pcs'),
                'spec': {
                    'width': spec.get('width'),
                    'height': spec.get('height'),
                    'unit': spec.get('unit'),
                    'glass1': spec.get('glass1'),
                },
            })
        changelog = []
        for entry in order.get('changelog') or []:
            changes = []
            for change in entry.get('changes') or []:
                if change.get('field') in MONEY_LOG_FIELDS:
                    continue
                if change.get('field') == 'line':
                    changes.append({
                        'field': 'line',
                        'action': change.get('action'),
                        'old': _public_line_log(change.get('old'), False),
                        'new': _public_line_log(change.get('new'), False),
                    })
                else:
                    changes.append(change)
            if changes:
                changelog.append({'at': entry.get('at'), 'user': entry.get('user'), 'changes': changes})
        base.update({'items': items, 'changelog': changelog, 'prices_visible': False})
        return base
    view = dict(base)
    view['items'] = order.get('items') or []
    view['discount'] = summary.get('discount') or {'type': 'percent', 'value': 0}
    view['discount_amount'] = summary.get('discount_amount', 0)
    view['extra_charges'] = summary.get('extra_charges') or []
    view['igu_subtotal'] = summary.get('igu_subtotal', money(order.get('total')))
    view['igu_total'] = summary.get('igu_total', view['igu_subtotal'])
    view['extras_total'] = summary.get('extras_total', 0)
    view['total'] = summary.get('total', money(order.get('total')))
    view['changelog'] = list(order.get('changelog') or [])
    view['prices_visible'] = True
    if with_coverage:
        customer = dict(load_customers().get(order.get('customer_id') or '') or {})
        customer.setdefault('id', order.get('customer_id'))
        view['coverage'] = coverage(view, customer, load_orders(), load_invoices(), load_payments())
    return view


def _public_line_log(snapshot, prices):
    if not isinstance(snapshot, dict):
        return None
    if prices:
        return snapshot
    return {
        'name': snapshot.get('name') or '',
        'pcs': snapshot.get('pcs'),
        'width': snapshot.get('width'),
        'height': snapshot.get('height'),
        'unit': snapshot.get('unit') or '',
        'glass1': snapshot.get('glass1') or '',
    }


def _usable_size_spec(spec):
    if not isinstance(spec, dict):
        return False
    glass = spec.get('glass1') or ''
    if not glass or glass == 'n/a':
        return False
    return area_sqf(spec.get('width'), spec.get('height'), spec.get('unit')) > 0


def _order_only_from_request(data, customers, tiers, user):
    new_customer = data.get('new_customer')
    if not isinstance(new_customer, dict):
        return None, None, 'Choose a customer.'
    name = clean_text(new_customer.get('name'), 200)
    if not name:
        return None, None, 'Customer name is required.'
    tier, error = _chosen_tier(data.get('tier') if data.get('tier') not in (None, '') else new_customer.get('tier'), tiers)
    if error:
        return None, None, error
    customer_id = new_customer_id(name, customers)
    customer = blank_customer(customer_id, name, tier)
    customer['kind'] = 'order_only'
    customer['contact'] = clean_text(new_customer.get('contact'), 120)
    customer['email'] = clean_text(new_customer.get('email'), 200)
    customer['phone'] = clean_text(new_customer.get('phone'), 40)
    customer['address'] = clean_text(new_customer.get('address'), 300)
    terms_error = apply_payment_terms(customer, new_customer)
    if terms_error:
        return None, None, terms_error
    if user.get('role') == 'admin':
        credit_error = apply_credit_limit(customer, {
            'credit_limit': new_customer.get('credit_limit', 0),
            'credit_currency': new_customer.get('credit_currency') or 'usd',
        })
        if credit_error:
            return None, None, credit_error
    else:
        customer['credit_limit'] = 0
        customer['credit_currency'] = 'usd'
    customers[customer_id] = customer
    save_customers(customers)
    return customer_id, customer, None


def _customer_for_staff_order(data, customers, tiers, user):
    """Staff pick a saved customer, or create an order-only record that cannot sign in."""
    if isinstance(data.get('new_customer'), dict):
        return _order_only_from_request(data, customers, tiers, user)
    customer_id = clean_text(data.get('customer_id'), 64)
    if not customer_id or not ID_RE.fullmatch(customer_id):
        return None, None, 'Choose a customer.'
    customer = customers.get(customer_id)
    if not customer:
        return None, None, 'Unknown customer.'
    if is_order_only(customer) and (customer.get('tier') or '') not in tiers:
        tier, error = _chosen_tier(data.get('tier'), tiers)
        if error:
            return None, None, error
        customer['tier'] = tier
        customers[customer_id] = customer
        save_customers(customers)
    return customer_id, customer, None


@app.route('/api/quotes/<int:quote_id>/order', methods=['POST'])
@module_required('quote-history')
def convert_quote_to_order(quote_id):
    user = current_user()
    staff = staff_can_order(user)
    management = customer_subrole(user) == 'management'
    if not staff and not management:
        return json_error('Only customer management, or Ora Glass staff with Orders, can send a quote to production.', 403)
    data = json_body()
    if data is None:
        data = {}
    quotes = load_quotes()
    quote = next((row for row in quotes if row.get('id') == quote_id), None)
    if not quote:
        return json_error('Not found', 404)
    if management and quote.get('customer_id') != user.get('customer_id'):
        return json_error('Not found', 404)
    if quote.get('type') != 'size':
        return json_error('Only per-size quotes can be sent to production.', 400)
    if quote.get('order_id'):
        return json_error('This quote was already sent to production.', 409)
    items = quote.get('items') or []
    if not items:
        return json_error('This quote has no lines.', 400)
    customers = load_customers()
    tiers = load_tiers()
    if management:
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id or '') or {}
        company_tier = _company_tier(customer, tiers)
        company = customer.get('name') or quote.get('company') or ''
    else:
        customer_id, customer, error = _customer_for_staff_order(data, customers, tiers, user)
        if error:
            return json_error(error, 400)
        company_tier = _company_tier(customer, tiers)
        company = customer.get('name') or ''
    currency = quote.get('currency') or 'usd'
    priced_items = []
    for item in items:
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if not _usable_size_spec(spec):
            return json_error('This quote does not include the size inputs needed to reprice it.', 400)
        priced = reprice_size_line(spec, item.get('pcs'), company_tier, currency, PRICING, tiers)
        priced_items.append({
            'name': item.get('name') or '',
            'spec': spec,
            'sqf': priced['sqf'],
            'price_sqf': priced['price_sqf'],
            'price_pc': priced['price_pc'],
            'pcs': priced['pcs'],
            'total': priced['total'],
        })
    grand = round(sum(line['total'] for line in priced_items), 2)
    credit_check = conversion_allowed(
        grand, customer, str(currency).lower(), load_orders(), load_invoices(), load_payments(),
    )
    warning = None
    if not credit_check['allowed']:
        message = credit_message(credit_check)
        if management:
            return json_error(message, 400)
        warning = message
    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    order = append_order({
        'quote_id': quote_id,
        'customer_id': customer_id,
        'company': company,
        'project': quote.get('project') or '',
        'notes': '',
        'currency': currency,
        'tier': company_tier,
        'tier_label': tier_label(company_tier, tiers),
        'items': priced_items,
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'extra_charges': [],
        'igu_subtotal': grand,
        'igu_total': grand,
        'extras_total': 0,
        'total': grand,
        'status': 'approved',
        'changelog': [],
        'approved_by': user.get('email'),
        'approved_at': approved_at,
        'date': approved_at[:10],
    })
    updated = dict(quote)
    updated['order_id'] = order['id']
    updated['order_status'] = 'Approved for order'
    replace_quote(quote_id, updated)
    payload = {
        'success': True,
        'order_id': order['id'],
        'tier': company_tier,
        'customer_id': customer_id,
        'customer_name': company,
        'kind': 'order_only' if is_order_only(customer) else 'account',
        'total': grand,
    }
    if warning:
        payload['credit_warning'] = warning
    return jsonify(payload)


@app.route('/api/orders', methods=['GET'])
@module_required('orders')
def get_orders():
    user = current_user()
    rows = [order_for_viewer(order, user) for order in orders_visible_to(user, load_orders())]
    return jsonify(filter_order_rows(rows))


@app.route('/api/orders/<int:order_id>', methods=['GET'])
@module_required('orders')
def get_order(order_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        return json_error('Not found', 404)
    return jsonify(order_for_viewer(order, user, with_coverage=True))


def _stamp_log(order, user, changes):
    if not changes:
        return order
    log = list(order.get('changelog') or [])
    log.append({
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'user': user.get('email') or '',
        'changes': changes,
    })
    order['changelog'] = log
    return order


def _parse_order_items(raw, tier, currency, tiers):
    if not isinstance(raw, list) or not raw:
        return None, 'An order needs at least one line.'
    items = []
    for raw_item in raw:
        if not isinstance(raw_item, dict):
            return None, 'Each line must be an object.'
        spec_in = raw_item.get('spec')
        if not isinstance(spec_in, dict):
            return None, 'Each line needs a size spec.'
        spec = {}
        for key in SPEC_KEYS:
            if key in {'width', 'height'}:
                spec[key] = _dimension(spec_in.get(key))
            elif key == 'unit':
                unit = clean_text(spec_in.get(key) or 'in', 8).lower()
                spec[key] = unit if unit in {'in', 'mm'} else 'in'
            else:
                spec[key] = clean_text(spec_in.get(key), 120)
        if not _usable_size_spec(spec):
            return None, 'Each line needs a glass type and a positive size.'
        for key in ('glass1', 'glass2', 'glass3', 'spacer1', 'spacer2', 'sealant1', 'sealant2'):
            if spec.get(key) and not _known_priced_name(spec.get(key)):
                return None, 'Unknown material on a line.'
        try:
            pcs = int(raw_item.get('pcs') or 0)
        except (TypeError, ValueError):
            return None, 'Quantity must be a whole number.'
        if pcs < 1 or pcs > 10000:
            return None, 'Quantity must be at least 1.'
        priced = reprice_size_line(spec, pcs, tier, currency, PRICING, tiers)
        name = clean_text(raw_item.get('name'), 200)
        if not name:
            name = f"{spec.get('glass1')} - {spec.get('width')} x {spec.get('height')} {spec.get('unit')}"
        items.append({
            'name': name,
            'spec': spec,
            'sqf': priced['sqf'],
            'price_sqf': priced['price_sqf'],
            'price_pc': priced['price_pc'],
            'pcs': priced['pcs'],
            'total': priced['total'],
        })
    return items, None


def _parse_charges(raw):
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, 'Extra charges must be a list.'
    if len(raw) > 50:
        return None, 'Too many extra charges.'
    charges = []
    for row in raw:
        if not isinstance(row, dict):
            return None, 'Each extra charge needs a label and an amount.'
        label = clean_text(row.get('label'), 80)
        if not label:
            return None, 'Each extra charge needs a label.'
        try:
            amount = money(row.get('amount'))
        except (TypeError, ValueError):
            return None, 'Extra charge amounts must be numbers.'
        if amount < 0:
            return None, 'Extra charge amounts cannot be negative.'
        charges.append({'label': label, 'amount': amount})
    return charges, None


def _edited_order(order, data, tiers):
    currency = order.get('currency') or 'usd'
    tier = order.get('tier')
    if 'items' in data:
        items, error = _parse_order_items(data.get('items'), tier, currency, tiers)
        if error:
            return None, error
    else:
        items = order.get('items') or []
    if 'extra_charges' in data:
        charges, error = _parse_charges(data.get('extra_charges'))
        if error:
            return None, error
    else:
        charges = order.get('extra_charges') or []
    discount = data.get('discount') if 'discount' in data else (order.get('discount') or {'type': 'percent', 'value': 0})
    summary, error = price_summary(items, discount, charges)
    if error:
        return None, error
    updated = dict(order)
    updated['items'] = items
    updated['project'] = clean_text(data.get('project'), 200) if 'project' in data else (order.get('project') or '')
    updated['notes'] = clean_text(data.get('notes'), 2000) if 'notes' in data else (order.get('notes') or '')
    updated.update(summary)
    updated['changelog'] = list(order.get('changelog') or [])
    return updated, None


def _order_is_invoiced(order):
    return bool(order.get('invoice_id')) or order_status(order) == 'delivered'


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
@module_required('orders')
def edit_order(order_id):
    user = current_user()
    if not can_edit_orders(user):
        return json_error('You do not have permission to edit orders.', 403)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    if _order_is_invoiced(visible) and user.get('role') != 'admin':
        return json_error('This order has been invoiced and can only be changed by an administrator.', 403)

    def mutator(order):
        if _order_is_invoiced(order) and user.get('role') != 'admin':
            return None, 'This order has been invoiced and can only be changed by an administrator.'
        updated, error = _edited_order(order, data, load_tiers())
        if error:
            return None, error
        if order_status(order) in {'sent', 'made'}:
            customer = dict(load_customers().get(order.get('customer_id') or '') or {})
            customer.setdefault('id', order.get('customer_id'))
            probe = dict(updated)
            check = coverage(probe, customer, load_orders(), load_invoices(), load_payments())
            if not check['can_send']:
                return None, (
                    f"This order is not covered. Shortfall {check['currency'].upper()} ${check['shortfall']:.2f}."
                )
        changes = diff_changes(order, updated)
        _stamp_log(updated, user, changes)
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


def _apply_prepayments_to_invoice(order_id, invoice_id, user):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payments = load_payments()
    changed = False
    for payment in payments:
        if payment.get('voided'):
            continue
        moved = False
        allocations = []
        for row in payment.get('allocations') or []:
            if row.get('target_type') == 'order' and row.get('target_id') == order_id and not row.get('voided'):
                allocations.append({
                    'target_type': 'invoice',
                    'target_id': invoice_id,
                    'amount': money(row.get('amount')),
                    'at': row.get('at'),
                    'user': row.get('user'),
                    'applied_from_order': order_id,
                    'applied_at': now,
                    'applied_by': user.get('email'),
                })
                moved = True
            else:
                allocations.append(row)
        if moved:
            payment['allocations'] = allocations
            log = list(payment.get('log') or [])
            log.append({
                'at': now,
                'user': user.get('email') or '',
                'action': 'applied_to_invoice',
                'invoice_id': invoice_id,
                'order_id': order_id,
            })
            payment['log'] = log
            changed = True
    if changed:
        save_payments(payments)
    return payments


@app.route('/api/orders/<int:order_id>/status', methods=['POST'])
@module_required('orders')
def set_order_status(order_id):
    user = current_user()
    if not can_update_order_status(user):
        return json_error('You do not have permission to update order status.', 403)
    data = json_body() or {}
    target = clean_text(data.get('status'), 20).lower()
    if target not in NEXT_STATUS.values():
        return json_error('Unknown order status.', 400)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)

    def mutator(order):
        current = order_status(order)
        if NEXT_STATUS.get(current) != target:
            return None, f'An order that is {STATUS_LABELS[current]} cannot be marked {STATUS_LABELS[target]}.'
        if target == 'sent':
            customer = dict(load_customers().get(order.get('customer_id') or '') or {})
            customer.setdefault('id', order.get('customer_id'))
            check = coverage(order, customer, load_orders(), load_invoices(), load_payments())
            if not check['can_send']:
                return None, (
                    f"This order is not covered. Shortfall {check['currency'].upper()} ${check['shortfall']:.2f}. "
                    f"Available credit ${check['available_credit']:.2f}, "
                    f"prepayments on this order ${check['prepayments']:.2f}."
                )
        updated = dict(order)
        updated['status'] = target
        changes = [{
            'field': 'status',
            'old': STATUS_LABELS[current],
            'new': STATUS_LABELS[target],
        }]
        if target == 'delivered':
            if updated.get('invoice_id'):
                return None, 'This order already has an invoice.'
            invoice = _invoice_from_order(updated, user)
            stored = append_invoice(invoice)
            _apply_prepayments_to_invoice(order.get('id'), stored['id'], user)
            fresh = next((row for row in load_invoices() if row.get('id') == stored['id']), stored)
            payments = load_payments()
            fresh['status'] = invoice_status(fresh, payments)
            fresh['balance'] = invoice_balance(fresh, payments)
            invoices = load_invoices()
            for index, row in enumerate(invoices):
                if row.get('id') == fresh['id']:
                    invoices[index] = fresh
            save_invoices(invoices)
            updated['invoice_id'] = fresh['id']
            updated['invoice_number'] = fresh['number']
            changes.append({'field': 'invoice', 'action': 'added', 'new': fresh['number']})
        _stamp_log(updated, user, changes)
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        status = 400
        return json_error(error, status)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


def _invoice_from_order(order, user):
    customer = load_customers().get(order.get('customer_id') or '') or {}
    days = payment_term_days(customer)
    invoice_day = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    due = invoice_day + timedelta(days=days)
    summary, _error = price_summary(
        order.get('items') or [],
        order.get('discount') or {'type': 'percent', 'value': 0},
        order.get('extra_charges') or [],
    )
    summary = summary or {}
    return {
        'order_id': order.get('id'),
        'quote_id': order.get('quote_id'),
        'customer_id': order.get('customer_id'),
        'company': order.get('company') or customer.get('name') or '',
        'project': order.get('project') or '',
        'currency': order.get('currency') or 'usd',
        'items': json.loads(json.dumps(order.get('items') or [])),
        'discount': summary.get('discount') or {'type': 'percent', 'value': 0},
        'discount_amount': summary.get('discount_amount', 0),
        'extra_charges': json.loads(json.dumps(summary.get('extra_charges') or [])),
        'igu_subtotal': summary.get('igu_subtotal', 0),
        'igu_total': summary.get('igu_total', 0),
        'extras_total': summary.get('extras_total', 0),
        'total': summary.get('total', money(order.get('total'))),
        'invoice_date': invoice_day.isoformat(),
        'due_date': due.isoformat(),
        'payment_terms': customer.get('payment_terms') or 'Net 30',
        'payment_terms_days': days,
        'status': 'unpaid',
        'balance': summary.get('total', money(order.get('total'))),
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'created_by': user.get('email') or '',
    }


def _invoices_visible(user):
    rows = load_invoices()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        return [row for row in rows if row.get('customer_id') == customer_id]
    return rows


def _present_invoice(invoice, payments):
    view = dict(invoice)
    view['applied'] = round(money(invoice.get('total')) - invoice_balance(invoice, payments), 2)
    view['balance'] = invoice_balance(invoice, payments)
    view['status'] = invoice_status(invoice, payments)
    view['status_label'] = invoice_status_label(view['status'])
    return view


def _can_view_books(user):
    return user_has_module(user, 'invoices') or user_has_module(user, 'financial')


def _scoped_customer_id(user, requested):
    if user.get('role') == 'customer':
        return user.get('customer_id'), None
    customer_id = clean_text(requested, 80)
    if not customer_id:
        return None, 'Choose a customer.'
    if customer_id not in load_customers():
        return None, 'Unknown customer.'
    return customer_id, None


@app.route('/api/invoices', methods=['GET'])
@login_required
def get_invoices():
    user = current_user()
    if not user_has_module(user, 'invoices'):
        return json_error('Access denied', 403)
    payments = load_payments()
    rows = [_present_invoice(row, payments) for row in _invoices_visible(user)]
    customer_id = clean_text(request.args.get('customer_id'), 80)
    status = clean_text(request.args.get('status'), 20).lower()
    date_from = _query_date('from')
    date_to = _query_date('to')
    filtered = []
    for row in rows:
        if user.get('role') != 'customer' and customer_id and row.get('customer_id') != customer_id:
            continue
        if status in {'unpaid', 'partial', 'paid'} and row.get('status') != status:
            continue
        day = str(row.get('invoice_date') or '')
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: row.get('id') or 0, reverse=True)
    return jsonify(filtered)


@app.route('/api/invoices/<int:invoice_id>', methods=['GET'])
@module_required('invoices')
def get_invoice(invoice_id):
    user = current_user()
    invoice = next((row for row in _invoices_visible(user) if row.get('id') == invoice_id), None)
    if not invoice:
        return json_error('Not found', 404)
    return jsonify(_present_invoice(invoice, load_payments()))


@app.route('/api/invoices/<int:invoice_id>/pdf', methods=['GET'])
@module_required('invoices')
def invoice_pdf(invoice_id):
    user = current_user()
    invoice = next((row for row in _invoices_visible(user) if row.get('id') == invoice_id), None)
    if not invoice:
        return json_error('Not found', 404)
    view = _present_invoice(invoice, load_payments())
    return _render_invoice_pdf(view)


def _render_invoice_pdf(invoice):
    from xml.sax.saxutils import escape
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.45 * inch, bottomMargin=0.45 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('InvTitle', parent=styles['Normal'], fontSize=14, fontName='Helvetica-Bold', alignment=1)
    small_style = ParagraphStyle('InvSmall', parent=styles['Normal'], fontSize=8, leading=10)
    cell_style = ParagraphStyle('InvCell', parent=styles['Normal'], fontSize=8, leading=10)
    header_cell = ParagraphStyle('InvHead', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.white)
    label_style = ParagraphStyle('InvLabel', parent=styles['Normal'], fontSize=9, leading=12)
    logo_path = 'logo.png' if os.path.exists('logo.png') else None
    logo = Image(logo_path, width=1.2 * inch, height=0.8 * inch) if logo_path else Paragraph(
        '<font color="#C41E3A" size="18"><b>ORA</b></font><br/><font size="10">GLASS</font>', small_style,
    )
    currency = str(invoice.get('currency') or 'usd').upper()
    header = [[
        logo,
        Paragraph('<b>Invoice</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title_style),
        Paragraph(
            f"<font size='8'><b>{escape(invoice.get('number') or '')}</b><br/>"
            f"Invoice date: {escape(invoice.get('invoice_date') or '')}<br/>"
            f"Due: {escape(invoice.get('due_date') or '')}<br/>"
            f"Terms: {escape(invoice.get('payment_terms') or 'Net 30')}</font>",
            small_style,
        ),
    ]]
    elements = []
    header_table = Table(header, colWidths=[1.5 * inch, 3.2 * inch, 2.6 * inch])
    header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements.append(header_table)
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width='100%', thickness=1, color=colors.black))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        f"<b>Customer:</b> {escape(invoice.get('company') or '')}<br/>"
        f"<b>Project:</b> {escape(invoice.get('project') or '-')}<br/>"
        f"<b>Order:</b> {escape(str(invoice.get('order_id') or ''))}"
        f" &nbsp; <b>Quote:</b> {escape(str(invoice.get('quote_id') or ''))}",
        label_style,
    ))
    elements.append(Spacer(1, 10))
    table_data = [[
        Paragraph('<b>Item</b>', header_cell),
        Paragraph('<b>Qty</b>', header_cell),
        Paragraph(f'<b>{currency}</b>', header_cell),
    ]]
    for item in invoice.get('items') or []:
        table_data.append([
            Paragraph(escape(str(item.get('name') or '')), cell_style),
            Paragraph(escape(str(item.get('pcs') or '')), cell_style),
            Paragraph(f"${money(item.get('total')):.2f}", cell_style),
        ])
    items_table = Table(table_data, colWidths=[5.2 * inch, 0.8 * inch, 1.3 * inch])
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C41E3A')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 10))
    discount = invoice.get('discount') or {}
    discount_label = f"{money(discount.get('value')):.2f}%" if discount.get('type') == 'percent' else 'fixed'
    summary_rows = [
        ['IGU subtotal', f"${money(invoice.get('igu_subtotal')):.2f}"],
        [f"Discount ({discount_label})", f"-${money(invoice.get('discount_amount')):.2f}"],
        ['IGU total', f"${money(invoice.get('igu_total')):.2f}"],
    ]
    for charge in invoice.get('extra_charges') or []:
        summary_rows.append([charge.get('label') or 'Extra', f"${money(charge.get('amount')):.2f}"])
    summary_rows.append(['Grand total', f"${money(invoice.get('total')):.2f}"])
    summary_rows.append(['Payments applied', f"${money(invoice.get('applied')):.2f}"])
    summary_rows.append(['Balance due', f"${money(invoice.get('balance')):.2f}"])
    summary = [[Paragraph(escape(label), cell_style), Paragraph(amount, cell_style)] for label, amount in summary_rows]
    summary_table = Table(summary, colWidths=[5.5 * inch, 1.8 * inch])
    summary_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, -3), (-1, -1), 'Helvetica-Bold'),
        ('LINEABOVE', (0, -3), (-1, -3), 0.5, colors.black),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 16))
    elements.append(Paragraph('<font size="7">Ora Glass Inc Terms And Conditions of Sale apply.</font>', small_style))
    doc.build(elements)
    buffer.seek(0)
    filename = f"{invoice.get('number') or 'invoice'}.pdf"
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)


def _present_payment(payment):
    view = dict(payment)
    view['unallocated'] = unallocated_amount(payment)
    view['allocated'] = round(money(payment.get('amount')) - view['unallocated'], 2) if not payment.get('voided') else 0
    return view


@app.route('/api/payments', methods=['GET'])
@login_required
def get_payments():
    user = current_user()
    if not _can_view_books(user):
        return json_error('Access denied', 403)
    customer_id, error = _scoped_customer_id(user, request.args.get('customer_id'))
    if user.get('role') == 'customer':
        rows = [row for row in load_payments() if row.get('customer_id') == user.get('customer_id')]
    else:
        if error and customer_id is None and clean_text(request.args.get('customer_id'), 80):
            return json_error(error, 400)
        requested = clean_text(request.args.get('customer_id'), 80)
        rows = load_payments()
        if requested:
            rows = [row for row in rows if row.get('customer_id') == requested]
    rows = [_present_payment(row) for row in rows]
    rows.sort(key=lambda row: row.get('id') or 0, reverse=True)
    return jsonify(rows)


@app.route('/api/payments', methods=['POST'])
@login_required
def record_payment():
    user = current_user()
    if not can_record_payments(user):
        return json_error('You do not have permission to record payments.', 403)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    customer_id = clean_text(data.get('customer_id'), 80)
    customer = load_customers().get(customer_id or '')
    if not customer:
        return json_error('Unknown customer.', 400)
    try:
        amount = money(data.get('amount'))
    except (TypeError, ValueError):
        return json_error('Amount must be a number.', 400)
    if amount <= 0:
        return json_error('Amount must be greater than zero.', 400)
    currency = clean_text(data.get('currency'), 8).lower()
    if currency not in CURRENCIES:
        return json_error('Currency must be USD or CAD.', 400)
    method = clean_text(data.get('method'), 20).lower()
    if method not in PAYMENT_METHODS:
        return json_error('Unknown payment method.', 400)
    paid_on = clean_text(data.get('date'), 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', paid_on):
        return json_error('Payment date is required.', 400)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    stored = append_payment({
        'customer_id': customer_id,
        'company': customer.get('name') or '',
        'date': paid_on,
        'amount': amount,
        'currency': currency,
        'method': method,
        'reference': clean_text(data.get('reference'), 80),
        'notes': clean_text(data.get('notes'), 500),
        'voided': False,
        'allocations': [],
        'recorded_by': user.get('email') or '',
        'recorded_at': now,
        'log': [{
            'at': now,
            'user': user.get('email') or '',
            'action': 'recorded',
            'amount': amount,
            'currency': currency,
        }],
    })
    return jsonify(_present_payment(stored))


@app.route('/api/payments/<int:payment_id>/allocations', methods=['POST'])
@login_required
def allocate_payment(payment_id):
    user = current_user()
    if not can_record_payments(user):
        return json_error('You do not have permission to record payments.', 403)
    data = json_body() or {}
    requested = data.get('allocations')
    if not isinstance(requested, list) or not requested:
        return json_error('Choose at least one allocation.', 400)
    payments = load_payments()
    payment = next((row for row in payments if row.get('id') == payment_id), None)
    if not payment:
        return json_error('Not found', 404)
    if payment.get('voided'):
        return json_error('A voided payment cannot be allocated.', 400)
    room = unallocated_amount(payment)
    additions = []
    for row in requested:
        if not isinstance(row, dict):
            return json_error('Each allocation needs a target and an amount.', 400)
        target_type = clean_text(row.get('target_type'), 20)
        if target_type not in {'order', 'invoice'}:
            return json_error('Allocate to an order or an invoice.', 400)
        try:
            target_id = int(row.get('target_id'))
            amount = money(row.get('amount'))
        except (TypeError, ValueError):
            return json_error('Each allocation needs a target and an amount.', 400)
        if amount <= 0:
            return json_error('Allocation amounts must be greater than zero.', 400)
        additions.append({'target_type': target_type, 'target_id': target_id, 'amount': amount})
    if round(sum(item['amount'] for item in additions), 2) - room > 0.009:
        return json_error('That allocates more than the unallocated balance.', 400)
    orders = load_orders()
    invoices = load_invoices()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pending = {}
    for item in additions:
        if item['target_type'] == 'order':
            order = next((row for row in orders if row.get('id') == item['target_id']), None)
            if not order or order.get('customer_id') != payment.get('customer_id'):
                return json_error('Unknown order for this customer.', 400)
            if str(order.get('currency') or 'usd').lower() != payment.get('currency'):
                return json_error('The order currency does not match the payment.', 400)
            if order_status(order) == 'delivered' or order.get('invoice_id'):
                return json_error('Allocate a delivered order through its invoice.', 400)
            covered = allocated_to_target(payments, payment.get('customer_id'), payment.get('currency'), 'order', order.get('id'))
            covered += pending.get(('order', order.get('id')), 0)
            if covered + item['amount'] - money(order.get('total')) > 0.009:
                return json_error('That is more than the order total.', 400)
            pending[('order', order.get('id'))] = pending.get(('order', order.get('id')), 0) + item['amount']
        else:
            invoice = next((row for row in invoices if row.get('id') == item['target_id']), None)
            if not invoice or invoice.get('customer_id') != payment.get('customer_id'):
                return json_error('Unknown invoice for this customer.', 400)
            if str(invoice.get('currency') or 'usd').lower() != payment.get('currency'):
                return json_error('The invoice currency does not match the payment.', 400)
            balance = invoice_balance(invoice, payments) - pending.get(('invoice', invoice.get('id')), 0)
            if item['amount'] - balance > 0.009:
                return json_error('That is more than the invoice balance.', 400)
            pending[('invoice', invoice.get('id'))] = pending.get(('invoice', invoice.get('id')), 0) + item['amount']
        item['at'] = now
        item['user'] = user.get('email') or ''
    payment['allocations'] = list(payment.get('allocations') or []) + additions
    log = list(payment.get('log') or [])
    log.append({
        'at': now,
        'user': user.get('email') or '',
        'action': 'allocated',
        'allocations': [{'target_type': item['target_type'], 'target_id': item['target_id'], 'amount': item['amount']} for item in additions],
    })
    payment['log'] = log
    for index, row in enumerate(payments):
        if row.get('id') == payment_id:
            payments[index] = payment
    save_payments(payments)
    _refresh_invoice_statuses(payments)
    return jsonify(_present_payment(payment))


def _refresh_invoice_statuses(payments):
    invoices = load_invoices()
    changed = False
    for invoice in invoices:
        status = invoice_status(invoice, payments)
        balance = invoice_balance(invoice, payments)
        if invoice.get('status') != status or money(invoice.get('balance')) != balance:
            invoice['status'] = status
            invoice['balance'] = balance
            changed = True
    if changed:
        save_invoices(invoices)


@app.route('/api/payments/<int:payment_id>/void', methods=['POST'])
@login_required
def void_payment(payment_id):
    user = current_user()
    if not can_record_payments(user):
        return json_error('You do not have permission to record payments.', 403)
    data = json_body() or {}
    reason = clean_text(data.get('reason'), 300)
    if not reason:
        return json_error('A void reason is required.', 400)
    payments = load_payments()
    payment = next((row for row in payments if row.get('id') == payment_id), None)
    if not payment:
        return json_error('Not found', 404)
    if payment.get('voided'):
        return json_error('This payment is already void.', 400)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payment['voided'] = True
    payment['void_reason'] = reason
    payment['voided_by'] = user.get('email') or ''
    payment['voided_at'] = now
    log = list(payment.get('log') or [])
    log.append({'at': now, 'user': user.get('email') or '', 'action': 'voided', 'reason': reason})
    payment['log'] = log
    for index, row in enumerate(payments):
        if row.get('id') == payment_id:
            payments[index] = payment
    save_payments(payments)
    _refresh_invoice_statuses(payments)
    return jsonify(_present_payment(payment))


@app.route('/api/credit', methods=['GET'])
@login_required
def get_credit():
    user = current_user()
    if not _can_view_books(user) and not user_has_module(user, 'orders'):
        return json_error('Access denied', 403)
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
    else:
        customer_id = clean_text(request.args.get('customer_id'), 80)
        if not customer_id:
            return json_error('Choose a customer.', 400)
    customer = load_customers().get(customer_id or '')
    if not customer:
        return json_error('Unknown customer.', 404)
    if user.get('role') == 'customer' and customer_id != user.get('customer_id'):
        return json_error('Access denied', 403)
    currency = clean_text(request.args.get('currency'), 8).lower() or str(customer.get('credit_currency') or 'usd').lower()
    if currency not in CURRENCIES:
        return json_error('Currency must be USD or CAD.', 400)
    orders = load_orders()
    invoices = load_invoices()
    payments = load_payments()
    available, limit, receivables = available_credit(customer, currency, orders, invoices, payments)
    return jsonify({
        'customer_id': customer_id,
        'company': customer.get('name') or '',
        'credit_limit': limit if str(customer.get('credit_currency') or 'usd').lower() == currency else money(customer.get('credit_limit') or 0),
        'credit_currency': str(customer.get('credit_currency') or 'usd').lower(),
        'currency': currency,
        'open_receivables': receivables,
        'available_credit': available,
        'unallocated_payments': unallocated_credit(payments, customer_id, currency),
        'read_only': user.get('role') == 'customer' or not can_record_payments(user),
    })


@app.route('/api/customers/<cid>/credit', methods=['PUT'])
@admin_required
def update_customer_credit(cid):
    if not ID_RE.fullmatch(cid or ''):
        return json_error('Not found', 404)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    customers = load_customers()
    customer = customers.get(cid)
    if not customer:
        return json_error('Not found', 404)
    error = apply_credit_limit(customer, data)
    if error:
        return json_error(error, 400)
    customers[cid] = customer
    save_customers(customers)
    return jsonify({
        'success': True,
        'credit_limit': customer.get('credit_limit'),
        'credit_currency': customer.get('credit_currency'),
    })


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


PUNCH_LABELS = {
    'in': 'Punch in',
    'out': 'Punch out',
    'break_start': 'Break start',
    'break_end': 'Break end',
}


def _remember_phone():
    session.permanent = True
    session['long_session'] = True
    session.modified = True


def _punch_snapshot(punch):
    return {
        'action': punch.get('action'),
        'at': punch.get('at'),
        'note': punch.get('note') or '',
        'lat': punch.get('lat'),
        'lng': punch.get('lng'),
        'accuracy': punch.get('accuracy'),
        'no_location': bool(punch.get('no_location')),
        'no_photo': bool(punch.get('no_photo', True)),
        'kiosk_id': punch.get('kiosk_id'),
    }


def _punch_view(punch):
    view = _punch_snapshot(punch)
    view.update({
        'id': punch.get('id'),
        'user': punch.get('user'),
        'action_label': PUNCH_LABELS.get(punch.get('action'), punch.get('action')),
        'pacific': pacific_label(punch.get('at')),
        'corrected': bool(punch.get('corrections')),
        'original': punch.get('original'),
        'corrections': punch.get('corrections') or [],
        'has_photo': bool(punch.get('photo')) and not punch.get('no_photo'),
    })
    return view


def _punch_owner(user):
    return user.get('account') or user.get('email')


def _punches_for(account):
    return [row for row in load_punches() if row.get('user') == account]


def _own_clock(user):
    rows = _punches_for(_punch_owner(user))
    status, since = clock_status(rows)
    today = pacific_date(datetime.now(timezone.utc))
    history_start = today - timedelta(days=13)
    week_start = today - timedelta(days=today.weekday())
    days, _period = summarize(rows, history_start, today)
    _week_days, week_hours = summarize(rows, week_start, today)
    history = [
        _punch_view(row) for row in sorted(rows, key=lambda row: (row.get('at') or '', row.get('id') or 0), reverse=True)
        if pacific_date(row.get('at')) >= history_start
    ]
    return {
        'status': status,
        'since': stamp(since) if since else None,
        'since_pacific': pacific_clock(since) if since else None,
        'today_hours': live_hours(rows, today, today),
        'week_hours': week_hours,
        'days': days,
        'history': history,
    }


def _can_punch(user):
    return uses_time_clock(user)


def _shop_gate():
    user = current_user()
    if not user or user.get('role') != 'shop':
        return None
    if request.endpoint in {
        'punch', 'punch_login', 'my_punches', 'create_punch', 'logout',
        'web_manifest', 'service_worker', 'static', 'kiosk', 'kiosk_workers',
        'kiosk_identify', 'kiosk_punch',
    }:
        return None
    if wants_json():
        return json_error('Access denied', 403)
    return redirect(url_for('punch'))


app.before_request(_shop_gate)


@app.route('/manifest.webmanifest')
def web_manifest():
    path = os.path.join(app.root_path, 'static', 'manifest.webmanifest')
    return send_file(path, mimetype='application/manifest+json')


@app.route('/sw.js')
def service_worker():
    path = os.path.join(app.root_path, 'static', 'sw.js')
    response = send_file(path, mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/punch', methods=['GET'])
def punch():
    user = current_user()
    if user and not _can_punch(user):
        return forbidden()
    if user:
        _remember_phone()
    return render_template('punch.html', user=user, can_manage=can_manage_time(user) if user else False)


WORKER_PIN_LIMIT = 5
KIOSK_PIN_LIMIT = 10


def _token_hash(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _kiosk_from_request():
    token = request.headers.get('X-Kiosk-Token') or request.cookies.get('kiosk_token') or ''
    digest = _token_hash(token)
    for kiosk in load_kiosks():
        if kiosk.get('revoked'):
            continue
        stored = kiosk.get('token_hash') or ''
        if len(stored) == len(digest) and hmac.compare_digest(stored, digest):
            return kiosk
    return None


def _save_user(key, user):
    users = load_users()
    users[key] = user
    save_users(users)


def _register_pin_failure(user, key, kiosk):
    if user and key:
        user['pin_failures'] = int(user.get('pin_failures') or 0) + 1
        if user['pin_failures'] >= WORKER_PIN_LIMIT:
            user['pin_locked'] = True
        _save_user(key, user)
    if kiosk:
        kiosks = load_kiosks()
        for row in kiosks:
            if row.get('id') == kiosk.get('id'):
                row['pin_failures'] = int(row.get('pin_failures') or 0) + 1
                if row['pin_failures'] >= KIOSK_PIN_LIMIT:
                    row['locked'] = True
                kiosk.update(row)
        save_kiosks(kiosks)


def _clear_pin_failure(user, key, kiosk):
    if user and key:
        user['pin_failures'] = 0
        user['pin_locked'] = False
        _save_user(key, user)
    if kiosk and (kiosk.get('pin_failures') or kiosk.get('locked')):
        kiosks = load_kiosks()
        for row in kiosks:
            if row.get('id') == kiosk.get('id') and not row.get('locked'):
                row['pin_failures'] = 0
        save_kiosks(kiosks)


def _check_pin(number, pin, kiosk=None):
    if kiosk and kiosk.get('locked'):
        return None, None, json_error('This tablet is locked. Ask an administrator to unlock it.', 423)
    key, user = find_shop_by_number(number)
    if user and user.get('pin_locked'):
        return None, None, json_error('This PIN is locked. Ask an administrator to unlock it.', 423)
    if not user or not user.get('active', True) or not verify_password(user.get('pin_hash'), pin):
        if user and user.get('active', True):
            _register_pin_failure(user, key, kiosk)
            if user.get('pin_locked') or (kiosk and kiosk.get('locked')):
                return None, None, json_error('Too many PIN attempts. Ask an administrator to unlock it.', 423)
        elif kiosk:
            _register_pin_failure(None, None, kiosk)
            if kiosk.get('locked'):
                return None, None, json_error('This tablet is locked. Ask an administrator to unlock it.', 423)
        return None, None, json_error('Invalid employee number or PIN.', 401)
    _clear_pin_failure(user, key, kiosk)
    return key, user, None


@app.route('/punch/login', methods=['POST'])
def punch_login():
    number = normalize_employee_number(request.form.get('employee_number'))
    pin = str(request.form.get('pin') or '').strip()
    ip = request.remote_addr or ''
    if login_limiter.is_limited('pin:' + number, ip):
        flash('Too many login attempts. Try again in a few minutes.', 'error')
        return render_template('punch.html', user=None, can_manage=False), 429
    key, user = find_shop_by_number(number)
    if user and user.get('pin_locked'):
        flash('This PIN is locked. Ask an administrator to unlock it.', 'error')
        return render_template('punch.html', user=None, can_manage=False), 423
    if not user or not user.get('active', True) or not user.get('allow_phone_punch') or not verify_password(user.get('pin_hash'), pin):
        if user and user.get('active', True) and user.get('allow_phone_punch'):
            _register_pin_failure(user, key, None)
            if user.get('pin_locked'):
                flash('This PIN is locked. Ask an administrator to unlock it.', 'error')
                return render_template('punch.html', user=None, can_manage=False), 423
        elif user and user.get('active', True) and not user.get('allow_phone_punch') and verify_password(user.get('pin_hash'), pin):
            flash('Use the shop tablet. Phone punch is not enabled for this worker.', 'error')
            return render_template('punch.html', user=None, can_manage=False), 403
        login_limiter.record_failure('pin:' + number, ip)
        flash('Invalid employee number or PIN.', 'error')
        return render_template('punch.html', user=None, can_manage=False)
    _clear_pin_failure(user, key, None)
    login_limiter.clear('pin:' + number, ip)
    session.clear()
    session['user'] = key
    session.permanent = True
    session['long_session'] = True
    return redirect(url_for('punch'))


def _kiosk_cookie(response, token):
    response.set_cookie(
        'kiosk_token',
        token,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite='Lax',
        secure=session_cookie_secure(),
    )
    return response


def _public_kiosk(kiosk):
    return {
        'id': kiosk.get('id'),
        'name': kiosk.get('name') or '',
        'created_at': kiosk.get('created_at'),
        'created_by': kiosk.get('created_by'),
        'revoked': bool(kiosk.get('revoked')),
        'locked': bool(kiosk.get('locked')),
    }


@app.route('/api/time-clock/kiosks', methods=['GET'])
@login_required
def list_kiosks():
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    return jsonify([_public_kiosk(row) for row in load_kiosks()])


@app.route('/api/time-clock/kiosks', methods=['POST'])
@login_required
def activate_kiosk():
    actor = current_user()
    if not can_manage_time(actor):
        return json_error('Access denied', 403)
    data = json_body() or {}
    name = clean_text(data.get('name') or 'Shop tablet', 80) or 'Shop tablet'
    token = secrets.token_urlsafe(32)
    kiosks = load_kiosks()
    kiosk = {
        'id': max([row.get('id') or 0 for row in kiosks] or [0]) + 1,
        'name': name,
        'token_hash': _token_hash(token),
        'created_at': stamp(datetime.now(timezone.utc)),
        'created_by': actor.get('email') or '',
        'revoked': False,
        'locked': False,
        'pin_failures': 0,
    }
    kiosks.append(kiosk)
    save_kiosks(kiosks)
    session.clear()
    response = jsonify({'success': True, 'kiosk': _public_kiosk(kiosk), 'token': token})
    return _kiosk_cookie(response, token)


@app.route('/api/time-clock/kiosks/<int:kiosk_id>/revoke', methods=['POST'])
@login_required
def revoke_kiosk(kiosk_id):
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    kiosks = load_kiosks()
    found = None
    for row in kiosks:
        if row.get('id') == kiosk_id:
            row['revoked'] = True
            row['revoked_at'] = stamp(datetime.now(timezone.utc))
            found = row
    if not found:
        return json_error('Not found', 404)
    save_kiosks(kiosks)
    return jsonify(_public_kiosk(found))


@app.route('/api/time-clock/kiosks/<int:kiosk_id>/unlock', methods=['POST'])
@login_required
def unlock_kiosk(kiosk_id):
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    kiosks = load_kiosks()
    found = None
    for row in kiosks:
        if row.get('id') == kiosk_id:
            row['locked'] = False
            row['pin_failures'] = 0
            found = row
    if not found:
        return json_error('Not found', 404)
    save_kiosks(kiosks)
    return jsonify(_public_kiosk(found))


@app.route('/api/users/<path:account>/pin/unlock', methods=['POST'])
@admin_required
def unlock_shop_pin(account):
    account = normalize_email(account)
    users = load_users()
    user = users.get(account)
    if not user or user.get('role') != 'shop':
        return json_error('Not found', 404)
    user['pin_locked'] = False
    user['pin_failures'] = 0
    users[account] = user
    save_users(users)
    return jsonify({'success': True})


def _store_punch_photo(punch_id, raw):
    try:
        from io import BytesIO as _Bytes
        from PIL import Image
        image = Image.open(_Bytes(raw)).convert('RGB')
    except Exception:
        return False
    image.thumbnail((640, 640))
    path = os.path.join(punch_photo_dir(), f'{int(punch_id)}.jpg')
    image.save(path, 'JPEG', quality=70)
    return True


def _purge_old_photos():
    days = load_time_settings().get('photo_retention_days', 90)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    punches = load_punches()
    changed = False
    for punch in punches:
        if not punch.get('photo'):
            continue
        try:
            moment = as_utc(punch.get('at'))
        except ValueError:
            continue
        if moment >= cutoff:
            continue
        path = os.path.join(punch_photo_dir(), f"{int(punch.get('id'))}.jpg")
        if os.path.exists(path):
            os.remove(path)
        punch['photo'] = False
        punch['no_photo'] = True
        punch['photo_purged'] = True
        changed = True
    if changed:
        save_punches(punches)


def _record_kiosk_punch(kiosk, user, key, action, note, photo_bytes, lat, lng, accuracy):
    moment = stamp(datetime.now(timezone.utc))
    existing = _punches_for(key)
    try:
        replay(existing + [{'id': 10 ** 9, 'action': action, 'at': moment}])
    except ValueError as exc:
        status = 409 if action == 'in' else 400
        return json_error(str(exc), status)
    no_location = lat is None or lng is None
    snapshot = {
        'action': action,
        'at': moment,
        'note': note,
        'lat': lat,
        'lng': lng,
        'accuracy': accuracy,
        'no_location': no_location,
        'no_photo': True,
        'kiosk_id': kiosk.get('id'),
    }
    stored = append_punch({
        'user': key,
        'photo': False,
        'corrections': [],
        'original': dict(snapshot),
        **snapshot,
    })
    if photo_bytes and _store_punch_photo(stored['id'], photo_bytes):
        punches = load_punches()
        for row in punches:
            if row.get('id') == stored['id']:
                row['photo'] = True
                row['no_photo'] = False
                row['original']['no_photo'] = False
                stored = row
        save_punches(punches)
    _purge_old_photos()
    status, since = clock_status(_punches_for(key))
    return jsonify({
        'success': True,
        'name': user.get('name') or '',
        'employee_number': user.get('employee_number') or '',
        'action': action,
        'action_label': PUNCH_LABELS.get(action, action),
        'pacific': pacific_label(moment),
        'status': status,
        'since_pacific': pacific_clock(since) if since else None,
        'no_photo': bool(stored.get('no_photo')),
        'kiosk_id': kiosk.get('id'),
    })


@app.route('/kiosk')
def kiosk():
    device = _kiosk_from_request()
    return render_template('kiosk.html', kiosk=_public_kiosk(device) if device else None)


@app.route('/api/kiosk/workers', methods=['GET'])
def kiosk_workers():
    device = _kiosk_from_request()
    if not device:
        return json_error('This tablet is not an active time clock.', 403)
    if device.get('locked'):
        return json_error('This tablet is locked. Ask an administrator to unlock it.', 423)
    rows = []
    for _key, user in load_users().items():
        if user.get('role') == 'shop' and user.get('active', True) and user.get('employee_number'):
            rows.append({'name': user.get('name') or '', 'employee_number': user.get('employee_number')})
    rows.sort(key=lambda row: row['name'].lower())
    return jsonify(rows)


@app.route('/api/kiosk/identify', methods=['POST'])
@csrf.exempt
def kiosk_identify():
    device = _kiosk_from_request()
    if not device:
        return json_error('This tablet is not an active time clock.', 403)
    data = json_body() or {}
    number = normalize_employee_number(data.get('employee_number'))
    pin = str(data.get('pin') or '').strip()
    key, user, error = _check_pin(number, pin, device)
    if error:
        return error
    status, since = clock_status(_punches_for(key))
    today = pacific_date(datetime.now(timezone.utc))
    return jsonify({
        'name': user.get('name') or '',
        'employee_number': user.get('employee_number') or '',
        'status': status,
        'since_pacific': pacific_clock(since) if since else None,
        'today_hours': live_hours(_punches_for(key), today, today),
    })


@app.route('/api/kiosk/punch', methods=['POST'])
@csrf.exempt
def kiosk_punch():
    device = _kiosk_from_request()
    if not device:
        return json_error('This tablet is not an active time clock.', 403)
    number = normalize_employee_number(request.form.get('employee_number') or (json_body() or {}).get('employee_number'))
    pin = str(request.form.get('pin') or (json_body() or {}).get('pin') or '').strip()
    action = clean_text(request.form.get('action') or (json_body() or {}).get('action'), 20)
    note = clean_text(request.form.get('note') or (json_body() or {}).get('note'), 300)
    if action not in ACTIONS:
        return json_error('Unknown punch.', 400)
    key, user, error = _check_pin(number, pin, device)
    if error:
        return error
    lat = lng = accuracy = None
    raw_lat = request.form.get('lat')
    raw_lng = request.form.get('lng')
    if raw_lat not in (None, '') and raw_lng not in (None, ''):
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
            raw_accuracy = request.form.get('accuracy')
            accuracy = None if raw_accuracy in (None, '') else float(raw_accuracy)
        except (TypeError, ValueError):
            lat = lng = accuracy = None
    photo = request.files.get('photo')
    photo_bytes = photo.read() if photo else b''
    return _record_kiosk_punch(device, user, key, action, note, photo_bytes, lat, lng, accuracy)


@app.route('/api/time-clock/photos/<int:punch_id>', methods=['GET'])
@login_required
def punch_photo(punch_id):
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    path = os.path.join(punch_photo_dir(), f'{int(punch_id)}.jpg')
    punch = next((row for row in load_punches() if row.get('id') == punch_id), None)
    if not punch or not punch.get('photo') or not os.path.exists(path):
        return json_error('Not found', 404)
    return send_file(path, mimetype='image/jpeg')


@app.route('/api/time-clock/settings', methods=['GET', 'PUT'])
@login_required
def time_clock_settings():
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    if request.method == 'GET':
        _purge_old_photos()
        return jsonify(load_time_settings())
    data = json_body() or {}
    try:
        days = int(data.get('photo_retention_days'))
    except (TypeError, ValueError):
        return json_error('Photo retention must be a number of days.', 400)
    if days < 90:
        return json_error('Photos are kept for at least 90 days.', 400)
    if days > 3650:
        return json_error('Photo retention is too long.', 400)
    save_time_settings({'photo_retention_days': days})
    return jsonify(load_time_settings())


@app.route('/time-clock')
@login_required
def time_clock_admin():
    if not can_manage_time(current_user()):
        return forbidden()
    return render_template('time_clock.html')


@app.route('/api/punches/me', methods=['GET'])
@login_required
def my_punches():
    user = current_user()
    if not _can_punch(user):
        return json_error('Access denied', 403)
    _remember_phone()
    return jsonify(_own_clock(user))


@app.route('/api/users/<path:account>/pin', methods=['POST'])
@admin_required
def reset_shop_pin(account):
    account = normalize_email(account)
    data = json_body() or {}
    pin = str(data.get('pin') or '').strip()
    if not pin_ok(pin):
        return json_error('PIN must be 4 to 6 digits.', 400)
    users = load_users()
    user = users.get(account)
    if not user:
        return json_error('Not found', 404)
    if user.get('role') != 'shop':
        return json_error('Only shop workers use a PIN.', 400)
    user['pin_hash'] = hash_password(pin)
    user['pin_failures'] = 0
    user['pin_locked'] = False
    users[account] = user
    save_users(users)
    return jsonify({'success': True})


@app.route('/api/punches', methods=['POST'])
@login_required
def create_punch():
    user = current_user()
    if not _can_punch(user):
        return json_error('Access denied', 403)
    if user.get('role') == 'shop' and not user.get('allow_phone_punch'):
        return json_error('Use the shop tablet. Phone punch is not enabled for this worker.', 403)
    _remember_phone()
    data = json_body() or {}
    action = clean_text(data.get('action'), 20)
    if action not in ACTIONS:
        return json_error('Unknown punch.', 400)
    note = clean_text(data.get('note'), 300)
    lat = lng = accuracy = None
    no_location = True
    if data.get('lat') not in (None, '') or data.get('lng') not in (None, ''):
        try:
            lat = float(data.get('lat'))
            lng = float(data.get('lng'))
            accuracy = None if data.get('accuracy') in (None, '') else float(data.get('accuracy'))
        except (TypeError, ValueError):
            return json_error('Location must be numbers.', 400)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return json_error('Location is out of range.', 400)
        no_location = False
    moment = stamp(datetime.now(timezone.utc))
    existing = _punches_for(_punch_owner(user))
    try:
        replay(existing + [{'id': 10 ** 9, 'action': action, 'at': moment}])
    except ValueError as exc:
        status = 409 if action == 'in' else 400
        return json_error(str(exc), status)
    snapshot = {
        'action': action,
        'at': moment,
        'note': note,
        'lat': lat,
        'lng': lng,
        'accuracy': accuracy,
        'no_location': no_location,
        'no_photo': True,
        'kiosk_id': None,
    }
    append_punch({
        'user': _punch_owner(user),
        'photo': False,
        'corrections': [],
        'original': dict(snapshot),
        **snapshot,
    })
    return jsonify(_own_clock(user))


def _date_arg(name):
    text = clean_text(request.args.get(name), 10)
    if not text:
        return None
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def _timesheet_rows(user_email, start, end):
    punches = load_punches()
    if user_email:
        punches = [row for row in punches if row.get('user') == user_email]
    selected = []
    for row in punches:
        day = pacific_date(row.get('at'))
        if start and day < start:
            continue
        if end and day > end:
            continue
        selected.append(row)
    by_user = {}
    for row in selected:
        by_user.setdefault(row.get('user'), []).append(row)
    days = []
    for email, rows in sorted(by_user.items()):
        summarized, _total = summarize(rows, start, end)
        for day in summarized:
            days.append(dict(day, user=email))
    period_hours = round(sum(day['hours'] for day in days), 2)
    selected.sort(key=lambda row: (row.get('at') or '', row.get('id') or 0))
    return selected, days, period_hours


@app.route('/api/time-clock', methods=['GET'])
@login_required
def time_clock_data():
    user = current_user()
    if not can_manage_time(user):
        return json_error('Access denied', 403)
    start = _date_arg('from')
    end = _date_arg('to')
    email = normalize_email(request.args.get('user'))
    _purge_old_photos()
    punches, days, period_hours = _timesheet_rows(email, start, end)
    employees = []
    for key, record in load_users().items():
        if record.get('role') == 'customer':
            continue
        label = record.get('name') or key
        if record.get('employee_number'):
            label = f"{label} ({record.get('employee_number')})"
        employees.append({'email': key, 'name': label})
    employees.sort(key=lambda row: (row['name'] or '').lower())
    names = {key: (record.get('name') or key) for key, record in load_users().items()}
    punch_rows = []
    for row in punches:
        view = _punch_view(row)
        view['name'] = names.get(row.get('user'), row.get('user'))
        punch_rows.append(view)
    return jsonify({
        'employees': employees,
        'punches': punch_rows,
        'days': days,
        'period_hours': period_hours,
    })


@app.route('/api/time-clock/<int:punch_id>/correct', methods=['POST'])
@login_required
def correct_punch(punch_id):
    actor = current_user()
    if not can_manage_time(actor):
        return json_error('Access denied', 403)
    data = json_body() or {}
    reason = clean_text(data.get('reason'), 300)
    if not reason:
        return json_error('A reason is required.', 400)

    def mutator(punch, all_rows):
        updated = dict(punch)
        if 'action' in data and data.get('action'):
            action = clean_text(data.get('action'), 20)
            if action not in ACTIONS:
                return None, 'Unknown punch.'
            updated['action'] = action
        if 'note' in data:
            updated['note'] = clean_text(data.get('note'), 300)
        if data.get('at_pacific'):
            try:
                updated['at'] = stamp(as_utc(data.get('at_pacific'), naive_is_pacific=True))
            except ValueError:
                return None, 'That time is not valid.'
        if not updated.get('original'):
            updated['original'] = _punch_snapshot(punch)
        trial = []
        for row in all_rows:
            if row.get('user') != punch.get('user'):
                continue
            trial.append(updated if row.get('id') == punch.get('id') else row)
        try:
            replay(trial)
        except ValueError as exc:
            return None, str(exc)
        before = _punch_snapshot(punch)
        after = _punch_snapshot(updated)
        corrections = list(updated.get('corrections') or [])
        corrections.append({
            'at': stamp(datetime.now(timezone.utc)),
            'by': actor.get('email') or '',
            'reason': reason,
            'before': before,
            'after': after,
        })
        updated['corrections'] = corrections
        return updated, None

    saved, error = update_punch(punch_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(_punch_view(saved))


@app.route('/api/time-clock/export.csv', methods=['GET'])
@login_required
def export_time_csv():
    import csv
    from io import StringIO
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    email = normalize_email(request.args.get('user'))
    punches, days, period_hours = _timesheet_rows(email, _date_arg('from'), _date_arg('to'))
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['id', 'user', 'action', 'at_utc', 'at_pacific', 'note', 'lat', 'lng', 'accuracy', 'no_location', 'corrected'])
    for row in punches:
        view = _punch_view(row)
        writer.writerow([
            view['id'], view['user'], view['action'], view['at'], view['pacific'], view['note'],
            '' if view['lat'] is None else view['lat'], '' if view['lng'] is None else view['lng'],
            '' if view['accuracy'] is None else view['accuracy'], 'yes' if view['no_location'] else 'no',
            'yes' if view['corrected'] else 'no',
        ])
    writer.writerow([])
    writer.writerow(['date', 'user', 'hours', 'missing_punch_out'])
    for day in days:
        writer.writerow([day['date'], day['user'], f"{day['hours']:.2f}", 'yes' if day['missing_out'] else 'no'])
    writer.writerow(['period_hours', '', f'{period_hours:.2f}', ''])
    payload = buffer.getvalue().encode('utf-8')
    return send_file(
        BytesIO(payload), mimetype='text/csv', as_attachment=True, download_name='timesheet.csv',
    )


@app.route('/api/time-clock/timesheet.pdf', methods=['GET'])
@login_required
def export_time_pdf():
    if not can_manage_time(current_user()):
        return json_error('Access denied', 403)
    email = normalize_email(request.args.get('user'))
    if not email:
        return json_error('Choose an employee.', 400)
    start = _date_arg('from')
    end = _date_arg('to')
    punches, days, period_hours = _timesheet_rows(email, start, end)
    users = load_users()
    name = (users.get(email) or {}).get('name') or email
    return _timesheet_pdf(name, email, start, end, punches, days, period_hours)


def _timesheet_pdf(name, email, start, end, punches, days, period_hours):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, HRFlowable

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.45 * inch, bottomMargin=0.45 * inch)
    styles = getSampleStyleSheet()
    small = ParagraphStyle('TsSmall', parent=styles['Normal'], fontSize=8, leading=10)
    title = ParagraphStyle('TsTitle', parent=styles['Normal'], fontSize=14, fontName='Helvetica-Bold', alignment=1)
    cell = ParagraphStyle('TsCell', parent=styles['Normal'], fontSize=8, leading=10)
    head = ParagraphStyle('TsHead', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.white)
    logo_path = 'logo.png' if os.path.exists('logo.png') else None
    logo = Image(logo_path, width=1.2 * inch, height=0.8 * inch) if logo_path else Paragraph('ORA GLASS', title)
    period = f"{start.isoformat() if start else 'start'} to {end.isoformat() if end else 'today'}"
    elements = []
    header = Table([[
        logo,
        Paragraph('<b>Timesheet</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title),
        Paragraph(f"<font size='8'><b>{escape(name)}</b><br/>{escape(email)}<br/>{escape(period)}<br/>Pacific time</font>", small),
    ]], colWidths=[1.5 * inch, 3.1 * inch, 2.7 * inch])
    header.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements.append(header)
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width='100%', thickness=1, color=colors.black))
    elements.append(Spacer(1, 8))
    table_data = [[Paragraph(f'<b>{label}</b>', head) for label in ('Date', 'Hours', 'Missing punch-out')]]
    for day in days:
        table_data.append([
            Paragraph(escape(day['date']), cell),
            Paragraph(f"{day['hours']:.2f}", cell),
            Paragraph('Yes' if day['missing_out'] else '', cell),
        ])
    table_data.append([
        Paragraph('<b>Period total</b>', cell),
        Paragraph(f'<b>{period_hours:.2f}</b>', cell),
        Paragraph('', cell),
    ])
    table = Table(table_data, colWidths=[2.2 * inch, 1.5 * inch, 3.6 * inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9f1830')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.black),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f6f6f6')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 12))
    punch_data = [[Paragraph(f'<b>{label}</b>', head) for label in ('When', 'Action', 'Note', 'Location')]]
    for row in punches:
        view = _punch_view(row)
        place = 'No location' if view['no_location'] else f"{view['lat']}, {view['lng']}"
        punch_data.append([
            Paragraph(escape(view['pacific']), cell),
            Paragraph(escape(view['action_label']), cell),
            Paragraph(escape(view['note']), cell),
            Paragraph(escape(place), cell),
        ])
    if len(punch_data) == 1:
        punch_data.append([Paragraph('No punches', cell), '', '', ''])
    details = Table(punch_data, colWidths=[1.7 * inch, 1.3 * inch, 2.2 * inch, 2.1 * inch])
    details.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9f1830')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(details)
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='timesheet.pdf')


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

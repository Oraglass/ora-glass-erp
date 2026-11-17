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
import tempfile
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from zoneinfo import ZoneInfo

import click
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

import shipments
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
    credit_note_applied,
    document_type,
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
    unapplied_credits,
)
import production
from quoting import (
    allowed_tier_ids,
    area_sqf,
    calculate_price_sqf,
    classify,
    DRAWING_NOTE,
    dwg_creation_line,
    expand_irregular_lines,
    irregular_glass_lines,
    default_quote_tier,
    load_pricing,
    reprice_size_line,
    resolve_effective_tier,
    sealant_match_map,
    suggest_sealant,
    tier_for_request,
)
from order_import import (
    OrderImportError,
    add_import,
    add_request,
    check_upload,
    choose_template,
    company_mappings,
    load_imports,
    load_requests,
    load_templates,
    open_request_count,
    parse_upload,
    public_request,
    public_template,
    remember_mappings,
    replace_import,
    safe_filename,
    save_requests,
    save_templates,
    store_upload,
    upload_abspath,
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
from payroll import (
    GROSS_LABEL,
    WEEKDAY_NAMES,
    attendance_range,
    build_stub,
    normalize_settings as normalize_payroll_settings,
    period_containing,
    q_money,
    validate_settings as validate_payroll_settings,
    weeks_covering,
)
from storage import (
    append_payroll_audit,
    append_punch,
    append_quote,
    load_kiosks,
    load_pay_adjustments,
    load_pricing_settings,
    save_pricing_settings,
    load_pay_rates,
    load_paystubs,
    load_payroll_audit,
    load_shift_assignments,
    load_shifts,
    load_time_settings,
    punch_photo_dir,
    save_kiosks,
    save_pay_adjustments,
    save_pay_rates,
    save_paystubs,
    save_shift_assignments,
    save_shifts,
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
    MAX_CONTENT_LENGTH=9 * 1024 * 1024,
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


def _price_options():
    import pricing_catalog
    return pricing_catalog.calc_options()


def _live_materials():
    import pricing_catalog
    return pricing_catalog.material_lists()


def _percent(value, limit=100):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    if number > limit:
        return float(limit)
    return number


def _price_factor(percent):
    return 1 + _percent(percent, 1000) / 100.0


def _scale_prices(priced, factor):
    if not priced or factor == 1:
        return priced
    priced = dict(priced)
    priced['price_sqf'] = round(priced['price_sqf'] * factor, 2)
    priced['price_pc'] = round(priced['price_pc'] * factor, 2)
    priced['total'] = round(priced['price_pc'] * priced['pcs'], 2)
    return priced


def _sell_price(data, tier, tiers, price_factor=1):
    options = _price_options()
    price = calculate_price_sqf(
        data, tier, options['pricing'], tiers,
        temper_prices=options['temper_prices'],
        tier5_scale=options['tier5_scale'],
        unknown_temper=options['unknown_temper'],
        extra_per_sqf=options['extra_per_sqf'],
        triple_factor=options['triple_factor'],
    )
    return round(price * (price_factor or 1), 2)


def _effective_minimum(customer):
    """Company minimum per piece wins. Otherwise the Pricing formula minimum is used."""
    try:
        company = float((customer or {}).get('minimum_sqf_per_piece') or 0)
    except (TypeError, ValueError):
        company = 0.0
    if company > 0:
        return company
    return float(_price_options().get('minimum_sqf') or 0)


def _reprice(spec, pcs, tier, currency, tiers, fx_rate=None, price_factor=1, minimum_sqf=None):
    options = _price_options()
    minimum = options['minimum_sqf'] if minimum_sqf is None else minimum_sqf
    priced = reprice_size_line(
        spec, pcs, tier, currency, options['pricing'], tiers, fx_rate,
        temper_prices=options['temper_prices'],
        tier5_scale=options['tier5_scale'],
        unknown_temper=options['unknown_temper'],
        extra_per_sqf=options['extra_per_sqf'],
        triple_factor=options['triple_factor'],
        minimum_sqf=minimum,
        minimum_charge=options['minimum_charge'],
    )
    return _scale_prices(priced, price_factor or 1)


def _size_price_fields(priced):
    return {
        'sqf': priced['sqf'],
        'real_sqf': priced.get('real_sqf', priced['sqf']),
        'minimum_sqf': priced.get('minimum_sqf') or 0,
        'price_sqf': priced['price_sqf'],
        'price_pc': priced['price_pc'],
        'pcs': priced['pcs'],
        'total': priced['total'],
    }

_state = {'bootstrapped': False}


def reset_runtime_state():
    _state['bootstrapped'] = False
    login_limiter.reset()


def json_error(message, status):
    return jsonify({'error': message}), status


def _qbo(action, *args):
    """QuickBooks sync never rolls back an ERP save. Failures stay in the sync log."""
    try:
        import quickbooks
        getattr(quickbooks, action)(*args)
    except Exception:
        logger.exception('QuickBooks %s failed', action)


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
        # Orders follows "Can place production orders", not the module checkbox.
        if can_place_production_orders(user):
            if 'orders' not in chosen:
                chosen.append('orders')
        else:
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


def can_manage_payroll(user):
    """Admins always can. Other staff need Manage payroll. Shop workers and customers cannot."""
    if not user or user.get('role') in {'customer', 'shop'}:
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('manage_payroll'))


def apply_manage_payroll(record, data):
    role = record.get('role')
    if role == 'admin' or role == 'customer' or role == 'shop' or role not in LEGACY_STAFF_ROLES:
        record['manage_payroll'] = False
        return
    if 'manage_payroll' in data:
        record['manage_payroll'] = bool(data.get('manage_payroll'))
    elif 'manage_payroll' not in record:
        record['manage_payroll'] = False


def uses_time_clock(user):
    """Shop workers punch on the tablet. Other staff punch only when Uses time clock is on."""
    if not user or user.get('role') in {'customer', 'admin'}:
        return False
    if user.get('role') == 'shop':
        return True
    if 'uses_time_clock' in user:
        return bool(user.get('uses_time_clock'))
    return 'time-clock' in (user.get('modules') or [])


def apply_uses_time_clock(record, data):
    """Employees punch only when Uses time clock is on. Shop workers never get modules."""
    role = record.get('role')
    if role == 'shop':
        record['modules'] = []
        record['uses_time_clock'] = False
        record['manage_time'] = False
        record['manage_payroll'] = False
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
    record['modules'] = [module_id for module_id in modules if module_id != 'time-clock']
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


def _resale_label(customer):
    import resale
    return resale.resale_label(customer)


def _qbo_tax_label(customer):
    if customer.get('tax_exempt'):
        import quickbooks
        exempt = quickbooks.suggested_tax_code('', tax_exempt=True)
        name = quickbooks.tax_code_name(exempt or customer.get('qbo_tax_code'))
        return name or 'Tax exempt'
    import quickbooks
    return quickbooks.tax_code_name(customer.get('qbo_tax_code'))


def _current_fx():
    import currency_rules
    return currency_rules.rate_on(date.today())


def _stored_fx(record):
    """The USD/CAD rate saved on a quote or order. Missing rates use the rate in effect today."""
    try:
        rate = float((record or {}).get('fx_rate'))
    except (TypeError, ValueError):
        rate = 0
    if rate > 0:
        return rate
    rate, _effective = _current_fx()
    return rate


def _currency_factor(target, fx_rate):
    """Multiply a USD amount to reach the target currency. CAD uses the stored ERP rate."""
    if str(target or 'usd').lower() != 'cad':
        return 1.0
    return float(fx_rate) if fx_rate else 1.0


def _switch_factor(target, fx_rate):
    """Convert an amount that is already in the other currency."""
    rate = float(fx_rate) if fx_rate else 1.0
    if rate <= 0:
        rate = 1.0
    return rate if str(target or 'usd').lower() == 'cad' else (1 / rate)


def _required_currency(customer):
    import currency_rules
    return currency_rules.customer_currency(customer or {})


def _shown_credit_currency(customer):
    import quickbooks
    if quickbooks.is_canadian(customer):
        return _required_currency(customer)
    return str((customer or {}).get('credit_currency') or 'usd').lower()


def _lock_credit_currency(customer):
    import quickbooks
    if quickbooks.is_canadian(customer):
        customer['credit_currency'] = _required_currency(customer)


def _erp_fx(day):
    """CAD per 1 USD from the rate history, for the invoice date."""
    import currency_rules
    return currency_rules.rate_on(day)


def public_customer(customer_id, customer, tiers, user_count):
    return {
        'id': customer_id,
        'name': customer.get('name') or '',
        'domain': customer.get('domain') or '',
        'address': customer.get('address') or '',
        'city': customer.get('city') or '',
        'state': customer.get('state') or '',
        'zip': customer.get('zip') or '',
        'country': customer.get('country') or 'United States',
        'province': customer.get('province') or '',
        'pst_number': customer.get('pst_number') or '',
        'qst_number': customer.get('qst_number') or '',
        'tax_code_override': bool(customer.get('tax_code_override')),
        'tax_exempt': bool(customer.get('tax_exempt')),
        'qbo_tax_code': customer.get('qbo_tax_code') or '',
        'qbo_tax_code_label': _qbo_tax_label(customer),
        'us_state': customer.get('us_state') or '',
        'export_no_tax': bool(customer.get('export_no_tax')),
        'resale_label': _resale_label(customer),
        'phone': customer.get('phone') or '',
        'email': customer.get('email') or '',
        'credit_approved': bool(customer.get('credit_approved')),
        'payment_terms': customer.get('payment_terms') or 'Net 30',
        'payment_terms_days': payment_term_days(customer),
        'credit_limit': money(customer.get('credit_limit') or 0),
        'credit_currency': _shown_credit_currency(customer),
        'tier': customer.get('tier') or '',
        'tier_label': tier_label(customer.get('tier'), tiers),
        'additional_discount_percent': _percent(customer.get('additional_discount_percent')),
        'minimum_sqf_per_piece': _percent(customer.get('minimum_sqf_per_piece'), 100000),
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
        'place_production_orders': can_place_production_orders(user) if user.get('role') == 'customer' else False,
        'edit_orders': bool(user.get('edit_orders')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'update_order_status': bool(user.get('update_order_status')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'record_payments': bool(user.get('record_payments')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'issue_invoices': bool(user.get('issue_invoices')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'manage_time': bool(user.get('manage_time')) if user.get('role') in LEGACY_STAFF_ROLES else False,
        'manage_payroll': bool(user.get('manage_payroll')) if user.get('role') in LEGACY_STAFF_ROLES else False,
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
    return 'Management' if customer_subrole(user) == 'management' else 'Operations'


def can_place_production_orders(user):
    """Customer privilege to send a quote to production.

    A missing value keeps today's rule: Management can, Operations cannot.
    Ora Glass staff are not this privilege; they use the Orders module.
    """
    if not user or user.get('role') != 'customer':
        return False
    if 'place_production_orders' in user:
        return bool(user.get('place_production_orders'))
    return customer_subrole(user) == 'management'


def apply_place_production_orders(record, data):
    if record.get('role') != 'customer':
        record.pop('place_production_orders', None)
        return
    if 'place_production_orders' in data:
        record['place_production_orders'] = bool(data.get('place_production_orders'))
    elif 'place_production_orders' not in record:
        record['place_production_orders'] = customer_subrole(record) == 'management'


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
        return 'Customer role must be Management or Operations.'
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


def staff_can_order(user):
    """Admins and Ora Glass staff who have the Orders module. Customers never qualify."""
    if not user or user.get('role') == 'customer':
        return False
    return user_has_module(user, 'orders')


def _is_ora_staff(user):
    role = (user or {}).get('role') or ''
    return role == 'admin' or role in LEGACY_STAFF_ROLES


def can_edit_job_address(user, order=None):
    """Ora Glass staff with Orders, and Management at that company, can correct the job address."""
    if not user:
        return False
    if order and user.get('role') == 'customer' and user.get('customer_id') != order.get('customer_id'):
        return False
    if staff_can_order(user):
        return True
    return user.get('role') == 'customer' and customer_subrole(user) == 'management'


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


def can_issue_invoices(user):
    """Admins can create invoices and credit notes. Employees need Issue invoices."""
    if not _is_ora_staff(user):
        return False
    if user.get('role') == 'admin':
        return True
    return bool(user.get('issue_invoices'))


def apply_order_privileges(record, data):
    """Store staff-only order privileges. Customers and admins never keep them."""
    role = record.get('role')
    if role == 'admin' or role == 'customer' or role not in LEGACY_STAFF_ROLES:
        record['edit_orders'] = False
        record['update_order_status'] = False
        record['record_payments'] = False
        record['issue_invoices'] = False
        return
    for field in ('edit_orders', 'update_order_status', 'record_payments', 'issue_invoices'):
        if field in data:
            record[field] = bool(data.get(field))
        elif field not in record:
            record[field] = False


def account_customer(customer_id, customers):
    """A customer company a person can be linked to."""
    if not customer_id or not ID_RE.fullmatch(customer_id or ''):
        return None
    customer = (customers or {}).get(customer_id)
    if not customer:
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
        'country': 'United States',
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
    if creating or 'country' in data:
        country = clean_text(data.get('country') or customer.get('country') or 'United States', 80)
        lowered = country.lower()
        if lowered in {'canada', 'ca'}:
            country = 'Canada'
        elif lowered in {'', 'us', 'usa', 'united states', 'united states of america'}:
            country = 'United States'
        customer['country'] = country
    if creating or 'province' in data or 'state' in data or 'zip' in data or 'address' in data or 'country' in data:
        from storage import PROVINCE_CODES, US_STATE_CODES, inferred_province, inferred_us_state
        chosen = clean_text(data.get('province') if 'province' in data else customer.get('province'), 8).upper()
        if chosen not in PROVINCE_CODES and customer.get('country') == 'Canada':
            probe = dict(customer)
            probe['province'] = ''
            chosen = inferred_province(probe)
        customer['province'] = chosen if chosen in PROVINCE_CODES else ''
        state = clean_text(data.get('us_state') if 'us_state' in data else customer.get('us_state'), 8).upper()
        if state not in US_STATE_CODES and customer.get('country') != 'Canada':
            probe = dict(customer)
            probe['us_state'] = ''
            state = inferred_us_state(probe)
        customer['us_state'] = state if state in US_STATE_CODES else ''
    if 'export_no_tax' in data:
        customer['export_no_tax'] = bool(data.get('export_no_tax'))
    if creating or 'tax_exempt' in data:
        customer['tax_exempt'] = bool(data.get('tax_exempt'))
    if creating or 'pst_number' in data:
        customer['pst_number'] = clean_text(data.get('pst_number'), 40)
    if creating or 'qst_number' in data:
        customer['qst_number'] = clean_text(data.get('qst_number'), 40)
    if creating or 'tax_code_override' in data:
        customer['tax_code_override'] = bool(data.get('tax_code_override'))
    if creating or 'qbo_tax_code' in data or 'province' in data or 'us_state' in data or 'tax_exempt' in data or 'export_no_tax' in data:
        code = clean_text(data.get('qbo_tax_code') if 'qbo_tax_code' in data else customer.get('qbo_tax_code'), 40)
        if not code and not customer.get('export_no_tax'):
            import quickbooks
            if customer.get('country') == 'Canada' and customer.get('tax_exempt'):
                code = quickbooks.suggested_tax_code(customer.get('province'), True)
            elif customer.get('country') == 'United States':
                code = quickbooks.suggested_us_tax_code(customer.get('us_state'))
        customer['qbo_tax_code'] = code
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
    _lock_credit_currency(customer)
    if creating or 'credit_approved' in data:
        customer['credit_approved'] = bool(data.get('credit_approved'))
    if creating or 'tier' in data:
        tier = clean_text(data.get('tier'), 20)
        if tier not in tiers:
            return 'Unknown pricing tier.'
        customer['tier'] = tier
    if creating or 'additional_discount_percent' in data:
        customer['additional_discount_percent'] = _percent(data.get('additional_discount_percent'))
    if creating or 'price_addon_percent' in data:
        customer['price_addon_percent'] = _percent(data.get('price_addon_percent'), 1000)
    if creating or 'minimum_sqf_per_piece' in data:
        customer['minimum_sqf_per_piece'] = _percent(data.get('minimum_sqf_per_piece'), 100000)
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
    try:
        import highq_owners
        highq_owners.reassign_quote_owners()
    except Exception:
        logger.exception('HighQ quote owner fix failed')
    _state['bootstrapped'] = True


@app.before_request
def _bootstrap_before_request():
    bootstrap_once()
    try:
        import quickbooks
        quickbooks.maybe_poll()
    except Exception:
        logger.exception('QuickBooks poll failed')


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
    nav = [item for item in MODULES if item['id'] in allowed]
    extras = []
    if user and _is_ora_staff(user):
        extras.append({'id': 'production-settings', 'label': 'Production settings', 'endpoint': 'production_settings'})
    if user and staff_can_order(user):
        extras.append({'id': 'shipments', 'label': 'Shipments', 'endpoint': 'shipments_page'})
    if extras:
        placed = False
        for index, item in enumerate(nav):
            if item['id'] == 'orders':
                nav[index + 1:index + 1] = extras
                placed = True
                break
        if not placed:
            nav.extend(extras)
    if user and user.get('role') == 'customer':
        nav.append({'id': 'company', 'label': 'Company', 'endpoint': 'company_page'})
    return {
        'current_user': user,
        'nav_modules': nav,
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


def _quickbooks_expired(user):
    if not user or user.get('role') != 'admin':
        return False
    import quickbooks
    return quickbooks.any_connection_expired()


def role_display_name(user):
    role = (user or {}).get('role') or ''
    if role in ROLES:
        return ROLES[role]['name']
    return {'sales': 'Ora Glass employee', 'production': 'Ora Glass employee', 'viewer': 'Ora Glass employee'}.get(role, role)


def _own_customer(user):
    if not user or user.get('role') != 'customer':
        return None
    return load_customers().get(user.get('customer_id') or '')


def _customer_uses_resale(user):
    import quickbooks
    customer = _own_customer(user)
    return bool(customer) and not quickbooks.is_canadian(customer)


def _canada_tax_label(user):
    import quickbooks
    customer = _own_customer(user)
    if not customer or not quickbooks.is_canadian(customer):
        return ''
    if str(customer.get('province') or '').upper() == 'QC':
        return 'QST number'
    return 'PST number'


def _resale_warnings(user):
    import resale
    customers = load_customers()
    if user.get('role') == 'customer':
        customer = customers.get(user.get('customer_id') or '')
        scoped = {user.get('customer_id'): customer} if customer else {}
        return resale.warnings_for(scoped)
    if user.get('role') in {'admin', 'employee'}:
        return resale.warnings_for(customers)
    return []


def _can_review_resale(user):
    return bool(user) and user.get('role') in {'admin', 'employee'}


def _can_view_resale(user, customer_id):
    if not user:
        return False
    if _can_review_resale(user):
        return True
    return user.get('role') == 'customer' and user.get('customer_id') == customer_id


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
        can_payroll=can_manage_payroll(user),
        can_time=can_manage_time(user),
        template_requests=open_request_count() if user.get('role') == 'admin' else 0,
        qbo_expired=_quickbooks_expired(user),
        resale_warnings=_resale_warnings(user),
        show_resale=_customer_uses_resale(user),
        canada_tax_label=_canada_tax_label(user),
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
    glass, spacers, sealants = _live_materials()
    return {
        'glass': glass,
        'spacers': spacers,
        'sealants': sealants,
        'can_choose_tier': not locked,
        'tiers': choices,
        'default_tier': effective,
        'locked_tier': locked_id,
        'locked_tier_label': tier_label(locked_id, tiers),
        'sealant_matches': sealant_match_map(spacers, sealants),
        'import_admin': user.get('role') == 'admin',
        'import_customer': user.get('role') == 'customer',
        'import_customer_id': user.get('customer_id') or '',
        'irregular_rate': load_pricing_settings()['irregular_shape_surcharge'],
        'drawing_note': DRAWING_NOTE,
        'erp_rate': _current_fx()[0] if user.get('role') != 'customer' else 1,
        'show_fx_rate': user.get('role') != 'customer',
        'company_discount_percent': _percent((customers.get(user.get('customer_id') or '') or {}).get('additional_discount_percent')) if user.get('role') == 'customer' else 0,
        'company_minimum_sqf': _effective_minimum(customers.get(user.get('customer_id') or '')) if user.get('role') == 'customer' else 0,
        'formula_minimum_sqf': float(_price_options().get('minimum_sqf') or 0),
        'currency_locked': user.get('role') == 'customer',
        'locked_currency': _required_currency(customers.get(user.get('customer_id') or '')) if user.get('role') == 'customer' else '',
        'import_companies': [
            {
                'id': customer_id,
                'name': (customer or {}).get('name') or customer_id,
                'currency': _required_currency(customer),
                'discount_percent': _percent(customer.get('additional_discount_percent')),
                'minimum_sqf': _effective_minimum(customer),
            }
            for customer_id, customer in sorted(customers.items(), key=lambda item: ((item[1] or {}).get('name') or '').lower())
        ] if user.get('role') != 'customer' else [],
        'import_glass': [item.get('name') for item in glass],
        'import_spacers': [item.get('name') for item in spacers],
    }


@app.route('/eula')
def eula():
    return render_template('eula.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/quickbooks')
@app.route('/quickbooks/launch')
def quickbooks_launch():
    user = current_user()
    if not user:
        return redirect(url_for('login', next='/settings/quickbooks'))
    if user.get('must_change_password'):
        return redirect(url_for('change_password'))
    if user.get('role') != 'admin':
        return forbidden()
    return redirect(url_for('quickbooks_settings'))


@app.route('/quickbooks/disconnected')
def quickbooks_disconnected():
    import quickbooks
    realm = (request.args.get('realmId') or '').strip()
    cleared = False
    mismatched = False
    if realm:
        key = quickbooks.connection_for_realm(realm)
        if key:
            with quickbooks.connection_scope(key):
                tokens = quickbooks.load_tokens()
                if tokens.get('refresh_token') and str(tokens.get('realm_id') or '') == realm:
                    quickbooks.disconnect()
                    cleared = True
                elif tokens.get('refresh_token'):
                    mismatched = True
        elif quickbooks.load_tokens().get('refresh_token'):
            mismatched = True
    else:
        tokens = quickbooks.load_tokens()
        if tokens.get('refresh_token'):
            quickbooks.disconnect()
            cleared = True
    return render_template('quickbooks_disconnected.html', cleared=cleared, mismatched=mismatched)


def _quickbooks_tab_redirect(key=None):
    chosen = key or session.get('qbo_connection') or 'us'
    if chosen not in {'us', 'ca'}:
        chosen = 'us'
    return redirect(url_for('quickbooks_settings', company=chosen) + '#' + chosen)


@app.route('/settings/quickbooks')
@admin_required
def quickbooks_settings():
    import quickbooks
    return render_template('quickbooks.html', status=quickbooks.status_view())


@app.route('/quickbooks/connect')
@admin_required
def quickbooks_connect():
    import secrets as token_secrets
    import quickbooks
    try:
        state = token_secrets.token_urlsafe(24)
        session['qbo_state'] = state
        key = request.args.get('connection') or 'us'
        session['qbo_connection'] = key if key in {'us', 'ca'} else 'us'
        return redirect(quickbooks.authorize_url(state))
    except quickbooks.QuickBooksError as exc:
        flash(str(exc), 'error')
        return _quickbooks_tab_redirect()


@app.route('/quickbooks/callback')
@admin_required
def quickbooks_callback():
    import quickbooks
    if request.args.get('error'):
        flash('QuickBooks authorization was cancelled.', 'error')
        return _quickbooks_tab_redirect()
    state = request.args.get('state') or ''
    if not state or state != session.get('qbo_state'):
        flash('QuickBooks authorization could not be verified.', 'error')
        return _quickbooks_tab_redirect()
    code = request.args.get('code') or ''
    realm_id = request.args.get('realmId') or ''
    if not code or not realm_id:
        flash('QuickBooks did not return a company.', 'error')
        return _quickbooks_tab_redirect()
    try:
        key = session.get('qbo_connection') or 'us'
        with quickbooks.connection_scope(key if key in {'us', 'ca'} else 'us'):
            quickbooks.exchange_code(code, realm_id)
    except quickbooks.QuickBooksError as exc:
        flash(str(exc), 'error')
        return _quickbooks_tab_redirect()
    session.pop('qbo_state', None)
    session.pop('qbo_connection', None)
    flash('QuickBooks is connected.', 'success')
    return _quickbooks_tab_redirect(key)


@app.route('/api/quickbooks/status', methods=['GET'])
@admin_required
def quickbooks_status():
    import quickbooks
    return jsonify(quickbooks.status_view())


@app.route('/api/quickbooks/disconnect', methods=['POST'])
@admin_required
def quickbooks_disconnect():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    with quickbooks.connection_scope(key):
        quickbooks.disconnect()
    return jsonify(quickbooks.status_view())


@app.route('/api/quickbooks/settings', methods=['PUT'])
@admin_required
def quickbooks_save_settings():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    with quickbooks.connection_scope(key):
        return _quickbooks_save_settings(quickbooks, data)


def _quickbooks_save_settings(quickbooks, data):
    settings = quickbooks.load_settings()
    default_item = clean_text(data.get('default_item') or settings.get('default_item'), 120)
    tax_code = clean_text(data.get('tax_code') or settings.get('tax_code'), 40)
    extra = data.get('extra_items') if isinstance(data.get('extra_items'), dict) else settings.get('extra_items') or {}
    cleaned = {}
    for key, value in extra.items():
        label = clean_text(key, 80)
        item = clean_text(value, 120)
        if label and item:
            cleaned[label] = item
    settings['default_item'] = default_item or 'Insulated Glass Units'
    settings['irregular_item'] = clean_text(
        data.get('irregular_item') or settings.get('irregular_item') or 'Irregular shape surcharge',
        120,
    ) or 'Irregular shape surcharge'
    settings['dwg_item'] = clean_text(
        data.get('dwg_item') or settings.get('dwg_item') or 'DWG drawing creation',
        120,
    ) or 'DWG drawing creation'
    connection = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    if connection == 'ca':
        from storage import PROVINCE_CODES
        raw_tax = data.get('province_tax') if isinstance(data.get('province_tax'), dict) else settings.get('province_tax') or {}
        province_tax = {}
        allowed_cases = {'hst', 'gst', 'with_pst', 'gst_only', 'with_qst'}
        for code in PROVINCE_CODES:
            value = raw_tax.get(code)
            if isinstance(value, dict):
                cleaned = {}
                for case, item in value.items():
                    if case not in allowed_cases:
                        continue
                    text = clean_text(item, 40)
                    if text:
                        cleaned[case] = text
                if cleaned:
                    province_tax[code] = cleaned
                continue
            text = clean_text(value, 40)
            if text:
                province_tax[code] = text
        settings['province_tax'] = province_tax
        if 'tax_exempt_code' in data:
            settings['tax_exempt_code'] = clean_text(data.get('tax_exempt_code'), 40)
    else:
        settings['tax_code'] = tax_code or 'NON'
        if 'state_tax' in data:
            from storage import US_STATE_CODES
            raw_states = data.get('state_tax') if isinstance(data.get('state_tax'), dict) else {}
            settings['state_tax'] = {
                code: clean_text(raw_states.get(code), 40)
                for code in US_STATE_CODES
                if clean_text(raw_states.get(code), 40)
            }
    settings['extra_items'] = cleaned
    quickbooks.save_settings(settings)
    return jsonify(quickbooks.status_view())


@app.route('/api/quickbooks/tax-codes', methods=['GET'])
@module_required('customers')
def quickbooks_tax_codes():
    import quickbooks
    return jsonify(quickbooks.canada_tax_choices())


@app.route('/api/quickbooks/sync', methods=['POST'])
@admin_required
def quickbooks_sync():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    with quickbooks.connection_scope(key):
        if not quickbooks.connected():
            return json_error('QuickBooks is not connected.', 400)
        try:
            result = quickbooks.poll_cdc()
        except quickbooks.QuickBooksError as exc:
            return json_error(str(exc), 400)
    body = quickbooks.status_view()
    body['result'] = result
    return jsonify(body)


@app.route('/api/quickbooks/import/preview', methods=['POST'])
@admin_required
def quickbooks_import_preview():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    with quickbooks.connection_scope(key):
        if not quickbooks.connected():
            return json_error('QuickBooks is not connected.', 400)
        try:
            rows = quickbooks.preview_import()
        except quickbooks.QuickBooksError as exc:
            return json_error(str(exc), 400)
    return jsonify({'customers': rows, 'connection': key})


@app.route('/api/quickbooks/import/apply', methods=['POST'])
@admin_required
def quickbooks_import_apply():
    import quickbooks
    data = json_body() or {}
    decisions = data.get('decisions') if isinstance(data.get('decisions'), dict) else {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    try:
        with quickbooks.connection_scope(key):
            result = quickbooks.apply_import(decisions)
    except quickbooks.QuickBooksError as exc:
        return json_error(str(exc), 400)
    return jsonify(result)


@app.route('/api/quickbooks/link-customer', methods=['POST'])
@admin_required
def quickbooks_link_customer():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    try:
        with quickbooks.connection_scope(key):
            linked = quickbooks.link_customer(data.get('qbo_id'), data.get('customer_id'))
    except quickbooks.QuickBooksError as exc:
        return json_error(str(exc), 400)
    body = quickbooks.status_view()
    body['result'] = linked
    body['message'] = f"Linked {linked['name']} to {linked['company']}."
    return jsonify(body)


@app.route('/api/quickbooks/unlink-customer', methods=['POST'])
@admin_required
def quickbooks_unlink_customer():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    try:
        with quickbooks.connection_scope(key):
            unlinked = quickbooks.unlink_customer(data.get('customer_id'))
    except quickbooks.QuickBooksError as exc:
        return json_error(str(exc), 400)
    body = quickbooks.status_view()
    body['result'] = unlinked
    body['message'] = f"Unlinked {unlinked['company']} on this card."
    return jsonify(body)


@app.route('/api/quickbooks/import/history', methods=['POST'])
@admin_required
def quickbooks_import_history():
    import quickbooks
    data = json_body() or {}
    key = data.get('connection') if data.get('connection') in {'us', 'ca'} else 'us'
    try:
        with quickbooks.connection_scope(key):
            result = quickbooks.import_linked_history()
    except quickbooks.QuickBooksError as exc:
        return json_error(str(exc), 400)
    body = quickbooks.status_view()
    body['result'] = result
    body['message'] = result['message']
    return jsonify(body)


@app.route('/api/quickbooks/intercompany', methods=['POST'])
@admin_required
def quickbooks_intercompany():
    import quickbooks
    data = json_body() or {}
    action = clean_text(data.get('action') or 'match', 20)
    try:
        if action == 'create':
            qbo_id = quickbooks.create_intercompany_customer()
        elif action == 'save':
            qbo_id = quickbooks.remember_intercompany_customer(data.get('qbo_id'))
        else:
            qbo_id = quickbooks.match_intercompany_customer()
    except quickbooks.QuickBooksError as exc:
        return json_error(str(exc), 400)
    body = quickbooks.status_view()
    body['intercompany_qbo_id'] = qbo_id
    if not qbo_id:
        body['message'] = 'Ora Glass (Canada) Inc is not a customer in the US QuickBooks company yet.'
    return jsonify(body)


@app.route('/quickbooks/webhook', methods=['POST'])
@csrf.exempt
def quickbooks_webhook():
    import quickbooks
    payload = request.get_data() or b''
    signature = request.headers.get('intuit-signature', '')
    if not quickbooks.verify_webhook(payload, signature):
        return json_error('Invalid signature', 401)
    if not quickbooks.any_connected():
        return jsonify({'ok': True})
    try:
        return jsonify(quickbooks.handle_webhook(payload))
    except quickbooks.QuickBooksError as exc:
        quickbooks.log('error', str(exc))
        return json_error(str(exc), 400)


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
    customer = None
    if user.get('role') == 'customer':
        customer = customers.get(user.get('customer_id') or '')
    else:
        requested = clean_text(data.get('customer_id'), 64)
        customer = customers.get(requested) if requested else None
    price = _sell_price(data, tier, tiers, _price_factor(_percent((customer or {}).get('price_addon_percent'), 1000)))
    minimum_sqf = _effective_minimum(customer)
    try:
        supplied = float(data.get('fx_rate'))
    except (TypeError, ValueError):
        supplied = 0
    fx_rate = supplied if supplied > 0 else _current_fx()[0]
    body = {'price_sqf': price, 'tier': tier, 'minimum_sqf': minimum_sqf}
    if user.get('role') == 'customer':
        if _required_currency(customer) == 'cad':
            body['price_sqf'] = round(price * fx_rate, 2)
    else:
        body['fx_rate'] = fx_rate
    return jsonify(body)


def _job_lines(value):
    if isinstance(value, list):
        return [str(line).strip() for line in value if str(line).strip()]
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    return [line.strip() for line in text.split('\n') if line.strip()]


def _address_lines(customer):
    if not customer:
        return []
    lines = []
    street = str(customer.get('address') or '').strip()
    if street:
        lines.append(street)
    city = str(customer.get('city') or '').strip()
    state = str(customer.get('state') or customer.get('province') or customer.get('us_state') or '').strip()
    postal = str(customer.get('zip') or '').strip()
    place = ', '.join(part for part in (city, state) if part)
    if postal:
        place = f'{place} {postal}'.strip()
    if place:
        lines.append(place)
    country = str(customer.get('country') or '').strip()
    if country and country not in {'United States', 'USA', 'US'}:
        lines.append(country)
    return lines


def _pdf_document(source, kind, user=None):
    """Fields the shared quote and order PDF knows how to print. The add-on is not one of them."""
    import documents
    import pricing_catalog
    customers = load_customers()
    customer_id = (source or {}).get('customer_id') or ''
    customer = customers.get(customer_id)
    if not customer and user and user.get('role') == 'customer':
        customer = customers.get(user.get('customer_id') or '')
    company = (source or {}).get('company') or (customer or {}).get('name') or ''
    quote_by = (source or {}).get('user') or ''
    updated_by = (source or {}).get('updated_by') or ''
    if kind == 'order':
        quote_id = source.get('quote_id')
        quote = None
        if quote_id not in (None, ''):
            quote = next((row for row in load_quotes() if row.get('id') == quote_id), None)
        if quote and quote.get('user'):
            quote_by = quote.get('user')
        elif source.get('approved_by'):
            quote_by = source.get('approved_by')
        log = source.get('changelog') or []
        if log and isinstance(log[-1], dict) and log[-1].get('user'):
            updated_by = log[-1].get('user')
    return {
        'kind': kind,
        'id': '' if source.get('id') in (None, '') else source.get('id'),
        'date': source.get('date') or source.get('approved_at') or '',
        'project': source.get('project') or '',
        'company': company,
        'sold_to': _address_lines(customer),
        'job_address': _job_lines(source.get('job_address')),
        'currency': source.get('currency') or 'usd',
        'quote_by': quote_by,
        'updated_by': updated_by,
        'po': source.get('po') or source.get('customer_po') or '',
        'payment_terms': (customer or {}).get('payment_terms') or '',
        'remarks': source.get('notes') or source.get('remarks') or '',
        'items': source.get('items') or [],
        'extra_charges': source.get('extra_charges') or [],
        'additional_discount_percent': source.get('additional_discount_percent') or 0,
        'additional_discount_amount': source.get('additional_discount_amount') or 0,
        'tax_label': source.get('tax_label') or '',
        'tax_amount': source.get('tax_amount'),
        'type': source.get('type') or ('size' if kind == 'order' else ''),
        'weight_factor': pricing_catalog.formula_number('glass_lb_per_sqf_mm', 0.512),
        '_builder': documents,
    }


def _pdf_bytes(document):
    import documents
    payload = dict(document)
    payload.pop('_builder', None)
    return documents.build_pdf(payload)


@app.route('/api/export-quote', methods=['POST'])
@module_required('quotes')
def export_quote():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    user = current_user()
    stored = None
    raw_id = data.get('id')
    if raw_id not in (None, ''):
        try:
            stored = _visible_quote(user, int(raw_id))
        except (TypeError, ValueError):
            stored = None
    if stored:
        document = _pdf_document(_with_quote_tax(stored), 'quote', user)
    else:
        items = data.get('items', [])
        if not isinstance(items, list):
            return json_error('Items must be a list', 400)
        items = expand_irregular_lines(items, load_pricing_settings()['irregular_shape_surcharge'])
        tax_label = clean_text(data.get('tax_label'), 200)
        if not tax_label and user and user.get('role') == 'customer':
            owner = load_customers().get(user.get('customer_id') or '')
            tax_label = _canada_tax_fields(owner, data.get('ship_province')).get('tax_label') or ''
        customer_id = ''
        if user and user.get('role') == 'customer':
            customer_id = user.get('customer_id') or ''
        else:
            customer_id = clean_text(data.get('customer_id'), 64)
        customer = load_customers().get(customer_id or '')
        document = _pdf_document({
            'id': '',
            'date': data.get('date') or date.today().isoformat(),
            'project': clean_text(data.get('project'), 200),
            'job_address': clean_text(data.get('job_address'), 500),
            'company': (customer or {}).get('name') or clean_text(data.get('company'), 200),
            'customer_id': customer_id,
            'currency': data.get('currency') or 'usd',
            'user': user.get('email') if user else '',
            'notes': clean_text(data.get('remarks') or data.get('notes'), 2000),
            'items': items,
            'extra_charges': data.get('extra_charges') or [],
            'additional_discount_percent': data.get('additional_discount_percent') or 0,
            'tax_label': tax_label,
            'type': data.get('type') or '',
            'po': clean_text(data.get('po'), 80),
        }, 'quote', user)
    buffer = BytesIO(_pdf_bytes(document))
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='ORA_Quote.pdf')


@app.route('/shipments')
@module_required('orders')
def shipments_page():
    user = current_user()
    if not staff_can_order(user):
        return forbidden()
    return render_template('shipments.html')


def _open_shipment_rows(user):
    rows = []
    for order in orders_visible_to(user, load_orders()):
        if order_status(order) not in {'sent', 'made', 'delivered'} and not order.get('shipments'):
            continue
        progress = shipments.shipment_progress(order)
        if not progress['open']:
            continue
        rows.append({
            'id': order.get('id'),
            'company': order.get('company') or '',
            'project': order.get('project') or '',
            'status': order_status(order),
            'shipping_label': progress['label'],
            'remaining_pcs': progress['remaining_pcs'],
            'remaining_sqf': progress['remaining_sqf'],
            'remaining_value': progress['remaining_value'],
            'currency': order.get('currency') or 'usd',
            'lines': progress['lines'],
        })
    rows.sort(key=lambda row: row.get('id') or 0)
    return rows


@app.route('/api/shipments/open', methods=['GET'])
@module_required('orders')
def open_shipments():
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    return jsonify(_open_shipment_rows(user))


@app.route('/api/shipments/open.xlsx', methods=['GET'])
@module_required('orders')
def export_open_shipments_excel():
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    payload = shipments.backorder_workbook(_open_shipment_rows(user))
    return send_file(
        BytesIO(payload),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='backorders.xlsx',
    )


@app.route('/api/shipments/open.pdf', methods=['GET'])
@module_required('orders')
def export_open_shipments_pdf():
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    payload = shipments.backorder_pdf(_open_shipment_rows(user))
    return send_file(BytesIO(payload), mimetype='application/pdf', as_attachment=True, download_name='backorders.pdf')


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
        can_job_address=can_edit_job_address(user),
        show_fx_rate=user.get('role') != 'customer',
        can_status=can_update_order_status(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in _live_materials()[0]],
        irregular_rate=load_pricing_settings()['irregular_shape_surcharge'],
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
        can_job_address=can_edit_job_address(user, order),
        show_fx_rate=user.get('role') != 'customer',
        can_status=can_update_order_status(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in _live_materials()[0]],
        irregular_rate=load_pricing_settings()['irregular_shape_surcharge'],
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


def _save_canada_tax_number(customer, form):
    province = str(customer.get('province') or '').upper()
    if province == 'QC':
        customer['qst_number'] = clean_text(form.get('qst_number'), 40)
    else:
        customer['pst_number'] = clean_text(form.get('pst_number'), 40)


def _canada_tax_page(customer_id, user):
    import quickbooks
    customer = load_customers().get(customer_id)
    if not customer:
        return json_error('Unknown customer.', 404) if wants_json() else redirect(url_for('customers_page'))
    province = str(customer.get('province') or '').upper()
    kind = 'qst' if province == 'QC' else 'pst'
    can_edit = user.get('role') == 'admin' or (
        user.get('role') == 'customer' and user.get('customer_id') == customer_id
    )
    if request.method == 'POST' and can_edit:
        customers = load_customers()
        current = customers.get(customer_id) or customer
        _save_canada_tax_number(current, request.form)
        customers[customer_id] = current
        save_customers(customers)
        flash('Tax number saved.', 'success')
        customer = current
    return render_template(
        'company_tax.html',
        customer_id=customer_id,
        company=customer.get('name') or customer_id,
        province=province,
        kind=kind,
        pst_number=customer.get('pst_number') or '',
        qst_number=customer.get('qst_number') or '',
        can_edit=can_edit,
        canadian=quickbooks.is_canadian(customer),
    )


def _resale_page(customer_id):
    import resale
    import quickbooks
    user = current_user()
    if not _can_view_resale(user, customer_id):
        return forbidden()
    customer = load_customers().get(customer_id)
    if not customer:
        return json_error('Unknown customer.', 404) if wants_json() else redirect(url_for('customers_page'))
    if quickbooks.is_canadian(customer):
        return _canada_tax_page(customer_id, user)
    from storage import US_STATES
    return render_template(
        'resale.html',
        customer_id=customer_id,
        company=customer.get('name') or customer_id,
        certificates=[resale.public_certificate(row) for row in customer.get('resale_certificates') or []],
        can_review=_can_review_resale(user),
        states=US_STATES,
        warning=(resale.warnings_for({customer_id: customer}) or [{}])[0].get('text', ''),
    )


@app.route('/company', methods=['GET', 'POST'])
@login_required
def company_page():
    user = current_user()
    if user.get('role') != 'customer' or not user.get('customer_id'):
        return forbidden()
    return _resale_page(user.get('customer_id'))


@app.route('/customers/<cid>/resale', methods=['GET', 'POST'])
@login_required
def customer_resale_page(cid):
    if not _can_review_resale(current_user()):
        return forbidden()
    return _resale_page(cid)


@app.route('/api/customers/<cid>/resale', methods=['GET'])
@login_required
def customer_resale_list(cid):
    import resale
    import quickbooks
    user = current_user()
    if not _can_view_resale(user, cid):
        return json_error('Access denied', 403)
    customer = load_customers().get(cid)
    if customer and quickbooks.is_canadian(customer):
        return json_error('A Canadian company uses a PST or QST number, not a US resale certificate.', 400)
    if not customer:
        return json_error('Unknown customer.', 404)
    return jsonify([resale.public_certificate(row) for row in customer.get('resale_certificates') or []])


@app.route('/api/customers/<cid>/resale', methods=['POST'])
@login_required
def customer_resale_upload(cid):
    import resale
    import quickbooks
    user = current_user()
    customer = load_customers().get(cid)
    if customer and quickbooks.is_canadian(customer):
        return json_error('A Canadian company uses a PST or QST number, not a US resale certificate.', 400)
    if user.get('role') == 'customer':
        if user.get('customer_id') != cid:
            return json_error('Access denied', 403)
        verified = False
    elif _can_review_resale(user):
        verified = str(request.form.get('verified') or '').lower() in {'1', 'true', 'on', 'yes'}
    else:
        return json_error('Access denied', 403)
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return json_error('Choose a certificate file.', 400)
    payload = upload.read(9 * 1024 * 1024)
    try:
        record = resale.add_certificate(
            cid,
            upload.filename,
            payload,
            clean_text(request.form.get('number'), 80),
            clean_text(request.form.get('state'), 8),
            clean_text(request.form.get('expires'), 10),
            user.get('email') or '',
            verified=verified,
        )
    except resale.ResaleError as exc:
        return json_error(str(exc), 400)
    return jsonify(resale.public_certificate(record))


@app.route('/api/customers/<cid>/resale/<certificate_id>', methods=['POST'])
@login_required
def customer_resale_review(cid, certificate_id):
    import resale
    import quickbooks
    user = current_user()
    if not _can_review_resale(user):
        return json_error('Access denied', 403)
    customer = load_customers().get(cid)
    if customer and quickbooks.is_canadian(customer):
        return json_error('A Canadian company uses a PST or QST number, not a US resale certificate.', 400)
    data = json_body() or {}
    try:
        record = resale.review_certificate(
            cid,
            certificate_id,
            clean_text(data.get('status'), 20),
            user.get('email') or '',
            clean_text(data.get('note'), 300),
        )
    except resale.ResaleError as exc:
        return json_error(str(exc), 400)
    return jsonify(resale.public_certificate(record))


@app.route('/api/customers/<cid>/resale/<certificate_id>/file', methods=['GET'])
@login_required
def customer_resale_file(cid, certificate_id):
    import resale
    user = current_user()
    if not _can_view_resale(user, cid):
        return json_error('Access denied', 403)
    import quickbooks
    customer = load_customers().get(cid) or {}
    if quickbooks.is_canadian(customer):
        return json_error('A Canadian company uses a PST or QST number, not a US resale certificate.', 400)
    found = next((row for row in customer.get('resale_certificates') or [] if row.get('id') == certificate_id), None)
    if not found:
        return json_error('Unknown certificate.', 404)
    path = resale.certificate_path(cid, found.get('stored'))
    if not path:
        return json_error('The file is missing.', 404)
    return send_file(path, mimetype=found.get('content_type') or 'application/octet-stream', as_attachment=True, download_name=found.get('filename') or 'certificate')


@app.route('/inventory')
@module_required('inventory')
def inventory():
    return render_template('module.html', module='Inventory', note='')


def _form_rows(fields):
    columns = {field: request.form.getlist(field) for field in fields}
    count = max((len(values) for values in columns.values()), default=0)
    rows = []
    for index in range(count):
        rows.append({field: (values[index] if index < len(values) else '') for field, values in columns.items()})
    return rows


def _flag(value):
    return str(value or '').strip().lower() in {'yes', '1', 'true', 'on'}


def _save_currency_action(user):
    import currency_rules
    import pricing_catalog
    action = clean_text(request.form.get('action'), 20)
    try:
        if action == 'countries':
            before = currency_rules.load_currency_settings()['countries']
            currency_rules.save_country_map({
                'Canada': request.form.get('canada'),
                'United States': request.form.get('united_states'),
            })
            after = currency_rules.load_currency_settings()['countries']
            for name in ('Canada', 'United States'):
                pricing_catalog.record_change('currency', name, 'currency', before.get(name), after.get(name), user.get('email'))
            flash('Country currencies saved.', 'success')
        elif action == 'rate':
            old_rate, _effective = currency_rules.rate_on(date.today())
            currency_rules.record_rate(
                request.form.get('rate'),
                request.form.get('effective') or date.today().isoformat(),
                user.get('email'),
            )
            new_rate, _effective = currency_rules.rate_on(request.form.get('effective') or date.today().isoformat())
            pricing_catalog.record_change('currency', 'ERP exchange rate', 'rate', old_rate, new_rate, user.get('email'))
            flash('ERP exchange rate saved.', 'success')
        else:
            flash('Unknown currency action.', 'error')
    except ValueError as exc:
        flash(str(exc), 'error')


@app.route('/pricing', methods=['GET', 'POST'])
@module_required('pricing')
def pricing_admin():
    import currency_rules
    import pricing_catalog
    user = current_user()
    if user.get('role') != 'admin':
        flash('Only an administrator can change pricing.', 'error')
        return redirect(url_for('dashboard'))
    tab = clean_text(request.values.get('tab') or 'markups', 20)
    if tab not in pricing_catalog.TAB_IDS:
        tab = 'markups'
    if request.method == 'POST':
        action = clean_text(request.form.get('action') or 'markups', 40)
        if action in {'countries', 'rate'}:
            _save_currency_action(user)
            return redirect(url_for('pricing_admin', tab='currency'))
        if action == 'markups':
            _save_markups(user)
            return redirect(url_for('pricing_admin', tab='markups'))
        if action == 'glass':
            rows = _material_posts(('id', 'name', 'thickness', 'sheet_width', 'sheet_height', 'raw_price', 'tariff_percent', 'waste_percent', 'temperable', 'annealed', 'in_price', 'active'), 'glass')
            new_name = clean_text(request.form.get('new_name'), 120)
            if new_name:
                rows.append({
                    'name': new_name,
                    'kind': 'glass',
                    'thickness': clean_text(request.form.get('new_thickness'), 20),
                    'sheet_width': request.form.get('new_sheet_width'),
                    'sheet_height': request.form.get('new_sheet_height'),
                    'raw_price': request.form.get('new_raw_price'),
                    'tariff_percent': request.form.get('new_tariff_percent'),
                    'waste_percent': request.form.get('new_waste_percent'),
                    'temperable': _flag(request.form.get('new_temperable')),
                    'annealed': _flag(request.form.get('new_annealed')),
                    'in_price': True,
                    'active': True,
                })
            pricing_catalog.update_materials(rows, user.get('email'), 'glass')
            flash('Glass prices saved. Saved quotes and orders keep their prices.', 'success')
            return redirect(url_for('pricing_admin', tab='glass'))
        if action == 'temper':
            pricing_catalog.update_temper(_form_rows(('thickness', 'addon')), user.get('email'))
            flash('Tempering add-ons saved. Saved quotes and orders keep their prices.', 'success')
            return redirect(url_for('pricing_admin', tab='temper'))
        if action == 'components':
            rows = _material_posts(('id', 'name', 'price_per_ft', 'foot_factor', 'price_per_set', 'spacer_width_mm', 'raw_price', 'tariff_percent', 'waste_percent', 'in_price', 'active'), None)
            new_name = clean_text(request.form.get('new_name'), 120)
            if new_name:
                kind = clean_text(request.form.get('new_kind'), 20)
                if kind not in {'spacer', 'sealant', 'desiccant', 'butyl', 'corner'}:
                    kind = 'spacer'
                rows.append({
                    'name': new_name,
                    'kind': kind,
                    'price_per_ft': request.form.get('new_price_per_ft'),
                    'price_per_set': request.form.get('new_price_per_set'),
                    'spacer_width_mm': request.form.get('new_spacer_width_mm'),
                    'raw_price': request.form.get('new_raw_price'),
                    'tariff_percent': request.form.get('new_tariff_percent'),
                    'waste_percent': request.form.get('new_waste_percent'),
                    'in_price': _flag(request.form.get('new_in_price')),
                    'active': True,
                })
            pricing_catalog.update_materials(rows, user.get('email'), 'components')
            if request.form.get('technoform_foot_factor') not in (None, ''):
                pricing_catalog.update_formula(
                    {'technoform_foot_factor': request.form.get('technoform_foot_factor')},
                    user.get('email'),
                )
            flash('Component prices saved. Saved quotes and orders keep their prices.', 'success')
            return redirect(url_for('pricing_admin', tab='components'))
        if action == 'formula':
            posted = {}
            for row in pricing_catalog.FORMULA_DEFAULTS:
                posted[row['key']] = request.form.get(row['key'])
            pricing_catalog.update_formula(posted, user.get('email'))
            flash('Formula settings saved. Saved quotes and orders keep their prices.', 'success')
            return redirect(url_for('pricing_admin', tab='formula'))
        flash('Unknown pricing action.', 'error')
        return redirect(url_for('pricing_admin', tab=tab))
    return _render_pricing(tab)


def _material_posts(fields, kind):
    rows = []
    for posted in _form_rows(fields):
        if not posted.get('id') and not posted.get('name'):
            continue
        item = {'id': posted.get('id') or '', 'name': clean_text(posted.get('name'), 120)}
        if kind:
            item['kind'] = kind
        for key in ('thickness',):
            if key in posted:
                item[key] = clean_text(posted.get(key), 40)
        for key in ('sheet_width', 'sheet_height', 'raw_price', 'tariff_percent', 'waste_percent', 'price_per_ft', 'foot_factor', 'price_per_set', 'spacer_width_mm'):
            if key in posted:
                item[key] = posted.get(key)
        for key in ('temperable', 'annealed', 'in_price', 'active'):
            if key in posted:
                item[key] = _flag(posted.get(key))
        rows.append(item)
    return rows


def _save_markups(user):
    import pricing_catalog
    tiers = load_tiers()
    before = {key: dict(cfg) for key, cfg in tiers.items()}
    errors = []
    for key, cfg in list(tiers.items()):
        if key == 'T5':
            continue
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
    try:
        surcharge = round(float(request.form.get('irregular_shape_surcharge', 50)), 2)
        dwg_fee = round(float(request.form.get('dwg_creation_fee', 50)), 2)
    except (TypeError, ValueError):
        surcharge = -1
        dwg_fee = -1
    if surcharge < 0 or surcharge > 100000:
        errors.append('surcharge')
    if dwg_fee < 0 or dwg_fee > 100000:
        errors.append('dwg')
    if errors:
        flash('Check markup values for: ' + ', '.join(errors), 'error')
        return
    save_tiers(tiers)
    previous = load_pricing_settings()
    save_pricing_settings({'irregular_shape_surcharge': surcharge, 'dwg_creation_fee': dwg_fee})
    changes = []
    for key, cfg in tiers.items():
        if key == 'T5':
            continue
        old = before.get(key) or {}
        if old.get('label') != cfg.get('label'):
            changes.append({'tab': 'markups', 'item': key, 'field': 'label', 'old': old.get('label'), 'new': cfg.get('label')})
        if old.get('glass_markup') != cfg.get('glass_markup'):
            changes.append({'tab': 'markups', 'item': key, 'field': 'glass_markup', 'old': old.get('glass_markup'), 'new': cfg.get('glass_markup')})
        if old.get('spacer_markup') != cfg.get('spacer_markup'):
            changes.append({'tab': 'markups', 'item': key, 'field': 'spacer_markup', 'old': old.get('spacer_markup'), 'new': cfg.get('spacer_markup')})
    if previous['irregular_shape_surcharge'] != surcharge:
        changes.append({'tab': 'markups', 'item': 'Irregular shape surcharge', 'field': 'amount', 'old': previous['irregular_shape_surcharge'], 'new': surcharge})
    if previous['dwg_creation_fee'] != dwg_fee:
        changes.append({'tab': 'markups', 'item': 'DWG drawing creation', 'field': 'amount', 'old': previous['dwg_creation_fee'], 'new': dwg_fee})
    if changes:
        pricing_catalog.save_catalog(pricing_catalog.load_catalog(), user.get('email'), changes)
    flash('Tier markups saved. Tier 5 is twice Tier 2. Irregular shape surcharge and DWG drawing fee saved.', 'success')


def _render_pricing(tab):
    import currency_rules
    import pricing_catalog
    tiers = load_tiers()
    catalog = pricing_catalog.load_catalog()
    rate, effective = currency_rules.rate_on(date.today())
    settings = currency_rules.load_currency_settings()
    price_rows = pricing_catalog.price_list(tiers, rate) if tab == 'prices' else []
    return render_template(
        'pricing.html',
        tabs=pricing_catalog.TABS,
        tab=tab,
        tiers=ordered_tiers(tiers),
        irregular_shape_surcharge=load_pricing_settings()['irregular_shape_surcharge'],
        dwg_creation_fee=load_pricing_settings()['dwg_creation_fee'],
        glass_rows=[row for row in catalog['materials'] if row['kind'] == 'glass'],
        component_rows=[row for row in catalog['materials'] if row['kind'] != 'glass'],
        temper_rows=catalog['temper'],
        formula_rows=catalog['formula'],
        gaps=pricing_catalog.GAPS,
        history=pricing_catalog.history_for(tab, catalog),
        settings=settings,
        current_rate=rate,
        current_effective=effective,
        today=date.today().isoformat(),
        currency_history=list(reversed(settings['history'])),
        price_rows=price_rows,
        catalog_source=catalog.get('source') or '',
    )


def _production_staff():
    user, response = _enforce()
    if response is not None:
        return None, response
    if not _is_ora_staff(user):
        return None, forbidden()
    return user, None


def _remember_igus(items, used_on=None, order_id=None):
    try:
        production.remember_configurations(items, used_on or date.today().isoformat(), order_id)
    except Exception:
        logger.exception('Could not record IGU types')


def _write_production_file(order):
    if not production.load_export_settings().get('generate_on_confirm', True):
        return
    try:
        production.write_order_workbook(order)
    except Exception:
        logger.exception('Could not write the production Excel file')


@app.route('/currency', methods=['GET', 'POST'])
@app.route('/currency-settings', methods=['GET', 'POST'])
@login_required
def currency_settings():
    """Old currency page. The editor now lives on the Pricing currency tab."""
    user = current_user()
    if user.get('role') != 'admin':
        flash('Only an administrator can change currency settings.', 'error')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        _save_currency_action(user)
    return redirect(url_for('pricing_admin', tab='currency'))


@app.route('/production-settings', methods=['GET', 'POST'])
def production_settings():
    _user, response = _production_staff()
    if response is not None:
        return response
    tab = clean_text(request.values.get('tab') or 'ids', 20)
    if tab not in {item['id'] for item in production.PRODUCTION_TABS}:
        tab = 'ids'
    list_filter = clean_text(request.values.get('filter') or 'missing', 20)
    if list_filter not in {'missing', 'all'}:
        list_filter = 'missing'
    if request.method == 'POST':
        action = clean_text(request.form.get('action'), 20)
        if action == 'excel':
            production.save_export_settings(request.form.get('generate_on_confirm') == 'on')
            flash('Production Excel settings saved.', 'success')
            return redirect(url_for('production_settings', tab='excel'))
        if action == 'upload':
            upload = request.files.get('product_list')
            if not upload or not upload.filename:
                flash('Choose an Excel file.', 'error')
                return redirect(url_for('production_settings', tab='ids', filter=list_filter))
            import tempfile
            handle, path = tempfile.mkstemp(suffix='.xlsx')
            os.close(handle)
            try:
                upload.save(path)
                rows = production.read_product_sheet(path)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if not rows:
                flash('That file needs Product ID and Product Name columns.', 'error')
                return redirect(url_for('production_settings', tab='ids', filter=list_filter))
            result = production.apply_product_upload(rows, replace=request.form.get('replace_ids') == 'on')
            flash(
                f"Matched {len(result['matched'])} names. "
                f"{len(result['unmatched'])} names were not in the registry. "
                f"{len(result['kept'])} existing IDs were left unchanged.",
                'success',
            )
            return redirect(url_for('production_settings', tab='ids', filter=list_filter))
        keys = request.form.getlist('row_key')
        ids = request.form.getlist('product_id')
        names = request.form.getlist('product_name')
        updates = {}
        for key, product_id, product_name in zip(keys, ids, names):
            updates[key] = {'product_id': product_id, 'product_name': product_name}
        production.save_production_overrides(updates)
        flash('Production IDs saved.', 'success')
        return redirect(url_for('production_settings', tab='ids', filter=list_filter))
    rows = production.registry_rows()
    if list_filter == 'missing':
        rows = [row for row in rows if row['missing']]
    return render_template(
        'production_settings.html',
        tabs=production.PRODUCTION_TABS,
        tab=tab,
        rows=rows,
        list_filter=list_filter,
        seed=production.seed_report(),
        settings=production.load_export_settings(),
        headers=production.PRODUCTION_HEADERS,
    )


@app.route('/users')
@module_required('users')
def users_page():
    user = current_user()
    return render_template(
        'users.html',
        tiers=ordered_tiers(),
        roles=ROLES,
        can_manage=user.get('role') == 'admin',
        module_catalog=[{'id': item['id'], 'label': item['label']} for item in MODULES],
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
        can_order=can_place_production_orders(user) or staff,
        staff_order=staff,
        staff_tools=user.get('role') != 'customer',
        show_fx_rate=user.get('role') != 'customer',
        order_tiers=[
            {'id': tid, 'label': (cfg or {}).get('label') or tid}
            for tid, cfg in ordered_tiers()
        ] if staff else [],
        is_admin=user.get('role') == 'admin',
        dwg_rate=load_pricing_settings()['dwg_creation_fee'],
        drawing_note=DRAWING_NOTE,
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
        can_issue=can_issue_invoices(user),
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
        can_issue=can_issue_invoices(user),
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
        row = public_customer(customer_id, customer, tiers, counts.get(customer_id, 0))
        return jsonify([row])
    rows = []
    for customer_id, customer in customers.items():
        if customer.get('books_entity'):
            continue
        row = public_customer(customer_id, customer, tiers, counts.get(customer_id, 0))
        row['price_addon_percent'] = _percent(customer.get('price_addon_percent'), 1000)
        rows.append(row)
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
    _qbo('push_customer', customer_id)
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
    _qbo('push_customer', cid)
    return jsonify({'success': True})


@app.route('/api/customers/<cid>', methods=['DELETE'])
@admin_required
def delete_customer(cid):
    if not ID_RE.fullmatch(cid or ''):
        return json_error('Not found', 404)
    customers = load_customers()
    customer = customers.get(cid)
    if not customer:
        return json_error('Not found', 404)
    if customer_user_counts().get(cid):
        return json_error("Reassign or delete this customer's users first.", 409)
    del customers[cid]
    save_customers(customers)
    _qbo('deactivate_customer', cid)
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
    user['manage_payroll'] = False
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
    apply_place_production_orders(record, data)
    apply_order_privileges(record, data)
    apply_manage_time(record, data)
    apply_manage_payroll(record, data)
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
    apply_place_production_orders(user, data)
    apply_order_privileges(user, data)
    apply_manage_time(user, data)
    apply_manage_payroll(user, data)
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


def _ship_province(value, customer):
    from storage import PROVINCE_CODES
    chosen = clean_text(value, 8).upper()
    if chosen in PROVINCE_CODES:
        return chosen
    billing = str((customer or {}).get('province') or '').strip().upper()
    return billing if billing in PROVINCE_CODES else ''


def _canada_tax_fields(customer, ship_province=None):
    """Resolved Canada tax for a document. Empty for customers outside Canada."""
    import quickbooks
    if not customer or not quickbooks.is_canadian(customer):
        return {}
    province = _ship_province(ship_province, customer)
    code, _warning, label = quickbooks.resolve_canada_tax(customer, province)
    return {'ship_province': province, 'qbo_tax_code': code, 'tax_label': label}


def _with_quote_tax(quote):
    row = dict(quote)
    owner = load_customers().get(row.get('customer_id') or '')
    tax = _canada_tax_fields(owner, row.get('ship_province'))
    if tax:
        row['ship_province'] = row.get('ship_province') or tax.get('ship_province') or ''
        row['tax_label'] = tax.get('tax_label') or ''
        row['qbo_tax_code'] = tax.get('qbo_tax_code') or ''
    return row


def _remember_floor(item):
    """Keep one Floor value. A mark or location on an older line is the same field."""
    if not isinstance(item, dict):
        return item
    text = ''
    for key in ('floor', 'mark', 'location', 'floor_code'):
        if str(item.get(key) or '').strip():
            text = clean_text(item.get(key), 80)
            break
    for key in ('mark', 'location', 'floor_code'):
        item.pop(key, None)
    if text:
        item['floor'] = text
    else:
        item.pop('floor', None)
    return item


def _export_discount(data, subtotal):
    percent = _percent((data or {}).get('additional_discount_percent'))
    if not percent:
        return 0, 0.0
    return percent, round(float(subtotal) * percent / 100.0, 2)


def _line_subtotal(items):
    total = 0.0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = item.get('total') if item.get('total') not in (None, '') else item.get('price')
        try:
            total += float(raw or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _charge_saved_size_lines(items, minimum):
    """Bill each size line at the charged square feet and remember the minimum used.

    Per-square-foot lines have no size spec and are left unchanged.
    """
    from quoting import area_sqf
    try:
        floor = float(minimum or 0)
    except (TypeError, ValueError):
        floor = 0.0
    if floor < 0:
        floor = 0.0
    for item in items or []:
        if not isinstance(item, dict) or item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if not spec or spec.get('width') in (None, '') or spec.get('height') in (None, ''):
            continue
        real = round(area_sqf(spec.get('width'), spec.get('height'), spec.get('unit')), 2)
        charged = round(max(real, floor), 2) if floor > 0 else real
        item['real_sqf'] = real
        item['minimum_sqf'] = floor
        item['sqf'] = charged
        try:
            price_sqf = float(item.get('price_sqf') or 0)
        except (TypeError, ValueError):
            price_sqf = 0.0
        try:
            pieces = int(item.get('pcs') or 1)
        except (TypeError, ValueError):
            pieces = 1
        if pieces < 1:
            pieces = 1
        if price_sqf and charged > real + 0.001:
            item['price_pc'] = round(price_sqf * charged, 2)
            item['total'] = round(item['price_pc'] * pieces, 2)
    return items


def _stamp_terms(document, items, discount_percent, addon_percent):
    document['additional_discount_percent'] = _percent(discount_percent)
    document['additional_discount_amount'] = round(
        _line_subtotal(items) * document['additional_discount_percent'] / 100.0, 2,
    )
    document['price_addon_percent'] = _percent(addon_percent, 1000)


def _saved_or_company_terms(existing, customer):
    """Keep a percent already stored on the document. A new document takes the company values."""
    if existing and 'additional_discount_percent' in existing:
        discount = existing.get('additional_discount_percent')
    else:
        discount = (customer or {}).get('additional_discount_percent')
    if existing and 'price_addon_percent' in existing:
        addon = existing.get('price_addon_percent')
    else:
        addon = (customer or {}).get('price_addon_percent')
    return discount, addon


def _item_description(item):
    from xml.sax.saxutils import escape
    description = escape(str(item.get('name') or ''))
    floor = production.floor_code(item)
    if floor:
        description += '<br/>Floor: ' + escape(floor)
    if str(item.get('remarks') or '').strip():
        description += '<br/>' + escape(str(item.get('remarks')).strip())
    return description


@app.route('/api/quotes', methods=['POST'])
@module_required('quotes')
def save_quote():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    items = [_remember_floor(dict(item)) if isinstance(item, dict) else item for item in data.get('items', [])]
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
    fx_rate, _fx_effective = _current_fx()
    raw_id = data.get('id')
    if raw_id not in (None, ''):
        try:
            prior = _visible_quote(user, int(raw_id))
        except (TypeError, ValueError):
            prior = None
        if prior and prior.get('fx_rate'):
            fx_rate = _stored_fx(prior)
    currency_changed = False
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id or '')
        if not customer:
            return json_error('Your account is not linked to a customer.', 400)
        company = customer.get('name') or 'Customer'
        required = _required_currency(customer)
        currency_changed = currency != required
        if currency_changed and quote_type != 'size':
            factor = _switch_factor(required, fx_rate)
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ('price', 'total', 'price_sqf', 'price_pc'):
                    if item.get(key) in (None, ''):
                        continue
                    try:
                        item[key] = round(float(item[key]) * factor, 2)
                    except (TypeError, ValueError):
                        pass
        currency = required
    else:
        raw_customer = clean_text(data.get('customer_id'), 64)
        customer = customers.get(raw_customer) if raw_customer else None
        if raw_customer and not customer:
            return json_error('Unknown customer.', 400)
        if customer:
            customer_id = raw_customer
            company = customer.get('name') or 'Customer'
            required = _required_currency(customer)
            currency_changed = currency != required
            if currency_changed and quote_type != 'size':
                factor = _switch_factor(required, fx_rate)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for key in ('price', 'total', 'price_sqf', 'price_pc'):
                        if item.get(key) in (None, ''):
                            continue
                        try:
                            item[key] = round(float(item[key]) * factor, 2)
                        except (TypeError, ValueError):
                            pass
            currency = required
        else:
            customer_id = None
            customer = None
            company = 'ORA'
    tiers = load_tiers()
    tier = tier_for_request(user, clean_text(data.get('tier'), 20), customers, tiers)
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    project = clean_text(data.get('project'), 200)
    new_discount, new_addon = _saved_or_company_terms(None, customer)
    if currency_changed and quote_type == 'size':
        items = _repriced_quote_items(items, tier, currency, tiers, fx_rate, _price_factor(new_addon))
    priced = expand_irregular_lines(items, load_pricing_settings()['irregular_shape_surcharge'])
    quote_id = data.get('id')
    if quote_id not in (None, ''):
        try:
            quote_id = int(quote_id)
        except (TypeError, ValueError):
            return json_error('Not found', 404)
        existing = _visible_quote(user, quote_id)
        if not existing or existing.get('status') != 'draft':
            return json_error('That quote cannot be replaced.', 400)
        existing['project'] = project
        existing['customer_id'] = customer_id
        existing['company'] = company
        kept_discount, kept_addon = _saved_or_company_terms(existing, customer)
        existing['items'] = _repriced_quote_items(
            items, tier, currency, tiers, fx_rate, _price_factor(kept_addon),
            _effective_minimum(customer),
        ) if quote_type == 'size' else priced
        _stamp_terms(existing, existing['items'], kept_discount, kept_addon)
        existing['currency'] = currency
        existing['fx_rate'] = fx_rate
        existing['type'] = quote_type
        existing['tier'] = tier
        existing['time'] = datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M')
        existing['updated_by'] = user.get('email') or ''
        if 'notes' in data or 'remarks' in data:
            existing['notes'] = clean_text(data.get('notes') or data.get('remarks'), 2000)
        if 'job_address' in data:
            existing['job_address'] = clean_text(data.get('job_address'), 500)
        if 'extra_charges' in data:
            existing['extra_charges'] = _copied_extras(data.get('extra_charges'))
        owner = customers.get(existing.get('customer_id') or '')
        if 'ship_province' in data:
            existing['ship_province'] = _ship_province(data.get('ship_province'), owner)
        existing.update(_canada_tax_fields(owner, existing.get('ship_province')))
        saved = replace_quote(quote_id, existing)
        if saved:
            _remember_igus(saved.get('items'), saved.get('date'))
        if not saved:
            return json_error('Not found', 404)
        return jsonify({'success': True, 'id': saved['id']})
    quote = {
        'date': date.today().isoformat(),
        'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'),
        'user': user.get('email'),
        'company': company,
        'customer_id': customer_id,
        'project': project,
        'job_address': clean_text(data.get('job_address'), 500),
        'items': _charge_saved_size_lines(priced, _effective_minimum(customer)) if quote_type == 'size' else priced,
        'currency': currency,
        'fx_rate': fx_rate,
        'type': quote_type,
        'tier': tier,
    }
    if 'notes' in data or 'remarks' in data:
        quote['notes'] = clean_text(data.get('notes') or data.get('remarks'), 2000)
    _stamp_terms(quote, quote['items'], new_discount, new_addon)
    if user.get('role') == 'customer':
        quote.update(_canada_tax_fields(customer, data.get('ship_province')))
    stored = append_quote(quote)
    _remember_igus(stored.get('items'), stored.get('date'))
    return jsonify({'success': True, 'id': stored['id']})


def _quote_for_viewer(quote, user):
    row = dict(quote)
    if user.get('role') == 'customer':
        row.pop('price_addon_percent', None)
        row.pop('fx_rate', None)
    return row


def _visible_quote(user, quote_id):
    if not (user_has_module(user, 'quotes') or user_has_module(user, 'quote-history')):
        return None
    return next((row for row in quotes_visible_to(user, load_quotes()) if row.get('id') == quote_id), None)


def _copy_note(quote_id):
    return f'Copied from quote #{quote_id}'


def _copied_extras(raw):
    charges = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        label = clean_text(row.get('label'), 80)
        if not label:
            continue
        try:
            amount = round(float(row.get('amount') or 0), 2)
        except (TypeError, ValueError):
            continue
        charges.append({'label': label, 'amount': amount})
    return charges


def _repriced_quote_items(items, tier, currency, tiers, fx_rate=None, price_factor=1, minimum_sqf=0):
    """Price a copy at the current tier. Surcharge lines and drawing files are left out."""
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if spec and spec.get('width') not in (None, '') and spec.get('height') not in (None, ''):
            stored_minimum = item.get('minimum_sqf') if 'minimum_sqf' in item else minimum_sqf
            priced = _reprice(spec, item.get('pcs'), tier, currency, tiers, fx_rate, price_factor, stored_minimum)
            copied_line = {
                'name': item.get('name') or 'Glass',
                'spec': spec,
                'irregular_shape': bool(item.get('irregular_shape')),
            }
            copied_line.update(_size_price_fields(priced))
            if item.get('remarks'):
                copied_line['remarks'] = clean_text(item.get('remarks'), 500)
            floor = production.floor_code(item)
            if floor:
                copied_line['floor'] = floor
            rows.append(copied_line)
            continue
        plain = {
            'name': item.get('name') or '',
            'price': item.get('price') if item.get('price') not in (None, '') else item.get('total') or 0,
            'size': item.get('size') or '-',
            'pcs': item.get('pcs') if item.get('pcs') not in (None, '') else '-',
            'currency': currency,
        }
        if item.get('remarks'):
            plain['remarks'] = clean_text(item.get('remarks'), 500)
        floor = production.floor_code(item)
        if floor:
            plain['floor'] = floor
        rows.append(plain)
    return expand_irregular_lines(rows, load_pricing_settings()['irregular_shape_surcharge'])


def _tier_for_copy(user, source, customers, tiers):
    requested = str((source or {}).get('tier') or '').strip()
    chosen = tier_for_request(user, requested, customers, tiers)
    if chosen:
        return chosen
    return tier_for_request(user, '', customers, tiers)


@app.route('/api/quotes/<int:quote_id>', methods=['GET'])
@login_required
def get_quote(quote_id):
    user = current_user()
    quote = _visible_quote(user, quote_id)
    if not quote:
        return json_error('Not found', 404)
    return jsonify(_with_quote_tax(_quote_for_viewer(quote, user)))


@app.route('/api/quotes/<int:quote_id>/copy', methods=['POST'])
@login_required
def copy_quote(quote_id):
    user = current_user()
    source = _visible_quote(user, quote_id)
    if not source:
        return json_error('Not found', 404)
    customers = load_customers()
    tiers = load_tiers()
    tier = _tier_for_copy(user, source, customers, tiers)
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    currency = str(source.get('currency') or 'usd').lower()
    if currency not in {'usd', 'cad'}:
        currency = 'usd'
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id or '')
        if not customer or source.get('customer_id') != customer_id:
            return json_error('Not found', 404)
        company = customer.get('name') or source.get('company') or 'Customer'
        currency = _required_currency(customer)
    else:
        customer_id = source.get('customer_id')
        company = source.get('company') or 'ORA'
        owner_for_currency = customers.get(customer_id or '')
        if owner_for_currency:
            currency = _required_currency(owner_for_currency)
    fx_rate = _stored_fx(source)
    note = _copy_note(quote_id)
    previous = clean_text(source.get('notes'), 2000)
    notes = previous if note in previous else (previous + '\n' + note).strip()
    quote_type = source.get('type') if source.get('type') in {'sqf', 'size'} else 'sqf'
    owner = customers.get(customer_id or '')
    ship = source.get('ship_province') or ''
    copied = {
        'date': date.today().isoformat(),
        'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'),
        'user': user.get('email'),
        'company': company,
        'customer_id': customer_id,
        'project': source.get('project') or '',
        'job_address': source.get('job_address') or '',
        'notes': notes,
        'copy_note': note,
        'copied_from': quote_id,
        'items': _repriced_quote_items(
            source.get('items'), tier, currency, tiers, fx_rate,
            _price_factor(source.get('price_addon_percent') if 'price_addon_percent' in source else (owner or {}).get('price_addon_percent')),
        ),
        'fx_rate': fx_rate,
        'extra_charges': _copied_extras(source.get('extra_charges')),
        'currency': currency,
        'type': quote_type,
        'tier': tier,
        'status': 'draft',
    }
    if ship:
        copied['ship_province'] = ship
    copied.update(_canada_tax_fields(owner, ship))
    copy_discount, copy_addon = _saved_or_company_terms(source if 'price_addon_percent' in source or 'additional_discount_percent' in source else None, owner)
    _stamp_terms(copied, copied['items'], copy_discount, copy_addon)
    stored = append_quote(copied)
    _remember_igus(stored.get('items'), stored.get('date'))
    return jsonify({'success': True, 'id': stored['id'], 'copy_note': note})


@app.route('/api/quotes/<int:quote_id>/currency', methods=['POST'])
@login_required
def change_quote_currency(quote_id):
    """Reprice a quote in the customer's currency. Staff and the quote's owner can do this."""
    user = current_user()
    quote = _visible_quote(user, quote_id)
    if not quote:
        return json_error('Not found', 404)
    if quote.get('order_id'):
        return json_error('This quote was already sent to production.', 409)
    data = json_body() or {}
    requested = clean_text(data.get('currency'), 8).lower()
    if requested not in {'usd', 'cad'}:
        return json_error('Currency must be usd or cad.', 400)
    if user.get('role') == 'customer' and quote.get('customer_id') != user.get('customer_id'):
        return json_error('Not found', 404)
    if user.get('role') != 'customer' and not _is_ora_staff(user):
        return json_error('You do not have permission to change this quote.', 403)
    customers = load_customers()
    tiers = load_tiers()
    tier = quote.get('tier') or _tier_for_copy(user, quote, customers, tiers)
    fx_rate = _stored_fx(quote)
    items = _repriced_quote_items(
        quote.get('items'), tier, requested, tiers, fx_rate, _price_factor(quote.get('price_addon_percent')),
    )
    if quote.get('type') != 'size' and str(quote.get('currency') or 'usd').lower() != requested:
        factor = _switch_factor(requested, fx_rate)
        for item in items:
            if item.get('spec'):
                continue
            for key in ('price', 'total'):
                if item.get(key) in (None, ''):
                    continue
                try:
                    item[key] = round(float(item[key]) * factor, 2)
                except (TypeError, ValueError):
                    pass
    quote['items'] = expand_irregular_lines(items, load_pricing_settings()['irregular_shape_surcharge'])
    _stamp_terms(quote, quote['items'], quote.get('additional_discount_percent'), quote.get('price_addon_percent'))
    quote['currency'] = requested
    quote['fx_rate'] = fx_rate
    saved = replace_quote(quote_id, quote)
    if not saved:
        return json_error('Not found', 404)
    return jsonify(_quote_for_viewer(saved, user))


_IMPORT_COLUMNS = ('width', 'height', 'qty', 'glass', 'spacer', 'mark')


def _catalog_names():
    glass, spacers, _sealants = _live_materials()
    return [item['name'] for item in glass], [item['name'] for item in spacers]


def _known_product(name, names):
    token = str(name or '').strip()
    if not token:
        return ''
    folded = {item.casefold(): item for item in names}
    return folded.get(token.casefold(), '')


def _import_company(user, requested):
    customers = load_customers()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id') or ''
        customer = customers.get(customer_id)
        if not customer:
            return None, None, 'Your account is not linked to a customer.'
        return customer_id, customer, None
    customer_id = clean_text(requested, 64)
    customer = customers.get(customer_id)
    if not customer_id or not customer:
        return None, None, 'Choose a customer company.'
    return customer_id, customer, None


def _templates_visible(user, customer_id):
    rows = load_templates()
    if user.get('role') == 'customer':
        own = user.get('customer_id') or ''
        return [row for row in rows if row.get('customer_id') == own]
    if user.get('role') != 'admin':
        if not customer_id:
            return None
        return [row for row in rows if row.get('customer_id') == customer_id]
    if customer_id:
        return [row for row in rows if row.get('customer_id') == customer_id]
    return rows


def _owned_import(user, import_id):
    """Customers only reach their company's upload. Staff with Quotes can open any company import."""
    record = next((row for row in load_imports() if row.get('id') == import_id), None)
    if not record or user.get('role') == 'shop':
        return None
    if user.get('role') == 'customer' and record.get('customer_id') != user.get('customer_id'):
        return None
    return record


def _template_payload(data, existing=None):
    customer_id = clean_text(data.get('customer_id'), 64)
    if customer_id not in load_customers():
        return None, 'Choose a customer company.'
    name = clean_text(data.get('name'), 120)
    if not name:
        return None, 'Template name is required.'
    kind = clean_text(data.get('kind'), 20)
    if kind not in {'excel', 'pdf'}:
        return None, 'Template type must be excel or pdf.'
    unit = clean_text(data.get('unit') or 'auto', 10)
    if unit not in {'auto', 'mm', 'in'}:
        return None, 'Unit must be auto, mm, or in.'
    try:
        header_row = int(data.get('header_row') or 1)
    except (TypeError, ValueError):
        return None, 'Header row must be a number.'
    if header_row < 1 or header_row > 100:
        return None, 'Header row must be between 1 and 100.'
    incoming = data.get('columns') if isinstance(data.get('columns'), dict) else {}
    columns = {key: clean_text(incoming.get(key), 80) for key in _IMPORT_COLUMNS}
    pdf_mode = clean_text(data.get('pdf_mode') or 'table', 20)
    if pdf_mode not in {'table', 'regex'}:
        return None, 'PDF mode must be table or regex.'
    pattern = str(data.get('line_pattern') or '')[:300]
    if kind == 'pdf' and pdf_mode == 'regex':
        if not pattern:
            return None, 'A PDF line pattern is required.'
    else:
        for key in ('width', 'height', 'qty', 'glass'):
            if not columns[key]:
                return None, 'Map the width, height, quantity, and glass columns.'
    record = {
        'customer_id': customer_id,
        'name': name,
        'kind': kind,
        'unit': unit,
        'match': clean_text(data.get('match'), 80),
        'sheet': clean_text(data.get('sheet'), 80),
        'header_row': header_row,
        'columns': columns,
        'pdf_mode': pdf_mode,
        'line_pattern': pattern if kind == 'pdf' else '',
        'active': data.get('active', True) is not False,
    }
    if existing:
        record['id'] = existing.get('id')
    return record, None


def _trim_num(value):
    number = round(float(value), 4)
    if number == int(number):
        return str(int(number))
    text = f'{number:.4f}'.rstrip('0').rstrip('.')
    return text


def _import_spec(line):
    spacer = line.get('spacer_product') or ''
    sealant = ''
    if spacer:
        sealant = suggest_sealant(spacer, _live_materials()[2]).get('selected') or ''
    return {
        'glass1': line.get('glass_product') or '',
        'temper1': 'n/a',
        'spacer1': spacer,
        'sealant1': sealant,
        'glass2': '',
        'temper2': 'n/a',
        'spacer2': '',
        'sealant2': '',
        'glass3': '',
        'temper3': 'n/a',
        'width': line.get('width'),
        'height': line.get('height'),
        'unit': 'in',
    }


def _price_import_line(line, tier, currency, fx_rate=None):
    line['price_sqf'] = None
    line['price_pc'] = None
    line['sqf'] = None
    line['total'] = None
    if line.get('errors') or not line.get('glass_product') or not line.get('width') or not line.get('height') or not line.get('qty'):
        return
    priced = _reprice(_import_spec(line), line.get('qty'), tier, currency, load_tiers(), fx_rate)
    line.update(priced)


def _apply_saved_mappings(parsed, customer_id, tier, currency, extra_glass=None, extra_spacer=None):
    glass_names, spacer_names = _catalog_names()
    saved = company_mappings(customer_id)
    glass_map = {}
    spacer_map = {}
    for source, product in (saved.get('glass') or {}).items():
        known = _known_product(product, glass_names)
        if source and known:
            glass_map[str(source).casefold()] = known
    for source, product in (extra_glass or {}).items():
        known = _known_product(product, glass_names)
        if source and known:
            glass_map[str(source).casefold()] = known
    for source, product in (saved.get('spacer') or {}).items():
        known = _known_product(product, spacer_names)
        if source and known:
            spacer_map[str(source).casefold()] = known
    for source, product in (extra_spacer or {}).items():
        known = _known_product(product, spacer_names)
        if source and known:
            spacer_map[str(source).casefold()] = known
    exact_glass = {name.casefold(): name for name in glass_names}
    exact_spacer = {name.casefold(): name for name in spacer_names}
    unmatched_glass = []
    unmatched_spacer = []
    for line in parsed.get('lines') or []:
        errors = [error for error in (line.get('errors') or []) if not str(error).startswith('Unmatched')]
        raw_glass = line.get('glass_name') or ''
        product = glass_map.get(raw_glass.casefold()) or exact_glass.get(raw_glass.casefold(), '') if raw_glass else ''
        line['glass_product'] = product or ''
        if raw_glass and not product:
            errors.append('Unmatched glass')
            if raw_glass not in unmatched_glass:
                unmatched_glass.append(raw_glass)
        raw_spacer = line.get('spacer_name') or ''
        if raw_spacer:
            spacer = spacer_map.get(raw_spacer.casefold()) or exact_spacer.get(raw_spacer.casefold(), '')
            line['spacer_product'] = spacer or ''
            if not spacer:
                errors.append('Unmatched spacer')
                if raw_spacer not in unmatched_spacer:
                    unmatched_spacer.append(raw_spacer)
        else:
            line['spacer_product'] = ''
        line['errors'] = errors
        _price_import_line(line, tier, currency)
    return unmatched_glass, unmatched_spacer


def _import_tier(user, requested):
    tiers = load_tiers()
    tier = tier_for_request(user, clean_text(requested, 20), load_customers(), tiers)
    return tier


def _present_import(record, template, user, currency='usd', extra_glass=None, extra_spacer=None, tier_requested=''):
    currency = currency if currency in {'usd', 'cad'} else 'usd'
    tier = _import_tier(user, tier_requested)
    if not tier:
        return {'id': record['id'], 'status': 'failed', 'error': 'That pricing tier is not available.', 'lines': [], 'currency': currency, 'tier': ''}
    path = upload_abspath(record.get('stored'))
    if not path:
        record['status'] = 'failed'
        replace_import(record['id'], record)
        return {'id': record['id'], 'status': 'failed', 'error': 'The uploaded file is missing.', 'lines': [], 'currency': currency, 'tier': tier}
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    try:
        parsed = parse_upload(path, ext, template)
    except OrderImportError as exc:
        record['status'] = 'failed'
        record['template_id'] = template.get('id')
        replace_import(record['id'], record)
        return {
            'id': record['id'],
            'status': 'failed',
            'error': str(exc),
            'lines': [],
            'looks_wrong': True,
            'template': public_template(template),
            'currency': currency,
            'tier': tier or '',
        }
    unmatched_glass, unmatched_spacer = _apply_saved_mappings(
        parsed, record.get('customer_id'), tier, currency, extra_glass, extra_spacer,
    )
    if parsed.get('error') or parsed.get('looks_wrong'):
        status = 'needs_review'
    elif unmatched_glass or unmatched_spacer:
        status = 'needs_mapping'
    else:
        status = 'preview'
    record['status'] = status
    record['template_id'] = template.get('id')
    replace_import(record['id'], record)
    return {
        'id': record['id'],
        'status': status,
        'error': parsed.get('error') or '',
        'looks_wrong': bool(parsed.get('looks_wrong')),
        'unit_used': parsed.get('unit_used') or 'in',
        'template': public_template(template),
        'lines': parsed.get('lines') or [],
        'unmatched_glass': unmatched_glass,
        'unmatched_spacer': unmatched_spacer,
        'currency': currency,
        'tier': tier or '',
        'company': record.get('company') or '',
        'filename': record.get('filename') or '',
    }


def _find_template(template_id, customer_id):
    return next(
        (row for row in load_templates() if row.get('id') == template_id and row.get('customer_id') == customer_id),
        None,
    )


@app.route('/api/order-templates', methods=['GET'])
@module_required('quotes')
def list_order_templates():
    user = current_user()
    customer_id = user.get('customer_id') if user.get('role') == 'customer' else clean_text(request.args.get('customer_id'), 64)
    if user.get('role') not in {'admin', 'customer'} and not customer_id:
        return json_error('Choose a customer company.', 400)
    rows = _templates_visible(user, customer_id)
    return jsonify([public_template(row) for row in rows])


@app.route('/api/order-templates', methods=['POST'])
@admin_required
def create_order_template():
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    record, error = _template_payload(data)
    if error:
        return json_error(error, 400)
    rows = load_templates()
    record['id'] = max([int(row.get('id') or 0) for row in rows] or [0]) + 1
    rows.append(record)
    save_templates(rows)
    return jsonify(public_template(record))


@app.route('/api/order-templates/<int:template_id>', methods=['PUT'])
@admin_required
def update_order_template(template_id):
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    rows = load_templates()
    existing = next((row for row in rows if row.get('id') == template_id), None)
    if not existing:
        return json_error('Not found', 404)
    record, error = _template_payload(data, existing)
    if error:
        return json_error(error, 400)
    for index, row in enumerate(rows):
        if row.get('id') == template_id:
            rows[index] = record
    save_templates(rows)
    return jsonify(public_template(record))


@app.route('/api/order-templates/<int:template_id>', methods=['DELETE'])
@admin_required
def delete_order_template(template_id):
    rows = [row for row in load_templates() if row.get('id') != template_id]
    if len(rows) == len(load_templates()):
        return json_error('Not found', 404)
    save_templates(rows)
    return jsonify({'success': True})


@app.route('/api/order-templates/preview', methods=['POST'])
@admin_required
def preview_order_template():
    raw = request.form.get('template') or ''
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return json_error('Template details must be JSON.', 400)
    if not isinstance(data, dict):
        return json_error('Template details must be JSON.', 400)
    record, error = _template_payload(data)
    if error:
        return json_error(error, 400)
    request_id = clean_text(request.form.get('request_id'), 20)
    filename = 'sample'
    temp_path = None
    try:
        if request_id:
            found = next((row for row in load_requests() if str(row.get('id')) == request_id), None)
            if not found:
                return json_error('That template request was not found.', 404)
            path = upload_abspath(found.get('stored'))
            filename = found.get('filename') or filename
            if not path:
                return json_error('The sample file is missing.', 404)
            ext = os.path.splitext(path)[1].lower().lstrip('.')
        else:
            upload = request.files.get('file')
            if not upload or not upload.filename:
                return json_error('Choose a sample file.', 400)
            payload = upload.read(9 * 1024 * 1024)
            _kind, ext = check_upload(upload.filename, payload)
            filename = safe_filename(upload.filename)
            fd, temp_path = tempfile.mkstemp(suffix='.' + ext)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
            path = temp_path
        parsed = parse_upload(path, ext, record)
    except OrderImportError as exc:
        return jsonify({'status': 'failed', 'error': str(exc), 'lines': [], 'looks_wrong': True})
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return jsonify({
        'status': 'needs_review' if parsed.get('looks_wrong') or parsed.get('error') else 'preview',
        'error': parsed.get('error') or '',
        'looks_wrong': bool(parsed.get('looks_wrong')),
        'unit_used': parsed.get('unit_used') or 'in',
        'filename': filename,
        'lines': parsed.get('lines') or [],
    })


@app.route('/api/order-mappings', methods=['GET'])
@module_required('quotes')
def list_order_mappings():
    user = current_user()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id') or ''
    else:
        customer_id = clean_text(request.args.get('customer_id'), 64)
        if not customer_id:
            return json_error('Choose a customer company.', 400)
        if customer_id not in load_customers():
            return json_error('Unknown customer.', 404)
    if user.get('role') == 'customer' and not customer_id:
        return json_error('Your account is not linked to a customer.', 400)
    return jsonify(company_mappings(customer_id))


@app.route('/api/order-imports', methods=['GET'])
@module_required('quotes')
def list_order_imports():
    user = current_user()
    customer_id = user.get('customer_id') if user.get('role') == 'customer' else clean_text(request.args.get('customer_id'), 64)
    if user.get('role') != 'admin' and not customer_id:
        return json_error('Choose a customer company.', 400)
    rows = []
    for row in load_imports():
        if user.get('role') == 'customer' and row.get('customer_id') != customer_id:
            continue
        if user.get('role') != 'admin' and user.get('role') != 'customer' and row.get('customer_id') != customer_id:
            continue
        if user.get('role') == 'admin' and customer_id and row.get('customer_id') != customer_id:
            continue
        rows.append({
            'id': row.get('id'),
            'customer_id': row.get('customer_id') or '',
            'company': row.get('company') or '',
            'filename': row.get('filename') or '',
            'status': row.get('status') or '',
            'created_at': row.get('created_at') or '',
            'user': row.get('user') or '',
        })
    return jsonify(rows)


@app.route('/api/order-imports', methods=['POST'])
@module_required('quotes')
def create_order_import():
    user = current_user()
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return json_error('Choose a file.', 400)
    payload = upload.read(9 * 1024 * 1024)
    try:
        check_upload(upload.filename, payload)
    except OrderImportError as exc:
        return json_error(str(exc), 400)
    customer_id, customer, error = _import_company(user, request.form.get('customer_id'))
    if error:
        return json_error(error, 400)
    stored = store_upload(upload.filename, payload)
    record = add_import({
        'customer_id': customer_id,
        'company': customer.get('name') or '',
        'user': user.get('email'),
        'filename': safe_filename(upload.filename),
        'stored': stored,
        'status': 'uploaded',
        'template_id': None,
        'project': clean_text(request.form.get('project'), 200),
    })
    templates = [row for row in load_templates() if row.get('customer_id') == customer_id and row.get('active', True)]
    requested = clean_text(request.form.get('template_id'), 20)
    currency = clean_text(request.form.get('currency') or 'usd', 8).lower()
    if requested:
        template = next((row for row in templates if str(row.get('id')) == requested), None)
        if not template:
            return json_error('That template is not for this company.', 400)
        return jsonify(_present_import(record, template, user, currency))
    path = upload_abspath(stored)
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    chosen, options, reason = choose_template(templates, upload.filename, ext, path)
    if reason == 'needs_template':
        record['status'] = 'needs_template'
        replace_import(record['id'], record)
        return jsonify({'id': record['id'], 'status': 'needs_template', 'templates': [], 'lines': [], 'company': record['company'], 'filename': record['filename']})
    if reason == 'choose':
        record['status'] = 'choose_template'
        replace_import(record['id'], record)
        return jsonify({
            'id': record['id'],
            'status': 'choose_template',
            'templates': [public_template(row) for row in options],
            'lines': [],
            'company': record['company'],
            'filename': record['filename'],
        })
    return jsonify(_present_import(record, chosen, user, currency))


@app.route('/api/order-imports/<int:import_id>/parse', methods=['POST'])
@module_required('quotes')
def parse_order_import(import_id):
    user = current_user()
    record = _owned_import(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body() or {}
    template = _find_template(int(data.get('template_id') or 0), record.get('customer_id'))
    if not template:
        return json_error('Choose a template for this company.', 400)
    currency = clean_text(data.get('currency') or 'usd', 8).lower()
    return jsonify(_present_import(record, template, user, currency, tier_requested=data.get('tier')))


@app.route('/api/order-imports/<int:import_id>/map', methods=['POST'])
@module_required('quotes')
def map_order_import(import_id):
    user = current_user()
    record = _owned_import(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    template = _find_template(int(record.get('template_id') or 0), record.get('customer_id'))
    if not template:
        return json_error('Choose a template before matching names.', 400)
    glass_names, spacer_names = _catalog_names()
    glass = data.get('glass') if isinstance(data.get('glass'), dict) else {}
    spacer = data.get('spacer') if isinstance(data.get('spacer'), dict) else {}
    valid_glass = {str(source): _known_product(product, glass_names) for source, product in glass.items()}
    valid_spacer = {str(source): _known_product(product, spacer_names) for source, product in spacer.items()}
    if any(source and not product for source, product in list(valid_glass.items()) + list(valid_spacer.items())):
        return json_error('Each name has to match one of our products.', 400)
    if data.get('remember', True):
        remember_mappings(record.get('customer_id'), valid_glass, valid_spacer)
        extra_glass, extra_spacer = None, None
    else:
        extra_glass, extra_spacer = valid_glass, valid_spacer
    currency = clean_text(data.get('currency') or 'usd', 8).lower()
    return jsonify(_present_import(record, template, user, currency, extra_glass, extra_spacer, data.get('tier')))


def _normalize_import_lines(raw_lines):
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > 200:
        return None, 'Add at least one line, and no more than 200.'
    glass_names, spacer_names = _catalog_names()
    lines = []
    blocked = False
    for raw in raw_lines:
        if not isinstance(raw, dict):
            return None, 'Each line must be an object.'
        errors = []
        try:
            width = round(float(raw.get('width')), 4)
            height = round(float(raw.get('height')), 4)
        except (TypeError, ValueError):
            width = height = 0
        if width <= 0 or height <= 0 or width > 300 or height > 300:
            errors.append('Size must be a positive number of inches.')
        try:
            qty = int(raw.get('qty'))
        except (TypeError, ValueError):
            qty = 0
        if qty < 1 or qty > 10000:
            errors.append('Quantity must be a whole number.')
        glass = _known_product(raw.get('glass_product'), glass_names)
        if not glass:
            errors.append('Choose one of our glass products.')
        spacer_raw = raw.get('spacer_product') or ''
        spacer = _known_product(spacer_raw, spacer_names) if spacer_raw else ''
        if spacer_raw and not spacer:
            errors.append('Choose one of our spacers.')
        if errors:
            blocked = True
        line = {
            'mark': clean_text(raw.get('mark'), 80),
            'width': width,
            'height': height,
            'unit': 'in',
            'qty': qty,
            'glass_name': clean_text(raw.get('glass_name'), 200),
            'spacer_name': clean_text(raw.get('spacer_name'), 200),
            'glass_product': glass,
            'spacer_product': spacer,
            'irregular_shape': bool(raw.get('irregular_shape') or raw.get('irregular')),
            'errors': errors,
        }
        lines.append(line)
    if blocked:
        return lines, 'Fix the highlighted lines before creating the quote.'
    return lines, None


@app.route('/api/order-imports/<int:import_id>/quote', methods=['POST'])
@module_required('quotes')
def quote_from_import(import_id):
    user = current_user()
    record = _owned_import(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    lines, error = _normalize_import_lines(data.get('lines'))
    if error:
        return json_error(error, 400)
    currency = clean_text(data.get('currency') or 'usd', 8).lower()
    if currency not in {'usd', 'cad'}:
        return json_error('Currency must be usd or cad.', 400)
    tier = _import_tier(user, data.get('tier'))
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    fx_rate, _fx_effective = _current_fx()
    items = []
    for line in lines:
        _price_import_line(line, tier, currency, fx_rate)
        spec = _import_spec(line)
        size = f"{_trim_num(line['width'])} x {_trim_num(line['height'])} in"
        parts = [line.get('mark') or '', line.get('glass_product') or '']
        if line.get('spacer_product'):
            parts.append(line['spacer_product'])
        parts.append(size)
        items.append({
            'name': ' - '.join(part for part in parts if part),
            'sqf': line.get('sqf'),
            'price_sqf': line.get('price_sqf'),
            'price_pc': line.get('price_pc'),
            'total': line.get('total'),
            'pcs': line.get('qty'),
            'spec': spec,
            'floor': line.get('mark') or '',
            'source_glass': line.get('glass_name') or '',
            'irregular_shape': bool(line.get('irregular_shape') or line.get('irregular')),
        })
    if user.get('role') == 'customer':
        company = record.get('company') or 'Customer'
        customer_id = record.get('customer_id')
    else:
        company = record.get('company') or 'ORA'
        customer_id = record.get('customer_id')
    quote = {
        'date': date.today().isoformat(),
        'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'),
        'user': user.get('email'),
        'company': company,
        'customer_id': customer_id,
        'project': clean_text(data.get('project') or record.get('project'), 200),
        'items': items,
        'currency': currency,
        'fx_rate': fx_rate,
        'type': 'size',
        'tier': tier,
        'import_id': record.get('id'),
    }
    quote['items'] = expand_irregular_lines(quote['items'], load_pricing_settings()['irregular_shape_surcharge'])
    stored = append_quote(quote)
    _remember_igus(stored.get('items'), stored.get('date'))
    record['status'] = 'quoted'
    record['quote_id'] = stored['id']
    replace_import(record['id'], record)
    return jsonify({'success': True, 'id': stored['id'], 'items': items, 'tier': tier, 'currency': currency})


@app.route('/api/order-imports/<int:import_id>/request', methods=['POST'])
@module_required('quotes')
def request_order_template(import_id):
    user = current_user()
    record = _owned_import(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body() or {}
    note = clean_text(data.get('note'), 2000)
    created = add_request({
        'import_id': record.get('id'),
        'customer_id': record.get('customer_id'),
        'company': record.get('company') or '',
        'user': user.get('email'),
        'filename': record.get('filename') or '',
        'stored': record.get('stored') or '',
        'note': note,
    })
    record['status'] = 'requested'
    record['request_id'] = created['id']
    replace_import(record['id'], record)
    return jsonify(public_request(created))


@app.route('/api/order-template-requests', methods=['GET'])
@admin_required
def list_template_requests():
    return jsonify([public_request(row) for row in load_requests()])


@app.route('/api/order-template-requests/<int:request_id>/file', methods=['GET'])
@admin_required
def download_template_request(request_id):
    found = next((row for row in load_requests() if row.get('id') == request_id), None)
    if not found:
        return json_error('Not found', 404)
    path = upload_abspath(found.get('stored'))
    if not path:
        return json_error('The sample file is missing.', 404)
    return send_file(path, as_attachment=True, download_name=found.get('filename') or 'order')


@app.route('/api/order-template-requests/<int:request_id>/done', methods=['POST'])
@admin_required
def finish_template_request(request_id):
    rows = load_requests()
    found = None
    for row in rows:
        if row.get('id') == request_id:
            row['status'] = 'done'
            row['done_by'] = current_user().get('email')
            found = row
    if not found:
        return json_error('Not found', 404)
    save_requests(rows)
    return jsonify(public_request(found))


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


def suggested_order_tier(quote, tiers):
    """Tier to offer when a customer is created from this quote. Falls back to the usual default."""
    stored = clean_text((quote or {}).get('tier'), 20)
    if stored and stored in (tiers or {}):
        return stored
    return _fallback_tier(tiers)


def _tier_for_new_customer(data, new_customer, tiers, suggested):
    raw = None
    if data.get('tier') not in (None, ''):
        raw = data.get('tier')
    elif isinstance(new_customer, dict) and new_customer.get('tier') not in (None, ''):
        raw = new_customer.get('tier')
    else:
        raw = suggested
    return _chosen_tier(raw, tiers)


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
        row = _quote_for_viewer(quote, user)
        # The quote keeps the tier and prices it was created with. Do not reprice it.
        if row.get('order_id') and not row.get('order_status'):
            row['order_status'] = 'Approved for order'
        visible.append(_with_quote_tax(row))
    return jsonify(filter_quote_rows(visible))


@app.route('/api/order-customers', methods=['GET'])
@login_required
def order_customer_choices():
    """Customers a staff member can attach to a production order."""
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
            'kind': 'account',
            'province': customer.get('province') or '',
            'country': customer.get('country') or '',
            'currency': _required_currency(customer),
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
        if not can_place_production_orders(user):
            return []
        customer_id = user.get('customer_id')
        own_company = [order for order in orders if order.get('customer_id') == customer_id]
        if customer_subrole(user) == 'management':
            return own_company
        email = user.get('email')
        return [
            order for order in own_company
            if (order.get('converted_by') or order.get('approved_by')) == email
        ]
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
    import pricing_catalog
    return name in pricing_catalog.known_names()


def order_for_viewer(order, user, with_coverage=False):
    status = order_status(order)
    summary, _error = price_summary(
        order.get('items') or [],
        order.get('discount') or {'type': 'percent', 'value': 0},
        order.get('extra_charges') or [],
    )
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary or {
        'igu_subtotal': 0, 'igu_total': 0, 'total': 0, 'discount_amount': 0,
        'discount': {'type': 'percent', 'value': 0}, 'extra_charges': [], 'extras_total': 0,
    }, order.get('additional_discount_percent'))
    base = {
        'id': order.get('id'),
        'quote_id': order.get('quote_id'),
        'customer_id': order.get('customer_id'),
        'company': order.get('company') or '',
        'project': order.get('project') or '',
        'job_address': order.get('job_address') or '',
        'notes': order.get('notes') or '',
        'approved_by': order.get('approved_by'),
        'converted_by': order.get('converted_by') or order.get('approved_by') or '',
        'approved_at': order.get('approved_at'),
        'date': order.get('date'),
        'currency': order.get('currency') or 'usd',
        'tier': order.get('tier'),
        'tier_label': tier_label(order.get('tier')) or order.get('tier_label'),
        'status': status,
        'status_label': STATUS_LABELS[status],
        'invoice_id': order.get('invoice_id'),
        'invoice_number': order.get('invoice_number'),
        'drawings': [
            {
                'line_id': drawing.get('line_id'),
                'filename': drawing.get('filename') or '',
                'is_dwg': bool(drawing.get('is_dwg')),
                'uploaded_at': drawing.get('uploaded_at') or '',
            }
            for drawing in order.get('drawings') or []
            if isinstance(drawing, dict)
        ],
        'ship_province': order.get('ship_province') or '',
        'tax_label': order.get('tax_label') or '',
        'documents': [],
    }
    staff_view = user.get('role') != 'customer'
    if staff_view and order.get('fx_rate') not in (None, ''):
        base['fx_rate'] = order.get('fx_rate')
    documents = []
    for row in load_invoices():
        if row.get('order_id') != order.get('id'):
            continue
        document = {
            'id': row.get('id'),
            'number': row.get('number'),
            'doc_type': document_type(row),
            'issuer': row.get('issuer') or 'us',
            'kind': row.get('kind') or 'customer',
            'currency': row.get('currency') or 'usd',
            'qbo_warning': row.get('qbo_warning') or '',
            'total': money(row.get('total')),
            'voided': bool(row.get('voided')),
        }
        if staff_view:
            document['fx_rate'] = row.get('fx_rate')
            document['fx_date'] = row.get('fx_date') or ''
            document['fx_source'] = row.get('fx_source') or ''
        documents.append(document)
    base['documents'] = documents
    owner = load_customers().get(order.get('customer_id') or '') or {}
    tax = _canada_tax_fields(owner, order.get('ship_province'))
    if tax:
        base['ship_province'] = tax.get('ship_province') or base.get('ship_province') or ''
        base['tax_label'] = tax.get('tax_label') or ''
        base['qbo_tax_code'] = tax.get('qbo_tax_code') or ''
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
    view['items'] = [production.present_order_line(item) for item in (order.get('items') or [])]
    view['shipping'] = shipments.shipment_progress(order)
    view['shipments'] = _public_shipments(order)
    view['billing_mode'] = _billing_mode(order)
    view['partial_invoicing'] = bool(order.get('partial_invoicing'))
    view['discount'] = summary.get('discount') or {'type': 'percent', 'value': 0}
    view['discount_amount'] = summary.get('discount_amount', 0)
    view['additional_discount_percent'] = summary.get('additional_discount_percent') or 0
    view['additional_discount_amount'] = summary.get('additional_discount_amount') or 0
    if user.get('role') != 'customer':
        view['price_addon_percent'] = _percent(order.get('price_addon_percent'), 1000)
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


def _order_only_from_request(data, customers, tiers, user, suggested=None):
    new_customer = data.get('new_customer')
    if not isinstance(new_customer, dict):
        return None, None, 'Choose a customer.'
    name = clean_text(new_customer.get('name'), 200)
    if not name:
        return None, None, 'Customer name is required.'
    tier, error = _tier_for_new_customer(data, new_customer, tiers, suggested)
    if error:
        return None, None, error
    customer_id = new_customer_id(name, customers)
    customer = blank_customer(customer_id, name, tier)
    customer['kind'] = 'account'
    customer['contact'] = clean_text(new_customer.get('contact'), 120)
    customer['email'] = clean_text(new_customer.get('email'), 200)
    customer['phone'] = clean_text(new_customer.get('phone'), 40)
    customer['address'] = clean_text(new_customer.get('address'), 300)
    country = clean_text(new_customer.get('country') or 'United States', 80)
    customer['country'] = 'Canada' if country.lower() in {'canada', 'ca'} else 'United States'
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
    _lock_credit_currency(customer)
    customers[customer_id] = customer
    save_customers(customers)
    return customer_id, customer, None


def _customer_for_staff_order(data, customers, tiers, user, suggested=None):
    """Staff pick a saved customer, or create a normal customer from the quote."""
    if isinstance(data.get('new_customer'), dict):
        return _order_only_from_request(data, customers, tiers, user, suggested)
    customer_id = clean_text(data.get('customer_id'), 64)
    if not customer_id or not ID_RE.fullmatch(customer_id):
        return None, None, 'Choose a customer.'
    customer = customers.get(customer_id)
    if not customer:
        return None, None, 'Unknown customer.'
    if (customer.get('tier') or '') not in tiers and data.get('tier') not in (None, ''):
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
    customer_order = can_place_production_orders(user)
    if not staff and not customer_order:
        return json_error('You are not allowed to send a quote to production.', 403)
    if request.files or (request.content_type or '').startswith('multipart/'):
        data = request.form.to_dict(flat=True)
        if data.get('new_customer'):
            try:
                data['new_customer'] = json.loads(data['new_customer'])
            except (TypeError, ValueError):
                return json_error('Customer details could not be read.', 400)
    else:
        data = json_body()
        if data is None:
            data = {}
    quotes = load_quotes()
    quote = next((row for row in quotes if row.get('id') == quote_id), None)
    if not quote:
        return json_error('Not found', 404)
    if customer_order:
        if quote.get('customer_id') != user.get('customer_id'):
            return json_error('Not found', 404)
        if not quotes_visible_to(user, [quote]):
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
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        customer = customers.get(customer_id or '') or {}
        company_tier = _company_tier(customer, tiers)
        company = customer.get('name') or quote.get('company') or ''
    else:
        suggested = suggested_order_tier(quote, tiers)
        customer_id, customer, error = _customer_for_staff_order(data, customers, tiers, user, suggested)
        if error:
            return json_error(error, 400)
        company_tier = _company_tier(customer, tiers)
        company = customer.get('name') or ''
    if not quote.get('fx_rate'):
        quote['fx_rate'] = _stored_fx(quote)
    currency = str(quote.get('currency') or 'usd').lower()
    expected = _required_currency(customer)
    currency_notice = None
    if currency != expected:
        currency_notice = f'Converted this quote to {expected.upper()} for {company}.'
        quote_tier = quote.get('tier') or company_tier
        converted = _repriced_quote_items(quote.get('items'), quote_tier, expected, tiers, _stored_fx(quote))
        quote['items'] = expand_irregular_lines(converted, load_pricing_settings()['irregular_shape_surcharge'])
        quote['currency'] = expected
        items = quote['items']
        currency = expected
    priced_items = []
    for item in items:
        if item.get('kind') == 'irregular_surcharge':
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if not _usable_size_spec(spec):
            return json_error('This quote does not include the size inputs needed to reprice it.', 400)
        priced = _reprice(
            spec, item.get('pcs'), company_tier, currency, tiers, _stored_fx(quote),
            _price_factor((customer or {}).get('price_addon_percent')),
            _effective_minimum(customer),
        )
        line = {
            'name': item.get('name') or '',
            'spec': spec,
            'irregular_shape': bool(item.get('irregular_shape')),
        }
        line.update(_size_price_fields(priced))
        if item.get('remarks'):
            line['remarks'] = clean_text(item.get('remarks'), 500)
        floor = production.floor_code(item)
        if floor:
            line['floor'] = clean_text(floor, 80)
        if item.get('line_id'):
            line['line_id'] = item.get('line_id')
        if item.get('surcharge_rate') not in (None, ''):
            line['surcharge_rate'] = item.get('surcharge_rate')
        priced_items.append(line)
    priced_items = expand_irregular_lines(priced_items, load_pricing_settings()['irregular_shape_surcharge'])
    pricing = load_pricing_settings()
    irregular_lines = irregular_glass_lines(priced_items)
    drawing_uploads = []
    if irregular_lines:
        missing = []
        for line in irregular_lines:
            upload = request.files.get('drawing_' + str(line.get('line_id')))
            if not upload or not upload.filename:
                missing.append({'line_id': line.get('line_id'), 'name': line.get('name') or 'Glass'})
            else:
                drawing_uploads.append((line, upload))
        if missing:
            return jsonify({
                'error': 'Upload a drawing for each irregular shape before the order can be confirmed.',
                'needs_drawings': True,
                'dwg_creation_fee': pricing['dwg_creation_fee'],
                'lines': missing,
                'note': DRAWING_NOTE,
            }), 400
        import drawings as drawing_store
        prepared = []
        for line, upload in drawing_uploads:
            payload = upload.read(drawing_store.MAX_BYTES + 1)
            prepared.append((line, upload.filename, payload))
            if not payload:
                return json_error('The drawing file is empty.', 400)
            if len(payload) > drawing_store.MAX_BYTES:
                return json_error('The drawing is larger than 25 MB.', 400)
            if not drawing_store.is_dwg(upload.filename):
                priced_items.append(dwg_creation_line(line.get('line_id'), pricing['dwg_creation_fee']))
    else:
        prepared = []
    grand = round(sum(line['total'] for line in priced_items), 2)
    if 'additional_discount_percent' in quote:
        order_discount_percent = quote.get('additional_discount_percent')
    else:
        order_discount_percent = (customer or {}).get('additional_discount_percent')
    order_addon_percent = (customer or {}).get('price_addon_percent')
    from billing import apply_additional_discount
    commercial = apply_additional_discount(
        {'igu_subtotal': grand, 'igu_total': grand, 'total': grand, 'discount_amount': 0},
        order_discount_percent,
    )
    credit_check = conversion_allowed(
        commercial['total'], customer, str(currency).lower(), load_orders(), load_invoices(), load_payments(),
    )
    warning = None
    if not credit_check['allowed']:
        message = credit_message(credit_check)
        if user.get('role') == 'customer':
            return json_error(message, 400)
        warning = message
    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ship = _ship_province(data.get('ship_province'), customer)
    tax = _canada_tax_fields(customer, ship)
    order = append_order({
        'quote_id': quote_id,
        'customer_id': customer_id,
        'company': company,
        'project': quote.get('project') or '',
        'job_address': quote.get('job_address') or '',
        'notes': '',
        'currency': currency,
        'fx_rate': _stored_fx(quote),
        'tier': company_tier,
        'tier_label': tier_label(company_tier, tiers),
        'items': priced_items,
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'additional_discount_percent': commercial['additional_discount_percent'],
        'additional_discount_amount': commercial['additional_discount_amount'],
        'price_addon_percent': _percent(order_addon_percent, 1000),
        'extra_charges': [],
        'igu_subtotal': grand,
        'igu_total': commercial['igu_total'],
        'extras_total': 0,
        'total': commercial['total'],
        'dwg_rate': pricing['dwg_creation_fee'] if irregular_lines else None,
        'ship_province': tax.get('ship_province') or ship,
        'qbo_tax_code': tax.get('qbo_tax_code') or '',
        'tax_label': tax.get('tax_label') or '',
        'drawings': [],
        'status': 'approved',
        'changelog': [],
        'approved_by': user.get('email'),
        'converted_by': user.get('email'),
        'approved_at': approved_at,
        'date': approved_at[:10],
    })
    if prepared:
        import drawings as drawing_store
        saved_drawings = []
        for line, filename, payload in prepared:
            try:
                record = drawing_store.store_drawing(order['id'], line.get('line_id'), filename, payload)
            except drawing_store.DrawingError as exc:
                return json_error(str(exc), 400)
            record['uploaded_by'] = user.get('email') or ''
            saved_drawings.append(record)
        def remember(row):
            row['drawings'] = saved_drawings
            return row, None
        update_order(order['id'], remember)
        order['drawings'] = saved_drawings
    _remember_igus(order.get('items'), order.get('date'), order.get('id'))
    _write_production_file(order)
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
        'kind': 'account',
        'total': grand,
    }
    if warning:
        payload['credit_warning'] = warning
    if currency_notice:
        payload['currency_notice'] = currency_notice
    return jsonify(payload)


def _owned_order(user, order_id):
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        return None
    if user.get('role') == 'customer' and user.get('customer_id') != order.get('customer_id'):
        return None
    return order


@app.route('/api/orders/<int:order_id>/production.xlsx', methods=['GET'])
@login_required
def download_production_excel(order_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    order = _owned_order(user, order_id)
    if not order:
        return json_error('Not found', 404)
    import currency_rules
    rate, _effective = currency_rules.rate_on(order.get('date'))
    payload = production.workbook_bytes(order.get('items') or [], order.get('currency'), rate)
    try:
        production.write_order_workbook(order)
    except Exception:
        logger.exception('Could not refresh the stored production Excel file')
    return send_file(
        BytesIO(payload),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'production-order-{order_id}.xlsx',
    )


@app.route('/api/orders/<int:order_id>/confirmation.pdf', methods=['GET'])
@module_required('orders')
def download_order_confirmation(order_id):
    user = current_user()
    order = _owned_order(user, order_id)
    if not order:
        return json_error('Not found', 404)
    document = _pdf_document(order, 'order', user)
    buffer = BytesIO(_pdf_bytes(document))
    return send_file(
        buffer, mimetype='application/pdf', as_attachment=True,
        download_name=f'order-{order_id}.pdf',
    )


@app.route('/api/orders/<int:order_id>/drawings/<line_id>', methods=['GET'])
@module_required('orders')
def download_order_drawing(order_id, line_id):
    user = current_user()
    order = _owned_order(user, order_id)
    drawing = next(
        (row for row in (order or {}).get('drawings') or [] if str(row.get('line_id')) == str(line_id)),
        None,
    )
    if not order or not drawing:
        return json_error('Not found', 404)
    import drawings as drawing_store
    path = drawing_store.drawing_path(order_id, line_id, drawing.get('stored'))
    if not path:
        return json_error('Not found', 404)
    return send_file(path, as_attachment=True, download_name=drawing.get('filename') or 'drawing')


@app.route('/api/orders/<int:order_id>/drawings/<line_id>', methods=['POST'])
@module_required('orders')
def replace_order_drawing(order_id, line_id):
    user = current_user()
    order = _owned_order(user, order_id)
    if not order:
        return json_error('Not found', 404)
    if not any(str(row.get('line_id')) == str(line_id) for row in order.get('drawings') or []):
        return json_error('Not found', 404)
    upload = request.files.get('drawing')
    if not upload or not upload.filename:
        return json_error('Choose a drawing file.', 400)
    import drawings as drawing_store
    payload = upload.read(drawing_store.MAX_BYTES + 1)
    try:
        record = drawing_store.store_drawing(order_id, line_id, upload.filename, payload)
    except drawing_store.DrawingError as exc:
        return json_error(str(exc), 400)
    record['uploaded_by'] = user.get('email') or ''

    def mutator(row):
        row['drawings'] = [
            record if str(item.get('line_id')) == str(line_id) else item
            for item in row.get('drawings') or []
        ]
        if row.get('dwg_rate') in (None, ''):
            row['dwg_rate'] = load_pricing_settings()['dwg_creation_fee']
        items = _apply_drawing_charges(row.get('items') or [], row)
        summary, error = price_summary(
            items,
            row.get('discount') or {'type': 'percent', 'value': 0},
            row.get('extra_charges') or [],
        )
        if error:
            return None, error
        from billing import apply_additional_discount
        summary = apply_additional_discount(summary, row.get('additional_discount_percent'))
        row['items'] = items
        row['discount_amount'] = summary['discount_amount']
        row['additional_discount_amount'] = summary['additional_discount_amount']
        row['igu_subtotal'] = summary['igu_subtotal']
        row['igu_total'] = summary['igu_total']
        row['extras_total'] = summary['extras_total']
        row['total'] = summary['total']
        return row, None

    saved, error = update_order(order_id, mutator)
    if error:
        return json_error(error if error != 'Not found' else 'Not found', 400 if error != 'Not found' else 404)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


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


def _apply_drawing_charges(items, order):
    """Keep one DWG drawing creation line for each stored drawing that is not a DWG."""
    kept = [item for item in items or [] if item.get('kind') != 'dwg_creation']
    rate = order.get('dwg_rate')
    if rate in (None, ''):
        rate = load_pricing_settings()['dwg_creation_fee']
    for drawing in order.get('drawings') or []:
        if drawing.get('is_dwg'):
            continue
        kept.append(dwg_creation_line(drawing.get('line_id'), rate))
    return kept


def _parse_order_items(raw, tier, currency, tiers, fx_rate=None, price_factor=1, minimum_sqf=None):
    if not isinstance(raw, list) or not raw:
        return None, 'An order needs at least one line.'
    items = []
    for raw_item in raw:
        if not isinstance(raw_item, dict):
            return None, 'Each line must be an object.'
        if raw_item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
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
        line_minimum = raw_item.get('minimum_sqf') if 'minimum_sqf' in raw_item else minimum_sqf
        priced = _reprice(spec, pcs, tier, currency, tiers, fx_rate, price_factor, line_minimum)
        name = clean_text(raw_item.get('name'), 200)
        if not name:
            name = f"{spec.get('glass1')} - {spec.get('width')} x {spec.get('height')} {spec.get('unit')}"
        line = {
            'name': name,
            'spec': spec,
            'irregular_shape': bool(raw_item.get('irregular_shape')),
        }
        line.update(_size_price_fields(priced))
        if raw_item.get('surcharge_rate') not in (None, ''):
            line['surcharge_rate'] = raw_item.get('surcharge_rate')
        if raw_item.get('remarks'):
            line['remarks'] = clean_text(raw_item.get('remarks'), 500)
        floor = production.floor_code(raw_item)
        if floor:
            line['floor'] = clean_text(floor, 80)
        items.append(line)
    items = expand_irregular_lines(items, load_pricing_settings()['irregular_shape_surcharge'])
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
        items, error = _parse_order_items(
            data.get('items'), tier, currency, tiers, _stored_fx(order),
            _price_factor(order.get('price_addon_percent')),
            _effective_minimum(load_customers().get(order.get('customer_id') or '')),
        )
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
    items = _apply_drawing_charges(items, order)
    summary, error = price_summary(items, discount, charges)
    if error:
        return None, error
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
    updated = dict(order)
    updated['items'] = items
    updated['project'] = clean_text(data.get('project'), 200) if 'project' in data else (order.get('project') or '')
    updated['job_address'] = clean_text(data.get('job_address'), 500) if 'job_address' in data else (order.get('job_address') or '')
    updated['notes'] = clean_text(data.get('notes'), 2000) if 'notes' in data else (order.get('notes') or '')
    if 'ship_province' in data:
        from storage import PROVINCE_CODES
        chosen = clean_text(data.get('ship_province'), 8).upper()
        updated['ship_province'] = chosen if chosen in PROVINCE_CODES else ''
    updated.update(summary)
    updated['changelog'] = list(order.get('changelog') or [])
    return updated, None


def _order_is_invoiced(order):
    return bool(order.get('invoice_id')) or order_status(order) == 'delivered'


@app.route('/api/orders/<int:order_id>', methods=['PUT'])
@module_required('orders')
def edit_order(order_id):
    user = current_user()
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    address_only = not can_edit_orders(user)
    if address_only:
        if not can_edit_job_address(user, visible):
            return json_error('You do not have permission to edit orders.', 403)
        if set(data.keys()) - {'job_address'}:
            return json_error('You can only edit the job address.', 403)
    elif _order_is_invoiced(visible) and user.get('role') != 'admin':
        return json_error('This order has been invoiced and can only be changed by an administrator.', 403)

    def mutator(order):
        if _order_is_invoiced(order) and user.get('role') != 'admin' and not address_only:
            return None, 'This order has been invoiced and can only be changed by an administrator.'
        updated, error = _edited_order(order, data, load_tiers())
        if error:
            return None, error
        if address_only:
            _stamp_log(updated, user, diff_changes(order, updated))
            return updated, None
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
    _remember_igus(saved.get('items'), saved.get('date'), saved.get('id'))
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
            if updated.get('shipments'):
                changes.append({'field': 'shipments', 'action': 'kept', 'new': 'Billing follows the shipments.'})
            elif updated.get('invoice_id'):
                return None, 'This order already has an invoice.'
            else:
                issued, issue_error = _issue_delivery_invoices(updated, user)
                if issue_error:
                    return None, issue_error
                changes.append({'field': 'invoice', 'action': 'added', 'new': issued})
        _stamp_log(updated, user, changes)
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        status = 400
        return json_error(error, status)
    if target == 'delivered' and saved and saved.get('invoice_id'):
        _qbo('push_document_id', saved.get('invoice_id'))
        if saved.get('intercompany_invoice_id'):
            _qbo('push_document_id', saved.get('intercompany_invoice_id'))
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


def _save_packing_list(order_id, shipment_id, filename, payload):
    if not payload:
        return ''
    if not payload.startswith(b'%PDF'):
        return None
    folder = os.path.join(data_dir(), 'shipment_files', str(order_id))
    os.makedirs(folder, exist_ok=True)
    safe = os.path.basename(filename or '') or 'packing-list.pdf'
    with open(os.path.join(folder, f'{shipment_id}.pdf'), 'wb') as handle:
        handle.write(payload)
    return safe


def _record_shipment(order, user, data, packing_name='', packing_bytes=b''):
    number = clean_text(data.get('number'), 40)
    if not number:
        return None, 'Enter the shipment number.'
    if any(row.get('number') == number for row in order.get('shipments') or []):
        return None, 'That shipment number is already on this order.'
    shipped_on = clean_text(data.get('date'), 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', shipped_on or ''):
        return None, 'Enter the shipment date.'
    quantities, error = shipments.parse_quantities(data.get('lines'), order)
    if error:
        return None, error
    weight = clean_text(data.get('weight'), 40)
    preview_lines = [{'index': index, 'qty': qty} for index, qty in quantities.items()]
    preview = {'lines': preview_lines, 'invoiced': False}
    probe = dict(order)
    probe['shipments'] = list(order.get('shipments') or []) + [preview]
    finishing = shipments.shipment_progress(probe)['fully_shipped']
    built, pcs, sqf = shipments.shipment_lines(order, quantities)
    full_value = shipments.shipment_progress(order)['ordered_value'] or money(order.get('total'))
    portion = round(sum(money(row.get('total')) for row in built), 2)
    charges = (order.get('extra_charges') or []) if finishing else []
    summary, error = price_summary(built, _scaled_discount(order, portion, full_value), charges)
    if error:
        return None, error
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
    shipment_id = max([row.get('id') or 0 for row in order.get('shipments') or []] or [0]) + 1
    stored_name = ''
    if packing_bytes:
        stored_name = _save_packing_list(order.get('id'), shipment_id, packing_name, packing_bytes)
        if stored_name is None:
            return None, 'The packing list must be a PDF.'
    shipment = {
        'id': shipment_id,
        'number': number,
        'date': shipped_on,
        'rack': clean_text(data.get('rack'), 200),
        'weight': weight,
        'lines': preview_lines,
        'pcs': pcs,
        'sqf': sqf,
        'total': summary.get('total'),
        'items': built,
        'invoiced': False,
        'packing_list': stored_name,
        'created_by': user.get('email') or '',
    }
    mode = _billing_mode(order)
    if mode != 'us_full':
        _number, error = _issue_shipment_invoice(order, shipment, user, finishing)
        if error:
            return None, error
    order.setdefault('shipments', []).append(shipment)
    error = _close_shipment_billing(order, user)
    if error:
        return None, error
    return order, None


@app.route('/api/orders/<int:order_id>/shipments/preview', methods=['POST'])
@module_required('orders')
def preview_packing_list(order_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to record a shipment.', 403)
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        return json_error('Not found', 404)
    upload = request.files.get('packing_list')
    if not upload or not upload.filename:
        return json_error('Choose a packing-list PDF.', 400)
    payload = upload.read(25 * 1024 * 1024 + 1)
    if len(payload) > 25 * 1024 * 1024:
        return json_error('The packing list is larger than 25 MB.', 400)
    if not payload.startswith(b'%PDF'):
        return json_error('The packing list must be a PDF.', 400)
    import packing
    try:
        parsed = packing.parse_packing_pdf(payload)
    except Exception:
        return json_error('The packing list could not be read.', 400)
    if not parsed.get('rows'):
        return json_error('No glass rows were found in that packing list.', 400)
    return jsonify(packing.match_packing(order, parsed))


@app.route('/api/orders/<int:order_id>/shipments', methods=['POST'])
@module_required('orders')
def create_shipment(order_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to record a shipment.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    packing_name, packing_bytes = '', b''
    if request.files or (request.content_type or '').startswith('multipart/'):
        data = request.form.to_dict(flat=True)
        try:
            data['lines'] = json.loads(data.get('lines') or '[]')
        except (TypeError, ValueError):
            return json_error('Shipment lines could not be read.', 400)
        upload = request.files.get('packing_list')
        if upload and upload.filename:
            packing_name = upload.filename
            packing_bytes = upload.read(25 * 1024 * 1024 + 1)
            if len(packing_bytes) > 25 * 1024 * 1024:
                return json_error('The packing list is larger than 25 MB.', 400)
    else:
        data = json_body()
        if data is None:
            return json_error('Expected a JSON object', 400)

    def mutator(order):
        updated = dict(order)
        updated['shipments'] = list(order.get('shipments') or [])
        recorded, error = _record_shipment(updated, user, data, packing_name, packing_bytes)
        if error:
            return None, error
        _stamp_log(recorded, user, [{
            'field': 'shipment',
            'action': 'added',
            'new': data.get('number'),
        }])
        return recorded, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/shipments/<int:shipment_id>/packing-list', methods=['GET'])
@module_required('orders')
def download_packing_list(order_id, shipment_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    shipment = next((row for row in (order or {}).get('shipments') or [] if row.get('id') == shipment_id), None)
    path = os.path.join(data_dir(), 'shipment_files', str(order_id), f'{shipment_id}.pdf')
    if not shipment or not shipment.get('packing_list') or not os.path.isfile(path):
        return json_error('Not found', 404)
    return send_file(path, mimetype='application/pdf', as_attachment=True, download_name=shipment.get('packing_list'))


@app.route('/api/orders/<int:order_id>/partial-invoicing', methods=['POST'])
@module_required('orders')
def set_partial_invoicing(order_id):
    user = current_user()
    if user.get('role') != 'admin':
        return json_error('Only an admin can turn on partial invoicing.', 403)
    data = json_body() or {}
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    if _billing_mode(visible) not in {'us_full', 'us_partial'}:
        return json_error('Partial invoicing is only for US customers.', 400)
    if visible.get('shipments'):
        return json_error('Partial invoicing has to be chosen before the first shipment.', 400)

    def mutator(order):
        updated = dict(order)
        updated['partial_invoicing'] = bool(data.get('partial_invoicing'))
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error:
        return json_error(error, 400)
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
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary or {
        'igu_subtotal': 0, 'igu_total': 0, 'total': 0, 'discount_amount': 0,
        'discount': {'type': 'percent', 'value': 0}, 'extra_charges': [], 'extras_total': 0,
    }, order.get('additional_discount_percent'))
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
        'additional_discount_percent': summary.get('additional_discount_percent') or 0,
        'additional_discount_amount': summary.get('additional_discount_amount') or 0,
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
        'issuer': 'us',
        'kind': 'customer',
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'created_by': user.get('email') or '',
    }


def _finish_invoice(stored, user):
    payments = load_payments()
    fresh = next((row for row in load_invoices() if row.get('id') == stored.get('id')), stored)
    fresh['status'] = invoice_status(fresh, payments)
    fresh['balance'] = invoice_balance(fresh, payments)
    invoices = load_invoices()
    for index, row in enumerate(invoices):
        if row.get('id') == fresh['id']:
            invoices[index] = fresh
    save_invoices(invoices)
    return fresh


def _canada_books_customer():
    customers = load_customers()
    for customer_id, customer in customers.items():
        if customer.get('books_entity') == 'ca':
            return customer_id
    customer_id = 'ora-glass-canada'
    if customer_id in customers:
        customer_id = new_customer_id('Ora Glass Canada', customers)
    customers[customer_id] = {
        'id': customer_id,
        'name': 'Ora Glass (Canada) Inc',
        'books_entity': 'ca',
        'domain': '',
        'address': '',
        'city': '',
        'state': '',
        'zip': '',
        'country': 'Canada',
        'phone': '',
        'email': '',
        'credit_approved': True,
        'payment_terms': 'Net 30',
        'credit_currency': 'usd',
        'tier': 'T1',
        'status': 'active',
    }
    save_customers(customers)
    return customer_id


def _issue_delivery_invoices(order, user):
    import quickbooks
    customer = load_customers().get(order.get('customer_id') or '') or {}
    base = _invoice_from_order(order, user)
    if not quickbooks.is_canadian(customer):
        stored = append_invoice(base)
        _apply_prepayments_to_invoice(order.get('id'), stored['id'], user)
        fresh = _finish_invoice(stored, user)
        order['invoice_id'] = fresh['id']
        order['invoice_number'] = fresh['number']
        return fresh['number'], None
    currency = str(base.get('currency') or 'usd').lower()
    invoice_day = date.fromisoformat(base['invoice_date'])
    if currency == 'cad':
        rate, fx_date = _erp_fx(invoice_day)
        usd_total = quickbooks.cad_to_usd(base.get('total'), rate)
    else:
        rate, fx_date = 1.0, invoice_day.isoformat()
        usd_total = money(base.get('total'))
    customer_invoice = dict(base)
    customer_invoice['issuer'] = 'ca'
    customer_invoice['kind'] = 'customer'
    customer_invoice.update(_canada_tax_fields(customer, order.get('ship_province')))
    stored_customer = append_invoice(customer_invoice)
    _apply_prepayments_to_invoice(order.get('id'), stored_customer['id'], user)
    fresh_customer = _finish_invoice(stored_customer, user)
    books_id = _canada_books_customer()
    intercompany = dict(base)
    intercompany.update({
        'issuer': 'us',
        'kind': 'intercompany',
        'customer_id': books_id,
        'company': 'Ora Glass (Canada) Inc',
        'currency': 'usd',
        'items': [{
            'name': 'Intercompany glass',
            'description': order.get('project') or 'Intercompany glass',
            'pcs': 1,
            'qty': 1,
            'unit_price': usd_total,
            'price_pc': usd_total,
            'total': usd_total,
        }],
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'extra_charges': [],
        'igu_subtotal': usd_total,
        'igu_total': usd_total,
        'extras_total': 0,
        'total': usd_total,
        'balance': usd_total,
        'fx_rate': rate,
        'fx_date': fx_date,
        'fx_source': 'erp',
        'fx_pair': 'FXUSDCAD',
        'cad_total': money(base.get('total')) if currency == 'cad' else None,
    })
    fresh_intercompany = _finish_invoice(append_invoice(intercompany), user)
    order['invoice_id'] = fresh_customer['id']
    order['invoice_number'] = fresh_customer['number']
    order['intercompany_invoice_id'] = fresh_intercompany['id']
    order['intercompany_invoice_number'] = fresh_intercompany['number']
    if not order.get('fx_rate'):
        order['fx_rate'] = rate
    order['fx_date'] = fx_date
    return f"{fresh_customer['number']} and {fresh_intercompany['number']}", None


def _order_customer(order):
    return load_customers().get(order.get('customer_id') or '') or {}


def _billing_mode(order):
    import quickbooks
    return shipments.billing_mode(order, _order_customer(order), quickbooks.is_canadian(_order_customer(order)))


def _public_shipments(order):
    rows = []
    for shipment in order.get('shipments') or []:
        rows.append({
            'id': shipment.get('id'),
            'number': shipment.get('number') or '',
            'date': shipment.get('date') or '',
            'rack': shipment.get('rack') or '',
            'weight': shipment.get('weight') or '',
            'pcs': shipment.get('pcs') or 0,
            'sqf': shipment.get('sqf') or 0,
            'total': shipment.get('total') or 0,
            'invoiced': bool(shipment.get('invoiced')),
            'invoice_id': shipment.get('invoice_id'),
            'invoice_number': shipment.get('invoice_number') or '',
            'packing_list': shipment.get('packing_list') or '',
        })
    return rows


def _scaled_discount(order, portion, full):
    discount = order.get('discount') or {'type': 'percent', 'value': 0}
    if discount.get('type') != 'amount' or full <= 0:
        return discount
    share = round(money(discount.get('value')) * portion / full, 2)
    return {'type': 'amount', 'value': share}


def _invoice_for_items(order, user, customer, items, summary, issuer, kind, shipment):
    days = payment_term_days(customer)
    invoice_day = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    due = invoice_day + timedelta(days=days)
    document = {
        'order_id': order.get('id'),
        'quote_id': order.get('quote_id'),
        'customer_id': customer.get('id') or order.get('customer_id'),
        'company': customer.get('name') or order.get('company') or '',
        'project': order.get('project') or '',
        'shipment_number': shipment.get('number') or '',
        'shipment_id': shipment.get('id'),
        'currency': order.get('currency') or 'usd',
        'items': items,
        'discount': summary.get('discount') or {'type': 'percent', 'value': 0},
        'discount_amount': summary.get('discount_amount', 0),
        'additional_discount_percent': summary.get('additional_discount_percent') or 0,
        'additional_discount_amount': summary.get('additional_discount_amount') or 0,
        'extra_charges': summary.get('extra_charges') or [],
        'igu_subtotal': summary.get('igu_subtotal', 0),
        'igu_total': summary.get('igu_total', 0),
        'extras_total': summary.get('extras_total', 0),
        'total': summary.get('total', 0),
        'invoice_date': invoice_day.isoformat(),
        'due_date': due.isoformat(),
        'payment_terms': customer.get('payment_terms') or 'Net 30',
        'payment_terms_days': days,
        'status': 'unpaid',
        'balance': summary.get('total', 0),
        'issuer': issuer,
        'kind': kind,
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'created_by': user.get('email') or '',
    }
    if issuer == 'ca':
        document.update(_canada_tax_fields(customer, order.get('ship_province')))
    return document


def _issue_shipment_invoice(order, shipment, user, include_extras):
    """Invoice this shipment once. Returns the invoice number."""
    import quickbooks
    customer = _order_customer(order)
    mode = _billing_mode(order)
    items = json.loads(json.dumps(shipment.get('items') or []))
    full_value = shipments.shipment_progress(order)['ordered_value'] or money(order.get('total'))
    charges = order.get('extra_charges') or [] if include_extras else []
    summary, error = price_summary(
        items,
        _scaled_discount(order, money(shipment.get('total')), full_value),
        charges,
    )
    if error:
        return None, error
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
    if mode == 'canada':
        invoice_day = datetime.now(ZoneInfo('America/Los_Angeles')).date()
        currency = str(order.get('currency') or 'usd').lower()
        if currency == 'cad':
            rate, fx_date = _erp_fx(invoice_day)
            usd_total = quickbooks.cad_to_usd(summary.get('total'), rate)
        else:
            rate, fx_date = 1.0, invoice_day.isoformat()
            usd_total = money(summary.get('total'))
        books = load_customers().get(_canada_books_customer()) or {}
        books['id'] = _canada_books_customer()
        intercompany_items = [{
            'name': 'Intercompany glass',
            'description': f"Shipment {shipment.get('number') or ''}".strip(),
            'pcs': shipment.get('pcs') or 1,
            'qty': shipment.get('pcs') or 1,
            'unit_price': usd_total,
            'price_pc': usd_total,
            'total': usd_total,
        }]
        document = _invoice_for_items(
            order, user, books, intercompany_items,
            {'discount': {'type': 'percent', 'value': 0}, 'discount_amount': 0, 'extra_charges': [],
             'igu_subtotal': usd_total, 'igu_total': usd_total, 'extras_total': 0, 'total': usd_total},
            'us', 'intercompany', shipment,
        )
        document.update({
            'currency': 'usd',
            'fx_rate': rate,
            'fx_date': fx_date,
            'fx_source': 'erp',
            'fx_pair': 'FXUSDCAD',
            'cad_total': money(summary.get('total')) if currency == 'cad' else None,
            'company': 'Ora Glass (Canada) Inc',
            'customer_id': books.get('id'),
        })
        stored = _finish_invoice(append_invoice(document), user)
        shipment['invoice_id'] = stored['id']
        shipment['invoice_number'] = stored['number']
        shipment['invoiced'] = True
        _qbo('push_document_id', stored['id'])
        return stored['number'], None
    document = _invoice_for_items(order, user, customer, items, summary, 'us', 'customer', shipment)
    stored = _finish_invoice(append_invoice(document), user)
    _apply_prepayments_to_invoice(order.get('id'), stored['id'], user)
    shipment['invoice_id'] = stored['id']
    shipment['invoice_number'] = stored['number']
    shipment['invoiced'] = True
    if not order.get('invoice_id'):
        order['invoice_id'] = stored['id']
        order['invoice_number'] = stored['number']
    _qbo('push_document_id', stored['id'])
    return stored['number'], None


def _issue_final_customer_invoice(order, user):
    """One invoice for the whole order. Used for US orders when shipping is complete, and for Canada."""
    import quickbooks
    if any(
        row.get('order_id') == order.get('id') and row.get('kind') != 'intercompany' and not row.get('voided')
        and (row.get('issuer') or 'us') == ('ca' if quickbooks.is_canadian(_order_customer(order)) else 'us')
        for row in load_invoices()
    ):
        return None, None
    customer = _order_customer(order)
    if quickbooks.is_canadian(customer):
        base = _invoice_from_order(order, user)
        base['issuer'] = 'ca'
        base['kind'] = 'customer'
        base.update(_canada_tax_fields(customer, order.get('ship_province')))
        stored = append_invoice(base)
        _apply_prepayments_to_invoice(order.get('id'), stored['id'], user)
        fresh = _finish_invoice(stored, user)
        order['customer_invoice_id'] = fresh['id']
        order['customer_invoice_number'] = fresh['number']
        _qbo('push_document_id', fresh['id'])
        return fresh['number'], None
    number, error = _issue_delivery_invoices(order, user)
    if not error and order.get('invoice_id'):
        _qbo('push_document_id', order.get('invoice_id'))
    return number, error


def _close_shipment_billing(order, user):
    progress = shipments.shipment_progress(order)
    mode = _billing_mode(order)
    if not progress['fully_shipped']:
        return None
    if mode == 'us_full':
        _number, error = _issue_final_customer_invoice(order, user)
        if error:
            return error
        for shipment in order.get('shipments') or []:
            shipment['invoiced'] = True
        return None
    if mode == 'canada':
        _number, error = _issue_final_customer_invoice(order, user)
        return error
    return None


def _invoices_visible(user):
    rows = load_invoices()
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        return [row for row in rows if row.get('customer_id') == customer_id]
    return rows


def _present_invoice(invoice, payments):
    view = dict(invoice)
    view.pop('price_addon_percent', None)
    view['doc_type'] = document_type(invoice)
    view['issuer'] = invoice.get('issuer') or 'us'
    view['issuer_label'] = 'Ora Glass (Canada) Inc' if view['issuer'] == 'ca' else 'Ora Glass Inc'
    view['kind'] = invoice.get('kind') or 'customer'
    if view['doc_type'] == 'credit_note':
        view['type_label'] = 'Credit note'
    elif view['kind'] == 'intercompany':
        view['type_label'] = 'Intercompany invoice'
    else:
        view['type_label'] = 'Invoice'
    view['audit'] = list(invoice.get('audit') or [])
    if view['doc_type'] == 'credit_note':
        applied = credit_note_applied(invoice)
        view['applied'] = applied
        view['unapplied'] = 0.0 if invoice.get('voided') else round(max(0.0, money(invoice.get('total')) - applied), 2)
        view['balance'] = view['unapplied']
        if invoice.get('voided'):
            view['status'] = 'void'
        elif view['unapplied'] <= 0.009:
            view['status'] = 'applied'
        elif applied > 0.009:
            view['status'] = 'partial'
        else:
            view['status'] = 'open'
        view['status_label'] = invoice_status_label(view['status'])
        return view
    view['applied'] = round(money(invoice.get('total')) - invoice_balance(invoice, payments), 2)
    view['balance'] = invoice_balance(invoice, payments)
    view['status'] = 'void' if invoice.get('voided') else invoice_status(invoice, payments)
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
    doc_type = clean_text(request.args.get('type'), 20).lower()
    date_from = _query_date('from')
    date_to = _query_date('to')
    filtered = []
    for row in rows:
        if user.get('role') != 'customer' and customer_id and row.get('customer_id') != customer_id:
            continue
        if doc_type in {'invoice', 'credit_note'} and row.get('doc_type') != doc_type:
            continue
        if status in {'unpaid', 'partial', 'paid', 'void', 'open', 'applied'} and row.get('status') != status:
            continue
        day = str(row.get('invoice_date') or '')
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: row.get('id') or 0, reverse=True)
    return jsonify(filtered)


def _audit(user, action, detail):
    return {
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'by': user.get('email') or '',
        'action': action,
        'detail': detail or '',
    }


def _manual_lines(data):
    raw = data.get('lines')
    if not isinstance(raw, list) or not raw:
        return None, 'Add at least one line.'
    lines = []
    for row in raw:
        if not isinstance(row, dict):
            return None, 'Each line needs a description, quantity, and unit price.'
        description = clean_text(row.get('description'), 200)
        if not description:
            return None, 'Each line needs a description.'
        try:
            qty = money(row.get('qty'))
            price = money(row.get('unit_price'))
        except (TypeError, ValueError):
            return None, 'Quantity and unit price must be numbers.'
        if qty <= 0:
            return None, 'Quantity must be greater than zero.'
        if price < 0:
            return None, 'Unit price cannot be negative.'
        total = round(qty * price, 2)
        lines.append({
            'description': description,
            'qty': qty,
            'unit_price': price,
            'total': total,
            'name': description,
            'pcs': qty,
            'price_pc': price,
        })
    return lines, None


@app.route('/api/invoices', methods=['POST'])
@module_required('invoices')
def create_document():
    user = current_user()
    if not can_issue_invoices(user):
        return json_error('You do not have permission to issue invoices.', 403)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    doc_type = clean_text(data.get('doc_type') or 'invoice', 20)
    if doc_type not in {'invoice', 'credit_note'}:
        return json_error('Type must be an invoice or a credit note.', 400)
    lines, error = _manual_lines(data)
    if error:
        return json_error(error, 400)
    orders = load_orders()
    invoices = load_invoices()
    customers = load_customers()
    order = None
    linked_invoice = None
    if data.get('order_id') not in (None, ''):
        try:
            order_id = int(data.get('order_id'))
        except (TypeError, ValueError):
            return json_error('Unknown order.', 400)
        order = next((row for row in orders if row.get('id') == order_id), None)
        if not order:
            return json_error('Unknown order.', 400)
        customer_id = order.get('customer_id')
        currency = str(order.get('currency') or 'usd').lower()
    else:
        customer_id = clean_text(data.get('customer_id'), 80)
        currency = None
    if data.get('invoice_id') not in (None, '') and doc_type == 'credit_note':
        try:
            linked_id = int(data.get('invoice_id'))
        except (TypeError, ValueError):
            return json_error('Unknown invoice.', 400)
        linked_invoice = next((row for row in invoices if row.get('id') == linked_id and document_type(row) == 'invoice'), None)
        if not linked_invoice or linked_invoice.get('voided'):
            return json_error('Unknown invoice.', 400)
        if order and linked_invoice.get('customer_id') != order.get('customer_id'):
            return json_error('That invoice belongs to a different customer.', 400)
        customer_id = linked_invoice.get('customer_id')
        currency = str(linked_invoice.get('currency') or 'usd').lower()
    customer = customers.get(customer_id or '')
    if not customer:
        return json_error('Choose a customer.', 400)
    import quickbooks
    if linked_invoice:
        issuer = linked_invoice.get('issuer') or 'us'
    elif quickbooks.is_canadian(customer):
        issuer = 'ca'
    else:
        issuer = 'us'
    if not currency:
        currency = _shown_credit_currency(customer)
    if issuer == 'ca' and quickbooks.is_canadian(customer) and data.get('order_id') in (None, ''):
        currency = _required_currency(customer)
    if currency not in CURRENCIES:
        return json_error('Currency must be USD or CAD.', 400)
    reason = clean_text(data.get('reason'), 300)
    if doc_type == 'credit_note' and not reason:
        return json_error('A credit note needs a reason.', 400)
    total = round(sum(line['total'] for line in lines), 2)
    if total <= 0:
        return json_error('The total must be greater than zero.', 400)
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
    days = payment_term_days(customer)
    due_text = clean_text(data.get('due_date'), 10)
    if due_text:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', due_text):
            return json_error('Due date is not a date.', 400)
        due = due_text
    else:
        due = (today + timedelta(days=days)).isoformat()
    document = {
        'doc_type': doc_type,
        'source': 'manual',
        'order_id': order.get('id') if order else (linked_invoice.get('order_id') if linked_invoice else None),
        'related_invoice_id': linked_invoice.get('id') if linked_invoice else None,
        'related_invoice_number': linked_invoice.get('number') if linked_invoice else '',
        'quote_id': order.get('quote_id') if order else None,
        'customer_id': customer_id,
        'company': customer.get('name') or (order.get('company') if order else ''),
        'project': (order.get('project') if order else '') or clean_text(data.get('project'), 200),
        'currency': currency,
        'lines': lines,
        'items': lines,
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'extra_charges': [],
        'igu_subtotal': total,
        'igu_total': total,
        'extras_total': 0,
        'subtotal': total,
        'tax_free': True,
        'total': total,
        'reason': reason,
        'applications': [],
        'invoice_date': today.isoformat(),
        'due_date': due if doc_type == 'invoice' else '',
        'payment_terms': customer.get('payment_terms') or 'Net 30',
        'payment_terms_days': days,
        'status': 'unpaid' if doc_type == 'invoice' else 'open',
        'balance': total,
        'voided': False,
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'created_by': user.get('email') or '',
        'issuer': issuer,
        'kind': 'customer',
    }
    if issuer == 'ca' and doc_type == 'invoice':
        ship = data.get('ship_province') or (order.get('ship_province') if order else None)
        document.update(_canada_tax_fields(customer, ship))
    document['audit'] = [_audit(user, 'created', document['doc_type'])]
    stored = append_invoice(document)
    _qbo('push_document', stored)
    return jsonify(_present_invoice(stored, load_payments()))


@app.route('/api/invoices/<int:invoice_id>/exchange-rate', methods=['POST'])
@module_required('invoices')
def override_exchange_rate(invoice_id):
    import quickbooks
    user = current_user()
    if not can_issue_invoices(user):
        return json_error('You do not have permission to issue invoices.', 403)
    data = json_body() or {}
    try:
        rate = float(data.get('rate'))
    except (TypeError, ValueError):
        return json_error('Enter an exchange rate.', 400)
    if rate <= 0:
        return json_error('Exchange rate must be greater than zero.', 400)
    invoices = load_invoices()
    invoice = next((row for row in invoices if row.get('id') == invoice_id), None)
    if not invoice or invoice.get('kind') != 'intercompany':
        return json_error('Only an intercompany invoice has an exchange rate.', 400)
    if invoice.get('cad_total') in (None, ''):
        return json_error('This invoice has no CAD amount to convert.', 400)
    usd_total = quickbooks.cad_to_usd(invoice.get('cad_total'), rate)
    invoice['fx_rate'] = rate
    invoice['fx_override'] = True
    invoice['total'] = usd_total
    invoice['balance'] = usd_total
    invoice['igu_subtotal'] = usd_total
    invoice['igu_total'] = usd_total
    for line in invoice.get('items') or []:
        line['unit_price'] = usd_total
        line['price_pc'] = usd_total
        line['total'] = usd_total
    invoice.setdefault('audit', []).append(_audit(user, 'exchange-rate', f'FXUSDCAD {rate}'))
    save_invoices(invoices)
    return jsonify(_present_invoice(invoice, load_payments()))


@app.route('/api/invoices/<int:note_id>/apply', methods=['POST'])
@module_required('invoices')
def apply_credit_note(note_id):
    user = current_user()
    if not can_issue_invoices(user):
        return json_error('You do not have permission to issue invoices.', 403)
    data = json_body() or {}
    invoices = load_invoices()
    note = next((row for row in invoices if row.get('id') == note_id), None)
    if not note or document_type(note) != 'credit_note':
        return json_error('Not found', 404)
    if note.get('voided'):
        return json_error('This credit note is void.', 400)
    try:
        invoice_id = int(data.get('invoice_id'))
        amount = money(data.get('amount'))
    except (TypeError, ValueError):
        return json_error('Choose an invoice and an amount.', 400)
    if amount <= 0:
        return json_error('Amount must be greater than zero.', 400)
    invoice = next((row for row in invoices if row.get('id') == invoice_id and document_type(row) == 'invoice'), None)
    if not invoice or invoice.get('voided'):
        return json_error('Unknown invoice.', 400)
    if invoice.get('customer_id') != note.get('customer_id'):
        return json_error('That invoice belongs to a different customer.', 400)
    if str(invoice.get('currency') or 'usd').lower() != str(note.get('currency') or 'usd').lower():
        return json_error('That invoice uses a different currency.', 400)
    remaining = round(money(note.get('total')) - credit_note_applied(note), 2)
    if amount - remaining > 0.009:
        return json_error('That is more than the unapplied credit.', 400)
    payments = load_payments()
    balance = invoice_balance(invoice, payments)
    if amount - balance > 0.009:
        return json_error('That is more than the invoice balance.', 400)
    payment = append_payment({
        'customer_id': note.get('customer_id'),
        'amount': amount,
        'currency': note.get('currency'),
        'method': 'credit_note',
        'reference': note.get('number') or '',
        'credit_note_id': note.get('id'),
        'date': datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat(),
        'notes': f"Applied {note.get('number') or 'credit note'}",
        'allocations': [{'target_type': 'invoice', 'target_id': invoice_id, 'amount': amount}],
        'voided': False,
        'created_by': user.get('email') or '',
    })
    applications = list(note.get('applications') or [])
    applications.append({
        'invoice_id': invoice_id,
        'invoice_number': invoice.get('number') or '',
        'amount': amount,
        'payment_id': payment.get('id'),
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'by': user.get('email') or '',
    })
    note['applications'] = applications
    audit = list(note.get('audit') or [])
    audit.append(_audit(user, 'applied', f"{invoice.get('number') or invoice_id} ${amount:.2f}"))
    note['audit'] = audit
    for index, row in enumerate(invoices):
        if row.get('id') == note.get('id'):
            invoices[index] = note
    save_invoices(invoices)
    _refresh_invoice_statuses(load_payments())
    fresh = next(row for row in load_invoices() if row.get('id') == note.get('id'))
    _qbo('push_payment', payment)
    return jsonify(_present_invoice(fresh, load_payments()))


@app.route('/api/invoices/<int:invoice_id>/void', methods=['POST'])
@module_required('invoices')
def void_document(invoice_id):
    user = current_user()
    if not can_issue_invoices(user):
        return json_error('You do not have permission to issue invoices.', 403)
    data = json_body() or {}
    reason = clean_text(data.get('reason'), 300)
    if not reason:
        return json_error('A void reason is required.', 400)
    invoices = load_invoices()
    document = next((row for row in invoices if row.get('id') == invoice_id), None)
    if not document:
        return json_error('Not found', 404)
    if document.get('voided'):
        return json_error('This document is already void.', 400)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    document['voided'] = True
    document['void_reason'] = reason
    document['voided_by'] = user.get('email') or ''
    document['voided_at'] = now
    audit = list(document.get('audit') or [])
    audit.append(_audit(user, 'voided', reason))
    document['audit'] = audit
    payments = load_payments()
    if document_type(document) == 'credit_note':
        for payment in payments:
            if payment.get('credit_note_id') == document.get('id') and not payment.get('voided'):
                payment['voided'] = True
                payment['void_reason'] = reason
                payment['voided_by'] = user.get('email') or ''
                payment['voided_at'] = now
        for application in document.get('applications') or []:
            application['voided'] = True
    else:
        for note in invoices:
            if document_type(note) != 'credit_note':
                continue
            for application in note.get('applications') or []:
                if application.get('invoice_id') == document.get('id') and not application.get('voided'):
                    application['voided'] = True
                    payment_id = application.get('payment_id')
                    payment = next((row for row in payments if row.get('id') == payment_id), None)
                    if payment and not payment.get('voided'):
                        payment['voided'] = True
                        payment['void_reason'] = reason
                        payment['voided_at'] = now
    for index, row in enumerate(invoices):
        if row.get('id') == document.get('id'):
            invoices[index] = document
    save_invoices(invoices)
    save_payments(payments)
    _refresh_invoice_statuses(payments)
    fresh = next(row for row in load_invoices() if row.get('id') == invoice_id)
    _qbo('void_document', fresh)
    return jsonify(_present_invoice(fresh, load_payments()))


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
        Paragraph(
            f"<b>{'Credit Note' if document_type(invoice) == 'credit_note' else 'Invoice'}</b>"
            '<br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>',
            title_style,
        ),
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
    if invoice.get('tax_label'):
        elements.append(Paragraph(f"<b>Tax:</b> {escape(invoice.get('tax_label'))}", label_style))
    if document_type(invoice) == 'credit_note' and invoice.get('reason'):
        elements.append(Paragraph(f"<b>Reason:</b> {escape(invoice.get('reason') or '')}", label_style))
    if invoice.get('tax_free'):
        elements.append(Paragraph('Tax-free subtotal. No tax is added on this document.', label_style))
    elements.append(Spacer(1, 10))
    table_data = [[
        Paragraph('<b>Item</b>', header_cell),
        Paragraph('<b>Qty</b>', header_cell),
        Paragraph(f'<b>{currency}</b>', header_cell),
    ]]
    for item in invoice.get('items') or []:
        description = _item_description(item)
        table_data.append([
            Paragraph(description, cell_style),
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
    subtotal_label = 'Tax-free subtotal' if invoice.get('tax_free') else 'IGU subtotal'
    summary_rows = [
        [subtotal_label, f"${money(invoice.get('subtotal') or invoice.get('igu_subtotal')):.2f}"],
        [f"Discount ({discount_label})", f"-${money(invoice.get('discount_amount')):.2f}"],
    ]
    extra_discount = money(invoice.get('additional_discount_amount'))
    if extra_discount:
        extra_percent = _percent(invoice.get('additional_discount_percent'))
        summary_rows.append([
            f"Additional discount ({extra_percent:g}%)",
            f"-${extra_discount:.2f}",
        ])
    summary_rows.append(
        ['Total before extras' if invoice.get('tax_free') else 'IGU total', f"${money(invoice.get('igu_total')):.2f}"],
    )
    for charge in invoice.get('extra_charges') or []:
        summary_rows.append([charge.get('label') or 'Extra', f"${money(charge.get('amount')):.2f}"])
    summary_rows.append(['Grand total', f"${money(invoice.get('total')):.2f}"])
    if document_type(invoice) == 'credit_note':
        summary_rows.append(['Applied to invoices', f"${money(invoice.get('applied')):.2f}"])
        summary_rows.append(['Unapplied credit', f"${money(invoice.get('balance')):.2f}"])
    else:
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
    _qbo('push_payment', stored)
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
    _qbo('push_payment', payment)
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
    _qbo('void_payment', payment)
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
        'unapplied_credits': unapplied_credits(invoices, customer_id, currency),
        'balance': round(receivables - unapplied_credits(invoices, customer_id, currency), 2),
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
    locked = _payroll_lock(key, moment)
    if locked:
        return locked
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
    current = load_time_settings()
    current['photo_retention_days'] = days
    save_time_settings(current)
    return jsonify(load_time_settings())


@app.route('/time-clock')
@login_required
def time_clock_admin():
    if not can_manage_time(current_user()):
        return forbidden()
    return redirect(url_for('payroll_page') + '#time-kiosk')


@app.route('/kiosk/setup')
@login_required
def kiosk_setup():
    if not can_manage_time(current_user()):
        return forbidden()
    return render_template('kiosk_setup.html')


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
    locked = _payroll_lock(_punch_owner(user), moment)
    if locked:
        return locked
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
    attendance = _attendance_rows(email, start, end, names)
    return jsonify({
        'employees': employees,
        'punches': punch_rows,
        'days': days,
        'attendance': attendance,
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
    existing = next((row for row in load_punches() if row.get('id') == punch_id), None)
    if not existing:
        return json_error('Not found', 404)
    moments = [existing.get('at')]
    if data.get('at_pacific'):
        moments.append(data.get('at_pacific'))
    locked = _payroll_lock(existing.get('user'), *moments)
    if locked:
        return locked

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


def _as_payroll_day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or '')
    naive = 'Z' not in text and '+' not in text[10:]
    try:
        return pacific_date(as_utc(value, naive_is_pacific=naive))
    except (TypeError, ValueError):
        return None


def _payroll_lock(worker, *moments):
    """Approved gross-pay periods stay fixed until someone reopens them."""
    if not worker:
        return None
    stubs = load_paystubs()
    for moment in moments:
        day = _as_payroll_day(moment)
        if not day:
            continue
        for stub in stubs:
            if stub.get('worker') != worker or stub.get('status') != 'approved':
                continue
            try:
                start = date.fromisoformat(stub.get('period_start'))
                end = date.fromisoformat(stub.get('period_end'))
            except (TypeError, ValueError):
                continue
            if start <= day <= end:
                return json_error('This pay period is approved. Reopen it before changing hours.', 409)
    return None


def _payroll_workers():
    rows = []
    for key, record in load_users().items():
        role = record.get('role')
        if role == 'shop' or (role in LEGACY_STAFF_ROLES and uses_time_clock(record)):
            rows.append({
                'account': key,
                'name': record.get('name') or key,
                'employee_number': record.get('employee_number') or '',
                'role': 'employee' if role in LEGACY_STAFF_ROLES else role,
            })
    rows.sort(key=lambda row: (row['name'] or '').lower())
    return rows


def _payroll_audit(actor, action, target, before=None, after=None, reason=''):
    append_payroll_audit({
        'at': stamp(datetime.now(timezone.utc)),
        'by': (actor or {}).get('email') or (actor or {}).get('account') or '',
        'action': action,
        'target': target,
        'before': before,
        'after': after,
        'reason': reason or '',
    })


def _stored_stub(worker, period_start):
    key = period_start.isoformat() if isinstance(period_start, date) else str(period_start)
    for stub in load_paystubs():
        if stub.get('worker') == worker and stub.get('period_start') == key:
            return stub
    return None


def _prior_ytd(worker, period_start):
    from decimal import Decimal
    total = Decimal('0')
    for stub in load_paystubs():
        if stub.get('worker') != worker or stub.get('status') != 'approved':
            continue
        try:
            start = date.fromisoformat(stub.get('period_start'))
        except (TypeError, ValueError):
            continue
        if start.year != period_start.year or start >= period_start:
            continue
        gross = (stub.get('snapshot') or {}).get('gross_pay')
        if gross not in (None, ''):
            total += Decimal(str(gross))
    return total


def _attendance_rows(user_filter, start, end, names):
    settings = normalize_payroll_settings(load_time_settings())
    shifts = {row.get('id'): row for row in load_shifts()}
    assignments = load_shift_assignments()
    punches = load_punches()
    if user_filter:
        keys = [user_filter]
    else:
        keys = [row['account'] for row in _payroll_workers()]
        for punch in punches:
            if punch.get('user') and punch.get('user') not in keys:
                keys.append(punch.get('user'))
    rows = []
    for key in keys:
        owned = [row for row in punches if row.get('user') == key]
        row_start, row_end = start, end
        if row_start is None or row_end is None:
            dates = []
            for punch in owned:
                day = _as_payroll_day(punch.get('at'))
                if day:
                    dates.append(day)
            if not dates:
                continue
            row_start = row_start or min(dates)
            row_end = row_end or max(dates)
        for item in attendance_range(owned, assignments.get(key) or [], shifts, settings, row_start, row_end):
            if item['scheduled_hours'] == '0.00' and item['actual_hours'] == '0.00' and not item['flags']:
                continue
            item = dict(item)
            item['user'] = key
            item['name'] = names.get(key, key)
            rows.append(item)
    rows.sort(key=lambda row: (row['date'], row.get('name') or ''))
    return rows


def _worker_stub(worker_key, period_start, period_end):
    settings = normalize_payroll_settings(load_time_settings())
    first, last = weeks_covering(period_start, period_end, settings['work_week_start'])
    punches = [row for row in load_punches() if row.get('user') == worker_key]
    shifts = {row.get('id'): row for row in load_shifts()}
    days = attendance_range(
        punches,
        load_shift_assignments().get(worker_key) or [],
        shifts,
        settings,
        first,
        last,
    )
    stub = build_stub(
        worker_key,
        period_start,
        period_end,
        days,
        load_pay_rates().get(worker_key) or [],
        load_pay_adjustments(),
        settings,
        _prior_ytd(worker_key, period_start),
    )
    record = load_users().get(worker_key) or {}
    stub['name'] = record.get('name') or worker_key
    stub['employee_number'] = record.get('employee_number') or ''
    stored = _stored_stub(worker_key, period_start)
    if stored and stored.get('status') == 'approved' and stored.get('snapshot'):
        frozen = dict(stored['snapshot'])
        frozen['status'] = 'approved'
        frozen['approved_at'] = stored.get('approved_at')
        frozen['approved_by'] = stored.get('approved_by')
        return frozen
    stub['status'] = (stored or {}).get('status') or 'draft'
    return stub


def _require_payroll():
    actor = current_user()
    if not can_manage_payroll(actor):
        return None, json_error('Access denied', 403)
    return actor, None


def _period_from_request():
    settings = normalize_payroll_settings(load_time_settings())
    raw = (request.args.get('start') or (json_body() or {}).get('period_start') or '').strip()
    if raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return None, None, json_error('Period start is not a date.', 400)
    else:
        day = pacific_date(datetime.now(timezone.utc))
    start, end = period_containing(day, settings)
    return (start, end), settings, None


def _money_ok(value):
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    if amount <= 0 or amount > Decimal('100000'):
        return None
    return q_money(amount)


@app.route('/payroll')
@login_required
def payroll_page():
    user = current_user()
    if not can_manage_payroll(user) and not can_manage_time(user):
        return forbidden()
    return render_template(
        'payroll.html',
        can_payroll=can_manage_payroll(user),
        can_time=can_manage_time(user),
    )


@app.route('/api/payroll/period', methods=['GET'])
@login_required
def payroll_period():
    _actor, denied = _require_payroll()
    if denied:
        return denied
    period, settings, error = _period_from_request()
    if error:
        return error
    start, end = period
    stubs = [_worker_stub(row['account'], start, end) for row in _payroll_workers()]
    from decimal import Decimal
    gross = sum(
        (Decimal(stub['gross_pay']) for stub in stubs if stub.get('gross_pay') not in (None, '')),
        Decimal('0'),
    )
    return jsonify({
        'settings': settings,
        'period_start': start.isoformat(),
        'period_end': end.isoformat(),
        'label': GROSS_LABEL,
        'workers': _payroll_workers(),
        'shifts': load_shifts(),
        'assignments': load_shift_assignments(),
        'rates': load_pay_rates(),
        'stubs': stubs,
        'gross_total': f'{gross:.2f}',
        'weekdays': list(WEEKDAY_NAMES),
    })


@app.route('/api/payroll/settings', methods=['PUT'])
@login_required
def save_payroll_settings():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    settings, error = validate_payroll_settings(data)
    if error:
        return json_error(error, 400)
    current = load_time_settings()
    before = normalize_payroll_settings(current)
    current.update(settings)
    save_time_settings(current)
    _payroll_audit(actor, 'settings', 'payroll', before, settings)
    return jsonify(normalize_payroll_settings(load_time_settings()))


@app.route('/api/payroll/shifts', methods=['POST'])
@login_required
def create_shift():
    actor, denied = _require_payroll()
    if denied:
        return denied
    shift, error = _shift_from_body(json_body() or {})
    if error:
        return json_error(error, 400)
    shifts = load_shifts()
    shift['id'] = max([row.get('id') or 0 for row in shifts] or [0]) + 1
    shift['active'] = True
    shifts.append(shift)
    save_shifts(shifts)
    _payroll_audit(actor, 'shift', str(shift['id']), None, shift)
    return jsonify(shift)


@app.route('/api/payroll/shifts/<int:shift_id>', methods=['PUT'])
@login_required
def update_shift(shift_id):
    actor, denied = _require_payroll()
    if denied:
        return denied
    shifts = load_shifts()
    current = next((row for row in shifts if row.get('id') == shift_id), None)
    if not current:
        return json_error('Not found', 404)
    incoming, error = _shift_from_body(json_body() or {}, current)
    if error:
        return json_error(error, 400)
    before = dict(current)
    current.update(incoming)
    save_shifts(shifts)
    _payroll_audit(actor, 'shift', str(shift_id), before, current)
    return jsonify(current)


def _shift_from_body(data, current=None):
    name = clean_text(data.get('name') if 'name' in data else (current or {}).get('name'), 80)
    start = str(data.get('start') if 'start' in data else (current or {}).get('start') or '').strip()
    end = str(data.get('end') if 'end' in data else (current or {}).get('end') or '').strip()
    if not name:
        return None, 'Shift name is required.'
    try:
        from payroll import shift_bounds
        shift_bounds(date(2026, 1, 5), {'start': start, 'end': end, 'unpaid_break_minutes': 0})
    except (TypeError, ValueError):
        return None, 'Shift times must look like 07:00 and 15:30.'
    weekdays = data.get('weekdays') if 'weekdays' in data else (current or {}).get('weekdays')
    if not isinstance(weekdays, list) or not weekdays:
        return None, 'Choose at least one weekday.'
    cleaned = []
    for item in weekdays:
        try:
            day = int(item)
        except (TypeError, ValueError):
            return None, 'Weekdays are not valid.'
        if day not in range(7) or day in cleaned:
            continue
        cleaned.append(day)
    if not cleaned:
        return None, 'Choose at least one weekday.'
    raw_break = data.get('unpaid_break_minutes') if 'unpaid_break_minutes' in data else (current or {}).get('unpaid_break_minutes')
    try:
        unpaid = int(raw_break or 0)
    except (TypeError, ValueError):
        return None, 'Unpaid break must be a number of minutes.'
    if unpaid < 0 or unpaid > 240:
        return None, 'Unpaid break must be between 0 and 240 minutes.'
    return {
        'name': name,
        'start': start,
        'end': end,
        'weekdays': cleaned,
        'unpaid_break_minutes': unpaid,
        'active': True if current is None else current.get('active', True),
    }, None


@app.route('/api/payroll/assignments', methods=['POST'])
@login_required
def assign_shift():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    worker = str(data.get('worker') or '').strip()
    if worker not in load_users() or worker not in {row['account'] for row in _payroll_workers()}:
        return json_error('Choose a shop worker or an employee who uses the time clock.', 400)
    try:
        effective = date.fromisoformat(str(data.get('effective_from') or ''))
    except ValueError:
        return json_error('Effective date is required.', 400)
    shift_id = data.get('shift_id')
    if shift_id in ('', None):
        shift_id = None
    else:
        try:
            shift_id = int(shift_id)
        except (TypeError, ValueError):
            return json_error('Unknown shift.', 400)
        if not any(row.get('id') == shift_id for row in load_shifts()):
            return json_error('Unknown shift.', 400)
    assignments = load_shift_assignments()
    history = list(assignments.get(worker) or [])
    before = next((dict(row) for row in history if row.get('effective_from') == effective.isoformat()), None)
    if before:
        for row in history:
            if row.get('effective_from') == effective.isoformat():
                row['shift_id'] = shift_id
                row['updated_at'] = stamp(datetime.now(timezone.utc))
                row['updated_by'] = actor.get('email') or ''
                saved = row
    else:
        saved = {
            'id': max([row.get('id') or 0 for rows in assignments.values() for row in rows] or [0]) + 1,
            'shift_id': shift_id,
            'effective_from': effective.isoformat(),
            'created_at': stamp(datetime.now(timezone.utc)),
            'created_by': actor.get('email') or '',
        }
        history.append(saved)
    assignments[worker] = history
    save_shift_assignments(assignments)
    _payroll_audit(actor, 'assignment', worker, before, saved)
    return jsonify(saved)


@app.route('/api/payroll/rates', methods=['POST'])
@login_required
def save_pay_rate():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    worker = str(data.get('worker') or '').strip()
    if worker not in {row['account'] for row in _payroll_workers()}:
        return json_error('Choose a shop worker or an employee who uses the time clock.', 400)
    amount = _money_ok(data.get('hourly_rate'))
    if not amount:
        return json_error('Hourly rate must be greater than zero.', 400)
    currency = str(data.get('currency') or 'USD').strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):
        return json_error('Currency must be a 3-letter code.', 400)
    try:
        effective = date.fromisoformat(str(data.get('effective_from') or ''))
    except ValueError:
        return json_error('Effective date is required.', 400)
    rates = load_pay_rates()
    history = list(rates.get(worker) or [])
    before = next((dict(row) for row in history if row.get('effective_from') == effective.isoformat()), None)
    if before:
        for row in history:
            if row.get('effective_from') == effective.isoformat():
                row['hourly_rate'] = amount
                row['currency'] = currency
                row['updated_at'] = stamp(datetime.now(timezone.utc))
                row['updated_by'] = actor.get('email') or ''
                saved = row
    else:
        saved = {
            'id': max([row.get('id') or 0 for rows in rates.values() for row in rows] or [0]) + 1,
            'hourly_rate': amount,
            'currency': currency,
            'effective_from': effective.isoformat(),
            'created_at': stamp(datetime.now(timezone.utc)),
            'created_by': actor.get('email') or '',
        }
        history.append(saved)
    rates[worker] = history
    save_pay_rates(rates)
    _payroll_audit(actor, 'pay_rate', worker, before, saved)
    return jsonify(saved)


@app.route('/api/payroll/adjustments', methods=['POST'])
@login_required
def add_pay_adjustment():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    worker = str(data.get('worker') or '').strip()
    if worker not in {row['account'] for row in _payroll_workers()}:
        return json_error('Choose a shop worker or an employee who uses the time clock.', 400)
    kind = str(data.get('kind') or '').strip()
    if kind not in {'bonus', 'reimbursement'}:
        return json_error('Adjustment must be a bonus or a reimbursement.', 400)
    amount = _money_ok(data.get('amount'))
    if not amount:
        return json_error('Amount must be greater than zero.', 400)
    reason = clean_text(data.get('reason'), 300)
    if not reason:
        return json_error('A reason is required.', 400)
    try:
        period_start = date.fromisoformat(str(data.get('period_start') or ''))
    except ValueError:
        return json_error('Period start is not a date.', 400)
    settings = normalize_payroll_settings(load_time_settings())
    start, _end = period_containing(period_start, settings)
    if start != period_start:
        return json_error('That date is not the start of a pay period.', 400)
    if _stored_stub(worker, period_start) and _stored_stub(worker, period_start).get('status') == 'approved':
        return json_error('This pay period is approved. Reopen it before changing pay.', 409)
    rows = load_pay_adjustments()
    saved = {
        'id': max([row.get('id') or 0 for row in rows] or [0]) + 1,
        'worker': worker,
        'period_start': period_start.isoformat(),
        'kind': kind,
        'amount': amount,
        'reason': reason,
        'created_at': stamp(datetime.now(timezone.utc)),
        'created_by': actor.get('email') or '',
    }
    rows.append(saved)
    save_pay_adjustments(rows)
    _payroll_audit(actor, 'adjustment', worker, None, saved, reason)
    return jsonify(saved)


@app.route('/api/payroll/adjustments/<int:adjustment_id>', methods=['DELETE'])
@login_required
def delete_pay_adjustment(adjustment_id):
    actor, denied = _require_payroll()
    if denied:
        return denied
    rows = load_pay_adjustments()
    found = next((row for row in rows if row.get('id') == adjustment_id), None)
    if not found:
        return json_error('Not found', 404)
    try:
        period_start = date.fromisoformat(found.get('period_start'))
    except (TypeError, ValueError):
        period_start = None
    if period_start and (_stored_stub(found.get('worker'), period_start) or {}).get('status') == 'approved':
        return json_error('This pay period is approved. Reopen it before changing pay.', 409)
    save_pay_adjustments([row for row in rows if row.get('id') != adjustment_id])
    _payroll_audit(actor, 'adjustment_delete', found.get('worker'), found, None, found.get('reason') or '')
    return jsonify({'success': True})


@app.route('/api/payroll/approve', methods=['POST'])
@login_required
def approve_paystub():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    worker = str(data.get('worker') or '').strip()
    try:
        period_start = date.fromisoformat(str(data.get('period_start') or ''))
    except ValueError:
        return json_error('Period start is not a date.', 400)
    settings = normalize_payroll_settings(load_time_settings())
    start, end = period_containing(period_start, settings)
    if start != period_start:
        return json_error('That date is not the start of a pay period.', 400)
    if worker not in {row['account'] for row in _payroll_workers()}:
        return json_error('Unknown worker.', 400)
    existing = _stored_stub(worker, period_start)
    if existing and existing.get('status') == 'approved':
        return json_error('This pay period is already approved.', 400)
    stub = _worker_stub(worker, start, end)
    if stub.get('mixed_currency'):
        return json_error('This period uses more than one currency.', 400)
    stub['status'] = 'approved'
    stub['approved_at'] = stamp(datetime.now(timezone.utc))
    stub['approved_by'] = actor.get('email') or ''
    rows = load_paystubs()
    saved = {
        'id': (existing or {}).get('id') or (max([row.get('id') or 0 for row in rows] or [0]) + 1),
        'worker': worker,
        'period_start': start.isoformat(),
        'period_end': end.isoformat(),
        'status': 'approved',
        'approved_at': stub['approved_at'],
        'approved_by': stub['approved_by'],
        'snapshot': stub,
        'history': (existing or {}).get('history') or [],
    }
    if existing:
        rows = [saved if row.get('id') == existing.get('id') else row for row in rows]
    else:
        rows.append(saved)
    save_paystubs(rows)
    _payroll_audit(actor, 'approve', worker, None, {'period_start': start.isoformat(), 'gross_pay': stub.get('gross_pay')})
    return jsonify(stub)


@app.route('/api/payroll/reopen', methods=['POST'])
@login_required
def reopen_paystub():
    actor, denied = _require_payroll()
    if denied:
        return denied
    data = json_body() or {}
    reason = clean_text(data.get('reason'), 300)
    if not reason:
        return json_error('A reason is required.', 400)
    worker = str(data.get('worker') or '').strip()
    try:
        period_start = date.fromisoformat(str(data.get('period_start') or ''))
    except ValueError:
        return json_error('Period start is not a date.', 400)
    rows = load_paystubs()
    found = next((row for row in rows if row.get('worker') == worker and row.get('period_start') == period_start.isoformat()), None)
    if not found or found.get('status') != 'approved':
        return json_error('That pay period is not approved.', 400)
    history = list(found.get('history') or [])
    history.append({
        'at': stamp(datetime.now(timezone.utc)),
        'by': actor.get('email') or '',
        'reason': reason,
        'snapshot': found.get('snapshot'),
    })
    found['history'] = history
    found['status'] = 'draft'
    found['snapshot'] = None
    found['reopened_at'] = history[-1]['at']
    save_paystubs(rows)
    _payroll_audit(actor, 'reopen', worker, {'period_start': period_start.isoformat()}, {'status': 'draft'}, reason)
    settings = normalize_payroll_settings(load_time_settings())
    start, end = period_containing(period_start, settings)
    return jsonify(_worker_stub(worker, start, end))


@app.route('/api/payroll/summary.csv', methods=['GET'])
@login_required
def payroll_summary_csv():
    import csv
    from io import StringIO
    _actor, denied = _require_payroll()
    if denied:
        return denied
    period, _settings, error = _period_from_request()
    if error:
        return error
    start, end = period
    stubs = [_worker_stub(row['account'], start, end) for row in _payroll_workers()]
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Gross pay (before taxes)', start.isoformat(), end.isoformat()])
    writer.writerow(['worker', 'name', 'regular_hours', 'overtime_hours', 'regular_pay', 'overtime_pay', 'adjustments', 'gross_pay', 'ytd_gross', 'status'])
    for stub in stubs:
        writer.writerow([
            stub.get('worker'), stub.get('name'), stub.get('regular_hours'), stub.get('overtime_hours'),
            stub.get('regular_pay'), stub.get('overtime_pay'), stub.get('adjustment_total'),
            stub.get('gross_pay') or '', stub.get('ytd_gross') or '', stub.get('status'),
        ])
    return send_file(
        BytesIO(buffer.getvalue().encode('utf-8')),
        mimetype='text/csv', as_attachment=True, download_name='payroll.csv',
    )


@app.route('/api/payroll/summary.pdf', methods=['GET'])
@login_required
def payroll_summary_pdf():
    _actor, denied = _require_payroll()
    if denied:
        return denied
    period, _settings, error = _period_from_request()
    if error:
        return error
    start, end = period
    stubs = [_worker_stub(row['account'], start, end) for row in _payroll_workers()]
    return _payroll_pdf(stubs, start, end, summary=True)


@app.route('/api/payroll/paystub.pdf', methods=['GET'])
@login_required
def paystub_pdf():
    _actor, denied = _require_payroll()
    if denied:
        return denied
    worker = str(request.args.get('worker') or '').strip()
    period, _settings, error = _period_from_request()
    if error:
        return error
    if worker not in {row['account'] for row in _payroll_workers()}:
        return json_error('Unknown worker.', 400)
    start, end = period
    return _payroll_pdf([_worker_stub(worker, start, end)], start, end, summary=False)


def _logo_flowable(width, height, title_style):
    from reportlab.platypus import Image, Paragraph
    for path in (
        os.path.join(app.root_path, 'static', 'logo.png'),
        os.path.join(app.root_path, 'logo.png'),
        'logo.png',
    ):
        if os.path.exists(path):
            return Image(path, width=width, height=height)
    return Paragraph('ORA GLASS', title_style)


def _payroll_pdf(stubs, start, end, summary):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.45 * inch, bottomMargin=0.45 * inch,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle('PaySmall', parent=styles['Normal'], fontSize=8, leading=10)
    title = ParagraphStyle('PayTitle', parent=styles['Normal'], fontSize=14, fontName='Helvetica-Bold', alignment=1)
    cell = ParagraphStyle('PayCell', parent=styles['Normal'], fontSize=8, leading=10)
    head = ParagraphStyle('PayHead', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.white)
    logo = _logo_flowable(1.2 * inch, 0.8 * inch, title)
    heading = 'Payroll summary' if summary else 'Paystub'
    elements = [
        Table([[
            logo,
            Paragraph(f'<b>{heading}</b><br/><font size="8">2312 Leatherwood Pl.<br/>Lynden, WA 98264, USA</font>', title),
            Paragraph(f"<font size='8'>{escape(start.isoformat())} to {escape(end.isoformat())}<br/>{escape(GROSS_LABEL)}</font>", small),
        ]], colWidths=[1.5 * inch, 3.2 * inch, 2.6 * inch]),
        Spacer(1, 8),
        HRFlowable(width='100%', thickness=1, color=colors.black),
        Spacer(1, 8),
    ]
    header = ['Worker', 'Regular h', 'OT h', 'Regular pay', 'OT pay', 'Adjustments', 'Gross']
    table_data = [[Paragraph(f'<b>{label}</b>', head) for label in header]]
    for stub in stubs:
        table_data.append([
            Paragraph(escape(stub.get('name') or ''), cell),
            Paragraph(escape(stub.get('regular_hours') or ''), cell),
            Paragraph(escape(stub.get('overtime_hours') or ''), cell),
            Paragraph(escape(stub.get('regular_pay') or ''), cell),
            Paragraph(escape(stub.get('overtime_pay') or ''), cell),
            Paragraph(escape(stub.get('adjustment_total') or ''), cell),
            Paragraph(escape(stub.get('gross_pay') or ''), cell),
        ])
    table = Table(table_data, colWidths=[1.7 * inch, 0.8 * inch, 0.7 * inch, 1.0 * inch, 0.9 * inch, 1.0 * inch, 1.0 * inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9f1830')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)
    if not summary and stubs:
        stub = stubs[0]
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(f"Year-to-date gross: {escape(stub.get('ytd_gross') or '0.00')} {escape(stub.get('currency') or 'USD')}", small))
        for rate in stub.get('rates') or []:
            elements.append(Paragraph(
                f"Rate {escape(rate.get('hourly_rate') or '')} {escape(rate.get('currency') or '')}: "
                f"{escape(rate.get('regular_hours') or '')} regular h, {escape(rate.get('overtime_hours') or '')} OT h",
                small,
            ))
        for adjustment in stub.get('adjustments') or []:
            elements.append(Paragraph(
                f"{escape(adjustment.get('kind') or '')} {escape(adjustment.get('amount') or '')}: {escape(adjustment.get('reason') or '')}",
                small,
            ))
        elements.append(Spacer(1, 8))
        elements.append(Paragraph('Gross pay (before taxes). This is not official tax withholding.', small))
    elif summary:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph('Gross pay (before taxes). This is not official tax withholding.', small))
    doc.build(elements)
    buffer.seek(0)
    name = 'payroll.pdf' if summary else 'paystub.pdf'
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=name)


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

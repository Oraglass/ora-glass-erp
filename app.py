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
from copy_notes import strip_copy_history
from billing import (
    CURRENCIES,
    NEXT_STATUS,
    PAYMENT_METHODS,
    RELEASED_STATUSES,
    REVIEW_STATUSES,
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
    open_receivables,
    allocated_to_target,
    spread_payment,
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
import orgadata_import
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
    load_orders as _load_stored_orders,
    load_payments,
    load_punches,
    load_quotes,
    quote_display_number,
    load_tiers,
    load_users,
    save_customers,
    save_invoices,
    save_orders,
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
    'driver': {'name': 'Driver'},
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
    if role == 'shop' or role == 'driver':
        return []
    stored = user.get('modules', None)
    if role == 'customer':
        if stored is None:
            chosen = list(CUSTOMER_CEILING)
        else:
            chosen = [module_id for module_id in stored if module_id in CUSTOMER_CEILING]
        if 'dashboard' not in chosen:
            chosen.insert(0, 'dashboard')
        # Every customer can open Production orders to print a confirmation.
        # Sending a quote to production stays the separate place-orders privilege.
        if 'orders' not in chosen:
            chosen.append('orders')
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
    if role == 'driver':
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


def _shown_fx_date(invoice):
    """The seed rate has no real effective day, so the invoice date is the date to show."""
    import currency_rules
    stored = str((invoice or {}).get('fx_date') or '')[:10]
    if stored and stored != currency_rules.SEED_EFFECTIVE:
        return stored
    return str((invoice or {}).get('invoice_date') or '')[:10] or stored


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


def can_edit_quote(user, quote):
    """Draft quotes can be changed in the calculator until they are ordered or voided.

    Admin and sales can edit any draft they can see. A customer can edit a draft
    of their own company when Quote History already shows it to them.
    """
    if not user or not isinstance(quote, dict):
        return False
    if quote.get('order_id'):
        return False
    if str(quote.get('status') or '').lower() in {'void', 'voided'}:
        return False
    if quote.get('status') != 'draft':
        return False
    if not quotes_visible_to(user, [quote]):
        return False
    role = user.get('role')
    if role in {'admin', 'sales'}:
        return True
    if role == 'customer':
        return True
    return False


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
    if creating:
        import tax_numbers
        for field, label in (('pst_number', 'PST'), ('qst_number', 'QST')):
            if field not in data:
                continue
            raw = tax_numbers.normalize(data.get(field))
            if raw and not tax_numbers.saved_number(raw):
                return f'Enter a {label} number with at least 5 characters and one digit.'
            customer[field] = tax_numbers.saved_number(raw)
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
    try:
        import copy_notes
        copy_notes.strip_stored_copy_notes()
    except Exception:
        logger.exception('Copy history cleanup failed')
    try:
        production.migrate_registry_names()
    except Exception:
        logger.exception('Production ID name migration failed')
    try:
        import tax_numbers
        tax_numbers.seed_current_numbers()
        tax_numbers.seed_seller_gst()
    except Exception:
        logger.exception('Tax number history seed failed')
    try:
        _migrate_order_one_review()
    except Exception:
        logger.exception('Order 1 review migration failed')
    try:
        _migrate_igu_names()
    except Exception:
        logger.exception('IGU name normalization failed')
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
        extras.append({'id': 'racks', 'label': 'Racks', 'endpoint': 'racks_page'})
    if user and user.get('role') == 'customer':
        nav.append({'id': 'racks', 'label': 'Racks', 'endpoint': 'racks_page'})
    if user and user.get('role') == 'driver':
        nav = [{'id': 'racks', 'label': 'Racks', 'endpoint': 'rack_scan'}]
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
    review_count = _orders_awaiting_staff(user)
    if review_count:
        nav = [
            dict(item, label=f"{item['label']} ({review_count})") if item.get('id') == 'orders' else item
            for item in nav
        ]
    return {
        'current_user': user,
        'nav_modules': nav,
        'has_dashboard': 'dashboard' in allowed,
        'review_count': review_count,
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
        if user.get('role') == 'shop':
            return redirect(url_for('punch'))
        if user.get('role') == 'driver':
            return redirect(url_for('rack_scan'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user() and request.method == 'GET':
        user = current_user()
        if user.get('role') == 'shop':
            return redirect(url_for('punch'))
        if user.get('role') == 'driver':
            return redirect(url_for('rack_scan'))
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
        if user.get('role') == 'shop':
            session['long_session'] = True
            return redirect(url_for('punch'))
        if user.get('role') == 'driver':
            return redirect(next_url or url_for('rack_scan'))
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
        review_count=_orders_awaiting_staff(user),
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
        'tax_rates': __import__('tax_numbers').TAX_RATES,
        'own_tax': {
            'province': (customers.get(user.get('customer_id') or '') or {}).get('province') or '',
            'pst_number': (customers.get(user.get('customer_id') or '') or {}).get('pst_number') or '',
            'qst_number': (customers.get(user.get('customer_id') or '') or {}).get('qst_number') or '',
        } if user.get('role') == 'customer' else None,
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
                'province': (customer or {}).get('province') or '',
                'pst_number': (customer or {}).get('pst_number') or '',
                'qst_number': (customer or {}).get('qst_number') or '',
            }
            for customer_id, customer in sorted(customers.items(), key=lambda item: ((item[1] or {}).get('name') or '').lower())
        ] if user.get('role') != 'customer' else [],
        'import_glass': [item.get('name') for item in glass],
        'import_spacers': [item.get('name') for item in spacers],
        'import_sealants': [item.get('name') for item in sealants],
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


@app.route('/api/quickbooks/gst-number', methods=['POST'])
@admin_required
def change_seller_gst():
    """Change Ora Glass (Canada) Inc's GST/HST number. The general settings save cannot."""
    import quickbooks
    import tax_numbers
    data = json_body() or {}
    user = current_user()
    with quickbooks.connection_scope('ca'):
        settings = quickbooks.load_settings()
        current = settings.get('gst_number') or ''
        stored, error = tax_numbers.validate_change(
            current, data.get('number'), data.get('number_confirm'),
            data.get('reason'), bool(data.get('remove')),
        )
        if error:
            return json_error(error, 400)
        settings['gst_number'] = stored
        quickbooks.save_settings(settings)
    tax_numbers.record_change(
        {'gst_number': tax_numbers.normalize(current)}, tax_numbers.SELLER_GST_ID, 'gst_number',
        stored, user.get('email') or '', data.get('reason'), request.remote_addr or '',
    )
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
    # A customer page hides the exchange rate and sends 1. Their CAD price still uses the ERP rate.
    if user.get('role') == 'customer':
        fx_rate = _current_fx()[0]
    else:
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
    remarks = strip_copy_history(source.get('notes') or source.get('remarks') or '')
    customer_note = strip_copy_history(source.get('customer_note') or '')
    if customer_note and customer_note not in remarks:
        remarks = (remarks + '\n' + customer_note).strip()
    return {
        'kind': kind,
        'revision': source.get('revision') or 0,
        'id': '' if quote_display_number(source) in (None, '') else quote_display_number(source),
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
        'remarks': remarks,
        'items': source.get('items') or [],
        'extra_charges': source.get('extra_charges') or [],
        'additional_discount_percent': source.get('additional_discount_percent') or 0,
        'additional_discount_amount': source.get('additional_discount_amount') or 0,
        'tax_label': source.get('tax_label') or '',
        'tax_amount': source.get('tax_amount'),
        'tax_lines': source.get('tax_lines') or [],
        'pst_number': source.get('pst_number') or '',
        'qst_number': source.get('qst_number') or '',
        'seller_gst_number': source.get('seller_gst_number') or '',
        'show_seller_gst': bool(source.get('show_seller_gst')),
        'ship_province': source.get('ship_province') or '',
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
        document = _pdf_document(_with_quote_tax({
            'id': '',
            'date': data.get('date') or date.today().isoformat(),
            'project': clean_text(data.get('project'), 200),
            'job_address': clean_text(data.get('job_address'), 500),
            'company': (customer or {}).get('name') or clean_text(data.get('company'), 200),
            'customer_id': customer_id,
            'currency': data.get('currency') or 'usd',
            'user': user.get('email') if user else '',
            'notes': strip_copy_history(clean_text(data.get('remarks') or data.get('notes'), 2000)),
            'items': items,
            'extra_charges': data.get('extra_charges') or [],
            'additional_discount_percent': data.get('additional_discount_percent') or 0,
            'additional_discount_amount': data.get('additional_discount_amount') or 0,
            'tax_label': tax_label,
            'ship_province': data.get('ship_province') or '',
            'type': data.get('type') or '',
            'po': clean_text(data.get('po'), 80),
        }), 'quote', user)
    buffer = BytesIO(_pdf_bytes(document))
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='ORA_Quote.pdf')


@app.route('/shipments')
@module_required('orders')
def shipments_page():
    user = current_user()
    if not staff_can_order(user):
        return forbidden()
    return render_template('shipments.html')


def _rack_user_ok(user, scan=False):
    if not user or user.get('role') == 'shop':
        return False
    if scan:
        return user.get('role') == 'driver' or staff_can_order(user)
    if user.get('role') == 'customer':
        return True
    return staff_can_order(user)


@app.route('/racks')
@login_required
def racks_page():
    user = current_user()
    if not _rack_user_ok(user):
        return forbidden()
    return render_template('racks.html', staff=staff_can_order(user), admin=user.get('role') == 'admin')


@app.route('/racks/scan')
@login_required
def rack_scan():
    user = current_user()
    if not _rack_user_ok(user, scan=True):
        return forbidden()
    return render_template('rack_scan.html')


@app.route('/api/racks', methods=['GET'])
@login_required
def rack_list():
    user = current_user()
    if not _rack_user_ok(user):
        return json_error('Access denied', 403)
    import racks as rack_book
    rack_book.backfill_from_orders(load_orders(), user)
    rows = rack_book.visible_rows(
        rack_book.load_rows(), user,
        status=clean_text(request.args.get('status'), 40),
        customer_id=clean_text(request.args.get('customer_id'), 80),
        number=clean_text(request.args.get('number'), 40),
    )
    return jsonify(rows)


@app.route('/api/racks/sites', methods=['GET'])
@login_required
def rack_sites():
    user = current_user()
    if not _rack_user_ok(user, scan=True):
        return json_error('Access denied', 403)
    rows = [
        {'id': customer_id, 'name': customer.get('name') or customer_id}
        for customer_id, customer in load_customers().items()
        if not customer.get('books_entity') and customer.get('status') != 'inactive'
    ]
    rows.sort(key=lambda row: row['name'].lower())
    return jsonify(rows)


@app.route('/api/racks/check', methods=['POST'])
@login_required
def rack_check():
    user = current_user()
    if not _rack_user_ok(user, scan=True):
        return json_error('Access denied', 403)
    data = json_body() or {}
    import racks as rack_book
    return jsonify(rack_book.check_scan(
        rack_book.load_rows(), data.get('text'),
        clean_text(data.get('customer_id'), 80), data.get('already') or [],
    ))


@app.route('/api/racks/pickup', methods=['POST'])
@login_required
def rack_pickup():
    user = current_user()
    if not _rack_user_ok(user, scan=True):
        return json_error('Access denied', 403)
    data = json_body() or {}
    customer_id = clean_text(data.get('customer_id'), 80)
    customer = load_customers().get(customer_id) if customer_id else None
    if not customer:
        return json_error('Choose the customer.', 400)
    import racks as rack_book
    result = rack_book.confirm_pickup(
        data.get('racks') or [], customer_id, customer.get('name') or '',
        clean_text(data.get('truck_plate'), 40), user,
    )
    if not result['moved']:
        return json_error('None of those racks are at this customer.', 400)
    return jsonify(result)


@app.route('/api/racks/<rack_number>/return', methods=['POST'])
@login_required
def mark_rack_returned(rack_number):
    user = current_user()
    if user.get('role') != 'admin':
        return json_error('Only an administrator can mark a rack returned.', 403)
    import racks as rack_book
    row = rack_book.mark_returned(rack_number, user)
    if not row:
        return json_error('That rack is not in a customer inventory.', 400)
    return jsonify(rack_book.present(row))


@app.route('/api/racks/labels.pdf', methods=['GET'])
@login_required
def rack_labels():
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    import racks as rack_book
    rows = rack_book.visible_rows(
        rack_book.load_rows(), user,
        status=clean_text(request.args.get('status'), 40),
        customer_id=clean_text(request.args.get('customer_id'), 80),
        number=clean_text(request.args.get('number'), 40),
    )
    payload = rack_book.labels_pdf(rows)
    return send_file(BytesIO(payload), mimetype='application/pdf', as_attachment=True, download_name='rack-labels.pdf')


def _open_shipment_rows(user):
    rows = []
    for order in orders_visible_to(user, load_orders()):
        has_packing = bool(order.get('packing_lists'))
        if not has_packing and order_status(order) not in {'sent', 'made', 'delivered'} and not order.get('shipments'):
            continue
        progress = shipments.shipment_progress(order)
        lines = shipments.open_order_lines(order)
        if not lines:
            continue
        ready = sum(int(line.get('ready_to_ship') or 0) for line in lines)
        rows.append({
            'id': order.get('id'),
            'company': order.get('company') or '',
            'project': order.get('project') or '',
            'status': order_status(order),
            'shipping_label': progress['label'],
            'remaining_pcs': progress['remaining_pcs'],
            'remaining_sqf': progress['remaining_sqf'],
            'remaining_value': progress['remaining_value'],
            'ready_to_ship': ready,
            'currency': order.get('currency') or 'usd',
            'lines': lines,
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
    glass, spacers, _sealants = _live_materials()
    return render_template(
        'orders.html',
        note=scope_note(),
        prices_visible=_order_prices_visible(user),
        staff_view=staff_can_order(user),
        can_edit=can_edit_orders(user),
        can_job_address=can_edit_job_address(user),
        can_decide=can_place_production_orders(user),
        show_fx_rate=user.get('role') != 'customer',
        can_status=can_update_order_status(user),
        can_remake=_can_create_remake(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in glass],
        spacer_names=[item.get('name') for item in spacers],
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
    glass, spacers, _sealants = _live_materials()
    return render_template(
        'orders.html',
        note=scope_note(),
        prices_visible=_order_prices_visible(user),
        staff_view=staff_can_order(user),
        can_edit=can_edit_orders(user),
        can_job_address=can_edit_job_address(user, order),
        can_decide=can_place_production_orders(user),
        show_fx_rate=user.get('role') != 'customer',
        can_status=can_update_order_status(user),
        can_remake=_can_create_remake(user),
        is_admin=user.get('role') == 'admin',
        glass_names=[item.get('name') for item in glass],
        spacer_names=[item.get('name') for item in spacers],
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


def _can_change_tax_number(user, customer_id):
    if not user:
        return False
    if user.get('role') == 'admin' or user.get('role') in LEGACY_STAFF_ROLES:
        return True
    return user.get('role') == 'customer' and user.get('customer_id') == customer_id


def _apply_tax_number_change(customer_id, field, new_value, confirm, reason, remove, user):
    import tax_numbers
    if field not in tax_numbers.FIELDS:
        return 'Choose PST or QST.'
    customers = load_customers()
    customer = customers.get(customer_id)
    if not customer:
        return 'Unknown customer.'
    stored, error = tax_numbers.validate_change(
        customer.get(field), new_value, confirm, reason, remove=remove,
    )
    if error:
        return error
    tax_numbers.record_change(
        customer, customer_id, field, stored, user.get('email') or '',
        reason, request.remote_addr or '',
    )
    customers[customer_id] = customer
    save_customers(customers)
    return None


def _canada_tax_page(customer_id, user):
    import quickbooks
    customer = load_customers().get(customer_id)
    if not customer:
        return json_error('Unknown customer.', 404) if wants_json() else redirect(url_for('customers_page'))
    province = str(customer.get('province') or '').upper()
    kind = 'qst' if province == 'QC' else 'pst'
    can_edit = _can_change_tax_number(user, customer_id)
    staff_view = user.get('role') != 'customer'
    import tax_numbers
    field = 'qst_number' if kind == 'qst' else 'pst_number'
    if request.method == 'POST' and can_edit:
        error = _apply_tax_number_change(
            customer_id, field,
            request.form.get('number'), request.form.get('number_confirm'),
            request.form.get('reason'), bool(request.form.get('remove')), user,
        )
        if error:
            flash(error, 'error')
        else:
            flash('Tax number saved.', 'success')
        customer = load_customers().get(customer_id) or customer
    import racks as rack_book
    return render_template(
        'company_tax.html',
        customer_id=customer_id,
        company=customer.get('name') or customer_id,
        province=province,
        kind=kind,
        field_label=tax_numbers.FIELDS[field],
        current_number=tax_numbers.saved_number(customer.get(field)),
        can_edit=can_edit,
        staff_view=staff_view,
        history=tax_numbers.history_for(customer_id) if staff_view else [],
        changelog=(customer.get('changelog') or []) if staff_view else [],
        canadian=quickbooks.is_canadian(customer),
        rack_count=rack_book.held_count(customer_id),
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
    import racks as rack_book
    return render_template(
        'resale.html',
        customer_id=customer_id,
        company=customer.get('name') or customer_id,
        certificates=[resale.public_certificate(row) for row in customer.get('resale_certificates') or []],
        can_review=_can_review_resale(user),
        states=US_STATES,
        warning=(resale.warnings_for({customer_id: customer}) or [{}])[0].get('text', ''),
        rack_count=rack_book.held_count(customer_id),
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


def _production_id_confirmed():
    return request.form.get('confirmed') == 'yes' and request.form.get('acknowledged') == 'yes'


def _audit_production_ids(changes):
    user = current_user() or {}
    reason = clean_text(request.form.get('reason'), 500)
    when = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    production.append_id_audit([
        {
            'at': when,
            'user': user.get('email') or '',
            'key': change.get('key') or '',
            'product_name': change.get('product_name') or '',
            'old': change.get('old_id') or '',
            'new': change.get('new_id') or '',
            'reason': reason,
            'order_count': change.get('order_count') or 0,
        }
        for change in changes or []
    ])


def _production_id_review(list_filter, plan, mode, pending, replace, unmatched):
    rows = production.registry_rows()
    if list_filter == 'missing':
        rows = [row for row in rows if row['missing']]
    review = {
        'changes': plan.get('changes') or [],
        'duplicates': plan.get('duplicates') or [],
        'sensitive': any(change.get('replaces_existing') for change in (plan.get('changes') or [])),
        'mode': mode,
        'pending': pending,
        'replace': replace,
        'unmatched': unmatched or [],
    }
    return render_template(
        'production_settings.html',
        tabs=production.PRODUCTION_TABS,
        tab='ids',
        rows=rows,
        list_filter=list_filter,
        seed=production.seed_report(),
        settings=production.load_export_settings(),
        headers=production.PRODUCTION_HEADERS,
        review=review,
    )


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
            replace = request.form.get('replace_ids') == 'on'
            planned = production._upload_plan(rows, replace=replace)
            if not planned['updates']:
                flash(
                    f"Matched {len(planned['matched'])} names. "
                    f"{len(planned['unmatched'])} names were not in the registry. "
                    f"{len(planned['kept'])} existing IDs were left unchanged.",
                    'success',
                )
                return redirect(url_for('production_settings', tab='ids', filter=list_filter))
            token = secrets.token_hex(8)
            production.save_pending_upload(token, rows, replace)
            return _production_id_review(
                list_filter, production.id_change_plan(planned['updates']),
                'upload', token, replace, planned['unmatched'],
            )
        if action == 'upload_confirm':
            token = clean_text(request.form.get('pending'), 40)
            pending = production.load_pending_upload(token)
            if not pending:
                flash('That upload review expired. Upload the file again.', 'error')
                return redirect(url_for('production_settings', tab='ids', filter=list_filter))
            planned = production._upload_plan(pending.get('rows') or [], replace=bool(pending.get('replace')))
            plan = production.id_change_plan(planned['updates'])
            if plan['duplicates'] or not _production_id_confirmed():
                return _production_id_review(
                    list_filter, plan, 'upload', token, bool(pending.get('replace')), planned['unmatched'],
                )
            production.apply_product_upload(pending.get('rows') or [], replace=bool(pending.get('replace')))
            _audit_production_ids(plan['changes'])
            production.drop_pending_upload(token)
            flash('Production IDs saved.', 'success')
            return redirect(url_for('production_settings', tab='ids', filter=list_filter))
        keys = request.form.getlist('row_key')
        ids = request.form.getlist('product_id')
        names = request.form.getlist('product_name')
        updates = {}
        for key, product_id, product_name in zip(keys, ids, names):
            updates[key] = {'product_id': product_id, 'product_name': product_name}
        plan = production.id_change_plan(updates)
        if not plan['changes'] and not plan['duplicates']:
            flash('No Product IDs changed.', 'success')
            return redirect(url_for('production_settings', tab='ids', filter=list_filter))
        if plan['duplicates'] or not _production_id_confirmed():
            return _production_id_review(list_filter, plan, 'ids', '', False, [])
        production.save_production_overrides(updates)
        _audit_production_ids(plan['changes'])
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
        review=None,
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
        is_admin=user.get('role') == 'admin',
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
        is_admin=user.get('role') == 'admin',
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
        import racks as rack_book
        row['rack_count'] = rack_book.held_count(customer_id)
        return jsonify([row])
    import racks as rack_book
    rack_rows = rack_book.load_rows()
    rows = []
    for customer_id, customer in customers.items():
        if customer.get('books_entity'):
            continue
        row = public_customer(customer_id, customer, tiers, counts.get(customer_id, 0))
        row['rack_count'] = rack_book.held_count(customer_id, rack_rows)
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
    import tax_numbers
    for field in tax_numbers.FIELDS:
        number = tax_numbers.saved_number(customer.get(field))
        if not number:
            customer[field] = ''
            continue
        customer[field] = ''
        tax_numbers.record_change(
            customer, customer_id, field, number, current_user().get('email') or '',
            'Initial number on file', request.remote_addr or '',
        )
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


@app.route('/api/customers/<cid>/tax-number', methods=['GET'])
@login_required
def get_tax_number_history(cid):
    if not _can_change_tax_number(current_user(), cid) or current_user().get('role') == 'customer':
        return json_error('Access denied', 403)
    if not ID_RE.fullmatch(cid or '') or cid not in load_customers():
        return json_error('Not found', 404)
    import tax_numbers
    return jsonify(tax_numbers.history_for(cid))


@app.route('/api/customers/<cid>/tax-number', methods=['POST'])
@login_required
def change_tax_number(cid):
    user = current_user()
    if not _can_change_tax_number(user, cid):
        return json_error('Access denied', 403)
    if not ID_RE.fullmatch(cid or '') or cid not in load_customers():
        return json_error('Not found', 404)
    data = json_body()
    if data is None:
        return json_error('Expected a JSON object', 400)
    error = _apply_tax_number_change(
        cid, clean_text(data.get('field'), 20),
        data.get('number'), data.get('number_confirm'),
        data.get('reason'), bool(data.get('remove')), user,
    )
    if error:
        return json_error(error, 400)
    import tax_numbers
    customer = load_customers()[cid]
    return jsonify({
        'success': True,
        'pst_number': customer.get('pst_number') or '',
        'qst_number': customer.get('qst_number') or '',
        'history': tax_numbers.history_for(cid),
        'changelog': customer.get('changelog') or [],
    })


@app.route('/customers/<cid>/tax-numbers.pdf', methods=['GET'])
@login_required
def download_tax_number_history(cid):
    user = current_user()
    if user.get('role') == 'customer' or not _can_change_tax_number(user, cid):
        return forbidden()
    if not ID_RE.fullmatch(cid or ''):
        return json_error('Not found', 404)
    customer = load_customers().get(cid)
    if not customer:
        return json_error('Not found', 404)
    import tax_numbers
    payload = tax_numbers.history_pdf(cid, customer.get('name') or cid, tax_numbers.history_for(cid))
    return send_file(
        BytesIO(payload), mimetype='application/pdf', as_attachment=True,
        download_name=f'{cid}-tax-numbers.pdf',
    )


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


def _tax_number_snapshot(customer):
    """PST and QST in effect now. Blank means the number is not saved."""
    import tax_numbers
    customer = customer or {}
    return {
        'pst_number': tax_numbers.saved_number(customer.get('pst_number')),
        'qst_number': tax_numbers.saved_number(customer.get('qst_number')),
    }


def _apply_canada_tax(document, customer, ship_province=None, rates=None):
    """Store the tax result and the numbers that produced it. Later edits do not rewrite this."""
    import tax_numbers
    document.update(_canada_tax_fields(customer, ship_province))
    document.update(_tax_number_snapshot(customer))
    document['seller_gst_number'] = tax_numbers.seller_gst_number()
    document['show_seller_gst'] = True
    pretax = document.get('pretax_total')
    if pretax in (None, ''):
        pretax = document.get('total')
    lines = tax_numbers.tax_breakdown(
        document.get('ship_province'), pretax,
        document.get('pst_number'), document.get('qst_number'), rates=rates,
    )
    document['tax_lines'] = lines
    document['tax_total'] = tax_numbers.tax_total(lines)
    document['pretax_total'] = round(float(pretax or 0), 2)
    document['total'] = round(document['pretax_total'] + document['tax_total'], 2)
    if 'balance' in document and not document.get('voided'):
        document['balance'] = document['total']
    return document


def _quote_pretax(quote):
    goods = 0.0
    for item in (quote or {}).get('items') or []:
        if isinstance(item, dict):
            raw = item.get('total') if item.get('total') not in (None, '') else item.get('price')
            try:
                goods += float(raw or 0)
            except (TypeError, ValueError):
                continue
    extras = 0.0
    for charge in (quote or {}).get('extra_charges') or []:
        if isinstance(charge, dict):
            try:
                extras += float(charge.get('amount') or 0)
            except (TypeError, ValueError):
                continue
    discount = float((quote or {}).get('additional_discount_amount') or 0)
    return round(goods + extras - discount, 2)


def _with_quote_tax(quote):
    row = dict(quote)
    owner = load_customers().get(row.get('customer_id') or '')
    tax = _canada_tax_fields(owner, row.get('ship_province'))
    if tax:
        import tax_numbers
        row['ship_province'] = row.get('ship_province') or tax.get('ship_province') or ''
        row['tax_label'] = tax.get('tax_label') or ''
        row['qbo_tax_code'] = tax.get('qbo_tax_code') or ''
        snapshot = _tax_number_snapshot(owner)
        row['pst_number'] = snapshot['pst_number']
        row['qst_number'] = snapshot['qst_number']
        row['seller_gst_number'] = tax_numbers.seller_gst_number()
        row['show_seller_gst'] = True
        row['tax_lines'] = tax_numbers.tax_breakdown(
            row.get('ship_province'), _quote_pretax(row),
            snapshot['pst_number'], snapshot['qst_number'],
        )
        row['tax_total'] = tax_numbers.tax_total(row['tax_lines'])
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
        if not existing or not can_edit_quote(user, existing):
            return json_error('That quote cannot be replaced.', 400)
        stored_tier = clean_text(existing.get('tier'), 20)
        if stored_tier in tiers:
            tier = stored_tier
        old_items = list(existing.get('items') or [])
        existing['project'] = project
        existing['customer_id'] = customer_id
        existing['company'] = company
        kept_discount, kept_addon = _saved_or_company_terms(existing, customer)
        existing['items'] = _assign_line_numbers(_repriced_quote_items(
            items, tier, currency, tiers, fx_rate, _price_factor(kept_addon),
            _effective_minimum(customer), keep_identity=True,
        ) if quote_type == 'size' else priced)
        _stamp_terms(existing, existing['items'], kept_discount, kept_addon)
        existing['currency'] = currency
        existing['fx_rate'] = fx_rate
        existing['type'] = quote_type
        existing['tier'] = tier
        existing['time'] = datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M')
        existing['updated_by'] = user.get('email') or ''
        if 'notes' in data or 'remarks' in data:
            existing['notes'] = strip_copy_history(clean_text(data.get('notes') or data.get('remarks'), 2000))
        if 'job_address' in data:
            existing['job_address'] = clean_text(data.get('job_address'), 500)
        if 'extra_charges' in data:
            existing['extra_charges'] = _copied_extras(data.get('extra_charges'))
        owner = customers.get(existing.get('customer_id') or '')
        if 'ship_province' in data:
            existing['ship_province'] = _ship_province(data.get('ship_province'), owner)
        existing.update(_canada_tax_fields(owner, existing.get('ship_province')))
        _record_quote_edit(existing, user, old_items)
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
        'items': _assign_line_numbers(_charge_saved_size_lines(priced, _effective_minimum(customer)) if quote_type == 'size' else priced),
        'currency': currency,
        'fx_rate': fx_rate,
        'type': quote_type,
        'tier': tier,
    }
    if 'notes' in data or 'remarks' in data:
        quote['notes'] = strip_copy_history(clean_text(data.get('notes') or data.get('remarks'), 2000))
    _stamp_terms(quote, quote['items'], new_discount, new_addon)
    if user.get('role') == 'customer':
        quote.update(_canada_tax_fields(customer, data.get('ship_province')))
    stored = append_quote(quote)
    _remember_igus(stored.get('items'), stored.get('date'))
    return jsonify({'success': True, 'id': stored['id']})


def _quote_for_viewer(quote, user, orders=None):
    row = dict(quote)
    row.pop('copy_note', None)
    row.pop('copied_from', None)
    row['notes'] = strip_copy_history(row.get('notes') or '')
    if isinstance(row.get('remarks'), str):
        row['remarks'] = strip_copy_history(row.get('remarks'))
    items = []
    for item in row.get('items') or []:
        if isinstance(item, dict):
            item = dict(item)
            if isinstance(item.get('remarks'), str):
                item['remarks'] = strip_copy_history(item.get('remarks'))
        items.append(item)
    import documents
    row['items'] = _assign_line_numbers(documents.describe_items(items))
    for item in row['items']:
        if not isinstance(item, dict) or item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        if not item.get('irregular_shape'):
            continue
        title, spec = documents.drawing_caption(item)
        item['drawing_title'] = title
        item['drawing_spec'] = spec
    if user.get('role') == 'customer':
        row.pop('price_addon_percent', None)
        row.pop('fx_rate', None)
    row['can_edit'] = can_edit_quote(user, quote)
    row['has_source_pdf'] = bool(row.get('source_file'))
    row.pop('source_file', None)
    row['source_filenames'] = _source_filenames(row)
    row.pop('source_files', None)
    row.update(_append_state(quote, orders))
    return row


def _visible_quote(user, quote_id):
    if not (user_has_module(user, 'quotes') or user_has_module(user, 'quote-history')):
        return None
    return next((row for row in quotes_visible_to(user, load_quotes()) if row.get('id') == quote_id), None)


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


def _quote_money_total(items):
    total = 0.0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        raw = item.get('total') if item.get('total') not in (None, '') else item.get('price') or 0
        try:
            total += float(raw)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _record_quote_edit(quote, user, old_items):
    """Keep the quote number and record who edited it, when, and which lines changed."""
    new_items = quote.get('items') or []
    number = int(quote.get('revision') or 0) + 1
    at = datetime.now(ZoneInfo('America/Los_Angeles')).replace(microsecond=0).isoformat()
    email = user.get('email') or ''
    revisions = [row for row in (quote.get('revisions') or []) if isinstance(row, dict)]
    revisions.append({
        'number': number,
        'at': at,
        'user': email,
        'kind': 'edit',
        'lines': _revision_lines(old_items, new_items),
        'old_total': _quote_money_total(old_items),
        'new_total': _quote_money_total(new_items),
    })
    quote['revisions'] = revisions
    quote['revision'] = number
    quote['edited_at'] = at
    quote['edited_by'] = email
    quote['updated_by'] = email


def _fill_open_sealants(spec):
    """A spacer with no sealant, or a sealant that does not fit it, gets the quote-form auto-select."""
    sealants = [item.get('name') for item in _live_materials()[2]]
    for index in (1, 2):
        spacer = spec.get(f'spacer{index}') or ''
        if not spacer:
            continue
        current = spec.get(f'sealant{index}') or ''
        suggestion = suggest_sealant(spacer, sealants)
        options = list(suggestion.get('options') or [])
        if current and current in options:
            continue
        spec[f'sealant{index}'] = suggestion.get('selected') or ''
    return spec


def _assign_line_numbers(items):
    """Glass lines are numbered 1, 2, 3… by their position. A copied number is not kept."""
    number = 1
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            item.pop('line_number', None)
            continue
        item['line_number'] = number
        number += 1
    return items


def _repriced_quote_items(items, tier, currency, tiers, fx_rate=None, price_factor=1, minimum_sqf=0, keep_identity=False):
    """Price a copy at the current tier. Surcharge lines and drawing files are left out."""
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if spec:
            spec = _fill_open_sealants(production.normalize_igu_spec(spec))
        if spec and spec.get('width') not in (None, '') and spec.get('height') not in (None, ''):
            stored_minimum = item.get('minimum_sqf') if 'minimum_sqf' in item else minimum_sqf
            priced = _reprice(spec, item.get('pcs'), tier, currency, tiers, fx_rate, price_factor, stored_minimum)
            copied_line = {
                'name': item.get('name') or 'Glass',
                'spec': spec,
                'irregular_shape': bool(item.get('irregular_shape')),
            }
            import documents
            label = documents.igu_name_from_spec(spec)
            if label:
                copied_line['name'] = label
            copied_line.update(_size_price_fields(priced))
            if item.get('remarks'):
                remark = strip_copy_history(clean_text(item.get('remarks'), 500))
                if remark:
                    copied_line['remarks'] = remark
            floor = production.floor_code(item)
            if floor:
                copied_line['floor'] = floor
            if keep_identity:
                for key in ('line_id', 'source_filename'):
                    if item.get(key) not in (None, ''):
                        copied_line[key] = item.get(key)
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
            remark = strip_copy_history(clean_text(item.get('remarks'), 500))
            if remark:
                plain['remarks'] = remark
        floor = production.floor_code(item)
        if floor:
            plain['floor'] = floor
        rows.append(plain)
    return expand_irregular_lines(_assign_line_numbers(rows), load_pricing_settings()['irregular_shape_surcharge'])


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
    import documents
    if documents.sync_item_names(quote.get('items'), only_mismatch=True):
        replace_quote(quote_id, quote)
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
    notes = strip_copy_history(clean_text(source.get('notes'), 2000))
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
    return jsonify({'success': True, 'id': stored['id']})


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
    sealants = _live_materials()[2]
    if line.get('igu'):
        glasses = [
            line.get('glass_product') or '',
            line.get('glass2_product') or '',
            line.get('glass3_product') or '',
        ]
        spacers = [line.get('spacer_product') or '', line.get('spacer2_product') or '']
        lites = line.get('lites') if isinstance(line.get('lites'), list) else []
        spec = {
            'width': line.get('width'),
            'height': line.get('height'),
            'unit': 'in',
            'argon': bool(line.get('argon')),
        }
        for index in range(3):
            glass = glasses[index]
            temper = 'n/a'
            if index < len(lites) and isinstance(lites[index], dict) and lites[index].get('temper') in {'yes', 'n/a'}:
                temper = lites[index]['temper']
            spec[f'glass{index + 1}'] = glass
            spec[f'temper{index + 1}'] = temper if glass else 'n/a'
            spacer = spacers[index] if index < 2 else ''
            spec[f'spacer{index + 1}'] = spacer
            spec[f'sealant{index + 1}'] = suggest_sealant(spacer, sealants).get('selected') or '' if spacer else ''
        return spec
    spacer = line.get('spacer_product') or ''
    sealant = ''
    if spacer:
        sealant = suggest_sealant(spacer, sealants).get('selected') or ''
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
    if line.get('igu') and (not line.get('glass2_product') or not line.get('spacer_product')):
        return
    if line.get('igu') and int(line.get('panes') or 2) >= 3 and (not line.get('glass3_product') or not line.get('spacer2_product')):
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
    aliases = orgadata_import.aliases_for(customer_id)
    glass_aliases = dict(aliases.get('glass') or {})
    spacer_aliases = dict(aliases.get('spacer') or {})
    for source, product in glass_map.items():
        glass_aliases.setdefault(orgadata_import.glass_alias_key(source), product)
    sealants = _live_materials()[2]
    unmatched_glass = []
    unmatched_spacer = []
    for line in parsed.get('lines') or []:
        errors = [error for error in (line.get('errors') or []) if not str(error).startswith('Unmatched') and not str(error).startswith('Choose an ERP')]
        raw_glass = line.get('glass_name') or ''
        build = orgadata_import.match_igu_description(
            raw_glass, glass_names, spacer_names, {'glass': glass_aliases, 'spacer': spacer_aliases},
        ) if raw_glass else None
        if build:
            line['igu'] = True
            line['argon'] = bool(build.get('argon'))
            line['lites'] = build.get('lites') or []
            line['spacers'] = build.get('spacers') or []
            line['panes'] = len(line['lites'])
            line['glass_product'] = line['lites'][0].get('product') if line['lites'] else ''
            line['glass2_product'] = line['lites'][1].get('product') if len(line['lites']) > 1 else ''
            line['glass3_product'] = line['lites'][2].get('product') if len(line['lites']) > 2 else ''
            line['spacer_product'] = line['spacers'][0].get('product') if line['spacers'] else ''
            line['spacer2_product'] = line['spacers'][1].get('product') if len(line['spacers']) > 1 else ''
            line['sealant'] = suggest_sealant(line['spacer_product'], sealants).get('selected') or '' if line['spacer_product'] else ''
            line['sealant2'] = suggest_sealant(line['spacer2_product'], sealants).get('selected') or '' if line['spacer2_product'] else ''
            if build.get('warnings'):
                errors.extend(build['warnings'])
            line['errors'] = errors
            _price_import_line(line, tier, currency)
            continue
        line['igu'] = False
        product = glass_map.get(raw_glass.casefold()) or exact_glass.get(raw_glass.casefold(), '') if raw_glass else ''
        line['glass_product'] = product or ''
        line['glass2_product'] = ''
        line['glass3_product'] = ''
        line['spacer2_product'] = ''
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
    igu_open = any(line.get('igu') and line.get('errors') for line in parsed.get('lines') or [])
    if parsed.get('error') or parsed.get('looks_wrong'):
        status = 'needs_review'
    elif unmatched_glass or unmatched_spacer or igu_open:
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
        glass_name = clean_text(raw.get('glass_name'), 500)
        parsed_igu = orgadata_import.parse_igu_description(glass_name)
        igu = bool(raw.get('igu')) or bool(parsed_igu)
        glass = _known_product(raw.get('glass_product'), glass_names)
        if not glass:
            errors.append('Choose one of our glass products.')
        spacer_raw = raw.get('spacer_product') or ''
        spacer = _known_product(spacer_raw, spacer_names) if spacer_raw else ''
        if spacer_raw and not spacer:
            errors.append('Choose one of our spacers.')
        glass2 = _known_product(raw.get('glass2_product'), glass_names) if raw.get('glass2_product') else ''
        glass3 = _known_product(raw.get('glass3_product'), glass_names) if raw.get('glass3_product') else ''
        spacer2 = _known_product(raw.get('spacer2_product'), spacer_names) if raw.get('spacer2_product') else ''
        panes = len(parsed_igu['lites']) if parsed_igu else (3 if glass3 or spacer2 else 2)
        if igu:
            if not glass2:
                errors.append('Choose the inner glass.')
            if not spacer:
                errors.append('Choose a spacer.')
            if panes >= 3 and not glass3:
                errors.append('Choose the middle glass.')
            if panes >= 3 and not spacer2:
                errors.append('Choose the second spacer.')
        if errors:
            blocked = True
        argon = raw.get('argon')
        if isinstance(argon, str):
            argon = argon.lower() in {'1', 'true', 'yes', 'argon'}
        elif argon is None and parsed_igu:
            argon = parsed_igu.get('argon')
        lites = []
        if parsed_igu:
            products = [glass, glass2, glass3]
            for index, lite in enumerate(parsed_igu['lites']):
                lites.append({
                    'source': lite.get('source') or '',
                    'thickness_mm': lite.get('thickness_mm'),
                    'product': products[index] if index < len(products) else '',
                    'temper': 'yes' if parsed_igu.get('tempered') else 'n/a',
                })
        line = {
            'mark': clean_text(raw.get('mark'), 80),
            'width': width,
            'height': height,
            'unit': 'in',
            'qty': qty,
            'glass_name': glass_name,
            'spacer_name': clean_text(raw.get('spacer_name'), 200),
            'glass_product': glass,
            'glass2_product': glass2,
            'glass3_product': glass3,
            'spacer_product': spacer,
            'spacer2_product': spacer2,
            'igu': igu,
            'panes': panes if igu else 1,
            'argon': bool(argon),
            'lites': lites,
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
        makeup = production.makeup_from_spec(spec) if line.get('igu') else ''
        if makeup and not line.get('argon'):
            makeup = makeup.replace(' with Argon', '')
        if makeup:
            parts = [line.get('mark') or '', makeup, size]
        else:
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
    filename = record.get('filename') or ''
    _stamp_import_lines(quote['items'], filename)
    quote['import_kind'] = 'order'
    quote['import_template_id'] = record.get('template_id')
    quote['source_filename'] = filename
    quote['source_files'] = [{'filename': filename, 'stored': record.get('stored') or ''}]
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


def _orgadata_owned(user, import_id):
    record = next((row for row in orgadata_import.load_imports() if row.get('id') == import_id), None)
    if not record or user.get('role') == 'shop':
        return None
    if user.get('role') == 'customer':
        if record.get('customer_id') != user.get('customer_id'):
            return None
        if record.get('user') != user.get('email') and customer_subrole(user) != 'management':
            return None
    return record


def _orgadata_choose_customer(user, requested, sender):
    customers = load_customers()
    matches = orgadata_import.matching_customers(sender, customers)
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id') or ''
        customer = customers.get(customer_id)
        if not customer:
            return '', None, matches, 'Your account is not linked to a customer.'
        return customer_id, customer, matches, ''
    customer_id = clean_text(requested, 64)
    if customer_id:
        customer = customers.get(customer_id)
        if not customer:
            return '', None, matches, 'Unknown customer.'
        return customer_id, customer, matches, ''
    if len(matches) == 1:
        return matches[0], customers.get(matches[0]), matches, ''
    return '', None, matches, ''


def _orgadata_apply_edits(mapped, edits, glass_names, spacer_names):
    sealant_names = [item.get('name') for item in _live_materials()[2]]
    if not isinstance(edits, dict):
        return mapped
    for key, limit in (('project', 200), ('po', 80), ('contact', 200), ('reference_date', 10)):
        if key in edits:
            mapped[key] = clean_text(edits.get(key), limit)
    incoming_blocks = edits.get('blocks') if isinstance(edits.get('blocks'), list) else []
    for index, incoming in enumerate(incoming_blocks):
        if index >= len(mapped.get('blocks') or []) or not isinstance(incoming, dict):
            continue
        block = mapped['blocks'][index]
        for field, limit in (('name', 500), ('configuration', 80), ('thickness', 80), ('spacer_source', 80)):
            if field in incoming:
                block[field] = clean_text(incoming.get(field), limit)
        lites_in = incoming.get('lites') if isinstance(incoming.get('lites'), list) else []
        for lite_index, lite_in in enumerate(lites_in):
            if lite_index >= len(block.get('lites') or []) or not isinstance(lite_in, dict):
                continue
            lite = block['lites'][lite_index]
            if 'product' in lite_in:
                lite['product'] = _known_product(lite_in.get('product'), glass_names)
                lite['matched'] = bool(lite['product'])
            if lite_in.get('temper') in {'yes', 'n/a'}:
                lite['temper'] = lite_in.get('temper')
        spacers_in = incoming.get('spacers') if isinstance(incoming.get('spacers'), list) else []
        for spacer_index, spacer_in in enumerate(spacers_in):
            if spacer_index >= len(block.get('spacers') or []) or not isinstance(spacer_in, dict):
                continue
            spacer = block['spacers'][spacer_index]
            if 'product' in spacer_in:
                spacer['product'] = _known_product(spacer_in.get('product'), spacer_names)
                spacer['matched'] = bool(spacer['product'])
            if 'sealant' in spacer_in:
                spacer['sealant'] = _known_product(spacer_in.get('sealant'), sealant_names)
    incoming_lines = edits.get('lines') if isinstance(edits.get('lines'), list) else []
    by_number = {}
    for block in mapped.get('blocks') or []:
        for line in block.get('lines') or []:
            by_number[line.get('line_number')] = line
    for incoming in incoming_lines:
        if not isinstance(incoming, dict):
            continue
        try:
            number = int(incoming.get('line_number'))
        except (TypeError, ValueError):
            continue
        line = by_number.get(number)
        if not line:
            continue
        if 'floor' in incoming:
            line['floor'] = clean_text(incoming.get('floor'), 80)
        if 'qty' in incoming:
            try:
                line['qty'] = int(incoming.get('qty'))
            except (TypeError, ValueError):
                line['qty'] = 0
        for key in ('width_mm', 'height_mm'):
            if key in incoming:
                try:
                    line[key] = float(incoming.get(key))
                except (TypeError, ValueError):
                    line[key] = 0
    return mapped


def _orgadata_refresh_warnings(mapped):
    mapped['warnings'] = [
        warning for warning in (mapped.get('warnings') or [])
        if not str(warning).startswith('Quantity ')
    ]
    total = 0
    for block in mapped.get('blocks') or []:
        block['warnings'] = [
            warning for warning in (block.get('warnings') or [])
            if not str(warning).startswith('Choose an ERP') and not str(warning).startswith('Quantity ')
        ]
        active_lites, active_spacers = _orgadata_active(block)
        for lite in active_lites:
            if not lite.get('product'):
                block['warnings'].append(f'Choose an ERP glass for {lite.get("source") or "this lite"}.')
        for spacer in active_spacers:
            if not spacer.get('product') and (spacer.get('source') or spacer.get('gap_mm')):
                block['warnings'].append(
                    f'Choose an ERP spacer for {spacer.get("source") or "the spacer"} {spacer.get("gap_mm")} mm.'
                )
        got = 0
        for line in block.get('lines') or []:
            try:
                got += int(line.get('qty') or 0)
            except (TypeError, ValueError):
                pass
        total += got
        if block.get('sum_qty') is not None and got != block.get('sum_qty'):
            block['warnings'].append(f'Quantity {got} does not match the Sum: row ({block.get("sum_qty")}).')
    if mapped.get('sum_quantity') is not None and total != mapped.get('sum_quantity'):
        mapped['warnings'].append(
            f'Quantity {total} does not match Sum Quantity: {mapped.get("sum_quantity")} Pcs.'
        )
    return mapped


def _orgadata_ready(block):
    lites, spacers = _orgadata_active(block)
    if not lites or any(not lite.get('product') for lite in lites):
        return False
    if any(not spacer.get('product') or not spacer.get('sealant') for spacer in spacers):
        return False
    return True


def _orgadata_spec(block, width_mm, height_mm):
    sealants = _live_materials()[2]
    spec = {
        'width': width_mm,
        'height': height_mm,
        'unit': 'mm',
        'argon': bool(block.get('argon')),
    }
    lites = block.get('lites') or []
    spacers = block.get('spacers') or []
    for index, lite in enumerate(lites[:3], 1):
        spec[f'glass{index}'] = lite.get('product') or ''
        spec[f'temper{index}'] = lite.get('temper') if lite.get('temper') in {'yes', 'n/a'} else 'n/a'
        if index <= len(spacers) and index < 3:
            chosen = spacers[index - 1]
            spacer = chosen.get('product') or ''
            spec[f'spacer{index}'] = spacer
            sealant = chosen.get('sealant') or ''
            if not sealant and spacer:
                sealant = suggest_sealant(spacer, sealants).get('selected') or ''
            spec[f'sealant{index}'] = sealant
    for index in range(len(lites) + 1, 4):
        spec.setdefault(f'glass{index}', '')
        spec.setdefault(f'temper{index}', 'n/a')
        spec.setdefault(f'spacer{index}', '')
        spec.setdefault(f'sealant{index}', '')
    return production.normalize_igu_spec(spec)


def _blank_product(value):
    return str(value or '').strip().lower() in {'', 'n/a'}


def _orgadata_active(block):
    """Lites and gaps that are still part of the unit. The first n/a lite ends it."""
    lites = list(block.get('lites') or [])
    spacers = list(block.get('spacers') or [])
    cut = len(lites)
    for index, lite in enumerate(lites):
        if index > 0 and _blank_product(lite.get('product')):
            cut = index
            break
    return lites[:cut], spacers[:max(cut - 1, 0)]


def _clear_spacer(spacer):
    spacer['product'] = ''
    spacer['sealant'] = ''
    spacer['matched'] = False
    spacer['sealant_options'] = []
    spacer['sealant_message'] = ''


def _orgadata_drop_empty_gaps(mapped, edits):
    """A lite set to n/a clears the gap in front of it. A spacer set to n/a clears its sealant.

    The rows stay on the preview so the lite or spacer can be chosen again. Pricing and
    the IGU name use only the lites that are still set.
    """
    incoming_blocks = edits.get('blocks') if isinstance(edits, dict) and isinstance(edits.get('blocks'), list) else []
    for index, block in enumerate(mapped.get('blocks') or []):
        lites = block.get('lites') or []
        spacers = block.get('spacers') or []
        posted = incoming_blocks[index] if index < len(incoming_blocks) and isinstance(incoming_blocks[index], dict) else {}
        posted_lites = posted.get('lites') if isinstance(posted.get('lites'), list) else []
        cut = len(lites)
        for lite_index, lite_in in enumerate(posted_lites):
            if lite_index == 0 or lite_index >= len(lites) or not isinstance(lite_in, dict) or 'product' not in lite_in:
                continue
            if _blank_product(lite_in.get('product')):
                cut = lite_index
                break
        if cut < len(lites):
            for lite in lites[cut:]:
                lite['product'] = ''
                lite['matched'] = False
                if lite.get('temper') not in {'yes', 'n/a'}:
                    lite['temper'] = 'n/a'
            for spacer in spacers[max(cut - 1, 0):]:
                _clear_spacer(spacer)
        posted_spacers = posted.get('spacers') if isinstance(posted.get('spacers'), list) else []
        for spacer_index, spacer_in in enumerate(posted_spacers):
            if spacer_index >= len(spacers) or not isinstance(spacer_in, dict) or 'product' not in spacer_in:
                continue
            if _blank_product(spacer_in.get('product')):
                _clear_spacer(spacers[spacer_index])
    return mapped


def _orgadata_fill_sealants(mapped):
    """Keep a chosen sealant when it fits the spacer. Otherwise use the quote-form rule."""
    sealants = _live_materials()[2]
    for block in mapped.get('blocks') or []:
        for spacer in block.get('spacers') or []:
            product = spacer.get('product') or ''
            if not product or product == 'n/a':
                spacer['sealant'] = ''
                spacer['sealant_options'] = []
                spacer['sealant_message'] = ''
                continue
            suggestion = suggest_sealant(product, sealants)
            options = list(suggestion.get('options') or [])
            posted = spacer.get('sealant') or ''
            if posted and posted in options:
                spacer['sealant'] = posted
            else:
                spacer['sealant'] = suggestion.get('selected') or ''
            spacer['sealant_options'] = options
            spacer['sealant_message'] = suggestion.get('message') or ''
            if spacer.get('product') and not spacer.get('sealant'):
                block.setdefault('warnings', [])
                block['warnings'].append(f'Choose a sealant for {spacer.get("product")}.')
    return mapped


def _pdf_overall_mm(thickness):
    match = re.search(r'=\s*(\d+(?:\.\d+)?)', str(thickness or ''))
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def _orgadata_build(block):
    """IGU name and overall thickness from the selected glass, spacers, and sealants."""
    import documents
    sample = next((line for line in (block.get('lines') or []) if line.get('width_mm') and line.get('height_mm')), None)
    spec = _orgadata_spec(
        block,
        (sample or {}).get('width_mm') or 0,
        (sample or {}).get('height_mm') or 0,
    )
    if not _orgadata_ready(block):
        return {'igu_name': '', 'computed_thickness_mm': None, 'pdf_thickness_mm': _pdf_overall_mm(block.get('thickness')), 'thickness_mismatch': False}
    total = documents.overall_thickness_mm(spec)
    pdf_mm = _pdf_overall_mm(block.get('thickness'))
    computed = int(total) if float(total).is_integer() else total
    mismatch = pdf_mm is not None and abs(float(computed) - float(pdf_mm)) > 0.01
    return {
        'igu_name': documents.build_label(spec),
        'computed_thickness_mm': computed,
        'pdf_thickness_mm': pdf_mm,
        'thickness_mismatch': mismatch,
    }


def _orgadata_aliases_from(mapped):
    glass = {}
    spacer = {}
    for block in mapped.get('blocks') or []:
        for lite in block.get('lites') or []:
            if lite.get('source') and lite.get('product'):
                glass[lite['source']] = lite['product']
        for item in block.get('spacers') or []:
            if item.get('product'):
                spacer[f'{item.get("source") or ""} {item.get("gap_mm")}'] = item['product']
    return glass, spacer


def _orgadata_preview(record, user, edits=None, locked=None):
    path = upload_abspath(record.get('stored'))
    if not path:
        return {'id': record.get('id'), 'source': orgadata_import.SOURCE_LABEL, 'error': 'The uploaded file is missing.', 'blocks': [], 'lines': []}
    try:
        parsed = orgadata_import.parse_pdf(path)
    except orgadata_import.OrgadataImportError as exc:
        return {'id': record.get('id'), 'source': orgadata_import.SOURCE_LABEL, 'error': str(exc), 'blocks': [], 'lines': []}
    requested = (edits or {}).get('customer_id') if isinstance(edits, dict) else None
    if requested in (None, ''):
        requested = record.get('customer_id')
    customer_id, customer, matches, error = _orgadata_choose_customer(user, requested, parsed.get('sender'))
    if error:
        return {'id': record.get('id'), 'source': orgadata_import.SOURCE_LABEL, 'error': error, 'blocks': [], 'lines': []}
    if customer_id and customer_id != record.get('customer_id'):
        record['customer_id'] = customer_id
        record['company'] = (customer or {}).get('name') or ''
        orgadata_import.replace_import(record.get('id'), record)
    glass_names, spacer_names = _catalog_names()
    aliases = orgadata_import.aliases_for(customer_id)
    mapped = orgadata_import.map_document(parsed, glass_names, spacer_names, aliases)
    mapped = _orgadata_apply_edits(mapped, edits or {}, glass_names, spacer_names)
    mapped = _orgadata_drop_empty_gaps(mapped, edits or {})
    mapped = _orgadata_refresh_warnings(mapped)
    mapped = _orgadata_fill_sealants(mapped)
    if isinstance(edits, dict) and edits.get('remember') and customer_id:
        glass, spacer = _orgadata_aliases_from(mapped)
        orgadata_import.remember_aliases(customer_id, glass, spacer)
    if customer and user.get('role') == 'customer' and customer_id not in matches and parsed.get('sender'):
        mapped['warnings'] = list(mapped.get('warnings') or []) + [
            f'This file is from {parsed.get("sender")}. The quote stays on your company.',
        ]
    currency = _required_currency(customer) if customer else 'usd'
    tier = _import_tier(user, (edits or {}).get('tier') if isinstance(edits, dict) else '')
    fx_rate = _current_fx()[0]
    addon = _price_factor(_percent((customer or {}).get('price_addon_percent'), 1000))
    minimum = _effective_minimum(customer)
    if isinstance(locked, dict):
        if locked.get('currency') in {'usd', 'cad'}:
            currency = locked['currency']
        if locked.get('tier'):
            tier = locked['tier']
        if locked.get('fx_rate') not in (None, ''):
            fx_rate = locked['fx_rate']
        if 'addon_percent' in locked:
            addon = _price_factor(locked.get('addon_percent'))
        if locked.get('minimum') is not None:
            minimum = locked['minimum']
    lines = []
    blocks_out = []
    for index, block in enumerate(mapped.get('blocks') or []):
        ready = _orgadata_ready(block)
        product_id = ''
        missing_id = False
        sample = None
        for line in block.get('lines') or []:
            if line.get('width_mm') and line.get('height_mm'):
                sample = line
                break
        if ready and sample:
            spec = _orgadata_spec(block, sample.get('width_mm'), sample.get('height_mm'))
            found = production.lookup_product(spec)
            product_id = str(found.get('product_id') or '').strip()
            missing_id = not product_id
            if missing_id:
                block['warnings'].append(
                    'No production ID yet. Staff can add it in Production settings. This import does not invent one.'
                )
        block_lines = []
        for line in block.get('lines') or []:
            warnings = []
            try:
                width = float(line.get('width_mm') or 0)
                height = float(line.get('height_mm') or 0)
                qty = int(line.get('qty') or 0)
            except (TypeError, ValueError):
                width = height = 0
                qty = 0
            if width <= 0 or height <= 0:
                warnings.append('Width and height must be millimeters greater than zero.')
            if qty < 1:
                warnings.append('Quantity must be a whole number.')
            priced = {}
            if ready and not warnings and tier:
                spec = _orgadata_spec(block, width, height)
                priced = _reprice(spec, qty, tier, currency, load_tiers(), fx_rate, addon, minimum)
            width_in = height_in = None
            if width > 0 and height > 0:
                _wmm, _hmm, width_in, height_in = production.dimensions_mm_in(width, height, 'mm')
            row = {
                'line_number': line.get('line_number'),
                'block': index,
                'floor': line.get('floor') or '',
                'qty': qty,
                'width_mm': width,
                'height_mm': height,
                'width_in': width_in,
                'height_in': height_in,
                'product_id': product_id if ready else '',
                'product_id_missing': missing_id if ready else False,
                'price_sqf': priced.get('price_sqf'),
                'price_pc': priced.get('price_pc'),
                'sqf': priced.get('sqf'),
                'real_sqf': priced.get('real_sqf'),
                'minimum_sqf': priced.get('minimum_sqf') or 0,
                'total': priced.get('total'),
                'warnings': warnings,
            }
            block_lines.append(row)
            lines.append(row)
        build = _orgadata_build(block)
        if build['thickness_mismatch']:
            block.setdefault('warnings', [])
            block['warnings'].append(
                f'Computed thickness {build["computed_thickness_mm"]} mm does not match the PDF thickness ({build["pdf_thickness_mm"]} mm).'
            )
        blocks_out.append({
            'name': block.get('name') or '',
            'igu_name': build['igu_name'],
            'configuration': block.get('configuration') or '',
            'thickness': block.get('thickness') or '',
            'computed_thickness_mm': build['computed_thickness_mm'],
            'pdf_thickness_mm': build['pdf_thickness_mm'],
            'thickness_mismatch': build['thickness_mismatch'],
            'spacer_source': block.get('spacer_source') or '',
            'tempered': bool(block.get('tempered')),
            'argon': bool(block.get('argon')),
            'sum_qty': block.get('sum_qty'),
            'lites': block.get('lites') or [],
            'spacers': block.get('spacers') or [],
            'product_id': product_id if ready else '',
            'product_id_missing': missing_id if ready else False,
            'warnings': block.get('warnings') or [],
            'lines': block_lines,
        })
    body = {
        'id': record.get('id'),
        'source': orgadata_import.SOURCE_LABEL,
        'filename': record.get('filename') or '',
        'sender': mapped.get('sender') or '',
        'customer_id': customer_id,
        'customer_matches': matches,
        'project': mapped.get('project') or '',
        'po': mapped.get('po') or '',
        'contact': mapped.get('contact') or '',
        'reference_date': mapped.get('reference_date') or '',
        'currency': currency,
        'tier': tier or '',
        'sum_quantity': mapped.get('sum_quantity'),
        'warnings': mapped.get('warnings') or [],
        'blocks': blocks_out,
        'lines': lines,
        'can_set_production_id': _is_ora_staff(user),
        'error': '',
    }
    if user.get('role') != 'customer':
        body['fx_rate'] = fx_rate
    return body


def _orgadata_quote_items(preview):
    items = []
    blocked = False
    for block in preview.get('blocks') or []:
        active_lites, active_spacers = _orgadata_active(block)
        if any(not lite.get('matched') for lite in active_lites):
            blocked = True
        if any(not spacer.get('matched') for spacer in active_spacers):
            blocked = True
        for line in block.get('lines') or []:
            if line.get('warnings') or line.get('total') in (None, ''):
                blocked = True
            width_in, height_in = production.dimensions_mm_in(line.get('width_mm'), line.get('height_mm'), 'mm')[2:]
            spec = _orgadata_spec(block, line.get('width_mm'), line.get('height_mm'))
            import documents
            items.append({
                'name': documents.build_label(spec) or block.get('name') or 'Glass',
                'floor': line.get('floor') or '',
                'pcs': line.get('qty'),
                'sqf': line.get('sqf'),
                'real_sqf': line.get('real_sqf') if line.get('real_sqf') not in (None, '') else line.get('sqf'),
                'minimum_sqf': line.get('minimum_sqf') or 0,
                'price_sqf': line.get('price_sqf'),
                'price_pc': line.get('price_pc'),
                'total': line.get('total'),
                'spec': spec,
                'width_in': width_in,
                'height_in': height_in,
            })
    if blocked:
        return None, 'Choose an ERP glass, spacer, and sealant for every lite before creating the quote.'
    if not items:
        return None, 'The file has no size rows.'
    return items, None


@app.route('/api/orgadata-imports', methods=['POST'])
@module_required('quotes')
def create_orgadata_import():
    user = current_user()
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return json_error('Choose a PDF.', 400)
    payload = upload.read(9 * 1024 * 1024)
    try:
        kind, ext = check_upload(upload.filename, payload)
    except OrderImportError as exc:
        return json_error(str(exc), 400)
    if kind != 'pdf' or ext != 'pdf':
        return json_error('Upload a PDF.', 400)
    stored_name = store_upload(upload.filename, payload)
    path = upload_abspath(stored_name)
    try:
        parsed = orgadata_import.parse_pdf(path)
    except orgadata_import.OrgadataImportError as exc:
        return json_error(str(exc), 400)
    customer_id, customer, _matches, error = _orgadata_choose_customer(
        user, request.form.get('customer_id'), parsed.get('sender'),
    )
    if error:
        return json_error(error, 400)
    if user.get('role') != 'customer' and not customer_id:
        return json_error('Choose a customer company.', 400)
    record = orgadata_import.add_import({
        'customer_id': customer_id,
        'company': (customer or {}).get('name') or '',
        'user': user.get('email'),
        'filename': safe_filename(upload.filename),
        'stored': stored_name,
        'sender': parsed.get('sender') or '',
        'status': 'preview',
    })
    return jsonify(_orgadata_preview(record, user))


@app.route('/api/orgadata-imports/<int:import_id>/preview', methods=['POST'])
@module_required('quotes')
def preview_orgadata_import(import_id):
    user = current_user()
    record = _orgadata_owned(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body() or {}
    return jsonify(_orgadata_preview(record, user, data))


@app.route('/api/orgadata-imports/<int:import_id>/quote', methods=['POST'])
@module_required('quotes')
def quote_from_orgadata(import_id):
    user = current_user()
    record = _orgadata_owned(user, import_id)
    if not record:
        return json_error('Not found', 404)
    data = json_body() or {}
    data = dict(data)
    data['remember'] = True if data.get('remember', True) else False
    preview = _orgadata_preview(record, user, data)
    if preview.get('error'):
        return json_error(preview['error'], 400)
    if not preview.get('customer_id'):
        return json_error('Choose a customer company.', 400)
    items, error = _orgadata_quote_items(preview)
    if error:
        return json_error(error, 400)
    filename = record.get('filename') or ''
    _stamp_import_lines(items, filename)
    customer = load_customers().get(preview['customer_id'])
    tier = preview.get('tier')
    if not tier:
        return json_error('That pricing tier is not available.', 400)
    contact = clean_text(preview.get('contact'), 200)
    notes = f'Person in Charge: {contact}' if contact else ''
    discount, addon = _saved_or_company_terms(None, customer)
    quote = {
        'date': date.today().isoformat(),
        'time': datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%H:%M'),
        'user': user.get('email'),
        'company': (customer or {}).get('name') or record.get('company') or '',
        'customer_id': preview['customer_id'],
        'project': clean_text(preview.get('project'), 200),
        'po': clean_text(preview.get('po'), 80),
        'reference_date': clean_text(preview.get('reference_date'), 10),
        'notes': notes,
        'items': items,
        'currency': preview.get('currency') or 'usd',
        'fx_rate': _current_fx()[0],
        'type': 'size',
        'tier': tier,
        'status': 'draft',
        'source': orgadata_import.SOURCE_LABEL,
        'source_file': record.get('stored') or '',
        'source_filename': filename,
        'source_files': [{'filename': filename, 'stored': record.get('stored') or ''}],
        'import_kind': 'orgadata',
        'orgadata_import_id': record.get('id'),
    }
    _stamp_terms(quote, items, discount, addon)
    quote.update(_canada_tax_fields(customer, None))
    stored = append_quote(quote)
    _remember_igus(stored.get('items'), stored.get('date'))
    record['status'] = 'quoted'
    record['quote_id'] = stored['id']
    record['customer_id'] = preview['customer_id']
    orgadata_import.replace_import(record['id'], record)
    shown = _quote_for_viewer(_with_quote_tax(stored), user)
    return jsonify({'success': True, 'id': stored['id'], 'source': orgadata_import.SOURCE_LABEL, 'quote': shown})


_CHARGE_KINDS = {'irregular_surcharge', 'dwg_creation'}


def _import_kind_of(quote):
    if not isinstance(quote, dict):
        return ''
    kind = quote.get('import_kind')
    if kind in {'orgadata', 'order'}:
        return kind
    if quote.get('source') == orgadata_import.SOURCE_LABEL:
        return 'orgadata'
    if quote.get('import_id'):
        return 'order'
    return ''


def _customer_invoiced_reason(order):
    if not order:
        return ''
    state, _label = _customer_invoice_state(order)
    if state == 'invoiced' or order.get('invoice_id') or order.get('customer_invoice_id'):
        return 'This order has been invoiced.'
    return ''


def _append_state(quote, orders=None):
    kind = _import_kind_of(quote)
    if not kind:
        return {'import_kind': '', 'can_append_file': False, 'append_blocked': ''}
    order = None
    order_id = (quote or {}).get('order_id')
    if order_id:
        rows = orders if orders is not None else load_orders()
        order = next((row for row in rows if row.get('id') == order_id), None)
        if order is None and orders is not None and len(orders) == 1 and orders[0].get('id') == order_id:
            order = orders[0]
    elif orders is not None and len(orders) == 1 and not quote:
        order = orders[0]
    reason = _customer_invoiced_reason(order)
    return {'import_kind': kind, 'can_append_file': not reason, 'append_blocked': reason}


def _source_filenames(quote):
    names = []
    for entry in (quote or {}).get('source_files') or []:
        if isinstance(entry, dict) and entry.get('filename') and entry['filename'] not in names:
            names.append(entry['filename'])
    filename = (quote or {}).get('source_filename') or ''
    if filename and filename not in names:
        names.insert(0, filename)
    return names


def _stamp_import_lines(items, filename):
    number = 1
    for item in items or []:
        if not isinstance(item, dict) or item.get('kind') in _CHARGE_KINDS:
            continue
        item['line_number'] = number
        item['source_filename'] = filename
        number += 1
    return items


def _next_import_line_number(items, default_filename=''):
    glass = [
        item for item in items or []
        if isinstance(item, dict) and item.get('kind') not in _CHARGE_KINDS
    ]
    if any(not item.get('line_number') for item in glass):
        number = 1
        for item in glass:
            item['line_number'] = number
            if default_filename and not item.get('source_filename'):
                item['source_filename'] = default_filename
            number += 1
        return number
    highest = 0
    for item in glass:
        try:
            highest = max(highest, int(item.get('line_number') or 0))
        except (TypeError, ValueError):
            pass
    return highest + 1


def _number_new_lines(items, filename, start):
    number = start
    for item in items or []:
        if not isinstance(item, dict) or item.get('kind') in _CHARGE_KINDS:
            continue
        item['line_number'] = number
        item['source_filename'] = filename
        number += 1
    return items


def _remember_source_file(quote, filename, stored):
    files = [dict(entry) for entry in (quote.get('source_files') or []) if isinstance(entry, dict)]
    if not files and (quote.get('source_filename') or quote.get('source_file')):
        files.append({
            'filename': quote.get('source_filename') or '',
            'stored': quote.get('source_file') or '',
        })
    files.append({'filename': filename or '', 'stored': stored or ''})
    quote['source_files'] = files


def _quote_pricing_lock(quote, customer):
    addon = quote.get('price_addon_percent') if isinstance(quote, dict) and 'price_addon_percent' in quote else (customer or {}).get('price_addon_percent')
    return {
        'tier': (quote or {}).get('tier') or '',
        'currency': str((quote or {}).get('currency') or 'usd').lower(),
        'fx_rate': _stored_fx(quote),
        'addon_percent': addon,
        'minimum': _effective_minimum(customer),
    }


def _items_from_order_import_lines(lines, tier, currency, fx_rate):
    built = []
    for line in lines or []:
        fresh = dict(line)
        fresh['errors'] = [error for error in (fresh.get('errors') or []) if error]
        _price_import_line(fresh, tier, currency, fx_rate)
        if fresh.get('total') in (None, ''):
            return None, 'Match every name before adding these lines.'
        spec = _import_spec(fresh)
        size = f"{_trim_num(fresh['width'])} x {_trim_num(fresh['height'])} in"
        makeup = production.makeup_from_spec(spec) if fresh.get('igu') else ''
        if makeup and not fresh.get('argon'):
            makeup = makeup.replace(' with Argon', '')
        if makeup:
            parts = [fresh.get('mark') or '', makeup, size]
        else:
            parts = [fresh.get('mark') or '', fresh.get('glass_product') or '']
            if fresh.get('spacer_product'):
                parts.append(fresh['spacer_product'])
            parts.append(size)
        built.append({
            'name': ' - '.join(part for part in parts if part),
            'sqf': fresh.get('sqf'),
            'real_sqf': fresh.get('real_sqf', fresh.get('sqf')),
            'minimum_sqf': fresh.get('minimum_sqf') or 0,
            'price_sqf': fresh.get('price_sqf'),
            'price_pc': fresh.get('price_pc'),
            'total': fresh.get('total'),
            'pcs': fresh.get('qty'),
            'spec': spec,
            'floor': fresh.get('mark') or '',
            'source_glass': fresh.get('glass_name') or '',
            'irregular_shape': bool(fresh.get('irregular_shape') or fresh.get('irregular')),
        })
    return expand_irregular_lines(built, load_pricing_settings()['irregular_shape_surcharge']), None


def _order_lines_for_appended(quote_items, order):
    customer = load_customers().get(order.get('customer_id') or '') or {}
    tiers = load_tiers()
    currency = str(order.get('currency') or 'usd').lower()
    tier = order.get('tier')
    factor = _price_factor(order.get('price_addon_percent'))
    minimum = _effective_minimum(customer)
    rows = []
    for item in quote_items or []:
        if not isinstance(item, dict) or item.get('kind') in _CHARGE_KINDS:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
        if not _usable_size_spec(spec):
            return None, 'A line in this file cannot be priced on the order.'
        priced = _reprice(
            spec, item.get('pcs'), tier, currency, tiers, _stored_fx(order), factor, minimum,
        )
        line = {
            'name': item.get('name') or '',
            'spec': spec,
            'irregular_shape': bool(item.get('irregular_shape')),
            'floor': item.get('floor') or '',
            'source_filename': item.get('source_filename') or '',
            'line_number': item.get('line_number'),
        }
        if item.get('line_id'):
            line['line_id'] = item.get('line_id')
        if item.get('surcharge_rate') not in (None, ''):
            line['surcharge_rate'] = item.get('surcharge_rate')
        line.update(_size_price_fields(priced))
        rows.append(line)
    return expand_irregular_lines(rows, load_pricing_settings()['irregular_shape_surcharge']), None


def _retotal_order(order):
    summary, error = price_summary(
        order.get('items') or [],
        order.get('discount') or {'type': 'percent', 'value': 0},
        order.get('extra_charges') or [],
    )
    if error:
        return error
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
    import tax_numbers
    tax_lines = tax_numbers.tax_breakdown(
        order.get('ship_province') or '', summary['total'],
        order.get('pst_number') or '', order.get('qst_number') or '',
    )
    tax_due = tax_numbers.tax_total(tax_lines)
    order['igu_subtotal'] = summary['igu_subtotal']
    order['discount_amount'] = summary['discount_amount']
    order['igu_total'] = summary['igu_total']
    order['extras_total'] = summary['extras_total']
    order['additional_discount_percent'] = summary['additional_discount_percent']
    order['additional_discount_amount'] = summary['additional_discount_amount']
    order['pretax_total'] = summary['total']
    order['tax_lines'] = tax_lines
    order['tax_total'] = tax_due
    order['total'] = round(summary['total'] + tax_due, 2)
    return None


def _save_appended_lines(quote, new_items, filename, stored, user):
    customer = load_customers().get(quote.get('customer_id') or '') or {}
    start = _next_import_line_number(quote.get('items') or [], quote.get('source_filename') or '')
    numbered = _number_new_lines(new_items, filename, start)
    added = sum(1 for item in numbered if isinstance(item, dict) and item.get('kind') not in _CHARGE_KINDS)
    updated = dict(quote)
    updated['items'] = list(quote.get('items') or []) + numbered
    updated['import_kind'] = _import_kind_of(quote)
    discount, addon = _saved_or_company_terms(updated, customer)
    _stamp_terms(updated, updated['items'], discount, addon)
    _remember_source_file(updated, filename, stored)
    saved_order = None
    order_id = quote.get('order_id')
    if order_id:
        current = next((row for row in load_orders() if row.get('id') == order_id), None)
        if current:
            reason = _customer_invoiced_reason(current)
            if reason:
                return None, None, reason
            extra, error = _order_lines_for_appended(numbered, current)
            if error:
                return None, None, error
            preview = dict(current)
            preview['items'] = list(current.get('items') or []) + extra
            if _retotal_order(preview):
                return None, None, 'The new lines could not be priced.'
            if user.get('role') == 'customer':
                check = conversion_allowed(
                    preview.get('total'), customer, str(preview.get('currency') or 'usd').lower(),
                    load_orders(), load_invoices(), load_payments(),
                )
                if not check['allowed']:
                    return None, None, credit_message(check)

            def mutator(row):
                blocked = _customer_invoiced_reason(row)
                if blocked:
                    return None, blocked
                row['items'] = list(row.get('items') or []) + extra
                failed = _retotal_order(row)
                if failed:
                    return None, failed
                _stamp_log(row, user, [{
                    'field': 'lines',
                    'action': 'added',
                    'new': f'{added} from {filename}',
                }])
                return row, None

            saved_order, error = update_order(order_id, mutator)
            if error:
                return None, None, error
            if order_status(saved_order) in RELEASED_STATUSES:
                _write_production_file(saved_order)
    saved_quote = replace_quote(quote['id'], updated)
    if not saved_quote:
        return None, None, 'Not found'
    _remember_igus(saved_quote.get('items'), saved_quote.get('date'), (saved_order or {}).get('id'))
    return saved_quote, saved_order, None


def _append_response(quote, order, user, added):
    body = {
        'success': True,
        'id': quote.get('id'),
        'added': added,
        'quote': _with_quote_tax(_quote_for_viewer(quote, user)),
    }
    if order:
        body['order'] = order_for_viewer(order, user)
    return jsonify(body)


def _glass_added(items):
    return sum(1 for item in items or [] if isinstance(item, dict) and item.get('kind') not in _CHARGE_KINDS)


def _orgadata_review(record, preview):
    glass, spacers = _catalog_names()
    return {
        'needs_review': True,
        'import_id': record.get('id'),
        'import_kind': 'orgadata',
        'filename': record.get('filename') or '',
        'error': preview.get('error') or '',
        'warnings': preview.get('warnings') or [],
        'blocks': preview.get('blocks') or [],
        'glass': glass,
        'spacers': spacers,
    }


def _order_import_review(presented):
    glass, spacers = _catalog_names()
    return {
        'needs_review': True,
        'import_id': presented.get('id'),
        'import_kind': 'order',
        'filename': presented.get('filename') or '',
        'status': presented.get('status') or '',
        'error': presented.get('error') or '',
        'lines': presented.get('lines') or [],
        'unmatched_glass': presented.get('unmatched_glass') or [],
        'unmatched_spacer': presented.get('unmatched_spacer') or [],
        'templates': presented.get('templates') or [],
        'glass': glass,
        'spacers': spacers,
    }


def _order_template_for(quote, record=None):
    customer_id = quote.get('customer_id')
    for raw in (quote.get('import_template_id'), (record or {}).get('template_id')):
        try:
            template_id = int(raw or 0)
        except (TypeError, ValueError):
            template_id = 0
        if template_id:
            found = _find_template(template_id, customer_id)
            if found:
                return found
    original = next((row for row in load_imports() if row.get('id') == quote.get('import_id')), None)
    if original and original.get('template_id'):
        return _find_template(int(original['template_id']), customer_id)
    return None


def _finish_orgadata_append(quote, user, record, edits):
    customer = load_customers().get(quote.get('customer_id') or '') or {}
    locked = _quote_pricing_lock(quote, customer)
    preview = _orgadata_preview(record, user, edits, locked)
    if preview.get('error'):
        return json_error(preview['error'], 400)
    items, error = _orgadata_quote_items(preview)
    if error:
        return jsonify(_orgadata_review(record, preview))
    saved, order, error = _save_appended_lines(
        quote, items, record.get('filename') or '', record.get('stored') or '', user,
    )
    if error:
        return json_error(error, 400)
    record['status'] = 'quoted'
    record['quote_id'] = quote.get('id')
    orgadata_import.replace_import(record.get('id'), record)
    return _append_response(saved, order, user, _glass_added(items))


def _finish_order_append(quote, user, record, presented):
    if presented.get('status') != 'preview':
        presented = dict(presented)
        presented['filename'] = presented.get('filename') or record.get('filename') or ''
        return jsonify(_order_import_review(presented))
    customer = load_customers().get(quote.get('customer_id') or '') or {}
    locked = _quote_pricing_lock(quote, customer)
    items, error = _items_from_order_import_lines(
        presented.get('lines') or [], locked['tier'], locked['currency'], locked['fx_rate'],
    )
    if error:
        return json_error(error, 400)
    saved, order, error = _save_appended_lines(
        quote, items, record.get('filename') or '', record.get('stored') or '', user,
    )
    if error:
        return json_error(error, 400)
    record['status'] = 'quoted'
    record['quote_id'] = quote.get('id')
    replace_import(record['id'], record)
    return _append_response(saved, order, user, _glass_added(items))


def _owned_append_import(user, quote, import_id):
    kind = _import_kind_of(quote)
    if kind == 'orgadata':
        return _orgadata_owned(user, import_id), 'orgadata'
    record = _owned_import(user, import_id)
    return record, 'order'


@app.route('/api/quotes/<int:quote_id>/import-file', methods=['POST'])
@login_required
def append_imported_file(quote_id):
    user = current_user()
    if not (user_has_module(user, 'quotes') or user_has_module(user, 'quote-history')):
        return json_error('You do not have access to that.', 403)
    quote = _visible_quote(user, quote_id)
    if not quote:
        return json_error('Not found', 404)
    if not _import_kind_of(quote):
        return json_error('This quote did not come from an imported file.', 400)
    if quote.get('type') not in (None, '', 'size'):
        return json_error('Only per-size quotes can take another file.', 400)
    linked = None
    if quote.get('order_id'):
        linked = next((row for row in load_orders() if row.get('id') == quote.get('order_id')), None)
    blocked = _customer_invoiced_reason(linked)
    if blocked:
        return json_error(blocked, 400)
    if request.is_json:
        data = json_body() or {}
        try:
            import_id = int(data.get('import_id') or 0)
        except (TypeError, ValueError):
            import_id = 0
        record, kind = _owned_append_import(user, quote, import_id)
        if not record:
            return json_error('Not found', 404)
        if kind == 'orgadata':
            edits = dict(data)
            edits['customer_id'] = quote.get('customer_id')
            edits['remember'] = True if data.get('remember', True) else False
            return _finish_orgadata_append(quote, user, record, edits)
        template = _order_template_for(quote, record)
        requested = data.get('template_id')
        if requested:
            template = _find_template(int(requested or 0), quote.get('customer_id')) or template
        if not template:
            return json_error('Choose a template for this company.', 400)
        glass = data.get('glass') if isinstance(data.get('glass'), dict) else {}
        spacer = data.get('spacer') if isinstance(data.get('spacer'), dict) else {}
        if glass or spacer:
            glass_names, spacer_names = _catalog_names()
            valid_glass = {str(source): _known_product(product, glass_names) for source, product in glass.items()}
            valid_spacer = {str(source): _known_product(product, spacer_names) for source, product in spacer.items()}
            if any(source and not product for source, product in list(valid_glass.items()) + list(valid_spacer.items())):
                return json_error('Each name has to match one of our products.', 400)
            if data.get('remember', True):
                remember_mappings(record.get('customer_id'), valid_glass, valid_spacer)
                extra_glass, extra_spacer = None, None
            else:
                extra_glass, extra_spacer = valid_glass, valid_spacer
        else:
            extra_glass = extra_spacer = None
        presented = _present_import(
            record, template, user, str(quote.get('currency') or 'usd').lower(),
            extra_glass, extra_spacer, quote.get('tier'),
        )
        return _finish_order_append(quote, user, record, presented)

    upload = request.files.get('file')
    if not upload or not upload.filename:
        return json_error('Choose a file.', 400)
    payload = upload.read(9 * 1024 * 1024)
    try:
        kind_name, ext = check_upload(upload.filename, payload)
    except OrderImportError as exc:
        return json_error(str(exc), 400)
    kind = _import_kind_of(quote)
    if kind == 'orgadata':
        if kind_name != 'pdf' or ext != 'pdf':
            return json_error('Upload a PDF.', 400)
        stored_name = store_upload(upload.filename, payload)
        path = upload_abspath(stored_name)
        try:
            parsed = orgadata_import.parse_pdf(path)
        except orgadata_import.OrgadataImportError as exc:
            return json_error(str(exc), 400)
        customer = load_customers().get(quote.get('customer_id') or '') or {}
        record = orgadata_import.add_import({
            'customer_id': quote.get('customer_id'),
            'company': customer.get('name') or quote.get('company') or '',
            'user': user.get('email'),
            'filename': safe_filename(upload.filename),
            'stored': stored_name,
            'sender': parsed.get('sender') or '',
            'status': 'preview',
            'quote_id': quote.get('id'),
        })
        return _finish_orgadata_append(quote, user, record, {
            'customer_id': quote.get('customer_id'),
            'remember': True,
        })

    stored_name = store_upload(upload.filename, payload)
    customer = load_customers().get(quote.get('customer_id') or '') or {}
    record = add_import({
        'customer_id': quote.get('customer_id'),
        'company': customer.get('name') or quote.get('company') or '',
        'user': user.get('email'),
        'filename': safe_filename(upload.filename),
        'stored': stored_name,
        'status': 'uploaded',
        'template_id': quote.get('import_template_id'),
        'project': quote.get('project') or '',
    })
    template = _order_template_for(quote, record)
    if not template:
        templates = [row for row in load_templates() if row.get('customer_id') == quote.get('customer_id') and row.get('active', True)]
        path = upload_abspath(stored_name)
        chosen, options, reason = choose_template(templates, upload.filename, ext, path)
        if reason == 'needs_template':
            record['status'] = 'needs_template'
            replace_import(record['id'], record)
            return json_error('No template can read this file.', 400)
        if reason == 'choose':
            record['status'] = 'choose_template'
            replace_import(record['id'], record)
            return jsonify(_order_import_review({
                'id': record['id'],
                'status': 'choose_template',
                'filename': record.get('filename') or '',
                'templates': [public_template(row) for row in options],
                'lines': [],
            }))
        template = chosen
    presented = _present_import(
        record, template, user, str(quote.get('currency') or 'usd').lower(),
        None, None, quote.get('tier'),
    )
    if presented.get('status') == 'failed':
        return json_error(presented.get('error') or 'The file could not be read.', 400)
    return _finish_order_append(quote, user, record, presented)


@app.route('/api/quotes/<int:quote_id>/source-pdf', methods=['GET'])
@module_required('quotes')
def download_orgadata_pdf(quote_id):
    user = current_user()
    quote = _visible_quote(user, quote_id)
    if not quote or quote.get('source') != orgadata_import.SOURCE_LABEL:
        return json_error('Not found', 404)
    path = upload_abspath(quote.get('source_file'))
    if not path:
        return json_error('The PDF is missing.', 404)
    return send_file(path, as_attachment=True, download_name=quote.get('source_filename') or 'orgadata-glass-order.pdf')


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


def _clock(value):
    """HH:MM:SS from a quote time or an order timestamp, so 9:05 sorts before 10:00."""
    text = str(value or '').strip()
    if 'T' in text:
        text = text.split('T', 1)[1]
    match = re.match(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', text)
    if not match:
        return ''
    hour, minute, second = int(match.group(1)), match.group(2), match.group(3) or '00'
    return f'{hour:02d}:{minute}:{second}'


def _newest_key(record):
    """Date, then time, then id. Reverse this to list the newest record first."""
    approved = str(record.get('approved_at') or '')
    day = str(record.get('date') or '')[:10] or approved[:10]
    clock = _clock(record.get('time')) or _clock(approved)
    try:
        record_id = int(record.get('id') or 0)
    except (TypeError, ValueError):
        record_id = 0
    return (day, clock, record_id)


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
        if project:
            name = str(quote.get('project') or '').lower()
            number = str(quote_display_number(quote)).lower()
            if project not in name and project not in number:
                continue
        quote_date = str(quote.get('date') or '')
        if date_from and quote_date < date_from:
            continue
        if date_to and quote_date > date_to:
            continue
        rows.append(quote)
    if sort in {'total_asc', 'total_desc'}:
        rows.sort(key=lambda row: (quote_total(row), row.get('id') or 0), reverse=sort == 'total_desc')
    else:
        rows.sort(key=_newest_key, reverse=True)
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
        if not status and order_status(order) == 'voided':
            continue
        if status in {'packed', 'partial'} and order.get('pack_status') != status:
            continue
        if status == 'remake' and (not order.get('remake') or order_status(order) == 'voided'):
            continue
        if status in STATUS_LABELS and order_status(order) != status:
            continue
        invoice_state = clean_text(request.args.get('invoice'), 20).lower()
        if invoice_state == 'not_invoiced' and order.get('invoice_state') == 'invoiced':
            continue
        if invoice_state in {'ready', 'pending', 'partial', 'invoiced'} and order.get('invoice_state') != invoice_state:
            continue
        rows.append(order)
    if sort in {'total_asc', 'total_desc'}:
        rows.sort(key=lambda row: (order_total_value(row), row.get('id') or 0), reverse=sort == 'total_desc')
    else:
        rows.sort(key=_newest_key, reverse=True)
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
    orders = load_orders()
    for quote in quotes_visible_to(user, load_quotes()):
        row = _quote_for_viewer(quote, user, orders)
        # The quote keeps the tier and prices it was created with. Do not reprice it.
        if row.get('order_id') and not row.get('order_status'):
            row['order_status'] = 'Approved for order'
        linked = next((item for item in orders if item.get('id') == row.get('order_id')), None)
        if linked:
            row['order_status'] = _order_review_label(linked)
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
    """Customers see every order for their company. Staff see every order."""
    if user.get('role') == 'customer':
        customer_id = user.get('customer_id')
        return [order for order in orders if order.get('customer_id') == customer_id]
    return list(orders)


def format_pacific(value):
    """Show an approval timestamp in Pacific time, for example 2026-11-18 7:37 AM PT."""
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(ZoneInfo('America/Los_Angeles'))
    hour = local.strftime('%I').lstrip('0') or '12'
    return f"{local:%Y-%m-%d} {hour}:{local:%M} {local:%p} PT"


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


def _scrub_shown_text(value):
    """Remove copy-history sentences from text that a screen or PDF will show."""
    if isinstance(value, str):
        return strip_copy_history(value)
    if isinstance(value, dict):
        return {key: _scrub_shown_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_shown_text(item) for item in value]
    return value


def _shown_order_line(item):
    row = production.present_order_line(item)
    if isinstance(row.get('remarks'), str):
        row['remarks'] = strip_copy_history(row.get('remarks'))
    return row


def _shown_order_items(items):
    import documents
    rows = [_shown_order_line(item) if isinstance(item, dict) else item for item in items or []]
    return documents.describe_items(rows)


def _packing_lines_for(order, entry):
    import packing
    catalog = {row['serial']: row for row in packing.serial_catalog(order)}
    lines = []
    for line in (entry or {}).get('lines') or []:
        try:
            serial = int(line.get('serial'))
        except (TypeError, ValueError):
            continue
        info = catalog.get(serial)
        if not info:
            continue
        rack = str(line.get('rack') or (entry or {}).get('rack') or '').strip()
        lines.append({
            'serial': serial,
            'floor': info['floor'],
            'product': info['product'],
            'size': info['size'],
            'qty': line.get('qty') or 0,
            'rack': rack,
        })
    return lines


def _intercompany_invoiced_by_serial(order):
    linked = {
        row.get('id') for row in load_invoices()
        if row.get('order_id') == order.get('id') and row.get('kind') == 'intercompany' and not row.get('voided')
    }
    totals = {}
    for shipment in order.get('shipments') or []:
        if shipment.get('invoice_id') not in linked:
            continue
        for line in shipment.get('lines') or []:
            try:
                serial = int(line.get('serial'))
                qty = int(line.get('qty') or 0)
            except (TypeError, ValueError):
                continue
            totals[serial] = totals.get(serial, 0) + qty
    return totals


def _invoice_documents(order, kind):
    rows = []
    for row in load_invoices():
        if row.get('order_id') != order.get('id') or row.get('voided'):
            continue
        if document_type(row) != 'invoice':
            continue
        row_kind = row.get('kind') or 'customer'
        if kind == 'intercompany':
            if row_kind == 'intercompany':
                rows.append(row)
        elif row_kind != 'intercompany':
            rows.append(row)
    return rows


def _invoiced_qty_by_serial(order, kind):
    """Pieces already on a live invoice of this kind, keyed by production serial."""
    totals = {}
    blanket = False
    for row in _invoice_documents(order, kind):
        glass = [
            line for line in (row.get('items') or [])
            if line.get('kind') not in {'irregular_surcharge', 'dwg_creation'}
        ]
        numbered = False
        for line in glass:
            try:
                serial = int(line.get('serial'))
                qty = int(line.get('pcs') or line.get('qty') or 0)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                numbered = True
                totals[serial] = totals.get(serial, 0) + qty
        if glass and not numbered:
            blanket = True
    if blanket:
        import packing
        for row in packing.serial_catalog(order):
            totals[row['serial']] = max(totals.get(row['serial'], 0), int(row.get('ordered') or 0))
    if kind == 'intercompany':
        for serial, qty in _intercompany_invoiced_by_serial(order).items():
            totals[serial] = max(totals.get(serial, 0), qty)
    return totals


def _needs_intercompany(order):
    import quickbooks
    customer = _order_customer(order)
    return bool(quickbooks.is_canadian(customer) or shipments.is_open_windows(customer))


def _unbilled_packed(order, kind):
    """Packed pieces that are not yet on an invoice of this kind. Index to quantity."""
    import packing
    if not (order or {}).get('packing_lists'):
        return {}
    billed = _invoiced_qty_by_serial(order, kind)
    chosen = {}
    for row in packing.packing_progress(order)['serials']:
        left = max(0, int(row.get('packed') or 0) - int(billed.get(row['serial'], 0)))
        if left > 0:
            chosen[row['index']] = left
    return chosen


def _scale_addon(item, ratio):
    """A line add-on follows its glass quantity, so a partial invoice takes the same share."""
    if ratio <= 0:
        return None
    scaled = dict(item)
    amount = round(money(item.get('total')) * ratio, 2)
    if amount <= 0:
        return None
    scaled['total'] = amount
    scaled['price_pc'] = amount
    scaled['unit_price'] = amount
    return scaled


def _limit_to_unbilled(order, items, kind):
    """Drop invoice lines whose serials are already billed, so a piece is never billed twice."""
    import packing
    billed = _invoiced_qty_by_serial(order, kind)
    catalog = {row['serial']: row for row in packing.serial_catalog(order)}
    kept = []
    pending = []
    last_ratio = None

    def flush(ratio):
        for charge in pending:
            scaled = _scale_addon(charge, ratio)
            if scaled:
                kept.append(scaled)
        pending.clear()

    for item in items or []:
        if item.get('kind') in _CHARGE_KINDS:
            pending.append(item)
            continue
        if pending:
            flush(0 if last_ratio is None else last_ratio)
        try:
            serial = int(item.get('serial'))
            pcs = int(item.get('pcs') or 0)
        except (TypeError, ValueError):
            kept.append(item)
            last_ratio = 1
            continue
        ordered = int((catalog.get(serial) or {}).get('ordered') or pcs)
        left = max(0, ordered - int(billed.get(serial, 0)))
        take = min(pcs, left)
        if take <= 0:
            last_ratio = 0
            continue
        ratio = take / pcs if pcs else 0
        if take == pcs:
            kept.append(item)
        else:
            scaled = dict(item)
            scaled['pcs'] = take
            scaled['qty'] = take
            scaled['total'] = round(money(item.get('total')) * ratio, 2)
            scaled['sqf'] = round(float(item.get('sqf') or 0) * ratio, 2)
            kept.append(scaled)
        last_ratio = ratio
    if pending:
        flush(0 if last_ratio is None else last_ratio)
    return kept


def _customer_invoice_state(order):
    """Ready as soon as packed pieces are not yet invoiced. Invoiced only when every piece is billed."""
    if (order or {}).get('legacy'):
        return 'not_invoiced', 'Not invoiced'
    if order.get('remake') and not order.get('charge_customer'):
        return 'not_invoiced', 'Not charged'
    if not order.get('packing_lists'):
        has_customer_invoice = bool(_invoice_documents(order, 'customer')) or bool(order.get('customer_invoice_id'))
        if has_customer_invoice:
            return 'invoiced', 'Invoiced'
        if shipments.shipment_progress(order)['fully_shipped']:
            return 'ready', 'Ready to invoice'
        return 'not_invoiced', 'Not invoiced'
    import packing
    progress = packing.packing_progress(order)
    customer_billed = _invoiced_qty_by_serial(order, 'customer')
    canada = _needs_intercompany(order)
    intercompany_billed = _invoiced_qty_by_serial(order, 'intercompany') if canada else {}
    fully_customer = all(int(customer_billed.get(row['serial'], 0)) >= int(row['ordered']) for row in progress['serials'])
    fully_intercompany = (not canada) or all(
        int(intercompany_billed.get(row['serial'], 0)) >= int(row['ordered']) for row in progress['serials']
    )
    if progress['serials'] and fully_customer and fully_intercompany:
        return 'invoiced', 'Invoiced'
    customer_waiting = any(int(row['packed']) > int(customer_billed.get(row['serial'], 0)) for row in progress['serials'])
    intercompany_waiting = canada and any(
        int(row['packed']) > int(intercompany_billed.get(row['serial'], 0)) for row in progress['serials']
    )
    if customer_waiting or intercompany_waiting:
        ic_caught_up = customer_waiting and not intercompany_waiting
        if ic_caught_up and (
            order.get('customer_invoice_deferred') or _invoice_documents(order, 'intercompany')
        ):
            return 'pending', 'Customer invoice pending'
        return 'ready', 'Ready to invoice'
    if int(progress['totals'].get('bo') or 0) > 0:
        return 'partial', 'Partially invoiced'
    return 'not_invoiced', 'Not invoiced'


def _can_invoice_packed(order):
    if (order or {}).get('legacy') or not (order or {}).get('packing_lists'):
        return False
    if order.get('remake') and not order.get('charge_customer'):
        return False
    if _unbilled_packed(order, 'customer'):
        return True
    return bool(_needs_intercompany(order) and _unbilled_packed(order, 'intercompany'))


def _known_rack_numbers():
    import racks as rack_book
    return {str(number) for number in (rack_book.load_rows() or {})}


def _rack_ref(number, known):
    text = str(number or '').strip()
    if not text:
        return None
    return {'number': text, 'known': text in known}


def _packing_payload(order, staff):
    import packing
    progress = packing.packing_progress(order)
    known = _known_rack_numbers()
    billed = _invoiced_qty_by_serial(order, 'intercompany')
    customer_billed = _invoiced_qty_by_serial(order, 'customer')
    racks_by_serial = {}
    for entry in order.get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        for line in entry.get('lines') or []:
            try:
                serial = int(line.get('serial'))
            except (TypeError, ValueError):
                continue
            ref = _rack_ref(line.get('rack') or entry.get('rack'), known)
            if not ref:
                continue
            bucket = racks_by_serial.setdefault(serial, [])
            if all(item['number'] != ref['number'] for item in bucket):
                bucket.append(ref)
    for row in progress['serials']:
        row['intercompany_invoiced'] = billed.get(row['serial'], 0)
        row['customer_invoiced'] = customer_billed.get(row['serial'], 0)
        row['shipped'] = shipments.shipped_qty(order, row['index'])
        row['racks'] = racks_by_serial.get(row['serial'], [])
    current = []
    history = []
    for entry in order.get('packing_lists') or []:
        lines = _packing_lines_for(order, entry)
        for line in lines:
            line['rack_known'] = bool(line.get('rack')) and line['rack'] in known
        shown = {
            'id': entry.get('id'),
            'number': entry.get('number') or '',
            'rack': entry.get('rack') or '',
            'rack_known': bool(str(entry.get('rack') or '').strip()) and str(entry.get('rack')).strip() in known,
            'date': entry.get('date') or '',
            'filename': entry.get('filename') or '',
            'uploaded_at': entry.get('uploaded_at') or '',
            'lines': lines,
        }
        if staff:
            shown['uploaded_by'] = entry.get('uploaded_by') or ''
            shown['unmatched'] = entry.get('unmatched') or []
        if entry.get('current', True):
            current.append(shown)
        elif staff:
            history.append(shown)
    payload = {
        'pack_status': progress['status'],
        'pack_status_label': progress['status_label'],
        'packing_totals': progress['totals'],
        'packing_serials': progress['serials'],
        'packing_lists': current,
    }
    if staff:
        payload['packing_history'] = history
    return payload


def _can_create_remake(user):
    """Admin, sales, and production can send a service order. Customers cannot."""
    return bool(user) and user.get('role') in {'admin', 'sales', 'production'}


def _remake_number(parent, orders):
    label = str((parent or {}).get('number') or (parent or {}).get('id') or '')
    prefix = f'{label}-R'
    taken = []
    for order in orders or []:
        if order.get('remake_of') != (parent or {}).get('id'):
            continue
        suffix = str(order.get('number') or '')
        if suffix.startswith(prefix) and suffix[len(prefix):].isdigit():
            taken.append(int(suffix[len(prefix):]))
    return f'{prefix}{max(taken or [0]) + 1}'


def _remake_view(order):
    orders = load_orders()
    parent = next((row for row in orders if row.get('id') == order.get('remake_of')), None)
    import packing
    import shipments as shipment_rules
    children = []
    for other in orders:
        if other.get('remake_of') != order.get('id'):
            continue
        progress = packing.packing_progress(other)
        shipped = shipment_rules.shipment_progress(other)
        if shipped['fully_shipped']:
            ship_label = 'Shipped'
        elif shipped['shipped_pcs'] > 0:
            ship_label = 'Partially shipped'
        else:
            ship_label = 'Not shipped'
        children.append({
            'id': other.get('id'),
            'number': other.get('number') or str(other.get('id') or ''),
            'status': order_status(other),
            'status_label': _order_review_label(other),
            'charge_customer': bool(other.get('charge_customer')),
            'reason': other.get('remake_reason') or '',
            'pack_status': progress['status'],
            'pack_status_label': progress['status_label'] or 'Not packed',
            'ship_status_label': ship_label,
            'packed': progress['totals']['packed'],
            'bo': progress['totals']['bo'],
            'shipped': shipped['shipped_pcs'],
        })
    children.sort(key=lambda row: row.get('number') or '')
    return {
        'number': order.get('number') or '',
        'remake': bool(order.get('remake')),
        'remake_of': order.get('remake_of'),
        'remake_of_number': (parent or {}).get('number') or (str(parent.get('id')) if parent else ''),
        'remake_reason': order.get('remake_reason') or '',
        'charge_customer': bool(order.get('charge_customer')) if order.get('remake') else None,
        'remakes': children,
    }


def _remake_line(source, qty, serial, reason):
    item = json.loads(json.dumps(source))
    try:
        ordered = int(item.get('pcs') or 0)
    except (TypeError, ValueError):
        ordered = 0
    each = item.get('price_pc')
    if each in (None, ''):
        each = item.get('unit_price')
    if each in (None, '') and ordered:
        each = money(item.get('total')) / ordered
    each = money(each)
    item['pcs'] = qty
    item['qty'] = qty
    item['price_pc'] = each
    item['unit_price'] = each
    item['total'] = round(each * qty, 2)
    item['source_serial'] = serial
    if reason:
        existing = strip_copy_history(item.get('remarks') or '')
        item['remarks'] = f'{existing} {reason}'.strip() if existing else reason
    return item


def _public_drawings(order):
    numbers = {}
    for item in (order or {}).get('items') or []:
        if isinstance(item, dict) and item.get('line_id') and item.get('line_number') not in (None, ''):
            numbers[str(item.get('line_id'))] = item.get('line_number')
    rows = []
    for drawing in (order or {}).get('drawings') or []:
        if not isinstance(drawing, dict):
            continue
        number = drawing.get('line_number')
        if number in (None, ''):
            number = numbers.get(str(drawing.get('line_id') or ''))
        rows.append({
            'line_id': drawing.get('line_id'),
            'line_number': number if number not in (None, '') else '',
            'filename': drawing.get('filename') or '',
            'is_dwg': bool(drawing.get('is_dwg')),
            'uploaded_at': drawing.get('uploaded_at') or '',
        })
    return rows


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
        'legacy': bool(order.get('legacy')),
        'project': order.get('project') or '',
        'po': order.get('po') or order.get('customer_po') or '',
        'job_address': order.get('job_address') or '',
        'notes': strip_copy_history(order.get('notes') or ''),
        'approved_by': order.get('approved_by'),
        'converted_by': order.get('converted_by') or order.get('approved_by') or '',
        'approved_at': order.get('approved_at'),
        'approved_at_display': format_pacific(order.get('approved_at')),
        'date': order.get('date'),
        'currency': order.get('currency') or 'usd',
        'tier': order.get('tier'),
        'tier_label': tier_label(order.get('tier')) or order.get('tier_label'),
        'status': status,
        'status_label': _order_review_label(order),
        'voided_by': order.get('voided_by') or '',
        'voided_at': order.get('voided_at') or '',
        'voided_at_display': format_pacific(order.get('voided_at')),
        'void_reason': order.get('void_reason') or '',
        'can_void': _can_void(user, order),
        **_remake_view(order),
        **_review_payload(order, user),
        'invoice_id': order.get('invoice_id'),
        'invoice_number': order.get('invoice_number'),
        'drawings': _public_drawings(order),
        'ship_province': order.get('ship_province') or '',
        'tax_label': order.get('tax_label') or '',
        'tax_lines': order.get('tax_lines') or [],
        'tax_total': order.get('tax_total') or 0,
        'pretax_total': order.get('pretax_total') if order.get('pretax_total') not in (None, '') else order.get('total'),
        'seller_gst_number': order.get('seller_gst_number') or '',
        'documents': [],
    }
    staff_view = user.get('role') != 'customer'
    if staff_view and order.get('legacy'):
        base['reassign_orders'] = [
            {'id': row.get('id'), 'project': row.get('project') or '', 'company': row.get('company') or ''}
            for row in load_orders()
            if row.get('customer_id') == order.get('customer_id') and not row.get('legacy') and row.get('id') != order.get('id')
            and str(row.get('status') or '').lower() != 'delivered'
        ]
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
            document['fx_date'] = _shown_fx_date(row)
            document['fx_source'] = row.get('fx_source') or ''
        documents.append(document)
    base['documents'] = documents
    base['pst_number'] = order.get('pst_number') or ''
    base['qst_number'] = order.get('qst_number') or ''
    base.update(_packing_payload(order, user.get('role') != 'customer'))
    invoice_state, invoice_label = _customer_invoice_state(order)
    base['invoice_state'] = invoice_state
    base['invoice_state_label'] = invoice_label
    base['needs_intercompany'] = _needs_intercompany(order)
    base['can_invoice_packed'] = _can_invoice_packed(order)
    linked_quote = None
    if order.get('quote_id'):
        linked_quote = next((row for row in load_quotes() if row.get('id') == order.get('quote_id')), None)
    base.update(_append_state(linked_quote or {}, [order]))
    base['source_filenames'] = _source_filenames(linked_quote or {})
    if not _order_prices_visible(user):
        items = []
        for item in _shown_order_items(order.get('items') or []):
            spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
            items.append({
                'name': item.get('name') or '',
                'sqf': item.get('sqf'),
                'pcs': item.get('pcs'),
                'line_number': item.get('line_number') or '',
                'source_filename': item.get('source_filename') or '',
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
        base.update({'items': items, 'changelog': _scrub_shown_text(changelog), 'prices_visible': False})
        return base
    view = dict(base)
    shown_items = _shown_order_items(order.get('items') or [])
    if user.get('role') == 'customer':
        shown_items = [
            {key: value for key, value in item.items() if not str(key).startswith('internal_')}
            if isinstance(item, dict) else item
            for item in shown_items
        ]
    view['items'] = shown_items
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
    view['changelog'] = _scrub_shown_text(list(order.get('changelog') or []))
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
            remark = strip_copy_history(clean_text(item.get('remarks'), 500))
            if remark:
                line['remarks'] = remark
        floor = production.floor_code(item)
        if floor:
            line['floor'] = clean_text(floor, 80)
        if item.get('line_id'):
            line['line_id'] = item.get('line_id')
        if item.get('source_filename'):
            line['source_filename'] = item.get('source_filename')
        line['line_number'] = len(priced_items) + 1
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
    ship = _ship_province(data.get('ship_province'), customer)
    tax = _canada_tax_fields(customer, ship)
    snapshot = _tax_number_snapshot(customer)
    import tax_numbers
    tax_lines = tax_numbers.tax_breakdown(
        tax.get('ship_province') or ship, commercial['total'],
        snapshot['pst_number'], snapshot['qst_number'],
    )
    tax_due = tax_numbers.tax_total(tax_lines)
    amount_due = round(commercial['total'] + tax_due, 2)
    credit_check = conversion_allowed(
        amount_due, customer, str(currency).lower(), load_orders(), load_invoices(), load_payments(),
    )
    warning = None
    if not credit_check['allowed']:
        message = credit_message(credit_check)
        if user.get('role') == 'customer':
            return json_error(message, 400)
        warning = message
    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    order = append_order({
        'quote_id': quote_id,
        'customer_id': customer_id,
        'company': company,
        'project': quote.get('project') or '',
        'po': quote.get('po') or quote.get('customer_po') or '',
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
        'pretax_total': commercial['total'],
        'tax_lines': tax_lines,
        'tax_total': tax_due,
        'total': amount_due,
        'dwg_rate': pricing['dwg_creation_fee'] if irregular_lines else None,
        'ship_province': tax.get('ship_province') or ship,
        'qbo_tax_code': tax.get('qbo_tax_code') or '',
        'tax_label': tax.get('tax_label') or '',
        'pst_number': snapshot['pst_number'],
        'qst_number': snapshot['qst_number'],
        'seller_gst_number': tax_numbers.seller_gst_number() if tax else '',
        'show_seller_gst': bool(tax),
        'drawings': [],
        'status': 'submitted' if user.get('role') == 'customer' else 'approved',
        'changelog': [],
        'revisions': [],
        'revision': 0,
        'customer_note': '',
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
            record['line_number'] = line.get('line_number')
            record['uploaded_by'] = user.get('email') or ''
            saved_drawings.append(record)
        def remember(row):
            row['drawings'] = saved_drawings
            return row, None
        update_order(order['id'], remember)
        order['drawings'] = saved_drawings
    _remember_igus(order.get('items'), order.get('date'), order.get('id'))
    if order.get('status') in RELEASED_STATUSES:
        _write_production_file(order)
    updated = dict(quote)
    updated['order_id'] = order['id']
    updated['order_status'] = (
        'Submitted - awaiting Ora Glass review'
        if order.get('status') == 'submitted' else 'Approved for order'
    )
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
    if order_status(order) not in RELEASED_STATUSES:
        return json_error(
            'This order is not approved for production yet. The Excel file is available after Ora Glass approves it for production.',
            409,
        )
    import currency_rules
    rate, _effective = currency_rules.rate_on(order.get('date'))
    payload = production.workbook_bytes(order.get('items') or [], order.get('currency'), rate)
    filename = production.production_filename(order)
    try:
        production.write_order_workbook(order)
    except Exception:
        logger.exception('Could not refresh the stored production Excel file')
    response = send_file(
        BytesIO(payload),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )
    response.headers['Content-Disposition'] = production.content_disposition(filename)
    return response


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
        buffer, mimetype='application/pdf', as_attachment=False,
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
    previous = next(
        (item for item in order.get('drawings') or [] if str(item.get('line_id')) == str(line_id)),
        None,
    )
    if previous and previous.get('line_number') not in (None, ''):
        record['line_number'] = previous.get('line_number')
    else:
        match = next(
            (item for item in order.get('items') or [] if str(item.get('line_id')) == str(line_id)),
            None,
        )
        if match and match.get('line_number') not in (None, ''):
            record['line_number'] = match.get('line_number')

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
    orders = load_orders()
    order = next((row for row in orders_visible_to(user, orders) if row.get('id') == order_id), None)
    if not order:
        return json_error('Not found', 404)
    import documents
    if documents.sync_item_names(order.get('items'), only_mismatch=True):
        save_orders(orders)
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
        spec = _fill_open_sealants(production.normalize_igu_spec(spec))
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
        import documents
        name = documents.igu_name_from_spec(spec) or clean_text(raw_item.get('name'), 200)
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
            remark = strip_copy_history(clean_text(raw_item.get('remarks'), 500))
            if remark:
                line['remarks'] = remark
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


def _orders_awaiting_staff(user):
    """Orders a customer submitted, or re-approved, that Ora Glass has not released."""
    if not user or not staff_can_order(user):
        return 0
    return sum(
        1 for order in load_orders()
        if order_status(order) in {'submitted', 'reapproved'} and order.get('review_for') != 'customer'
    )


def _migrate_igu_names():
    """Stored lines whose thickness prefix does not match the spec are rebuilt once."""
    import documents
    quotes = load_quotes()
    changed = False
    for quote in quotes:
        if documents.sync_item_names(quote.get('items'), only_mismatch=True):
            changed = True
    if changed:
        save_quotes(quotes)
        logger.info('Rebuilt IGU names on saved quotes so the thickness matches the spec.')
    orders = load_orders()
    changed = False
    for order in orders:
        if documents.sync_item_names(order.get('items'), only_mismatch=True):
            changed = True
    if changed:
        save_orders(orders)
        logger.info('Rebuilt IGU names on saved orders so the thickness matches the spec.')


def _migrate_order_one_review():
    """Order 1 stays Approved when a production file already exists. Otherwise it waits for review."""
    orders = load_orders()
    order = next((row for row in orders if row.get('id') == 1), None)
    if not order or order_status(order) != 'approved':
        return {'order': 1, 'action': 'unchanged'}
    folder = os.path.join(data_dir(), 'production_exports')
    exported = os.path.isfile(os.path.join(folder, production.production_filename(order)))
    if exported:
        logger.info('Order 1 already has a production Excel file, so it stays Approved.')
        return {'order': 1, 'action': 'kept', 'reason': 'exported'}
    order['status'] = 'submitted'
    save_orders(orders)
    logger.info('Order 1 was Approved and had not been exported. It now awaits Ora Glass review.')
    return {'order': 1, 'action': 'submitted'}


def _line_brief(item):
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
    parts = [str(item.get('name') or spec.get('glass1') or 'Line')]
    glass = spec.get('glass1') or ''
    if glass and glass not in parts[0]:
        parts.append(str(glass))
    if spec.get('width') not in (None, ''):
        parts.append(f"{spec.get('width')} x {spec.get('height')} {spec.get('unit') or ''}".strip())
    if item.get('pcs') not in (None, ''):
        parts.append(f"qty {item.get('pcs')}")
    floor = item.get('floor') or item.get('mark') or ''
    if floor:
        parts.append(f"floor {floor}")
    if item.get('remarks'):
        parts.append(str(item.get('remarks')))
    return ' · '.join(parts)


def _revision_lines(before_items, after_items):
    rows = []
    old_items = [item for item in before_items or [] if isinstance(item, dict)]
    new_items = [item for item in after_items or [] if isinstance(item, dict)]
    shared = min(len(old_items), len(new_items))
    for index in range(shared):
        old, new = old_items[index], new_items[index]
        if _line_brief(old) == _line_brief(new) and money(old.get('total')) == money(new.get('total')):
            continue
        rows.append({
            'name': new.get('name') or old.get('name') or 'Line',
            'before': _line_brief(old),
            'after': _line_brief(new),
            'old_total': money(old.get('total')),
            'new_total': money(new.get('total')),
        })
    for item in old_items[shared:]:
        rows.append({'name': item.get('name') or 'Line', 'before': _line_brief(item), 'after': 'Removed', 'old_total': money(item.get('total')), 'new_total': 0})
    for item in new_items[shared:]:
        rows.append({'name': item.get('name') or 'Line', 'before': '', 'after': _line_brief(item), 'old_total': 0, 'new_total': money(item.get('total'))})
    return rows


def _retax_snapshot(order):
    """Recalculate Canada tax from the numbers stored on the order. USD orders are unchanged."""
    import tax_numbers
    pretax = order.get('pretax_total')
    if pretax in (None, ''):
        pretax = order.get('total')
    if not order.get('show_seller_gst') and not order.get('tax_lines'):
        order['pretax_total'] = round(float(pretax or 0), 2)
        order['total'] = order['pretax_total']
        return order
    lines = tax_numbers.tax_breakdown(
        order.get('ship_province'), pretax, order.get('pst_number'), order.get('qst_number'),
    )
    order['tax_lines'] = lines
    order['tax_total'] = tax_numbers.tax_total(lines)
    order['pretax_total'] = round(float(pretax or 0), 2)
    order['total'] = round(order['pretax_total'] + order['tax_total'], 2)
    return order


def _technical_prices(original, updated):
    """Keep the customer's line prices. The recalculated price is stored for staff only."""
    priced = list(updated.get('items') or [])
    old_items = [
        item for item in (original.get('items') or [])
        if isinstance(item, dict) and item.get('kind') not in {'irregular_surcharge', 'dwg_creation'}
    ]
    internal = 0.0
    kept = []
    money_keys = ('sqf', 'real_sqf', 'minimum_sqf', 'price_sqf', 'price_pc', 'total')
    glass_index = 0
    for item in priced:
        row = dict(item)
        if row.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            kept.append(row)
            continue
        internal += money(row.get('total'))
        row['internal_total'] = money(row.get('total'))
        row['internal_price_sqf'] = row.get('price_sqf')
        if glass_index < len(old_items):
            source = old_items[glass_index]
            for key in money_keys:
                if key in source:
                    row[key] = source[key]
        else:
            row['price_sqf'] = 0
            row['price_pc'] = 0
            row['total'] = 0
        glass_index += 1
        kept.append(row)
    updated['items'] = kept
    for key in (
        'igu_subtotal', 'igu_total', 'discount_amount', 'additional_discount_amount',
        'extras_total', 'pretax_total', 'tax_lines', 'tax_total', 'total', 'discount', 'extra_charges',
    ):
        if key in original:
            updated[key] = original[key]
    customer_base = money(original.get('pretax_total') if original.get('pretax_total') not in (None, '') else original.get('total'))
    updated['internal_total'] = round(internal, 2)
    updated['internal_price_difference'] = round(internal - customer_base, 2)
    return updated


def _append_revision(order, user, kind, lines, price_choice='', note='', comment='', old_total=None, new_total=None):
    revisions = list(order.get('revisions') or [])
    number = int(order.get('revision') or 0)
    if kind == 'staff_edit':
        number += 1
    entry = {
        'number': number,
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'user': user.get('email') or '',
        'kind': kind,
        'price_choice': price_choice,
        'note': note,
        'comment': comment,
        'old_total': None if old_total is None else money(old_total),
        'new_total': None if new_total is None else money(new_total),
        'internal_total': order.get('internal_total'),
        'internal_price_difference': order.get('internal_price_difference'),
        'lines': lines,
    }
    revisions.append(entry)
    order['revisions'] = revisions
    order['revision'] = number
    return entry


def _review_payload(order, user):
    staff = user.get('role') != 'customer'
    prices = _order_prices_visible(user)
    revisions = []
    for rev in order.get('revisions') or []:
        if not isinstance(rev, dict):
            continue
        lines = []
        for line in rev.get('lines') or []:
            shown = {
                'name': line.get('name') or '',
                'before': line.get('before') or '',
                'after': line.get('after') or '',
            }
            if prices or staff:
                if rev.get('price_choice') != 'technical':
                    shown['old_total'] = line.get('old_total')
                    shown['new_total'] = line.get('new_total')
            lines.append(shown)
        shown_rev = {
            'number': rev.get('number') or 0,
            'at': rev.get('at') or '',
            'user': rev.get('user') or '',
            'kind': rev.get('kind') or '',
            'note': rev.get('note') or '',
            'comment': rev.get('comment') or '',
            'price_choice': rev.get('price_choice') or '',
            'lines': lines,
        }
        if prices and rev.get('price_choice') == 'apply':
            shown_rev['old_total'] = rev.get('old_total')
            shown_rev['new_total'] = rev.get('new_total')
        if staff:
            shown_rev['old_total'] = rev.get('old_total')
            shown_rev['new_total'] = rev.get('new_total')
            shown_rev['internal_total'] = rev.get('internal_total')
            shown_rev['internal_price_difference'] = rev.get('internal_price_difference')
        revisions.append(shown_rev)
    latest = next((rev for rev in reversed(revisions) if rev.get('kind') == 'staff_edit'), None)
    status = order_status(order)
    payload = {
        'customer_note': order.get('customer_note') or '',
        'customer_comment': order.get('customer_comment') or '',
        'price_choice': order.get('price_choice') or '',
        'revision': order.get('revision') or 0,
        'revisions': revisions,
        'change_summary': latest if status == 'customer_review' else None,
        'excel_ready': status in RELEASED_STATUSES and not order.get('legacy'),
        'can_release': staff and status in {'submitted', 'reapproved'},
        'can_unapprove': bool(staff and _can_unapprove(order)),
        'can_respond': bool(can_place_production_orders(user) and status == 'customer_review'),
        'can_approve_for_customer': user.get('role') == 'admin' and status == 'customer_review',
        'behalf_note': order.get('behalf_note') or '',
        'behalf_by': order.get('behalf_by') or '',
    }
    if staff:
        payload['internal_price_difference'] = order.get('internal_price_difference')
        payload['internal_total'] = order.get('internal_total')
    return payload


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
    updated['notes'] = strip_copy_history(clean_text(data.get('notes'), 2000) if 'notes' in data else (order.get('notes') or ''))
    if 'ship_province' in data:
        from storage import PROVINCE_CODES
        chosen = clean_text(data.get('ship_province'), 8).upper()
        updated['ship_province'] = chosen if chosen in PROVINCE_CODES else ''
    updated.update(summary)
    updated['changelog'] = list(order.get('changelog') or [])
    return updated, None


def _order_is_invoiced(order):
    return bool(order.get('invoice_id')) or order_status(order) == 'delivered'


def _log_is_staff_unapprove(entry):
    """A changelog row written by unapprove before review_for existed.

    The revision kind was already staff_unapprove. The log only recorded the
    status falling back to Submitted from Approved or Sent to production.
    A customer sending the order back uses a different previous status.
    """
    for change in (entry or {}).get('changes') or []:
        if not isinstance(change, dict) or change.get('field') != 'status':
            continue
        new = str(change.get('new') or '')
        old = str(change.get('old') or '')
        if 'unapprov' in new.casefold():
            return True
        if new == STATUS_LABELS['submitted'] and old in {
            STATUS_LABELS['approved'], STATUS_LABELS['sent'], 'Approved for production',
        }:
            return True
    return False


def _returned_to_customer(order):
    """A submitted order staff unapproved, including ones saved before review_for existed."""
    if order_status(order) != 'submitted':
        return False
    marker = (order or {}).get('review_for')
    if marker == 'customer':
        return True
    if marker == 'staff':
        return False
    revisions = [row for row in (order or {}).get('revisions') or [] if isinstance(row, dict)]
    if revisions:
        return revisions[-1].get('kind') == 'staff_unapprove'
    logs = [row for row in (order or {}).get('changelog') or [] if isinstance(row, dict)]
    if not logs:
        return False
    return _log_is_staff_unapprove(logs[-1])


def _stamp_unapproved_customer_review(orders):
    """Remember the customer-review flag the first time an older unapprove is read."""
    changed = False
    for order in orders or []:
        if not isinstance(order, dict) or order.get('review_for'):
            continue
        if _returned_to_customer(order):
            order['review_for'] = 'customer'
            changed = True
    return changed


def load_orders():
    orders = _load_stored_orders()
    if _stamp_unapproved_customer_review(orders):
        save_orders(orders)
    return orders


def _order_review_label(order):
    """Name the company when the order is back with that customer. Ora Glass only when it is waiting here."""
    status = order_status(order)
    if status == 'voided':
        return 'Voided'
    if _returned_to_customer(order):
        company = str((order or {}).get('company') or '').strip() or 'Customer'
        return f'{company} review'
    return STATUS_LABELS.get(status, status)


def _void_block_reason(order):
    status = order_status(order)
    if status == 'voided':
        return 'This order is already voided.'
    if order.get('remake'):
        if status in {'made', 'delivered'}:
            return 'This order cannot be voided because it has been sent to production.'
    elif status in {'sent', 'made', 'delivered'}:
        return 'This order cannot be voided because it has been sent to production.'
    if order.get('shipments'):
        return 'This order cannot be voided because it has a shipment.'
    state, _label = _customer_invoice_state(order)
    if state == 'invoiced' or order.get('invoice_id') or order.get('customer_invoice_id'):
        return 'This order cannot be voided because it has been invoiced.'
    if order.get('packing_lists'):
        return 'This order cannot be voided because a packing list is attached.'
    import packing
    if packing.packing_progress(order)['totals'].get('packed'):
        return 'This order cannot be voided because pieces have been packed.'
    return ''


def _can_void(user, order):
    if not user or not order or order_status(order) == 'voided':
        return False
    if user.get('role') == 'customer':
        if user.get('customer_id') != order.get('customer_id'):
            return False
    elif user.get('role') not in {'admin', 'sales'}:
        return False
    return _void_block_reason(order) == ''


def _can_unapprove(order):
    """Approved or sent orders can return to review until something has shipped or been invoiced."""
    if order.get('remake') or order.get('legacy'):
        return False
    if _order_is_invoiced(order) or (order.get('shipments') or []):
        return False
    return order_status(order) in {'approved', 'sent'}


def _needs_customer_reapproval(order):
    return order_status(order) in REVIEW_STATUSES or _can_unapprove(order)


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
    if order_status(visible) == 'voided':
        return json_error('This order is voided and cannot be changed.', 400)
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
        if _needs_customer_reapproval(order):
            choice = clean_text(data.get('price_choice'), 20)
            note = strip_copy_history(clean_text(data.get('customer_note'), 2000))
            if choice not in {'apply', 'technical'}:
                return None, 'Choose whether the new price applies to the customer.'
            if not note:
                return None, 'Write a note to the customer explaining the change.'
            old_total = money(order.get('total'))
            if choice == 'technical':
                _technical_prices(order, updated)
            else:
                updated.pop('internal_price_difference', None)
                updated.pop('internal_total', None)
                priced_pretax = updated.get('total')
                updated['pretax_total'] = priced_pretax
                _retax_snapshot(updated)
            updated['status'] = 'customer_review'
            updated['customer_note'] = note
            updated['price_choice'] = choice
            lines = _revision_lines(order.get('items'), updated.get('items'))
            _append_revision(
                updated, user, 'staff_edit', lines, choice, note,
                old_total=old_total, new_total=updated.get('total'),
            )
            changes = diff_changes(order, updated)
            changes.append({'field': 'customer_note', 'new': note})
            changes.append({'field': 'price_choice', 'new': choice})
            _stamp_log(updated, user, changes)
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


@app.route('/api/orders/<int:order_id>/approve-production', methods=['POST'])
@module_required('orders')
def approve_order_for_production(order_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to approve an order for production.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)

    def mutator(order):
        current = order_status(order)
        if current == 'voided':
            return None, 'This order is voided and cannot be changed.'
        if current not in {'submitted', 'reapproved'}:
            return None, f'An order that is {STATUS_LABELS[current]} cannot be approved for production.'
        updated = dict(order)
        updated['status'] = 'approved'
        updated.pop('review_for', None)
        updated['production_approved_by'] = user.get('email') or ''
        updated['production_approved_at'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _append_revision(updated, user, 'staff_approve', [], note='Approved for production')
        _stamp_log(updated, user, [{
            'field': 'status',
            'old': STATUS_LABELS[current],
            'new': 'Approved for production',
        }])
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    _write_production_file(saved)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/unapprove', methods=['POST'])
@module_required('orders')
def unapprove_order(order_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to unapprove an order.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)

    def mutator(order):
        current = order_status(order)
        if not _can_unapprove(order):
            return None, f'An order that is {STATUS_LABELS.get(current, current)} cannot be unapproved.'
        updated = dict(order)
        updated['status'] = 'submitted'
        updated['review_for'] = 'customer'
        company = (order.get('company') or 'the customer').strip() or 'the customer'
        _append_revision(updated, user, 'staff_unapprove', [], note=f'Unapproved. Back to {company} review.')
        _stamp_log(updated, user, [{
            'field': 'status',
            'old': STATUS_LABELS[current],
            'new': _order_review_label(updated),
        }])
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/void', methods=['POST'])
@module_required('orders')
def void_order(order_id):
    user = current_user()
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    if user.get('role') == 'customer':
        if user.get('customer_id') != visible.get('customer_id'):
            return json_error('Not found', 404)
    elif user.get('role') not in {'admin', 'sales'}:
        return json_error('You do not have permission to void this order.', 403)
    data = json_body() or {}
    reason = strip_copy_history(clean_text(data.get('reason'), 500))

    def mutator(order):
        blocked = _void_block_reason(order)
        if blocked:
            return None, blocked
        updated = dict(order)
        updated['status'] = 'voided'
        updated['voided_by'] = user.get('email') or ''
        updated['voided_at'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        updated['void_reason'] = reason
        note = 'Voided.' if not reason else f'Voided. {reason}'
        _append_revision(updated, user, 'void', [], note=note)
        _stamp_log(updated, user, [{
            'field': 'status',
            'old': _order_review_label(order),
            'new': 'Voided',
        }])
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/remake', methods=['POST'])
@module_required('orders')
def create_remake(order_id):
    """A service order for pieces that have to be made again. No new quote."""
    user = current_user()
    if not _can_create_remake(user):
        return json_error('You do not have permission to create a remake.', 403)
    parent = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not parent:
        return json_error('Not found', 404)
    if order_status(parent) == 'voided':
        return json_error('This order is voided.', 400)
    if order_status(parent) not in RELEASED_STATUSES:
        return json_error('A remake starts from a production order.', 400)
    data = json_body() or {}
    if 'charge_customer' not in data or not isinstance(data.get('charge_customer'), bool):
        return json_error('Choose whether to charge the customer.', 400)
    charge = data.get('charge_customer')
    reason = strip_copy_history(clean_text(data.get('reason'), 300))
    requested = data.get('lines')
    if not isinstance(requested, list) or not requested:
        return json_error('Select at least one glass line.', 400)
    import packing
    catalog = {row['serial']: row for row in packing.serial_catalog(parent)}
    chosen = {}
    for row in requested:
        if not isinstance(row, dict):
            return json_error('Each remake line needs a serial and a quantity.', 400)
        try:
            serial = int(row.get('serial'))
            qty = int(row.get('qty') or 0)
        except (TypeError, ValueError):
            return json_error('Remake quantity must be a whole number.', 400)
        if serial not in catalog:
            return json_error(f'Serial {serial} is not on this order.', 400)
        if serial in chosen:
            return json_error(f'Serial {serial} can only be entered once.', 400)
        ordered = int(catalog[serial]['ordered'] or 0)
        if qty < 1 or qty > ordered:
            return json_error(f'Remake quantity for serial {serial} must be from 1 to {ordered}.', 400)
        chosen[serial] = qty
    items = []
    for serial, qty in sorted(chosen.items()):
        source = (parent.get('items') or [])[catalog[serial]['index']]
        items.append(_remake_line(source, qty, serial, reason))
    discount = parent.get('discount') or {'type': 'percent', 'value': 0}
    if discount.get('type') != 'percent':
        discount = {'type': 'percent', 'value': 0}
    summary, error = price_summary(items, discount, [])
    if error:
        return json_error(error, 400)
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, parent.get('additional_discount_percent'))
    orders = load_orders()
    number = _remake_number(parent, orders)
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    record = {
        'remake': True,
        'number': number,
        'remake_of': parent.get('id'),
        'remake_reason': reason,
        'charge_customer': charge,
        'quote_id': None,
        'customer_id': parent.get('customer_id'),
        'company': parent.get('company') or '',
        'project': parent.get('project') or '',
        'po': parent.get('po') or parent.get('customer_po') or '',
        'job_address': parent.get('job_address') or '',
        'notes': f'Remake of Order {parent.get("number") or parent.get("id")}.' + (f' {reason}' if reason else ''),
        'currency': parent.get('currency') or 'usd',
        'fx_rate': parent.get('fx_rate'),
        'tier': parent.get('tier'),
        'tier_label': parent.get('tier_label') or '',
        'items': items,
        'discount': discount,
        'discount_amount': summary.get('discount_amount') or 0,
        'additional_discount_percent': summary.get('additional_discount_percent') or 0,
        'additional_discount_amount': summary.get('additional_discount_amount') or 0,
        'price_addon_percent': parent.get('price_addon_percent') or 0,
        'extra_charges': [],
        'igu_subtotal': summary.get('igu_subtotal') or 0,
        'igu_total': summary.get('igu_total') or 0,
        'extras_total': 0,
        'pretax_total': summary.get('total') or 0,
        'tax_lines': [],
        'tax_total': 0,
        'total': summary.get('total') or 0,
        'ship_province': parent.get('ship_province') or '',
        'qbo_tax_code': parent.get('qbo_tax_code') or '',
        'tax_label': parent.get('tax_label') or '',
        'pst_number': parent.get('pst_number') or '',
        'qst_number': parent.get('qst_number') or '',
        'seller_gst_number': parent.get('seller_gst_number') or '',
        'show_seller_gst': bool(parent.get('show_seller_gst')),
        'drawings': [],
        'status': 'sent',
        'changelog': [],
        'revisions': [],
        'revision': 0,
        'shipments': [],
        'packing_lists': [],
        'approved_by': user.get('email') or '',
        'converted_by': user.get('email') or '',
        'approved_at': created_at,
        'date': date.today().isoformat(),
    }
    if not charge:
        record['total'] = 0
        record['pretax_total'] = 0
        record['igu_subtotal'] = 0
        record['igu_total'] = 0
        record['discount_amount'] = 0
        record['additional_discount_amount'] = 0
    _stamp_log(record, user, [{
        'field': 'remake',
        'action': 'added',
        'new': number,
    }])
    saved = append_order(record)
    try:
        production.write_order_workbook(saved)
    except Exception:
        logger.exception('Could not write the remake production Excel file')
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/customer-decision', methods=['POST'])
@module_required('orders')
def customer_order_decision(order_id):
    user = current_user()
    if not can_place_production_orders(user):
        return json_error('You are not allowed to approve production changes.', 403)
    data = json_body() or {}
    action = clean_text(data.get('action'), 20)
    if action not in {'approve', 'reject'}:
        return json_error('Choose approve or reject.', 400)
    comment = strip_copy_history(clean_text(data.get('comment'), 2000))
    if action == 'reject' and not comment:
        return json_error('Write a comment for Ora Glass.', 400)
    visible = _owned_order(user, order_id)
    if not visible:
        return json_error('Not found', 404)

    def mutator(order):
        if user.get('customer_id') != order.get('customer_id'):
            return None, 'Not found'
        if order_status(order) != 'customer_review':
            return None, 'This order is not waiting for customer approval.'
        updated = dict(order)
        if action == 'approve':
            updated['status'] = 'reapproved'
            updated['customer_comment'] = ''
            _append_revision(updated, user, 'customer_approve', [], note='Approved the changes')
            _stamp_log(updated, user, [{
                'field': 'status',
                'old': STATUS_LABELS['customer_review'],
                'new': STATUS_LABELS['reapproved'],
            }])
        else:
            updated['status'] = 'submitted'
            updated['review_for'] = 'staff'
            updated['customer_comment'] = comment
            _append_revision(updated, user, 'customer_reject', [], comment=comment)
            _stamp_log(updated, user, [
                {'field': 'status', 'old': STATUS_LABELS['customer_review'], 'new': STATUS_LABELS['submitted']},
                {'field': 'comment', 'new': comment},
            ])
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/approve-for-customer', methods=['POST'])
@module_required('orders')
def approve_order_for_customer(order_id):
    """An admin records a customer's approval when it was given outside the ERP."""
    user = current_user()
    if user.get('role') != 'admin':
        return json_error('Only an administrator can approve on behalf of the customer.', 403)
    data = json_body() or {}
    staff_note = strip_copy_history(clean_text(data.get('note'), 500))
    if not staff_note:
        return json_error('Write a short note, such as who approved by phone.', 400)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    who = user.get('email') or 'admin'
    phrase = f'Approved on behalf of customer by {who}'

    def mutator(order):
        if order_status(order) != 'customer_review':
            return None, 'This order is not waiting for customer approval.'
        updated = dict(order)
        updated['status'] = 'reapproved'
        updated['behalf_note'] = staff_note
        updated['behalf_by'] = who
        _append_revision(
            updated, user, 'admin_approve_for_customer', [],
            note=phrase, comment=staff_note,
        )
        _stamp_log(updated, user, [
            {
                'field': 'status',
                'old': STATUS_LABELS['customer_review'],
                'new': STATUS_LABELS['reapproved'],
            },
            {'field': 'note', 'new': f'{phrase}. {staff_note}'},
        ])
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
    if order_status(visible) == 'voided':
        return json_error('This order is voided and cannot be changed.', 400)

    def mutator(order):
        current = order_status(order)
        if current == 'voided':
            return None, 'This order is voided and cannot be changed.'
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
            delivered_on = date.today().isoformat()
            stamped = []
            for shipment in updated.get('shipments') or []:
                recorded = dict(shipment) if isinstance(shipment, dict) else {}
                if str(recorded.get('status') or '').lower() != 'delivered':
                    recorded['status'] = 'delivered'
                    recorded['delivered_on'] = recorded.get('delivered_on') or delivered_on
                stamped.append(recorded)
            updated['shipments'] = stamped
            if updated.get('remake') and not updated.get('charge_customer'):
                changes.append({'field': 'invoice', 'action': 'kept', 'new': 'This remake is not charged to the customer.'})
            elif updated.get('legacy'):
                changes.append({'field': 'invoice', 'action': 'kept', 'new': 'Legacy jobs are not invoiced.'})
            elif updated.get('shipments'):
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
    if target == 'delivered' and saved:
        import racks as rack_book
        rack_book.deliver_order(saved, user)
        if saved.get('invoice_id'):
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
    serial_by_index = {row['index']: row['serial'] for row in shipments.glass_rows(order)}
    if order.get('packing_lists'):
        packed_left = shipments.packed_quantities_to_ship(order)
        for index, qty in quantities.items():
            allowed = packed_left.get(index, 0)
            if qty > allowed:
                return None, (
                    f'Only {allowed} packed pieces of serial {serial_by_index.get(index)} '
                    'are left to ship. The customer invoice is not required.'
                )
    weight = clean_text(data.get('weight'), 40)
    preview_lines = [
        {'index': index, 'qty': qty, 'serial': serial_by_index.get(index)}
        for index, qty in quantities.items()
    ]
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
        'rack': clean_text(data.get('rack'), 80),
        'truck_plate': clean_text(data.get('truck_plate'), 40),
        'notes': clean_text(data.get('notes'), 300),
        'weight': weight,
        'lines': preview_lines,
        'pcs': pcs,
        'sqf': sqf,
        'total': summary.get('total'),
        'items': built,
        'invoiced': False,
        'packing_list': stored_name,
        'created_by': user.get('email') or '',
        'status': 'delivered' if str(order.get('status') or '').lower() == 'delivered' else 'shipped',
    }
    if shipment['status'] == 'delivered':
        shipment['delivered_on'] = shipped_on
    mode = _billing_mode(order)
    if not order.get('legacy') and mode != 'us_full':
        _number, error = _issue_shipment_invoice(order, shipment, user, finishing)
        if error:
            return None, error
    order.setdefault('shipments', []).append(shipment)
    if not order.get('legacy'):
        error = _close_shipment_billing(order, user)
        if error:
            return None, error
    if shipment.get('status') == 'delivered':
        import racks as rack_book
        rack_book.deliver_order(order, user)
    return order, None


def _packing_inbox_path():
    return os.path.join(data_dir(), 'packing_inbox.json')


def _load_packing_inbox():
    from storage import lock_for, read_json
    path = _packing_inbox_path()
    with lock_for(path):
        rows = read_json(path, [])
    return rows if isinstance(rows, list) else []


def _save_packing_inbox(rows):
    from storage import atomic_write_json, lock_for
    path = _packing_inbox_path()
    with lock_for(path):
        atomic_write_json(path, rows)


_PACKING_FILE_SEQ = 0


def _write_packing_file(scope, filename, payload):
    global _PACKING_FILE_SEQ
    _PACKING_FILE_SEQ += 1
    folder = os.path.join(data_dir(), 'packing_files', str(scope))
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(filename or '') or 'packing-list')[:80]
    stored = f'{_PACKING_FILE_SEQ}-{safe}'
    with open(os.path.join(folder, stored), 'wb') as handle:
        handle.write(payload)
    return stored


def _packing_status_label(status):
    return {'packed': 'Packed', 'partial': 'Partially packed'}.get(status or '', 'Not packed')


def _attach_packing_list(order, parsed, user, filename, stored):
    """Save one list on an order. The same rack number replaces the previous version."""
    import packing
    matched, unmatched = packing.classify_rows(order, parsed)
    lists = list(order.get('packing_lists') or [])
    key = packing.list_key(parsed)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    next_id = max([int(row.get('id') or 0) for row in lists] or [0]) + 1
    replaced = None
    if key:
        for entry in lists:
            if entry.get('current', True) and entry.get('key') == key:
                entry['current'] = False
                entry['superseded_at'] = now
                replaced = entry
    lists.append({
        'id': next_id,
        'key': key,
        'current': True,
        'number': parsed.get('number') or '',
        'rack': parsed.get('rack') or '',
        'rack_name': parsed.get('rack_name') or '',
        'racks': [
            str(number).strip() for number in (parsed.get('racks') or [])
            if str(number or '').strip()
        ] or ([str(parsed.get('rack')).strip()] if str(parsed.get('rack') or '').strip() else []),
        'weight': parsed.get('weight'),
        'weight_unit': parsed.get('weight_unit') or '',
        'date': parsed.get('date') or '',
        'customer': parsed.get('customer') or '',
        'project': parsed.get('project') or '',
        'po': parsed.get('po') or '',
        'filename': filename,
        'stored': stored,
        'uploaded_at': now,
        'uploaded_by': user.get('email') or '',
        'lines': matched,
        'unmatched': unmatched,
        'replaces': replaced.get('id') if replaced else None,
    })
    order['packing_lists'] = lists
    before = order.get('pack_status') or ''
    progress = packing.packing_progress(order)
    order['pack_status'] = progress['status']
    label = parsed.get('number') or parsed.get('rack') or filename or 'Packing list'
    changes = []
    if replaced:
        changes.append({
            'field': 'packing list',
            'action': 'replaced',
            'old': label,
            'new': f'{label} was uploaded again. The latest file replaces the previous one.',
        })
    else:
        changes.append({'field': 'packing list', 'action': 'added', 'new': label})
    if unmatched:
        detail = ', '.join(
            str(row.get('serial')) if row.get('serial') not in (None, '') else 'no serial'
            for row in unmatched
        )
        changes.append({
            'field': 'packing list',
            'action': 'added',
            'new': f'Unmatched on {label}: {detail}.',
        })
    if before != (progress['status'] or ''):
        changes.append({
            'field': 'pack status',
            'old': _packing_status_label(before),
            'new': progress['status_label'] or 'Not packed',
        })
    _stamp_log(order, user, changes)
    import racks as rack_book
    rack_book.record_packing(
        order, parsed.get('rack') or '', parsed.get('number') or '', parsed.get('rack_name') or '', user,
    )
    return order, progress, unmatched


def _packing_order_choice(order):
    return {
        'id': order.get('id'),
        'customer_id': order.get('customer_id') or '',
        'company': order.get('company') or '',
        'project': order.get('project') or '',
        'po': order.get('po') or '',
    }


def _stale_packing_message(message):
    text = message or ''
    return (
        'No glass rows were found' in text
        or text == 'The packing list could not be read.'
        or text == 'The file is empty.'
    )


def _packing_name_key(filename):
    base = os.path.basename(str(filename or '')).strip().casefold()
    return re.sub(r'(\.(pdf|txt|csv|xlsx|xls))+$', '', base)


def _drop_stale_packing_inbox(filename, number=''):
    """A later successful read of the same file replaces the old 'no glass rows' note."""
    key = _packing_name_key(filename)
    number_key = str(number or '').strip().casefold()
    inbox = []
    for row in _load_packing_inbox():
        message = row.get('message') or ''
        stale = _stale_packing_message(message)
        same_file = bool(key) and _packing_name_key(row.get('filename')) == key
        same_number = bool(number_key) and str(row.get('number') or '').strip().casefold() == number_key
        if stale and (same_file or same_number):
            continue
        inbox.append(row)
    _save_packing_inbox(inbox)


def _prune_resolved_packing_inbox():
    """Drop a no-glass note once that file or packing number is already on an order."""
    names = set()
    numbers = set()
    for order in load_orders():
        if order_status(order) == 'voided':
            continue
        for entry in order.get('packing_lists') or []:
            key = _packing_name_key(entry.get('filename'))
            if key:
                names.add(key)
            number = str(entry.get('number') or '').strip().casefold()
            if number:
                numbers.add(number)
    kept = []
    changed = False
    for row in _load_packing_inbox():
        message = row.get('message') or ''
        if not _stale_packing_message(message):
            kept.append(row)
            continue
        key = _packing_name_key(row.get('filename'))
        number = str(row.get('number') or '').strip().casefold()
        if key in names or (number and number in numbers) or any(saved and saved in key for saved in numbers):
            changed = True
            continue
        kept.append(row)
    if changed:
        _save_packing_inbox(kept)
    return kept


def _queue_packing(parsed, filename, stored, user, reason, message, candidates):
    rows = _load_packing_inbox()
    next_id = max([int(row.get('id') or 0) for row in rows] or [0]) + 1
    record = {
        'id': next_id,
        'filename': filename,
        'stored': stored,
        'parsed': parsed,
        'uploaded_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'uploaded_by': user.get('email') or '',
        'reason': reason,
        'message': message,
        'customer': parsed.get('customer') or '',
        'project': parsed.get('project') or parsed.get('job') or '',
        'po': parsed.get('po') or '',
        'number': parsed.get('number') or '',
        'rack': parsed.get('rack') or '',
        'candidates': [_packing_order_choice(order) for order in candidates],
        'customer_locked': False,
        'customer_id': '',
        'customer_name': '',
        'choices': [],
        'customers': [],
    }
    import packing
    recognized = packing.recognized_customer_ids(parsed, load_orders())
    if len(recognized) != 1:
        named = packing.match_customer_ids(parsed.get('customer'), load_customers())
        if len(named) == 1:
            recognized = named
    if len(recognized) == 1:
        allowed = [
            order for order in packing.orders_for_customer(recognized[0], load_orders())
            if not order.get('legacy')
        ]
        company = (
            (load_customers().get(recognized[0]) or {}).get('name')
            or next(
                (order.get('company') for order in load_orders() if order.get('customer_id') == recognized[0] and order.get('company')),
                parsed.get('customer') or '',
            )
        )
        record['customer_locked'] = True
        record['customer_id'] = recognized[0]
        record['customer_name'] = company
        record['choices'] = [_packing_order_choice(order) for order in allowed]
        if reason == 'no_match':
            record['message'] = (
                f'{parsed.get("project") or parsed.get("job") or "This job"} does not match a '
                f'{company} project. Choose one of that customer\'s orders.'
            )
    else:
        seen = []
        for order in load_orders():
            customer_id = order.get('customer_id')
            if not customer_id or customer_id in seen:
                continue
            if str(order.get('status') or '').lower() == 'delivered':
                continue
            seen.append(customer_id)
            record['customers'].append({
                'id': customer_id,
                'name': order.get('company') or customer_id,
            })
        record['orders'] = [
            _packing_order_choice(order) for order in load_orders()
            if str(order.get('status') or '').lower() != 'delivered' and not order.get('legacy')
        ]
        if parsed.get('rows') and not (parsed.get('customer') or '').strip():
            record['message'] = 'This packing list has no customer. Pick the customer, then one of their orders.'
        elif parsed.get('rows') and reason == 'no_match':
            record['message'] = 'The customer on this packing list was not recognized. Pick the customer, then one of their orders.'
    rows.append(record)
    _save_packing_inbox(rows)
    return record


def _place_parsed_packing(parsed, filename, stored, user):
    import packing
    rows = parsed.get('rows') or []
    warnings = list(parsed.get('warnings') or [])
    if rows:
        _drop_stale_packing_inbox(filename, parsed.get('number') or '')
    if not rows:
        message = warnings[0] if warnings else 'No glass rows were found in that packing list.'
        queued = _queue_packing(parsed, filename, stored, user, 'no_match', message, [])
        return {'saved': None, 'review': queued}
    orders = load_orders()
    hits = packing.candidate_orders(parsed, orders)
    chosen = packing.choose_packing_order(parsed, hits, orders)
    if chosen is None:
        if len(hits) > 1:
            message = 'More than one order matches this customer and project. Choose the order.'
            reason = 'ambiguous'
        else:
            message = 'No order matches this customer and project.'
            reason = 'no_match'
        if warnings:
            message = message + ' ' + ' '.join(warnings)
        queued = _queue_packing(parsed, filename, stored, user, reason, message, hits)
        return {'saved': None, 'review': queued}
    order = chosen
    order_stored = _write_packing_file(order.get('id'), filename, _read_packing_file('inbox', stored))

    def mutator(current):
        updated, progress, unmatched = _attach_packing_list(current, parsed, user, filename, order_stored)
        return updated, None

    saved, error = update_order(order.get('id'), mutator)
    if error:
        queued = _queue_packing(parsed, filename, stored, user, 'no_match', error, [])
        return {'saved': None, 'review': queued}
    return {
        'saved': {
            'order_id': saved.get('id'),
            'company': saved.get('company') or '',
            'project': saved.get('project') or '',
            'number': parsed.get('number') or '',
            'rack': parsed.get('rack') or '',
            'pack_status': saved.get('pack_status') or '',
            'pack_status_label': _packing_status_label(saved.get('pack_status')),
            'totals': packing.packing_progress(saved)['totals'],
            'unmatched': (saved.get('packing_lists') or [])[-1].get('unmatched') or [],
        },
        'review': None,
    }


def _read_packing_file(scope, stored):
    path = os.path.join(data_dir(), 'packing_files', str(scope), os.path.basename(stored or ''))
    if not stored or not os.path.isfile(path):
        return b''
    with open(path, 'rb') as handle:
        return handle.read()


def _accept_packing_upload(filename, payload, user):
    import packing
    if not payload:
        parsed = {'rows': [], 'warnings': ['The file is empty.'], 'customer': '', 'project': '', 'po': '', 'number': '', 'rack': '', 'job': ''}
        stored = ''
        return [_place_parsed_packing(parsed, filename, stored, user)]
    if len(payload) > 25 * 1024 * 1024:
        parsed = {'rows': [], 'warnings': ['The packing list is larger than 25 MB.'], 'customer': '', 'project': '', 'po': '', 'number': '', 'rack': '', 'job': ''}
        return [_place_parsed_packing(parsed, filename, '', user)]
    try:
        documents = packing.parse_packing_upload(filename, payload)
    except Exception:
        documents = [{
            'rows': [], 'warnings': ['The packing list could not be read.'],
            'customer': '', 'project': '', 'po': '', 'number': '', 'rack': '', 'job': '',
        }]
    stored = _write_packing_file('inbox', filename, payload)
    return [_place_parsed_packing(parsed, filename, stored, user) for parsed in documents]


@app.route('/api/packing-lists', methods=['GET'])
@module_required('orders')
def list_packing_lists():
    user = current_user()
    if not staff_can_order(user):
        return json_error('Access denied', 403)
    import packing
    groups = []
    for order in orders_visible_to(user, load_orders()):
        current = [row for row in order.get('packing_lists') or [] if row.get('current', True)]
        if not current and not order.get('pack_status'):
            continue
        progress = packing.packing_progress(order)
        groups.append({
            'order_id': order.get('id'),
            'company': order.get('company') or '',
            'project': order.get('project') or '',
            'po': order.get('po') or '',
            'pack_status': progress['status'],
            'pack_status_label': progress['status_label'],
            'totals': progress['totals'],
            'lists': _packing_payload(order, True)['packing_lists'],
        })
    return jsonify({'projects': groups, 'inbox': _prune_resolved_packing_inbox()})


@app.route('/api/packing-lists', methods=['POST'])
@module_required('orders')
def upload_packing_lists():
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to upload packing lists.', 403)
    uploads = request.files.getlist('files') or request.files.getlist('file')
    uploads = [item for item in uploads if item and item.filename]
    if not uploads:
        return json_error('Choose at least one packing list.', 400)
    saved = []
    review = []
    for upload in uploads:
        payload = upload.read(25 * 1024 * 1024 + 1)
        for result in _accept_packing_upload(upload.filename, payload, user):
            if result.get('saved'):
                saved.append(result['saved'])
            if result.get('review'):
                review.append(result['review'])
    return jsonify({'saved': saved, 'review': review})


def _legacy_project(parsed):
    return str((parsed or {}).get('project') or (parsed or {}).get('job') or '').strip()


def _find_legacy_job(customer_id, project):
    import packing
    key = packing._fold_code(project)
    if not customer_id or not key:
        return None
    for order in load_orders():
        if order.get('legacy') and order.get('customer_id') == customer_id and packing._fold_code(order.get('project')) == key:
            return order
    return None


def _new_legacy_order(customer_id, customer, project, user):
    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return append_order({
        'legacy': True,
        'customer_id': customer_id,
        'company': (customer or {}).get('name') or '',
        'project': project,
        'po': '',
        'status': 'approved',
        'currency': _required_currency(customer) if customer else 'usd',
        'items': [],
        'total': 0,
        'igu_subtotal': 0,
        'igu_total': 0,
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'extra_charges': [],
        'shipments': [],
        'packing_lists': [],
        'notes': 'Legacy job. Packed before this order was in the ERP. No quote price and no customer invoice.',
        'date': date.today().isoformat(),
        'approved_by': user.get('email') or '',
        'approved_at': approved_at,
        'changelog': [],
        'quote_id': None,
    })


def _attach_inbox_to_order(record, order, user):
    payload = _read_packing_file('inbox', record.get('stored'))
    stored = _write_packing_file(order.get('id'), record.get('filename') or '', payload) if payload else ''

    def mutator(current):
        updated, _progress, _unmatched = _attach_packing_list(
            current, record.get('parsed') or {}, user, record.get('filename') or '', stored,
        )
        return updated, None

    return update_order(order.get('id'), mutator)


def _assign_legacy_packing(inbox_id, user, data):
    inbox = _load_packing_inbox()
    record = next((row for row in inbox if row.get('id') == inbox_id), None)
    if not record:
        return json_error('Not found', 404)
    parsed = record.get('parsed') or {}
    if not parsed.get('rows'):
        return json_error('This packing list has no glass rows to save.', 400)
    customer_id = record.get('customer_id') or clean_text(data.get('customer_id'), 80)
    if not customer_id:
        return json_error('Pick the customer first, then choose Not an ERP order.', 400)
    customer = load_customers().get(customer_id)
    if not customer:
        return json_error('Unknown customer.', 400)
    project = _legacy_project(parsed)
    if not project:
        return json_error('This packing list has no job name to keep as the legacy reference.', 400)
    existing = _find_legacy_job(customer_id, project)
    created = existing is None
    if created:
        existing = _new_legacy_order(customer_id, customer, project, user)
    saved, error = _attach_inbox_to_order(record, existing, user)
    if error:
        return json_error(error, 400)
    _save_packing_inbox([row for row in inbox if row.get('id') != inbox_id])
    import packing
    progress = packing.packing_progress(saved)
    return jsonify({
        'order_id': saved.get('id'),
        'legacy': True,
        'created': created,
        'company': saved.get('company') or '',
        'project': saved.get('project') or '',
        'pack_status': progress['status'],
        'pack_status_label': progress['status_label'],
        'totals': progress['totals'],
        'unmatched': (saved.get('packing_lists') or [])[-1].get('unmatched') or [],
    })


@app.route('/api/packing-inbox/<int:inbox_id>/assign', methods=['POST'])
@module_required('orders')
def assign_packing_list(inbox_id):
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to upload packing lists.', 403)
    data = json_body() or {}
    if str(data.get('order_id') or '') == 'legacy':
        return _assign_legacy_packing(inbox_id, user, data)
    try:
        order_id = int(data.get('order_id'))
    except (TypeError, ValueError):
        return json_error('Choose the order this packing list belongs to.', 400)
    inbox = _load_packing_inbox()
    record = next((row for row in inbox if row.get('id') == inbox_id), None)
    if not record:
        return json_error('Not found', 404)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    import packing
    recognized = packing.recognized_customer_ids(record.get('parsed') or {}, load_orders())
    if len(recognized) == 1 and visible.get('customer_id') != recognized[0]:
        company = record.get('customer_name') or (record.get('parsed') or {}).get('customer') or 'the packing list customer'
        return json_error(
            f'This packing list is for {company}. It cannot be saved on {visible.get("company") or "another customer"}.',
            400,
        )
    if len(recognized) != 1:
        customer_id = clean_text(data.get('customer_id'), 80)
        if not customer_id:
            return json_error('Pick the customer first, then one of that customer\'s orders.', 400)
        if visible.get('customer_id') != customer_id:
            return json_error('That order belongs to a different customer.', 400)
    if str(visible.get('status') or '').lower() == 'delivered':
        return json_error('That order is already delivered.', 400)
    payload = _read_packing_file('inbox', record.get('stored'))
    stored = _write_packing_file(order_id, record.get('filename'), payload) if payload else ''

    def mutator(current):
        updated, _progress, _unmatched = _attach_packing_list(
            current, record.get('parsed') or {}, user, record.get('filename') or '', stored,
        )
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error:
        return json_error(error, 400)
    _save_packing_inbox([row for row in inbox if row.get('id') != inbox_id])
    _drop_stale_packing_inbox(record.get('filename') or '', (record.get('parsed') or {}).get('number') or record.get('number') or '')
    import packing
    progress = packing.packing_progress(saved)
    return jsonify({
        'order_id': saved.get('id'),
        'pack_status': progress['status'],
        'pack_status_label': progress['status_label'],
        'totals': progress['totals'],
        'unmatched': (saved.get('packing_lists') or [])[-1].get('unmatched') or [],
    })


@app.route('/api/orders/<int:order_id>/packing-lists/<int:list_id>/reassign', methods=['POST'])
@module_required('orders')
def reassign_legacy_packing(order_id, list_id):
    """Move one packing list from a legacy job onto a real ERP order."""
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to change packing lists.', 403)
    data = json_body() or {}
    try:
        target_id = int(data.get('order_id'))
    except (TypeError, ValueError):
        return json_error('Choose the ERP order.', 400)
    legacy = next((row for row in load_orders() if row.get('id') == order_id), None)
    target = next((row for row in load_orders() if row.get('id') == target_id), None)
    if not legacy or not target:
        return json_error('Not found', 404)
    if not legacy.get('legacy'):
        return json_error('Only a packing list on a legacy job can be moved to an ERP order.', 400)
    if target.get('legacy'):
        return json_error('Choose an ERP order, not another legacy job.', 400)
    if target.get('customer_id') != legacy.get('customer_id'):
        return json_error('That order belongs to a different customer.', 400)
    if legacy.get('shipments'):
        return json_error('Shipments are already recorded on this legacy job. The packing list stays with it.', 400)
    entry = next((row for row in legacy.get('packing_lists') or [] if row.get('id') == list_id and row.get('current', True)), None)
    if not entry:
        return json_error('Not found', 404)
    payload = _read_packing_file(order_id, entry.get('stored'))
    stored = _write_packing_file(target_id, entry.get('filename') or '', payload) if payload else ''
    parsed = {
        'number': entry.get('number') or '',
        'date': entry.get('date') or '',
        'job': legacy.get('project') or '',
        'customer': entry.get('customer') or legacy.get('company') or '',
        'project': entry.get('project') or legacy.get('project') or '',
        'po': entry.get('po') or '',
        'rack': entry.get('rack') or '',
        'rack_name': entry.get('rack_name') or '',
        'rows': [{
            'serial': line.get('serial'),
            'qty': line.get('qty'),
            'floor': line.get('floor') or '',
            'description': line.get('description') or '',
            'width_in': line.get('width_in'),
            'height_in': line.get('height_in'),
            'width_mm': line.get('width_mm'),
            'height_mm': line.get('height_mm'),
        } for line in entry.get('lines') or []],
    }

    def add_to_target(current):
        updated, _progress, _unmatched = _attach_packing_list(
            current, parsed, user, entry.get('filename') or '', stored,
        )
        return updated, None

    saved, error = update_order(target_id, add_to_target)
    if error:
        return json_error(error, 400)

    def take_off_legacy(current):
        updated = dict(current)
        updated['packing_lists'] = [
            row for row in current.get('packing_lists') or [] if row.get('id') != list_id
        ]
        import packing
        progress = packing.packing_progress(updated)
        updated['pack_status'] = progress['status']
        return updated, None

    left, error = update_order(order_id, take_off_legacy)
    if error:
        return json_error(error, 400)
    if not left.get('shipments') and not any(row.get('current', True) for row in left.get('packing_lists') or []):
        save_orders([row for row in load_orders() if row.get('id') != order_id])
        left = None
    return jsonify({
        'order_id': saved.get('id'),
        'legacy_id': None if left is None else left.get('id'),
        'legacy_removed': left is None,
    })


@app.route('/api/orders/<int:order_id>/packing-lists/<int:list_id>/file', methods=['GET'])
@module_required('orders')
def download_order_packing_list(order_id, list_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    entry = next((row for row in (order or {}).get('packing_lists') or [] if row.get('id') == list_id), None)
    path = os.path.join(data_dir(), 'packing_files', str(order_id), os.path.basename((entry or {}).get('stored') or ''))
    if not order or not entry or not entry.get('stored') or not os.path.isfile(path):
        return json_error('Not found', 404)
    return send_file(path, as_attachment=True, download_name=entry.get('filename') or 'packing-list')


@app.route('/api/orders/<int:order_id>/packing-lists/<int:list_id>', methods=['DELETE'])
@module_required('orders')
def remove_packing_list(order_id, list_id):
    """Take one packing-list version off an order and recalculate packed quantity."""
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to change packing lists.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    entry = next((row for row in visible.get('packing_lists') or [] if row.get('id') == list_id), None)
    if not entry:
        return json_error('Not found', 404)
    if visible.get('shipments'):
        return json_error(
            'This packing list cannot be removed because the order already has a shipment. '
            'Remove the shipment first.',
            400,
        )
    if any(
        row.get('order_id') == order_id and row.get('kind') == 'intercompany' and not row.get('voided')
        for row in load_invoices()
    ):
        return json_error(
            'This packing list cannot be removed because the order already has an intercompany invoice.',
            400,
        )

    def mutator(order):
        updated = dict(order)
        label = entry.get('number') or entry.get('rack') or entry.get('filename') or 'Packing list'
        updated['packing_lists'] = [row for row in order.get('packing_lists') or [] if row.get('id') != list_id]
        import packing
        progress = packing.packing_progress(updated)
        updated['pack_status'] = progress['status']
        _stamp_log(updated, user, [{
            'field': 'packing list',
            'action': 'removed',
            'old': label,
            'new': 'Removed. Packed quantity was recalculated.',
        }, {
            'field': 'pack status',
            'old': _packing_status_label(order.get('pack_status')),
            'new': progress['status_label'] or 'Not packed',
        }])
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/packing-lists.pdf', methods=['GET'])
@module_required('orders')
def download_order_packing_lists_pdf(order_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        return json_error('Not found', 404)
    payload = shipments.packing_lists_pdf_bytes(order)
    return send_file(BytesIO(payload), mimetype='application/pdf', as_attachment=True, download_name=f'order-{order_id}-packing-list.pdf')


@app.route('/api/orders/<int:order_id>/backorder.pdf', methods=['GET'])
@module_required('orders')
def download_order_backorder_pdf(order_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not order:
        return json_error('Not found', 404)
    payload = shipments.backorder_list_pdf_bytes(order)
    return send_file(BytesIO(payload), mimetype='application/pdf', as_attachment=True, download_name=f'order-{order_id}-backorder.pdf')


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


@app.route('/api/orders/<int:order_id>/ship-packed', methods=['POST'])
@module_required('orders')
def ship_packed_pieces(order_id):
    """Ship every packed piece that has not gone out yet. The customer invoice is not created."""
    user = current_user()
    if not staff_can_order(user):
        return json_error('You do not have permission to record a shipment.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    if order_status(visible) == 'voided':
        return json_error('This order is voided and cannot be changed.', 400)
    data = json_body() or {}
    shipped_on = clean_text(data.get('date'), 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', shipped_on or ''):
        return json_error('Enter the shipping date.', 400)
    available = shipments.packed_quantities_to_ship(visible)
    if not available:
        return json_error('Nothing packed is left to ship.', 400)
    quantities = dict(available)
    if isinstance(data.get('lines'), list):
        serial_by_index = {row['index']: row['serial'] for row in shipments.glass_rows(visible)}
        chosen = {}
        for line in data.get('lines') or []:
            if not isinstance(line, dict):
                return json_error('Enter a quantity for each serial.', 400)
            try:
                index = int(line.get('index'))
                qty = int(line.get('qty') or 0)
            except (TypeError, ValueError):
                return json_error('Enter a quantity for each serial.', 400)
            if qty <= 0:
                continue
            allowed = available.get(index, 0)
            if qty > allowed:
                return json_error(
                    f'Only {allowed} packed pieces of serial {serial_by_index.get(index)} are left to ship.',
                    400,
                )
            chosen[index] = qty
        quantities = chosen
    if not quantities:
        return json_error('Nothing packed is left to ship.', 400)
    number = clean_text(data.get('number'), 40) or f"SHIP-{order_id}-{len(visible.get('shipments') or []) + 1}"
    racks = clean_text(data.get('rack'), 80)
    if not racks:
        racks = _racks_for_quantities(visible, quantities)
    payload = {
        'number': number,
        'date': shipped_on,
        'rack': racks,
        'truck_plate': data.get('truck_plate') or '',
        'notes': data.get('notes') or '',
        'weight': data.get('weight') or '',
        'lines': [{'index': index, 'qty': qty} for index, qty in quantities.items()],
    }

    def mutator(order):
        updated = dict(order)
        updated['shipments'] = list(order.get('shipments') or [])
        recorded, error = _record_shipment(updated, user, payload)
        if error:
            return None, error
        _stamp_log(recorded, user, [{
            'field': 'shipment',
            'action': 'added',
            'new': f'{number} · packed pieces only',
        }])
        return recorded, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


def _issue_packed_customer_invoice(order, user, quantities=None):
    """Customer invoice for packed pieces that are not on a customer invoice yet."""
    if quantities is None:
        quantities = _unbilled_packed(order, 'customer')
    if not quantities:
        return None, None
    items, _pcs, _sqf = shipments.shipment_lines(order, quantities)
    import packing
    progress = packing.packing_progress(order)
    billed = _invoiced_qty_by_serial(order, 'customer')
    finishing = all(
        int(billed.get(row['serial'], 0)) + int(quantities.get(row['index'], 0)) >= int(row['ordered'])
        for row in progress['serials']
    )
    full_value = shipments.shipment_progress(order)['ordered_value'] or money(order.get('total'))
    portion = round(sum(
        money(row.get('total')) for row in items
        if row.get('kind') not in {'irregular_surcharge', 'dwg_creation'}
    ), 2)
    charges = (order.get('extra_charges') or []) if finishing else []
    summary, error = price_summary(items, _scaled_discount(order, portion, full_value), charges)
    if error:
        return None, error
    from billing import apply_additional_discount
    summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
    customer = _order_customer(order)
    issuer = 'ca' if _needs_intercompany(order) else 'us'
    shipment = {
        'number': '', 'id': None, 'date': '', 'rack': '', 'truck_plate': '', 'notes': 'Packed pieces',
    }
    document = _invoice_for_items(order, user, customer, items, summary, issuer, 'customer', shipment)
    stored = append_invoice(document)
    _apply_prepayments_to_invoice(order.get('id'), stored['id'], user)
    fresh = _finish_invoice(stored, user)
    order['customer_invoice_deferred'] = False
    if not order.get('customer_invoice_id'):
        order['customer_invoice_id'] = fresh['id']
        order['customer_invoice_number'] = fresh['number']
    if not order.get('invoice_id'):
        order['invoice_id'] = fresh['id']
        order['invoice_number'] = fresh['number']
    _qbo('push_document_id', fresh['id'])
    return fresh['number'], None


def _issue_packed_intercompany(order, user):
    """Ora Glass Inc bills Ora Glass (Canada) Inc for packed pieces not yet on an intercompany invoice."""
    quantities = _unbilled_packed(order, 'intercompany')
    if not quantities:
        return None, None
    items, _pcs, _sqf = shipments.shipment_lines(order, quantities)
    shipment = {
        'id': None,
        'number': '',
        'date': datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat(),
        'rack': '',
        'truck_plate': '',
        'notes': 'Packed pieces',
        'items': items,
        'lines': [
            {'serial': item.get('serial'), 'qty': item.get('pcs'), 'index': item.get('source_index')}
            for item in items if item.get('serial') not in (None, '')
        ],
        'invoiced': False,
    }
    return _issue_shipment_invoice(order, shipment, user, False)


@app.route('/api/orders/<int:order_id>/customer-invoice', methods=['POST'])
@module_required('orders')
def invoice_completed_order(order_id):
    """Bill packed pieces. A back order does not hold up what is already packed."""
    user = current_user()
    if not can_issue_invoices(user):
        return json_error('You do not have permission to issue invoices.', 403)
    visible = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    if not visible:
        return json_error('Not found', 404)
    if order_status(visible) == 'voided':
        return json_error('This order is voided and cannot be invoiced.', 400)
    if visible.get('remake') and not visible.get('charge_customer'):
        return json_error('This remake is not charged to the customer.', 400)
    if visible.get('legacy'):
        return json_error('Legacy jobs are not invoiced in the ERP. There is no quote price to bill.', 400)
    data = json_body() or {}

    def mutator(order):
        if order_status(order) == 'voided':
            return None, 'This order is voided and cannot be invoiced.'
        if order.get('remake') and not order.get('charge_customer'):
            return None, 'This remake is not charged to the customer.'
        if order.get('legacy'):
            return None, 'Legacy jobs are not invoiced in the ERP. There is no quote price to bill.'
        if not order.get('packing_lists'):
            state, _label = _customer_invoice_state(order)
            if state == 'invoiced':
                return None, 'This order already has a customer invoice.'
            if state != 'ready':
                return None, 'Upload a packing list before invoicing packed pieces.'
            updated = dict(order)
            number, error = _issue_final_customer_invoice(updated, user)
            if error:
                return None, error
            _stamp_log(updated, user, [{
                'field': 'customer invoice',
                'action': 'added',
                'new': number or updated.get('customer_invoice_number') or updated.get('invoice_number') or '',
            }])
            return updated, None
        updated = dict(order)
        changes = []
        if data.get('create_intercompany', True) and _needs_intercompany(updated):
            number, error = _issue_packed_intercompany(updated, user)
            if error:
                return None, error
            if number:
                changes.append({'field': 'intercompany invoice', 'action': 'added', 'new': number})
        if 'invoice_customer' not in data:
            if changes:
                _stamp_log(updated, user, changes)
            return updated, None
        if data.get('invoice_customer'):
            number, error = _issue_packed_customer_invoice(updated, user)
            if error:
                return None, error
            if number:
                changes.append({'field': 'customer invoice', 'action': 'added', 'new': number})
        else:
            updated['customer_invoice_deferred'] = True
            changes.append({'field': 'customer invoice', 'action': 'kept', 'new': 'Customer invoice pending'})
        if changes:
            _stamp_log(updated, user, changes)
        return updated, None

    saved, error = update_order(order_id, mutator)
    if error == 'Not found':
        return json_error('Not found', 404)
    if error:
        return json_error(error, 400)
    if saved.get('customer_invoice_id'):
        _qbo('push_document_id', saved.get('customer_invoice_id'))
    elif saved.get('invoice_id'):
        _qbo('push_document_id', saved.get('invoice_id'))
    return jsonify(order_for_viewer(saved, user, with_coverage=True))


@app.route('/api/orders/<int:order_id>/shipments/<int:shipment_id>/pdf', methods=['GET'])
@module_required('orders')
def download_shipment_pdf(order_id, shipment_id):
    user = current_user()
    order = next((row for row in orders_visible_to(user, load_orders()) if row.get('id') == order_id), None)
    shipment = next((row for row in (order or {}).get('shipments') or [] if row.get('id') == shipment_id), None)
    if not order or not shipment:
        return json_error('Not found', 404)
    payload = shipments.shipment_pdf_bytes(order, shipment)
    return send_file(
        BytesIO(payload), mimetype='application/pdf', as_attachment=True,
        download_name=f"{shipment.get('number') or 'shipment'}.pdf",
    )


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
    import tax_numbers
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
    _apply_canada_tax(customer_invoice, customer, order.get('ship_province'))
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
        'pst_number': order.get('pst_number') or '',
        'qst_number': order.get('qst_number') or '',
        'seller_gst_number': tax_numbers.displayed_gst_number(),
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


def _racks_for_quantities(order, quantities):
    """Rack numbers on the current packing lists that cover the pieces being shipped."""
    import packing
    catalog = {row['index']: row['serial'] for row in packing.serial_catalog(order)}
    serials = {catalog[index] for index in quantities if index in catalog}
    racks = []
    for entry in order.get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        covers = False
        for line in entry.get('lines') or []:
            try:
                serial = int(line.get('serial'))
            except (TypeError, ValueError):
                continue
            if serial in serials:
                covers = True
                break
        rack = str(entry.get('rack') or '').strip()
        if covers and rack and rack not in racks:
            racks.append(rack)
    return ', '.join(racks)


def _public_shipments(order):
    rows = []
    for shipment in order.get('shipments') or []:
        rows.append({
            'id': shipment.get('id'),
            'number': shipment.get('number') or '',
            'date': shipment.get('date') or '',
            'rack': shipment.get('rack') or '',
            'truck_plate': shipment.get('truck_plate') or '',
            'notes': shipment.get('notes') or '',
            'weight': shipment.get('weight') or '',
            'pcs': shipment.get('pcs') or 0,
            'sqf': shipment.get('sqf') or 0,
            'total': shipment.get('total') or 0,
            'invoiced': bool(shipment.get('invoiced')),
            'invoice_id': shipment.get('invoice_id'),
            'invoice_number': shipment.get('invoice_number') or '',
            'packing_list': shipment.get('packing_list') or '',
            'status': shipment.get('status') or '',
            'delivered_on': shipment.get('delivered_on') or '',
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
        'shipment_date': (shipment or {}).get('date') or '',
        'shipment_rack': (shipment or {}).get('rack') or '',
        'truck_plate': (shipment or {}).get('truck_plate') or '',
        'shipment_notes': (shipment or {}).get('notes') or '',
        'status': 'unpaid',
        'balance': summary.get('total', 0),
        'issuer': issuer,
        'kind': kind,
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'created_by': user.get('email') or '',
    }
    if issuer == 'ca':
        _apply_canada_tax(document, customer, order.get('ship_province'))
    else:
        document.update(_tax_number_snapshot(customer))
    return document


def _items_repeat_invoice_total(items, total):
    """True when every row was stored as the whole invoice total."""
    rows = [row for row in items or [] if isinstance(row, dict)]
    if len(rows) < 2:
        return False
    target = money(total)
    if target <= 0:
        return False
    return all(abs(money(row.get('total')) - target) < 0.009 for row in rows)


def _usd_intercompany_items(items, rate):
    """Convert each CAD line, including a prorated surcharge, at this invoice rate."""
    import quickbooks
    usd_total = quickbooks.cad_to_usd(round(sum(money(row.get('total')) for row in items), 2), rate)
    remaining = usd_total
    built = []
    for index, item in enumerate(items):
        addon = item.get('kind') in _CHARGE_KINDS
        try:
            pcs = int(item.get('pcs') or 0)
        except (TypeError, ValueError):
            pcs = 0
        if index == len(items) - 1:
            share = remaining
        else:
            share = min(quickbooks.cad_to_usd(item.get('total'), rate), remaining)
        remaining = round(remaining - share, 2)
        if addon:
            built.append({
                'kind': item.get('kind'),
                'name': item.get('name') or 'Charge',
                'description': item.get('description') or item.get('name') or 'Charge',
                'serial': item.get('serial') or '',
                'pcs': '',
                'qty': '',
                'sqf': 0,
                'unit_price': share,
                'price_pc': share,
                'total': share,
            })
            continue
        each = round(share / pcs, 2) if pcs else share
        built.append({
            'name': item.get('name') or 'Glass',
            'description': item.get('description') or item.get('name') or 'Glass',
            'serial': item.get('serial'),
            'spec': item.get('spec'),
            'pcs': pcs,
            'qty': pcs,
            'sqf': item.get('sqf') or 0,
            'unit_price': each,
            'price_pc': each,
            'total': share,
        })
    return built, usd_total


def _invoice_quantities(order, invoice):
    import shipments
    by_serial = {row['serial']: row['index'] for row in shipments.glass_rows(order)}
    quantities = {}
    for item in (invoice or {}).get('items') or []:
        if not isinstance(item, dict) or item.get('kind') in _CHARGE_KINDS:
            continue
        try:
            serial = int(item.get('serial'))
            pcs = int(item.get('pcs') or item.get('qty') or 0)
        except (TypeError, ValueError):
            continue
        index = by_serial.get(serial)
        if index is None or pcs <= 0:
            continue
        quantities[index] = quantities.get(index, 0) + pcs
    return quantities


def _shown_intercompany_items(invoice):
    """Replace rows that were all saved as the invoice total with each line's own amount."""
    items = invoice.get('items') or []
    if not _items_repeat_invoice_total(items, invoice.get('total')):
        return items
    order = next((row for row in load_orders() if row.get('id') == invoice.get('order_id')), None)
    rate = invoice.get('fx_rate')
    if not order or not rate:
        return items
    quantities = _invoice_quantities(order, invoice)
    if not quantities:
        return items
    import shipments
    built, _pcs, _sqf = shipments.shipment_lines(order, quantities)
    if not built:
        return items
    converted, _total = _usd_intercompany_items(built, rate)
    return converted or items


def _scale_invoice_lines(items, new_total):
    """Keep each line's share when the invoice total changes."""
    rows = [dict(row) if isinstance(row, dict) else row for row in items or []]
    amounts = [money(row.get('total')) if isinstance(row, dict) else 0 for row in rows]
    base = round(sum(amounts), 2)
    if base <= 0:
        return rows
    remaining = money(new_total)
    last = max((index for index, row in enumerate(rows) if isinstance(row, dict)), default=None)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if index == last:
            share = remaining
        else:
            share = min(round(amounts[index] * money(new_total) / base, 2), remaining)
        remaining = round(remaining - share, 2)
        try:
            pcs = int(row.get('pcs') or 0)
        except (TypeError, ValueError):
            pcs = 0
        row['total'] = share
        row['unit_price'] = round(share / pcs, 2) if pcs else share
        row['price_pc'] = row['unit_price']
    return rows


def _issue_shipment_invoice(order, shipment, user, include_extras):
    """Invoice this shipment once. Returns the invoice number."""
    import quickbooks
    customer = _order_customer(order)
    mode = _billing_mode(order)
    if mode != 'canada' and order.get('remake') and not order.get('charge_customer'):
        return None, None
    items = json.loads(json.dumps(shipment.get('items') or []))
    kind = 'intercompany' if mode == 'canada' else 'customer'
    items = _limit_to_unbilled(order, items, kind)
    if not any(row.get('kind') not in {'irregular_surcharge', 'dwg_creation'} for row in items):
        return None, None
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
        portion = round(sum(money(row.get('total')) for row in items), 2)
        summary, error = price_summary(
            items, _scaled_discount(order, portion, full_value), [],
        )
        if error:
            return None, error
        summary = apply_additional_discount(summary, order.get('additional_discount_percent'))
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
        if currency == 'cad':
            intercompany_items, usd_total = _usd_intercompany_items(items, rate)
        else:
            intercompany_items, usd_total = _usd_intercompany_items(items, 1)
        document = _invoice_for_items(
            order, user, books, intercompany_items,
            {'discount': {'type': 'percent', 'value': 0}, 'discount_amount': 0, 'extra_charges': [],
             'igu_subtotal': usd_total, 'igu_total': usd_total, 'extras_total': 0, 'total': usd_total},
            'us', 'intercompany', shipment,
        )
        import tax_numbers
        document.update({
            'currency': 'usd',
            'fx_rate': rate,
            'fx_date': fx_date,
            'fx_source': 'erp',
            'fx_pair': 'FXUSDCAD',
            'cad_total': money(summary.get('total')) if currency == 'cad' else None,
            'company': 'Ora Glass (Canada) Inc',
            'customer_id': books.get('id'),
            'seller_gst_number': tax_numbers.displayed_gst_number(),
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
    if order.get('remake') and not order.get('charge_customer'):
        return None, None
    if order.get('packing_lists'):
        return _issue_packed_customer_invoice(order, user)
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
        _apply_canada_tax(base, customer, order.get('ship_province'))
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
        if order.get('packing_lists'):
            import packing
            if packing.packing_progress(order)['status'] != 'packed':
                return None
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
        _attach_invoice_gst(view, invoice)
        _attach_invoice_customs(view)
        return view
    view['applied'] = round(money(invoice.get('total')) - invoice_balance(invoice, payments), 2)
    view['balance'] = invoice_balance(invoice, payments)
    view['status'] = 'void' if invoice.get('voided') else invoice_status(invoice, payments)
    view['status_label'] = invoice_status_label(view['status'])
    doc_number = str(invoice.get('qbo_doc_number') or '')
    if not doc_number and invoice.get('source') == 'quickbooks':
        doc_number = str(invoice.get('project') or '')
    view['qbo_doc_number'] = doc_number
    if view.get('fx_rate') not in (None, ''):
        view['fx_date'] = _shown_fx_date(invoice)
    if view['kind'] == 'intercompany':
        view['items'] = _shown_intercompany_items(invoice)
    _attach_invoice_gst(view, invoice)
    _attach_invoice_customs(view)
    if view['kind'] == 'intercompany' and invoice.get('order_id'):
        order = next((row for row in load_orders() if row.get('id') == invoice.get('order_id')), None)
        if order:
            import packing
            summary = packing.packing_for_intercompany(
                _with_packing_measures(order), invoice, load_invoices(),
            )
            view['packing'] = summary if summary.get('lists') else None
    return view


def _attach_invoice_customs(view):
    if (view or {}).get('kind') != 'intercompany':
        view['customs'] = None
        return
    import packing
    view['customs'] = packing.intercompany_customs()


def _attach_invoice_gst(view, invoice):
    place = _invoice_gst_place(view)
    view['gst_place'] = place
    if not place:
        return
    import tax_numbers
    view['seller_gst_number'] = tax_numbers.displayed_gst_number(invoice.get('seller_gst_number'))


def _invoice_gst_place(invoice):
    """Where Ora Glass (Canada) Inc's GST number belongs on this invoice."""
    kind = (invoice or {}).get('kind') or 'customer'
    if kind == 'intercompany':
        return 'customer'
    issuer = (invoice or {}).get('issuer') or 'us'
    currency = str((invoice or {}).get('currency') or 'usd').lower()
    if issuer == 'ca' and currency == 'cad':
        return 'header'
    return ''


def _with_packing_measures(order):
    """Fill rack numbers and weight from the saved packing-list file when the order does not store them."""
    copied = dict(order or {})
    copied['packing_lists'] = [
        _measured_packing_list(entry, (order or {}).get('id'))
        for entry in (order or {}).get('packing_lists') or []
    ]
    return copied


def _measured_packing_list(entry, order_id):
    row = dict(entry or {})
    has_weight = row.get('weight') not in (None, '')
    has_racks = bool(row.get('racks') or row.get('rack'))
    if has_weight and has_racks:
        return row
    payload = _read_packing_file(order_id, row.get('stored'))
    if not payload:
        return row
    import packing
    try:
        if payload.startswith(b'%PDF'):
            parsed = packing.parse_packing_pdf(payload)
        else:
            parsed = packing.parse_packing_lines(payload.decode('utf-8', errors='replace').splitlines())
    except Exception:
        return row
    if not has_weight and parsed.get('weight') is not None:
        row['weight'] = parsed.get('weight')
        row['weight_unit'] = parsed.get('weight_unit') or ''
    if not row.get('racks') and parsed.get('racks'):
        row['racks'] = list(parsed.get('racks') or [])
    if not row.get('rack') and parsed.get('rack'):
        row['rack'] = parsed.get('rack')
    return row


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
    currency = clean_text(request.args.get('currency'), 8).lower()
    issuer = clean_text(request.args.get('issuer'), 8).lower()
    open_only = clean_text(request.args.get('balance'), 20).lower() == 'open'
    date_from = _query_date('from')
    date_to = _query_date('to')
    filtered = []
    for row in rows:
        if user.get('role') != 'customer' and customer_id and row.get('customer_id') != customer_id:
            continue
        if doc_type in {'invoice', 'credit_note'} and row.get('doc_type') != doc_type:
            continue
        if currency in CURRENCIES and str(row.get('currency') or 'usd').lower() != currency:
            continue
        if issuer in {'us', 'ca'} and (row.get('issuer') or 'us') != issuer:
            continue
        if open_only and (row.get('doc_type') != 'invoice' or row.get('voided') or money(row.get('balance')) <= 0.009):
            continue
        if status in {'unpaid', 'partial', 'paid', 'void', 'open', 'applied'} and row.get('status') != status:
            continue
        day = str(row.get('invoice_date') or '')
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        filtered.append(row)
    if open_only:
        filtered.sort(key=lambda row: (str(row.get('invoice_date') or ''), str(row.get('qbo_doc_number') or row.get('number') or ''), row.get('id') or 0))
    else:
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
        if order_status(order) == 'voided' and doc_type == 'invoice':
            return json_error('This order is voided and cannot be invoiced.', 400)
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
    ship = data.get('ship_province') or (order.get('ship_province') if order else None)
    if issuer == 'ca' and doc_type == 'invoice':
        _apply_canada_tax(document, customer, ship)
    else:
        document.update(_tax_number_snapshot(customer))
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
    old_total = money(invoice.get('total'))
    invoice['fx_rate'] = rate
    invoice['fx_override'] = True
    if _items_repeat_invoice_total(invoice.get('items'), old_total):
        repaired = _shown_intercompany_items(invoice)
        if not _items_repeat_invoice_total(repaired, old_total):
            invoice['items'] = repaired
            usd_total = round(sum(money(row.get('total')) for row in repaired if isinstance(row, dict)), 2)
        else:
            invoice['items'] = _scale_invoice_lines(invoice.get('items') or [], usd_total)
    else:
        invoice['items'] = _scale_invoice_lines(invoice.get('items') or [], usd_total)
    invoice['total'] = usd_total
    invoice['balance'] = usd_total
    invoice['igu_subtotal'] = usd_total
    invoice['igu_total'] = usd_total
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


def _quantities_on_invoice(order, invoice):
    """Glass quantities already on one invoice, keyed by order line index."""
    import packing
    catalog = {int(row['serial']): row['index'] for row in packing.serial_catalog(order)}
    quantities = {}
    for line in (invoice or {}).get('items') or []:
        if line.get('kind') in _CHARGE_KINDS:
            continue
        try:
            serial = int(line.get('serial'))
            pcs = int(line.get('pcs') or 0)
        except (TypeError, ValueError):
            continue
        index = catalog.get(serial)
        if index is None or pcs <= 0:
            continue
        quantities[index] = quantities.get(index, 0) + pcs
    return quantities


def _mark_invoice_void(document, user, reason):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    document['voided'] = True
    document['void_reason'] = reason
    document['voided_by'] = user.get('email') or ''
    document['voided_at'] = now
    audit = list(document.get('audit') or [])
    audit.append(_audit(user, 'voided', reason))
    document['audit'] = audit


@app.route('/api/invoices/<int:invoice_id>/regenerate', methods=['POST'])
@module_required('invoices')
def regenerate_invoice(invoice_id):
    """Rebuild an unpaid invoice so line add-ons are included, without billing the pieces twice."""
    user = current_user()
    if user.get('role') != 'admin':
        return json_error('Only an administrator can regenerate an invoice.', 403)
    invoices = load_invoices()
    document = next((row for row in invoices if row.get('id') == invoice_id), None)
    if not document:
        return json_error('Not found', 404)
    if document_type(document) != 'invoice':
        return json_error('Only an invoice can be regenerated.', 400)
    if document.get('voided'):
        return json_error('This document is already void.', 400)
    from billing import invoice_status
    payments = load_payments()
    if invoice_status(document, payments) != 'unpaid':
        return json_error('Only an unpaid invoice can be regenerated.', 400)
    order = next((row for row in load_orders() if row.get('id') == document.get('order_id')), None)
    if not order:
        return json_error('The order for this invoice was not found.', 404)
    quantities = _quantities_on_invoice(order, document)
    if not quantities:
        return json_error('This invoice has no glass pieces to regenerate.', 400)
    reason = 'Regenerated to include line add-ons'
    _mark_invoice_void(document, user, reason)
    for index, row in enumerate(invoices):
        if row.get('id') == document.get('id'):
            invoices[index] = document
    save_invoices(invoices)
    kind = document.get('kind') or 'customer'
    if kind == 'intercompany':
        items, _pcs, _sqf = shipments.shipment_lines(order, quantities)
        shipment = {
            'id': None,
            'number': document.get('shipment_number') or '',
            'date': document.get('shipment_date') or datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat(),
            'rack': document.get('shipment_rack') or '',
            'truck_plate': document.get('truck_plate') or '',
            'notes': 'Regenerated invoice',
            'items': items,
            'lines': [
                {'serial': item.get('serial'), 'qty': item.get('pcs'), 'index': item.get('source_index')}
                for item in items if item.get('serial') not in (None, '')
            ],
            'invoiced': False,
        }
        number, error = _issue_shipment_invoice(order, shipment, user, False)
    else:
        number, error = _issue_packed_customer_invoice(order, user, quantities)
    if error or not number:
        document['voided'] = False
        document.pop('void_reason', None)
        document.pop('voided_by', None)
        document.pop('voided_at', None)
        fresh_rows = load_invoices()
        for index, row in enumerate(fresh_rows):
            if row.get('id') == document.get('id'):
                fresh_rows[index] = document
        save_invoices(fresh_rows)
        return json_error(error or 'Could not regenerate this invoice.', 400)
    replacement = next(
        (row for row in load_invoices() if row.get('number') == number and not row.get('voided')),
        None,
    )
    return jsonify(_present_invoice(replacement or document, load_payments()))


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
    import documents

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
            + '<br/><font size="8">' + documents.header_address_block({
                'issuer': invoice.get('issuer'),
                'kind': invoice.get('kind'),
                'currency': invoice.get('currency'),
                'seller_gst_number': invoice.get('seller_gst_number') or '',
            }) + '</font>',
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
    customer_line = f"<b>Customer:</b> {escape(invoice.get('company') or '')}"
    if invoice.get('gst_place') == 'customer' and invoice.get('seller_gst_number'):
        customer_line += f"<br/>GST# {escape(invoice.get('seller_gst_number'))}"
    elements.append(Paragraph(
        customer_line + "<br/>"
        f"<b>Project:</b> {escape(invoice.get('project') or '-')}<br/>"
        f"<b>Order:</b> {escape(str(invoice.get('order_id') or ''))}"
        f" &nbsp; <b>Quote:</b> {escape(str(invoice.get('quote_id') or ''))}",
        label_style,
    ))
    if invoice.get('shipment_number') or invoice.get('shipment_rack') or invoice.get('truck_plate'):
        elements.append(Paragraph(
            f"<b>Shipment:</b> {escape(invoice.get('shipment_number') or '')}"
            f" &nbsp; <b>Ship date:</b> {escape(invoice.get('shipment_date') or '')}"
            f" &nbsp; <b>Rack:</b> {escape(invoice.get('shipment_rack') or '')}"
            f" &nbsp; <b>Truck:</b> {escape(invoice.get('truck_plate') or '')}",
            label_style,
        ))
        if invoice.get('shipment_notes'):
            elements.append(Paragraph(f"<b>Notes:</b> {escape(invoice.get('shipment_notes'))}", label_style))
    province = str(invoice.get('ship_province') or '').upper()
    if documents.canada_bill(invoice) and province == 'QC' and invoice.get('qst_number'):
        elements.append(Paragraph(f"<b>QST:</b> {escape(invoice.get('qst_number'))}", label_style))
    elif documents.canada_bill(invoice) and invoice.get('pst_number') and province in {'BC', 'SK', 'MB'}:
        elements.append(Paragraph(f"<b>PST:</b> {escape(invoice.get('pst_number'))}", label_style))
    for line in invoice.get('tax_lines') or []:
        if not isinstance(line, dict):
            continue
        elements.append(Paragraph(
            f"<b>{escape(str(line.get('name') or 'Tax'))} {escape(str(line.get('rate') or ''))}%:</b> "
            f"{currency} ${money(line.get('amount')):.2f}",
            label_style,
        ))
    if invoice.get('tax_label') and not invoice.get('tax_lines'):
        elements.append(Paragraph(f"<b>Tax:</b> {escape(invoice.get('tax_label'))}", label_style))
    packing = invoice.get('packing') or {}
    customs = invoice.get('customs') or {}
    if invoice.get('kind') == 'intercompany':
        lines = []
        if packing.get('lists'):
            lines.append(f"Racks: {int(packing.get('rack_count') or 0)}")
            for listed in packing.get('lists') or []:
                numbers = ', '.join(str(number) for number in (listed.get('racks') or [])) or '-'
                weight = ''
                if listed.get('weight') not in (None, ''):
                    weight = f" · {listed.get('weight')} {listed.get('weight_unit') or ''}".rstrip()
                lines.append(
                    f"{escape(listed.get('number') or 'Packing list')}: {escape(numbers)}{escape(weight)}"
                )
            if packing.get('weight_label'):
                lines.append(f"Total weight: {escape(packing.get('weight_label'))}")
        if customs.get('hts'):
            lines.append(f"HTS {escape(customs.get('hts'))}")
        if customs.get('origin'):
            lines.append(f"Origin: {escape(customs.get('origin'))}")
        if lines:
            elements.append(Paragraph('<br/>'.join(lines), label_style))
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
        quantity = '' if documents.is_charge_line(item) else item.get('pcs') or ''
        table_data.append([
            Paragraph(description, cell_style),
            Paragraph(escape(str(quantity)), cell_style),
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


def _payment_issuer(currency):
    return 'ca' if currency == 'cad' else 'us'


def _allocations_for_invoices(customer_id, currency, invoice_ids, amount, payments, invoices):
    """Turn a tick order into payment lines. Each invoice receives at most its open balance."""
    if not isinstance(invoice_ids, list) or not invoice_ids:
        return None, 'Choose at least one invoice.'
    seen = []
    rows = []
    issuer = _payment_issuer(currency)
    for raw in invoice_ids:
        try:
            invoice_id = int(raw)
        except (TypeError, ValueError):
            return None, 'Choose invoices from the list.'
        if invoice_id in seen:
            return None, 'Each invoice can only be selected once.'
        seen.append(invoice_id)
        invoice = next((row for row in invoices if row.get('id') == invoice_id), None)
        if not invoice or invoice.get('customer_id') != customer_id or document_type(invoice) != 'invoice':
            return None, 'Unknown invoice for this customer.'
        if invoice.get('voided'):
            return None, 'A voided invoice cannot take a payment.'
        if str(invoice.get('currency') or 'usd').lower() != currency:
            return None, 'The invoice currency does not match the payment.'
        if (invoice.get('issuer') or 'us') != issuer:
            return None, 'That invoice belongs to the other Ora Glass company.'
        balance = invoice_balance(invoice, payments)
        if balance <= 0.009:
            return None, 'That invoice is already paid.'
        rows.append({'id': invoice_id, 'balance': balance})
    plan = spread_payment(amount, rows)
    additions = [
        {'target_type': 'invoice', 'target_id': row['id'], 'amount': row['applied']}
        for row in plan['applications'] if row['applied'] > 0.009
    ]
    if not additions:
        return None, 'None of the selected invoices can take this payment.'
    return additions, None


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
    invoice_ids = data.get('invoice_ids')
    additions = []
    if invoice_ids not in (None, []):
        additions, error = _allocations_for_invoices(
            customer_id, currency, invoice_ids, amount, load_payments(), load_invoices(),
        )
        if error:
            return json_error(error, 400)
        for item in additions:
            item['at'] = now
            item['user'] = user.get('email') or ''
    log = [{
        'at': now,
        'user': user.get('email') or '',
        'action': 'recorded',
        'amount': amount,
        'currency': currency,
    }]
    if additions:
        log.append({
            'at': now,
            'user': user.get('email') or '',
            'action': 'allocated',
            'allocations': [
                {'target_type': 'invoice', 'target_id': item['target_id'], 'amount': item['amount']}
                for item in additions
            ],
        })
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
        'allocations': additions,
        'recorded_by': user.get('email') or '',
        'recorded_at': now,
        'log': log,
    })
    if additions:
        _refresh_invoice_statuses(load_payments())
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
    invoice_ids = data.get('invoice_ids')
    if invoice_ids not in (None, []) and requested:
        return json_error('Choose invoices or enter allocations, not both.', 400)
    if invoice_ids in (None, []) and (not isinstance(requested, list) or not requested):
        return json_error('Choose at least one invoice.', 400)
    payments = load_payments()
    payment = next((row for row in payments if row.get('id') == payment_id), None)
    if not payment:
        return json_error('Not found', 404)
    if payment.get('voided'):
        return json_error('A voided payment cannot be allocated.', 400)
    room = unallocated_amount(payment)
    if invoice_ids not in (None, []):
        additions, error = _allocations_for_invoices(
            payment.get('customer_id'), payment.get('currency'), invoice_ids, room, payments, load_invoices(),
        )
        if error:
            return json_error(error, 400)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for item in additions:
            item['at'] = now
            item['user'] = user.get('email') or ''
        payment['allocations'] = list(payment.get('allocations') or []) + additions
        log = list(payment.get('log') or [])
        log.append({
            'at': now,
            'user': user.get('email') or '',
            'action': 'allocated',
            'allocations': [
                {'target_type': 'invoice', 'target_id': item['target_id'], 'amount': item['amount']}
                for item in additions
            ],
        })
        payment['log'] = log
        for index, row in enumerate(payments):
            if row.get('id') == payment_id:
                payments[index] = payment
        save_payments(payments)
        _refresh_invoice_statuses(payments)
        _qbo('push_payment', payment)
        return jsonify(_present_payment(payment))
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
    requested_currency = clean_text(request.args.get('currency'), 8).lower()
    if requested_currency and requested_currency not in CURRENCIES:
        return json_error('Currency must be USD or CAD.', 400)
    orders = load_orders()
    invoices = load_invoices()
    payments = load_payments()
    preferred = str(customer.get('credit_currency') or 'usd').lower()
    if preferred not in CURRENCIES:
        preferred = 'usd'
    if requested_currency:
        currency = requested_currency
    else:
        other = 'cad' if preferred == 'usd' else 'usd'

        def _books_active(code):
            receivables = open_receivables(customer_id, code, orders, invoices, payments)
            unallocated = unallocated_credit(payments, customer_id, code)
            return receivables > 0.009 or unallocated > 0.009

        currency = other if not _books_active(preferred) and _books_active(other) else preferred
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


def _driver_gate():
    user = current_user()
    if not user or user.get('role') != 'driver':
        return None
    if request.endpoint in {
        'rack_scan', 'rack_check', 'rack_pickup', 'rack_sites', 'logout',
        'web_manifest', 'service_worker', 'static', 'change_password',
    }:
        return None
    if wants_json():
        return json_error('Access denied', 403)
    return redirect(url_for('rack_scan'))


app.before_request(_shop_gate)
app.before_request(_driver_gate)


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

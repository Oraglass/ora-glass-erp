"""JSON persistence. Paths come from the environment and writes are atomic."""

import json
import logging
import os
import re
import tempfile
import threading

logger = logging.getLogger('ora_glass.storage')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# T5 is the existing end-user tier (425% glass and spacer). The quote screen shows it as Tier 5 (End User).
DEFAULT_TIERS = {
    'T1': {'label': 'Tier 1', 'glass_markup': 0.30, 'spacer_markup': 0.20},
    'T2': {'label': 'Tier 2', 'glass_markup': 0.40, 'spacer_markup': 0.40},
    'T3': {'label': 'Tier 3', 'glass_markup': 0.60, 'spacer_markup': 0.60},
    'T4': {'label': 'Tier 4', 'glass_markup': 0.75, 'spacer_markup': 0.75},
    'T5': {'label': 'Tier 5 (End User)', 'glass_markup': 4.25, 'spacer_markup': 4.25},
}
TIER_ORDER = ('T1', 'T2', 'T3', 'T4', 'T5')

_locks = {}
_locks_guard = threading.Lock()


def data_dir():
    """Directory for JSON state. Follows DATA_DIR, else the folder that holds QUOTES_PATH."""
    explicit = os.environ.get('DATA_DIR', '').strip()
    if explicit:
        return explicit
    quotes = os.environ.get('QUOTES_PATH', '').strip()
    if quotes:
        parent = os.path.dirname(os.path.abspath(quotes))
        if parent:
            return parent
    return os.path.join(BASE_DIR, 'data')


def quotes_path():
    explicit = os.environ.get('QUOTES_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'quotes.json')


def customers_path():
    explicit = os.environ.get('CUSTOMERS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'customers.json')


def users_path():
    explicit = os.environ.get('USERS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'users.json')


def tiers_path():
    explicit = os.environ.get('TIERS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'tiers.json')


def orders_path():
    explicit = os.environ.get('ORDERS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'orders.json')


def pricing_search_paths(base_dir=None, data_directory=None):
    """Absolute paths, in order: PRICING_PATH, next to app.py, then DATA_DIR.

    Production main used the relative path data/pricing_base.xlsx. That file is not
    in the deploy, so the workbook never opened. The copy beside app.py is addressed
    from this file's location, not the process working directory.
    """
    base = os.path.abspath(base_dir or BASE_DIR)
    data = os.path.abspath(data_directory or data_dir())
    paths = []
    env = os.environ.get('PRICING_PATH', '').strip()
    if env:
        paths.append(os.path.abspath(env))
    beside_app = os.path.join(base, 'pricing_base.xlsx')
    on_disk = os.path.join(data, 'pricing_base.xlsx')
    for path in (beside_app, on_disk):
        if path not in paths:
            paths.append(path)
    return paths


def resolve_pricing_path(base_dir=None, data_directory=None):
    """First existing pricing workbook, or the path beside app.py when none exist."""
    paths = pricing_search_paths(base_dir, data_directory)
    for candidate in paths:
        if os.path.isfile(candidate):
            return candidate
    return paths[0]


def lock_for(path):
    key = os.path.abspath(path)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def atomic_write_json(path, data):
    """Write JSON via a temp file in the destination directory, then replace it."""
    destination = os.path.abspath(path)
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.exception('Failed to read %s', path)
        return default


def _read_quote_list(path):
    data = read_json(path, [])
    if isinstance(data, list):
        return data
    logger.error('Quotes file %s is not a list', path)
    return []


def load_quotes(legacy_dir=None):
    path = quotes_path()
    with lock_for(path):
        if os.path.exists(path):
            return _read_quote_list(path)
        legacy_root = BASE_DIR if legacy_dir is None else legacy_dir
        legacy = os.path.join(legacy_root, 'quotes.json')
        if os.path.abspath(legacy) == os.path.abspath(path) or not os.path.exists(legacy):
            return []
        data = read_json(legacy, [])
        if not isinstance(data, list):
            return []
        atomic_write_json(path, data)
        logger.info('Migrated %s quotes from %s to %s', len(data), legacy, path)
        return data


def save_quotes(quotes):
    if not isinstance(quotes, list):
        raise TypeError('quotes must be a list')
    path = quotes_path()
    with lock_for(path):
        atomic_write_json(path, quotes)


def replace_quote(quote_id, quote):
    path = quotes_path()
    with lock_for(path):
        quotes = _read_quote_list(path) if os.path.exists(path) else load_quotes()
        for index, row in enumerate(quotes):
            if row.get('id') == quote_id:
                quotes[index] = quote
                atomic_write_json(path, quotes)
                return quote
    return None


def load_orders():
    path = orders_path()
    with lock_for(path):
        data = read_json(path, [])
    return data if isinstance(data, list) else []


def save_orders(orders):
    if not isinstance(orders, list):
        raise TypeError('orders must be a list')
    path = orders_path()
    with lock_for(path):
        atomic_write_json(path, orders)


def append_order(order):
    path = orders_path()
    with lock_for(path):
        orders = read_json(path, [])
        if not isinstance(orders, list):
            orders = []
        stored = dict(order)
        stored['id'] = max([item.get('id') or 0 for item in orders] or [0]) + 1
        orders.append(stored)
        atomic_write_json(path, orders)
        return stored


def update_order(order_id, mutator):
    """Replace one order under the file lock.

    mutator(order) returns (new_order, error). An error leaves the file unchanged.
    """
    path = orders_path()
    with lock_for(path):
        orders = read_json(path, [])
        if not isinstance(orders, list):
            orders = []
        for index, row in enumerate(orders):
            if row.get('id') == order_id:
                updated, error = mutator(dict(row))
                if error:
                    return None, error
                orders[index] = updated
                atomic_write_json(path, orders)
                return updated, None
    return None, 'Not found'


def punches_path():
    explicit = os.environ.get('PUNCHES_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'punches.json')


def load_punches():
    path = punches_path()
    with lock_for(path):
        data = read_json(path, [])
    return data if isinstance(data, list) else []


def save_punches(punches):
    if not isinstance(punches, list):
        raise TypeError('punches must be a list')
    path = punches_path()
    with lock_for(path):
        atomic_write_json(path, punches)


def append_punch(punch):
    path = punches_path()
    with lock_for(path):
        punches = read_json(path, [])
        if not isinstance(punches, list):
            punches = []
        stored = dict(punch)
        stored['id'] = max([item.get('id') or 0 for item in punches] or [0]) + 1
        punches.append(stored)
        atomic_write_json(path, punches)
        return stored


def kiosks_path():
    explicit = os.environ.get('KIOSKS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'kiosks.json')


def load_kiosks():
    path = kiosks_path()
    with lock_for(path):
        data = read_json(path, [])
    return data if isinstance(data, list) else []


def save_kiosks(kiosks):
    if not isinstance(kiosks, list):
        raise TypeError('kiosks must be a list')
    path = kiosks_path()
    with lock_for(path):
        atomic_write_json(path, kiosks)


def time_settings_path():
    explicit = os.environ.get('TIME_SETTINGS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'time_settings.json')


def load_time_settings():
    data = read_json(time_settings_path(), {})
    if not isinstance(data, dict):
        data = {}
    else:
        data = dict(data)
    days = data.get('photo_retention_days', 90)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 90
    if days < 90:
        days = 90
    data['photo_retention_days'] = days
    return data


def save_time_settings(settings):
    if not isinstance(settings, dict):
        raise TypeError('settings must be an object')
    path = time_settings_path()
    with lock_for(path):
        atomic_write_json(path, settings)


def punch_photo_dir():
    path = os.path.join(data_dir(), 'punch_photos')
    os.makedirs(path, exist_ok=True)
    return path


def update_punch(punch_id, mutator):
    """Replace one punch under the file lock. mutator returns (new_punch, error)."""
    path = punches_path()
    with lock_for(path):
        punches = read_json(path, [])
        if not isinstance(punches, list):
            punches = []
        for index, row in enumerate(punches):
            if row.get('id') == punch_id:
                updated, error = mutator(dict(row), punches)
                if error:
                    return None, error
                punches[index] = updated
                atomic_write_json(path, punches)
                return updated, None
    return None, 'Not found'


def invoices_path():
    explicit = os.environ.get('INVOICES_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'invoices.json')


def load_invoices():
    path = invoices_path()
    with lock_for(path):
        data = read_json(path, [])
    return data if isinstance(data, list) else []


def save_invoices(invoices):
    if not isinstance(invoices, list):
        raise TypeError('invoices must be a list')
    path = invoices_path()
    with lock_for(path):
        atomic_write_json(path, invoices)


def payments_path():
    explicit = os.environ.get('PAYMENTS_PATH', '').strip()
    if explicit:
        return explicit
    return os.path.join(data_dir(), 'payments.json')


def load_payments():
    path = payments_path()
    with lock_for(path):
        data = read_json(path, [])
    return data if isinstance(data, list) else []


def save_payments(payments):
    if not isinstance(payments, list):
        raise TypeError('payments must be a list')
    path = payments_path()
    with lock_for(path):
        atomic_write_json(path, payments)


def append_payment(payment):
    path = payments_path()
    with lock_for(path):
        payments = read_json(path, [])
        if not isinstance(payments, list):
            payments = []
        stored = dict(payment)
        stored['id'] = max([item.get('id') or 0 for item in payments] or [0]) + 1
        payments.append(stored)
        atomic_write_json(path, payments)
        return stored


def append_invoice(invoice):
    path = invoices_path()
    with lock_for(path):
        invoices = read_json(path, [])
        if not isinstance(invoices, list):
            invoices = []
        stored = dict(invoice)
        stored['id'] = max([item.get('id') or 0 for item in invoices] or [0]) + 1
        prefix = 'CN' if stored.get('doc_type') == 'credit_note' else 'INV'
        pattern = re.compile(rf'{prefix}-(\d+)')
        highest = 0
        for item in invoices:
            match = pattern.fullmatch(str(item.get('number') or ''))
            if match:
                highest = max(highest, int(match.group(1)))
        if not stored.get('number'):
            stored['number'] = f'{prefix}-{highest + 1:04d}'
        invoices.append(stored)
        atomic_write_json(path, invoices)
        return stored



def append_quote(quote):
    path = quotes_path()
    with lock_for(path):
        quotes = load_quotes()
        stored = dict(quote)
        stored['id'] = max([item.get('id') or 0 for item in quotes] or [0]) + 1
        quotes.append(stored)
        atomic_write_json(path, quotes)
        return stored


def load_customers():
    data = read_json(customers_path(), {})
    return data if isinstance(data, dict) else {}


def save_customers(customers):
    if not isinstance(customers, dict):
        raise TypeError('customers must be an object')
    path = customers_path()
    with lock_for(path):
        atomic_write_json(path, customers)


def load_users():
    data = read_json(users_path(), {})
    return data if isinstance(data, dict) else {}


def save_users(users):
    if not isinstance(users, dict):
        raise TypeError('users must be an object')
    path = users_path()
    with lock_for(path):
        atomic_write_json(path, users)


def default_tiers():
    return json.loads(json.dumps(DEFAULT_TIERS))


def load_tiers():
    path = tiers_path()
    with lock_for(path):
        stored = read_json(path, None)
        defaults = default_tiers()
        if not isinstance(stored, dict):
            atomic_write_json(path, defaults)
            return defaults
        changed = False
        for key in list(stored):
            if key not in TIER_ORDER:
                del stored[key]
                changed = True
        for key, value in defaults.items():
            current = stored.get(key)
            if not isinstance(current, dict):
                stored[key] = value
                changed = True
                continue
            for field, fallback in value.items():
                if field not in current:
                    current[field] = fallback
                    changed = True
        # Earlier builds labeled T5 "Tier 5" or "End User". The displayed name is "Tier 5 (End User)".
        t5 = stored.get('T5')
        if isinstance(t5, dict) and t5.get('label') in {None, '', 'Tier 5', 'End User'}:
            t5['label'] = 'Tier 5 (End User)'
            changed = True
        if changed:
            atomic_write_json(path, stored)
        return stored


def save_tiers(tiers):
    if not isinstance(tiers, dict):
        raise TypeError('tiers must be an object')
    path = tiers_path()
    with lock_for(path):
        atomic_write_json(path, tiers)


def _json_list(path_fn):
    data = read_json(path_fn(), [])
    return data if isinstance(data, list) else []


def _json_dict(path_fn):
    data = read_json(path_fn(), {})
    return data if isinstance(data, dict) else {}


def _save_json(path_fn, payload):
    path = path_fn()
    with lock_for(path):
        atomic_write_json(path, payload)


def shifts_path():
    return os.path.join(data_dir(), 'shifts.json')


def load_shifts():
    return _json_list(shifts_path)


def save_shifts(shifts):
    if not isinstance(shifts, list):
        raise TypeError('shifts must be a list')
    _save_json(shifts_path, shifts)


def shift_assignments_path():
    return os.path.join(data_dir(), 'shift_assignments.json')


def load_shift_assignments():
    return _json_dict(shift_assignments_path)


def save_shift_assignments(assignments):
    if not isinstance(assignments, dict):
        raise TypeError('assignments must be an object')
    _save_json(shift_assignments_path, assignments)


def pay_rates_path():
    return os.path.join(data_dir(), 'pay_rates.json')


def load_pay_rates():
    return _json_dict(pay_rates_path)


def save_pay_rates(rates):
    if not isinstance(rates, dict):
        raise TypeError('rates must be an object')
    _save_json(pay_rates_path, rates)


def pay_adjustments_path():
    return os.path.join(data_dir(), 'pay_adjustments.json')


def load_pay_adjustments():
    return _json_list(pay_adjustments_path)


def save_pay_adjustments(adjustments):
    if not isinstance(adjustments, list):
        raise TypeError('adjustments must be a list')
    _save_json(pay_adjustments_path, adjustments)


def paystubs_path():
    return os.path.join(data_dir(), 'paystubs.json')


def load_paystubs():
    return _json_list(paystubs_path)


def save_paystubs(stubs):
    if not isinstance(stubs, list):
        raise TypeError('paystubs must be a list')
    _save_json(paystubs_path, stubs)


def payroll_audit_path():
    return os.path.join(data_dir(), 'payroll_audit.json')


def load_payroll_audit():
    return _json_list(payroll_audit_path)


def append_payroll_audit(entry):
    path = payroll_audit_path()
    with lock_for(path):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        rows.append(dict(entry))
        atomic_write_json(path, rows)


def ordered_tiers(tiers=None):
    tiers = tiers if tiers is not None else load_tiers()
    keys = [key for key in TIER_ORDER if key in tiers]
    keys.extend(key for key in tiers if key not in TIER_ORDER)
    return [(key, tiers[key]) for key in keys]

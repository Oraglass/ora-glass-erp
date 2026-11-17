"""Country currencies and the hand-set USD/CAD rate.

The rate is not a daily Bank of Canada quote. An administrator records it,
and an invoice uses the rate whose effective date is on or before the invoice date.
"""

import logging
from datetime import date, datetime, timezone

from storage import read_json, data_dir
import os

logger = logging.getLogger('ora_glass.currency')

DEFAULT_RATE = 1.4
COUNTRIES = ('Canada', 'United States')


def _path():
    return os.path.join(data_dir(), 'currency_settings.json')


def _blank():
    return {
        'countries': {'Canada': 'cad', 'United States': 'usd'},
        'history': [{
            'rate': DEFAULT_RATE,
            'effective': '2000-01-01',
            'at': '2000-01-01T00:00:00+00:00',
            'user': 'system',
        }],
    }


def load_currency_settings():
    raw = read_json(_path(), {})
    settings = _blank()
    if not isinstance(raw, dict):
        return settings
    stored = raw.get('countries') if isinstance(raw.get('countries'), dict) else {}
    for name in COUNTRIES:
        value = str(stored.get(name) or '').strip().lower()
        if value in {'usd', 'cad'}:
            settings['countries'][name] = value
    history = []
    for row in raw.get('history') or []:
        if not isinstance(row, dict):
            continue
        try:
            rate = float(row.get('rate'))
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        effective = str(row.get('effective') or '')[:10]
        if len(effective) != 10:
            continue
        history.append({
            'rate': rate,
            'effective': effective,
            'at': str(row.get('at') or ''),
            'user': str(row.get('user') or ''),
        })
    if history:
        settings['history'] = history
    return settings


def save_currency_settings(settings):
    from storage import atomic_write_json, lock_for
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock_for(path):
        atomic_write_json(path, settings)


def rate_on(day):
    """CAD per 1 USD in effect on this date. Returns (rate, effective date)."""
    if isinstance(day, datetime):
        text = day.date().isoformat()
    elif isinstance(day, date):
        text = day.isoformat()
    else:
        text = str(day or '')[:10]
    if len(text) != 10:
        text = date.today().isoformat()
    chosen = None
    for row in load_currency_settings()['history']:
        effective = row['effective']
        if effective > text:
            continue
        if chosen is None or effective > chosen['effective'] or (
            effective == chosen['effective'] and str(row.get('at') or '') >= str(chosen.get('at') or '')
        ):
            chosen = row
    if chosen is None:
        chosen = _blank()['history'][0]
    return float(chosen['rate']), chosen['effective']


def record_rate(rate, effective, user_email):
    value = float(rate)
    if value <= 0:
        raise ValueError('Exchange rate must be greater than zero.')
    day = str(effective or date.today().isoformat())[:10]
    if len(day) != 10:
        raise ValueError('Effective date is not a date.')
    settings = load_currency_settings()
    settings['history'].append({
        'rate': round(value, 6),
        'effective': day,
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'user': user_email or '',
    })
    save_currency_settings(settings)
    return settings


def save_country_map(countries):
    settings = load_currency_settings()
    for name in COUNTRIES:
        value = str((countries or {}).get(name) or '').strip().lower()
        if value not in {'usd', 'cad'}:
            raise ValueError(f'{name} must be USD or CAD.')
        settings['countries'][name] = value
    save_currency_settings(settings)
    return settings


def customer_currency(customer):
    """The currency this selling company uses. Canada is CAD unless an admin changes the map."""
    import quickbooks
    settings = load_currency_settings()
    if quickbooks.is_canadian(customer):
        return settings['countries'].get('Canada') or 'cad'
    return settings['countries'].get('United States') or 'usd'


def migrate_customer_currencies(customers):
    """Canadian companies, including Open Windows, keep their credit currency in CAD."""
    import quickbooks
    changed = []
    for customer_id, customer in (customers or {}).items():
        if not isinstance(customer, dict) or customer.get('books_entity'):
            continue
        name = str(customer.get('name') or '').strip().casefold()
        if customer_id == 'open-windows' or name in {'open windows', 'open windows inc'}:
            if customer.get('country') != 'Canada':
                customer['country'] = 'Canada'
                changed.append((customer_id, customer.get('name') or customer_id, 'country'))
        if not quickbooks.is_canadian(customer):
            continue
        current = str(customer.get('credit_currency') or 'usd').lower()
        required = customer_currency(customer)
        if current != required:
            customer['credit_currency'] = required
            changed.append((customer_id, customer.get('name') or customer_id, required))
            logger.info(
                'Set credit currency to %s for %s (%s).',
                required.upper(), customer.get('name') or customer_id, customer_id,
            )
    return changed

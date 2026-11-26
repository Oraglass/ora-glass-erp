"""Remind staff when a packed order has not been shipped or delivered.

The list is computed from the orders. A daily loop records that the check ran.
Nothing is emailed.
"""

import logging
import os
import threading
import time
from datetime import date, datetime, timezone

from storage import atomic_write_json, data_dir, lock_for, read_json

logger = logging.getLogger('ora_glass.packed_reminders')
DEFAULT_DAYS = 30
CHECK_INTERVAL = 24 * 60 * 60
_STARTED = False


def _settings_path():
    return os.path.join(data_dir(), 'order_reminder_settings.json')


def _check_path():
    return os.path.join(data_dir(), 'stale_packed_check.json')


def _stamp(moment=None):
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.replace(microsecond=0).isoformat()


def threshold_days():
    """Whole days a Packed order may sit before the reminder. Default 30."""
    raw = read_json(_settings_path(), {})
    if not isinstance(raw, dict):
        raw = {}
    value = raw.get('stale_packed_days', DEFAULT_DAYS)
    if isinstance(value, bool):
        return DEFAULT_DAYS
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days >= 1 else DEFAULT_DAYS


def save_threshold(days):
    if isinstance(days, bool):
        return None, 'Enter a whole number of days.'
    try:
        value = int(days)
    except (TypeError, ValueError):
        return None, 'Enter a whole number of days.'
    if value < 1 or value > 3650:
        return None, 'The reminder must be between 1 and 3650 days.'
    path = _settings_path()
    with lock_for(path):
        atomic_write_json(path, {'stale_packed_days': value})
    return value, None


def _as_date(value):
    text = str(value or '')[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def apply_pack_status(order, status, moment=None):
    """Start the packed clock when the order becomes Packed, and clear it otherwise."""
    if status == 'packed':
        if not order.get('packed_at'):
            order['packed_at'] = _stamp(moment)
    else:
        order['packed_at'] = ''
    return order


def packed_on(order):
    """The day the order first became Packed. Falls back to the packing-list log."""
    stamped = _as_date((order or {}).get('packed_at'))
    if stamped:
        return stamped
    earliest = None
    for entry in (order or {}).get('changelog') or []:
        if not isinstance(entry, dict):
            continue
        hit = False
        for change in entry.get('changes') or []:
            if isinstance(change, dict) and change.get('field') == 'pack status' and change.get('new') == 'Packed':
                hit = True
                break
        if not hit:
            continue
        found = _as_date(entry.get('at'))
        if found and (earliest is None or found < earliest):
            earliest = found
    if earliest:
        return earliest
    for item in (order or {}).get('packing_lists') or []:
        if not isinstance(item, dict) or not item.get('current', True):
            continue
        found = _as_date(item.get('uploaded_at'))
        if found and (earliest is None or found < earliest):
            earliest = found
    return earliest


def shipped_or_delivered(order):
    """True once the order, or one of its shipments, is shipped or delivered."""
    status = str((order or {}).get('status') or '').lower()
    if status in {'delivered', 'voided'}:
        return True
    for shipment in (order or {}).get('shipments') or []:
        if not isinstance(shipment, dict):
            continue
        if str(shipment.get('status') or '').lower() in {'shipped', 'delivered'}:
            return True
    return False


def _dismissed_for(order, packed):
    record = (order or {}).get('stale_packed_dismissed')
    if not isinstance(record, dict) or not str(record.get('reason') or '').strip():
        return False
    return _as_date(record.get('packed_at')) == packed


def stale_orders(orders, today=None, threshold=None):
    """Packed orders older than the threshold that are still not shipped or delivered."""
    today = today or date.today()
    limit = DEFAULT_DAYS if threshold is None else threshold
    found = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if (order.get('pack_status') or '') != 'packed':
            continue
        if shipped_or_delivered(order):
            continue
        packed = packed_on(order)
        if not packed:
            continue
        days = (today - packed).days
        if days <= limit:
            continue
        if _dismissed_for(order, packed):
            continue
        found.append({
            'id': order.get('id'),
            'company': order.get('company') or '',
            'customer_id': order.get('customer_id') or '',
            'project': order.get('project') or '',
            'packed_on': packed.isoformat(),
            'days': days,
            'status': order.get('status') or '',
        })
    found.sort(key=lambda row: (-(row.get('days') or 0), row.get('id') or 0))
    return found


def dismiss(order, user, reason, moment=None):
    """Hide the reminder for this packed date and keep the reason on the order."""
    packed = packed_on(order)
    if (order or {}).get('pack_status') != 'packed' or not packed:
        return None, 'This order is not packed.'
    text = str(reason or '').replace('\x00', '').strip()
    if not text:
        return None, 'Enter a reason.'
    text = text[:300]
    when = _stamp(moment)
    person = (user or {}).get('email') or (user or {}).get('phone') or (user or {}).get('name') or ''
    order['stale_packed_dismissed'] = {
        'at': when,
        'by': person,
        'reason': text,
        'packed_at': packed.isoformat(),
    }
    log = list(order.get('changelog') or [])
    log.append({
        'at': when,
        'user': person,
        'changes': [{
            'field': 'stale packed reminder',
            'action': 'dismissed',
            'new': text,
        }],
    })
    order['changelog'] = log
    return order, None


def check_view():
    record = read_json(_check_path(), {})
    if not isinstance(record, dict):
        record = {}
    return {
        'at': record.get('at') or '',
        'count': record.get('count') or 0,
        'threshold': record.get('threshold') or threshold_days(),
        'order_ids': record.get('order_ids') or [],
    }


def run_check(today=None):
    """Record the orders that are stale today. At most the caller decides how often."""
    from storage import load_orders
    rows = stale_orders(load_orders(), today=today, threshold=threshold_days())
    record = {
        'at': _stamp(),
        'count': len(rows),
        'threshold': threshold_days(),
        'order_ids': [row.get('id') for row in rows],
    }
    path = _check_path()
    with lock_for(path):
        atomic_write_json(path, record)
    return record


def _loop():
    while True:
        try:
            run_check()
        except Exception:
            logger.exception('Stale packed reminder check failed.')
        time.sleep(CHECK_INTERVAL)


def start_daily_check():
    """Start the in-process daily check. Production only, and only once in this process."""
    global _STARTED
    if _STARTED or os.environ.get('APP_ENV') != 'production':
        return None
    _STARTED = True
    threading.Thread(target=_loop, name='stale-packed-reminder', daemon=True).start()
    return True

"""Rack inventory. A rack number comes from the packing list, such as 1054.

Importing a packing list allocates those racks to the order's customer as
Packed. The overdue clock starts only when the order is marked Delivered.
A driver scan confirms the rack and does not open a second trip. Removing or
re-reading the packing list adjusts that allocation.
"""

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from storage import atomic_write_json, damage_reports_path, lock_for, racks_path, read_json

OVERDUE_DAYS = 14
PACIFIC = ZoneInfo('America/Los_Angeles')
WEEKLY_REPORT_HOUR = 7
_WEEKLY_STARTED = False
_ROLE_LABELS = {
    'admin': 'Admin',
    'driver': 'Driver',
    'sales': 'Sales',
    'employee': 'Employee',
    'customer': 'Customer',
    'shop': 'Shop',
    'production': 'Production',
    'viewer': 'Viewer',
}
_PACKED_NOTE = re.compile(r'^Packed on (.+)\.$')
_REUSE_NOTE = re.compile(r'packing list (\S+)')
AT_ORA = 'at_ora'
PACKED = 'packed'
AT_CUSTOMER = 'at_customer'
RETURNED = 'returned'
DAMAGED = 'damaged'
STATUS_LABELS = {
    AT_ORA: 'At Ora Glass',
    PACKED: 'Packed / allocated to customer',
    AT_CUSTOMER: 'At customer',
    RETURNED: 'Returned',
    DAMAGED: 'Damaged',
}
MAX_DAMAGE_PHOTOS = 8
MAX_DAMAGE_EDGE = 1600
REPORT_OPEN = 'open'
REPORT_REPAIRED = 'repaired'
REPORT_CHARGED = 'charged'
REPORT_LABELS = {
    REPORT_OPEN: 'To be repaired',
    REPORT_REPAIRED: 'Repaired, no cost',
    REPORT_CHARGED: 'Charge customer',
}


def _now(moment):
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.replace(microsecond=0)


def _stamp(moment=None):
    return _now(moment).isoformat()


def _days_since(value, today):
    text = str(value or '')[:10]
    if not text:
        return None
    try:
        start = date.fromisoformat(text)
    except ValueError:
        return None
    return (today - start).days


def rack_number_from_scan(text):
    """Existing QR labels contain only the rack number."""
    return str(text if text is not None else '').strip()


def _rack_numbers(text):
    """Rack numbers typed on the shipping panel, including a comma-separated list."""
    import re
    found = []
    for part in str(text or '').split(','):
        match = re.match(r'^(\d{3,})\b', part.strip())
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def racks_for_shipment(order, shipment):
    """Racks named on the shipment, plus current packing lists whose pieces shipped."""
    serials = set()
    for line in (shipment or {}).get('lines') or []:
        try:
            serials.add(int(line.get('serial')))
        except (TypeError, ValueError):
            continue
    names = {}
    for entry in (order or {}).get('packing_lists') or []:
        number = str(entry.get('rack') or '').strip()
        if number and entry.get('rack_name') and number not in names:
            names[number] = str(entry.get('rack_name') or '').strip()
    found = []
    seen = set()
    for number in _rack_numbers((shipment or {}).get('rack')):
        seen.add(number)
        found.append((number, names.get(number, '')))
    for entry in (order or {}).get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        hit = False
        for line in entry.get('lines') or []:
            try:
                serial = int(line.get('serial'))
            except (TypeError, ValueError):
                continue
            if serial in serials:
                hit = True
                break
        number = str(entry.get('rack') or '').strip()
        if hit and number and number not in seen:
            seen.add(number)
            found.append((number, str(entry.get('rack_name') or '').strip()))
    return found


def _blank(number, name=''):
    return {
        'number': number,
        'name': name,
        'status': AT_ORA,
        'customer_id': '',
        'customer_name': '',
        'since': '',
        'order_id': None,
        'shipment_id': None,
        'shipment_number': '',
        'truck_plate': '',
        'history': [],
    }


def _actor(user):
    person = user or {}
    login = person.get('email') or person.get('phone') or person.get('name') or ''
    return login, person.get('name') or '', person.get('role') or ''


def _scan_method(value):
    text = str(value or '').strip().lower()
    return text if text in {'camera', 'gun', 'typed'} else ''


def _location_of(row):
    status = (row or {}).get('status')
    if status == AT_CUSTOMER:
        return (row or {}).get('customer_name') or 'Customer'
    return 'Ora Glass'


def _event(action, user, moment, **extra):
    login, name, role = _actor(user)
    row = {
        'at': _stamp(moment),
        'action': action,
        'user': login,
        'user_name': name,
        'user_role': role,
        'customer_id': '',
        'customer_name': '',
        'customer_before_id': '',
        'customer_before_name': '',
        'customer_after_id': '',
        'customer_after_name': '',
        'location_before': '',
        'location_after': '',
        'order_id': None,
        'shipment_id': None,
        'shipment_number': '',
        'truck_plate': '',
        'packing_number': '',
        'scan_method': '',
        'method': '',
        'note': '',
    }
    row.update(extra)
    if row.get('scan_method'):
        row['scan_method'] = _scan_method(row.get('scan_method'))
    return _complete_event(row)


def _complete_event(event):
    """Fill the newer history fields. Existing notes and times stay as they are."""
    row = dict(event or {})
    note = str(row.get('note') or '')
    if not row.get('packing_number'):
        packed = _PACKED_NOTE.match(note)
        reused = _REUSE_NOTE.search(note)
        if packed:
            row['packing_number'] = packed.group(1).strip()
        elif reused:
            row['packing_number'] = reused.group(1).strip()
    action = row.get('action')
    customer_id = row.get('customer_id') or ''
    customer_name = row.get('customer_name') or ''
    if action == 'delivered':
        row['location_before'] = row.get('location_before') or 'Ora Glass'
        row['location_after'] = row.get('location_after') or customer_name
        row['customer_after_id'] = row.get('customer_after_id') or customer_id
        row['customer_after_name'] = row.get('customer_after_name') or customer_name
    elif action in {'returned', 'picked_up'}:
        row['location_before'] = row.get('location_before') or customer_name
        row['location_after'] = row.get('location_after') or 'Ora Glass'
        row['customer_before_id'] = row.get('customer_before_id') or customer_id
        row['customer_before_name'] = row.get('customer_before_name') or customer_name
    elif action == 'packed':
        row['location_after'] = row.get('location_after') or 'Ora Glass'
        row['customer_after_id'] = row.get('customer_after_id') or customer_id
        row['customer_after_name'] = row.get('customer_after_name') or customer_name
    elif action == 'scanned':
        place = row.get('location_before') or customer_name or 'Ora Glass'
        row['location_before'] = row.get('location_before') or place
        row['location_after'] = row.get('location_after') or row.get('location_before')
    for key in (
        'user_name', 'user_role', 'scan_method', 'method', 'packing_number',
        'location_before', 'location_after',
        'customer_before_id', 'customer_before_name',
        'customer_after_id', 'customer_after_name',
    ):
        row.setdefault(key, '')
    return row


def _complete_history(row):
    history = []
    for item in (row or {}).get('history') or []:
        if isinstance(item, dict):
            history.append(_complete_event(item))
        else:
            history.append(item)
    row['history'] = history
    return row


def _mutate(change):
    path = racks_path()
    with lock_for(path):
        racks = read_json(path, {})
        if not isinstance(racks, dict):
            racks = {}
        result = change(racks)
        atomic_write_json(path, racks)
        return result


def _shipment_delivered(order, shipment):
    """A shipment is delivered only after its own status, or the order, says so."""
    status = str((shipment or {}).get('status') or '').lower()
    if status == 'delivered':
        return True
    if status:
        return False
    return str((order or {}).get('status') or '').lower() == 'delivered'


def _delivery_day(order, shipment, moment=None):
    explicit = str((shipment or {}).get('delivered_on') or '')[:10]
    if explicit:
        return explicit
    for entry in reversed((order or {}).get('changelog') or []):
        for change in entry.get('changes') or []:
            if change.get('field') == 'status' and str(change.get('new') or '') == 'Delivered':
                found = str(entry.get('at') or '')[:10]
                if found:
                    return found
    dated = str((shipment or {}).get('date') or '')[:10]
    if dated:
        return dated
    return _stamp(moment)[:10]


def _delivered_shipments(order):
    found = []
    for shipment in (order or {}).get('shipments') or []:
        if _shipment_delivered(order, shipment):
            found.append(shipment)
    return found


def _apply_customer(row, order, shipment, when, name, user, moment):
    before_place = _location_of(row)
    before_id = row.get('customer_id') or ''
    before_name = row.get('customer_name') or ''
    if name:
        row['name'] = name
    row['status'] = AT_CUSTOMER
    row['customer_id'] = (order or {}).get('customer_id') or ''
    row['customer_name'] = (order or {}).get('company') or ''
    row['since'] = when
    row['order_id'] = (order or {}).get('id')
    row['shipment_id'] = (shipment or {}).get('id')
    row['shipment_number'] = (shipment or {}).get('number') or ''
    row['truck_plate'] = (shipment or {}).get('truck_plate') or ''
    delivered = _event(
        'delivered', user, moment,
        customer_id=row['customer_id'],
        customer_name=row['customer_name'],
        customer_before_id=before_id,
        customer_before_name=before_name,
        customer_after_id=row['customer_id'],
        customer_after_name=row['customer_name'],
        location_before=before_place,
        location_after=row['customer_name'],
        order_id=row['order_id'],
        shipment_number=row['shipment_number'],
        truck_plate=row['truck_plate'],
        packing_number=row.get('packing_number') or '',
        note=f"Delivered on {row['shipment_number'] or 'delivery'}.",
    )
    row['delivered_at'] = delivered['at']
    row.setdefault('history', []).append(delivered)
    return row


def receive_shipment(order, shipment, user, moment=None):
    """Older callers. A recorded shipment does not put racks at the customer."""
    if not _shipment_delivered(order, shipment):
        return []
    probe = dict(order or {})
    shipments = list(probe.get('shipments') or [])
    if shipment not in shipments:
        shipments.append(shipment)
    probe['shipments'] = shipments
    return deliver_order(probe, user, moment)


def _reuse_note(label):
    return f'Auto-returned (reused on packing list {label})'


def _already_noted(row, note):
    return any((item.get('note') or '') == note for item in (row or {}).get('history') or [])


def _assign_packed(row, order, number, label, name, user, moment=None):
    """Pack this rack for the order. A rack still at a customer is auto-returned first."""
    if row.get('status') == AT_CUSTOMER and str(row.get('packing_number') or '') == str(label):
        return row
    if row.get('status') == AT_CUSTOMER and str(row.get('packing_number') or '') != str(label):
        note = _reuse_note(label)
        if not _already_noted(row, note):
            previous_id = row.get('customer_id') or ''
            previous_name = row.get('customer_name') or ''
            row.setdefault('history', []).append(_event(
                'returned', user, moment,
                customer_id=previous_id,
                customer_name=previous_name,
                customer_before_id=previous_id,
                customer_before_name=previous_name,
                location_before=previous_name or 'Customer',
                location_after='Ora Glass',
                order_id=row.get('order_id'),
                shipment_number=row.get('shipment_number') or '',
                packing_number=label,
                note=note,
            ))
        row['status'] = RETURNED
        row['customer_id'] = ''
        row['customer_name'] = ''
        row['since'] = ''
        row['truck_plate'] = ''
    if name:
        row['name'] = name
    same = (
        row.get('status') == PACKED
        and row.get('order_id') == (order or {}).get('id')
        and str(row.get('packing_number') or '') == str(label)
    )
    row['status'] = PACKED
    row['number'] = number
    row['customer_id'] = (order or {}).get('customer_id') or ''
    row['customer_name'] = (order or {}).get('company') or ''
    row['order_id'] = (order or {}).get('id')
    row['packing_number'] = label
    row['since'] = ''
    row['shipment_id'] = None
    row['shipment_number'] = ''
    row['truck_plate'] = ''
    if not same:
        row.setdefault('history', []).append(_event(
            'packed', user, moment,
            customer_id=row['customer_id'],
            customer_name=row['customer_name'],
            customer_after_id=row['customer_id'],
            customer_after_name=row['customer_name'],
            location_before='Ora Glass',
            location_after='Ora Glass',
            order_id=row['order_id'],
            packing_number=label,
            method='packing list import',
            note=f'Packed on {label}.',
        ))
    return row


def _release_packed(row, user, moment, note):
    """Take a packed allocation off the customer. A delivered rack is left alone."""
    if row.get('status') != PACKED:
        return row
    before_id = row.get('customer_id') or ''
    before_name = row.get('customer_name') or ''
    label = row.get('packing_number') or ''
    order_id = row.get('order_id')
    row['status'] = AT_ORA
    row['customer_id'] = ''
    row['customer_name'] = ''
    row['since'] = ''
    row['order_id'] = None
    row['packing_number'] = ''
    row['shipment_id'] = None
    row['shipment_number'] = ''
    row['truck_plate'] = ''
    row.setdefault('history', []).append(_event(
        'unallocated', user, moment,
        customer_before_id=before_id,
        customer_before_name=before_name,
        location_before=before_name or 'Customer',
        location_after='Ora Glass',
        order_id=order_id,
        packing_number=label,
        method='packing list import',
        note=note,
    ))
    return row


def _import_actor(user):
    person = user or {}
    if person.get('email') or person.get('phone') or person.get('name'):
        return person
    return {'email': 'system import', 'name': 'System import', 'role': 'system'}


def current_packing_racks(order):
    """Rack numbers on the packing lists that are still current."""
    found = {}
    for entry in (order or {}).get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        numbers = [
            str(number).strip()
            for number in (entry.get('racks') or [])
            if str(number or '').strip()
        ]
        if not numbers and str(entry.get('rack') or '').strip():
            numbers = [str(entry.get('rack')).strip()]
        label = str(entry.get('number') or entry.get('rack') or '').strip()
        name = str(entry.get('rack_name') or '').strip()
        for number in numbers:
            found[number] = (label or number, name)
    return [(number, label, name) for number, (label, name) in found.items()]


def sync_order_racks(order, user, moment=None):
    """Match rack inventory to the order's current packing lists.

    Racks on a current list are allocated to the customer. Racks that were
    packed for this order and are no longer on a list are released. A rack
    already delivered is not moved.
    """
    actor = _import_actor(user)
    wanted = {number: (label, name) for number, label, name in current_packing_racks(order)}
    order_id = (order or {}).get('id')

    def change(racks):
        for number, (label, name) in wanted.items():
            row = racks.get(number) or _blank(number, name)
            racks[number] = _assign_packed(row, order, number, label, name, actor, moment)
        for number, row in list(racks.items()):
            if not isinstance(row, dict) or row.get('order_id') != order_id:
                continue
            if row.get('status') != PACKED or number in wanted:
                continue
            racks[number] = _release_packed(
                row, actor, moment, f'Packing list no longer includes rack {number}.',
            )
        return True

    return _mutate(change)


def release_order_packing(order_id, user, moment=None):
    """Release every rack still only packed for this order."""
    actor = _import_actor(user)

    def change(racks):
        for number, row in list(racks.items()):
            if not isinstance(row, dict) or row.get('order_id') != order_id:
                continue
            if row.get('status') != PACKED:
                continue
            racks[number] = _release_packed(
                row, actor, moment, f'Packing list removed from order {order_id}.',
            )
        return True

    return _mutate(change)


def record_packing(order, number, packing_number, name, user, moment=None):
    """A packing list records the rack as packed at Ora Glass.

    When the rack is still in a customer's inventory, record the return first,
    then pack it for this order.
    """
    number = str(number or '').strip()
    if not number:
        return None
    label = packing_number or number
    actor = _import_actor(user)

    def change(racks):
        row = racks.get(number) or _blank(number, name)
        racks[number] = _assign_packed(row, order, number, label, name, actor, moment)
        return racks[number].get('status')

    return _mutate(change)


def note_packing_return(number, packing_number, name, user, moment=None):
    """Kept for callers that only know the rack. Prefer record_packing with the order."""
    return record_packing(
        {'id': None, 'customer_id': '', 'company': ''},
        number, packing_number, name, user, moment,
    )


def deliver_order(order, user, moment=None):
    """Move racks on delivered shipments into that customer's inventory."""
    delivered = _delivered_shipments(order)
    if not delivered:
        return []

    def change(racks):
        moved = []
        for shipment in delivered:
            when = _delivery_day(order, shipment, moment)
            for number, name in racks_for_shipment(order, shipment):
                row = racks.get(number)
                if row and row.get('status') == AT_CUSTOMER:
                    continue
                row = row or _blank(number, name)
                racks[number] = _apply_customer(row, order, shipment, when, name, user, moment)
                if number not in moved:
                    moved.append(number)
        return moved

    return _mutate(change)


def _clock(value, end=False):
    text = str(value or '').strip()
    if not text:
        return ''
    if len(text) == 10:
        return text + ('T23:59:59+00:00' if end else 'T00:00:00+00:00')
    return text


def _moment_from_clock(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _rack_timeline(orders):
    """Packing lists, oldest first, with each delivered shipment placed on that day."""
    events = []
    for order in orders or []:
        for entry in (order or {}).get('packing_lists') or []:
            number = str(entry.get('rack') or '').strip()
            if not number:
                continue
            events.append({
                'kind': 'pack',
                'time': _clock(entry.get('uploaded_at') or entry.get('date')),
                'order_id': order.get('id') or 0,
                'seq': entry.get('id') or 0,
                'number': number,
                'order': order,
                'entry': entry,
            })
        for shipment in _delivered_shipments(order):
            when = _clock(shipment.get('delivered_on') or _delivery_day(order, shipment), end=True)
            for number, name in racks_for_shipment(order, shipment):
                events.append({
                    'kind': 'deliver',
                    'time': when,
                    'order_id': order.get('id') or 0,
                    'seq': shipment.get('id') or 0,
                    'number': number,
                    'name': name,
                    'order': order,
                    'shipment': shipment,
                })
    events.sort(key=lambda item: (item.get('time') or '', 0 if item.get('kind') == 'pack' else 1, item.get('order_id') or 0, item.get('seq') or 0))
    return events


def _play(row, event, user):
    moment = _moment_from_clock(event.get('time'))
    actor = user or {}
    if event['kind'] == 'pack':
        entry = event['entry']
        listed = entry.get('uploaded_by') or ''
        if listed:
            actor = {'email': listed}
        label = entry.get('number') or event['number']
        return _assign_packed(
            row, event['order'], event['number'], label, entry.get('rack_name') or '', actor, moment,
        )
    shipment = event['shipment']
    if (
        row.get('status') == AT_CUSTOMER
        and (
            (shipment.get('id') and row.get('shipment_id') == shipment.get('id') and row.get('order_id') == event['order'].get('id'))
            or (shipment.get('number') and row.get('shipment_number') == shipment.get('number') and row.get('order_id') == event['order'].get('id'))
        )
    ):
        return row
    when = _delivery_day(event['order'], shipment, moment)
    return _apply_customer(row, event['order'], shipment, when, event.get('name') or '', actor, moment)


def _events_after_delivery(row, events):
    """Events after the delivery that put this rack in the customer's inventory."""
    if row.get('status') != AT_CUSTOMER:
        return []
    later = []
    passed = False
    for event in events:
        if event.get('number') != str(row.get('number') or ''):
            continue
        if not passed and event['kind'] == 'deliver':
            shipment = event['shipment']
            same_order = row.get('order_id') == event['order'].get('id')
            same_shipment = (
                (shipment.get('id') and row.get('shipment_id') == shipment.get('id'))
                or (shipment.get('number') and row.get('shipment_number') == shipment.get('number'))
            )
            if same_order and same_shipment:
                passed = True
            continue
        if passed:
            later.append(event)
    return later


def backfill_from_orders(orders, user=None):
    """Replay packing lists in date order, then delivered shipments, without duplicating history."""
    events = _rack_timeline(orders)
    by_number = {}
    for event in events:
        by_number.setdefault(event['number'], []).append(event)

    def change(racks):
        for number, row in list(racks.items()):
            if isinstance(row, dict):
                racks[number] = _complete_history(row)
        created = []
        for number, rows in by_number.items():
            existing = racks.get(number)
            if existing is None:
                row = _blank(number)
                for event in rows:
                    row = _play(row, event, user)
                racks[number] = row
                created.append(number)
                continue
            if existing.get('status') == PACKED:
                last_pack = None
                trailing = None
                for event in rows:
                    if event['kind'] == 'pack':
                        last_pack = event
                        trailing = None
                    elif event['kind'] == 'deliver':
                        trailing = event
                label = str((last_pack or {}).get('entry', {}).get('number') or '')
                if trailing and (not label or str(existing.get('packing_number') or '') == label):
                    existing = _play(existing, trailing, user)
            for event in _events_after_delivery(existing, rows):
                existing = _play(existing, event, user)
            racks[number] = existing
        return created

    return _mutate(change)


def record_scan(raw, user, scan_method='', moment=None):
    """Keep a scan on the rack. An unknown number does not create a rack."""
    number = rack_number_from_scan(raw)
    method = _scan_method(scan_method) or 'typed'
    if not number:
        return False

    def change(racks):
        row = racks.get(number)
        if not isinstance(row, dict):
            return False
        place = _location_of(row)
        row.setdefault('history', []).append(_event(
            'scanned', user, moment,
            customer_id=row.get('customer_id') or '',
            customer_name=row.get('customer_name') or '',
            customer_before_id=row.get('customer_id') or '',
            customer_before_name=row.get('customer_name') or '',
            customer_after_id=row.get('customer_id') or '',
            customer_after_name=row.get('customer_name') or '',
            location_before=place,
            location_after=place,
            order_id=row.get('order_id'),
            shipment_number=row.get('shipment_number') or '',
            packing_number=row.get('packing_number') or '',
            scan_method=method,
            note=f'Scanned by {method}.',
        ))
        racks[number] = row
        return True

    return _mutate(change)


def check_scan(racks, raw, customer_id='', already=None):
    """Classify one scan before pickup.

    With no customer id, the rack's current customer is the answer. A chosen
    customer still warns when the rack is somewhere else.
    """
    rack = rack_number_from_scan(raw)
    raw_text = '' if raw is None else str(raw)
    seen = {str(item).strip() for item in (already or [])}
    if not rack:
        return {'result': 'unknown', 'rack': '', 'raw': raw_text, 'message': 'That scan did not include a rack number.', 'customer_id': '', 'customer_name': ''}
    if rack in seen:
        return {'result': 'already', 'rack': rack, 'raw': raw_text, 'message': 'Already scanned.', 'customer_id': '', 'customer_name': ''}
    row = (racks or {}).get(rack)
    if not row:
        return {
            'result': 'unknown',
            'rack': rack,
            'raw': raw_text,
            'message': 'Unknown rack. Not at a customer.',
            'customer_id': '',
            'customer_name': '',
        }
    holder_id = row.get('customer_id') or ''
    holder_name = row.get('customer_name') or ''
    at_customer = row.get('status') == AT_CUSTOMER and bool(holder_id)
    if not at_customer:
        where = STATUS_LABELS.get(row.get('status'), 'Ora Glass')
        return {
            'result': 'not_at_customer',
            'rack': rack,
            'raw': raw_text,
            'message': f'Not at a customer. {where}.',
            'customer_id': '',
            'customer_name': '',
            'name': row.get('name') or '',
        }
    if customer_id and holder_id != customer_id:
        return {
            'result': 'wrong_customer',
            'rack': rack,
            'raw': raw_text,
            'message': f'This rack is not at this customer. It is at {holder_name or "another customer"}.',
            'customer_id': holder_id,
            'customer_name': holder_name,
            'name': row.get('name') or '',
        }
    return {
        'result': 'ok',
        'rack': rack,
        'raw': raw_text,
        'message': holder_name,
        'customer_id': holder_id,
        'customer_name': holder_name,
        'name': row.get('name') or '',
    }


def confirm_pickup(numbers, customer_id, customer_name, truck_plate, user, moment=None, methods=None, damage=None):
    """Bring the scanned racks back to Ora Glass.

    An empty customer id uses the customer each rack is at now. The pickup is
    still written on that rack's history.
    """
    wanted = []
    for number in numbers or []:
        cleaned = rack_number_from_scan(number)
        if cleaned and cleaned not in wanted:
            wanted.append(cleaned)
    chosen = methods or {}
    flagged = damage if isinstance(damage, dict) else {}

    def change(racks):
        moved = []
        skipped = []
        reports = []
        for number in wanted:
            verdict = check_scan(racks, number, customer_id, moved)
            if verdict['result'] != 'ok':
                skipped.append(verdict)
                continue
            row = racks[number]
            held_id = row.get('customer_id') or customer_id
            held_name = row.get('customer_name') or customer_name
            held_order = row.get('order_id')
            held_shipment = row.get('shipment_id')
            held_shipment_number = row.get('shipment_number') or ''
            info = flagged.get(number) if isinstance(flagged.get(number), dict) else None
            damaged = info is not None
            note = str((info or {}).get('note') or '').strip()
            photos = [
                {'id': str(photo.get('id'))}
                for photo in ((info or {}).get('photos') or [])
                if isinstance(photo, dict) and photo.get('id')
            ]
            row['status'] = DAMAGED if damaged else RETURNED
            if damaged:
                row['damaged'] = True
                row['damage_note'] = note
                row['damage_photos'] = photos
            row['customer_id'] = ''
            row['customer_name'] = ''
            row['since'] = ''
            row['order_id'] = None
            row['shipment_id'] = None
            row['shipment_number'] = ''
            row['truck_plate'] = ''
            summary = 'Picked up damaged.' if damaged else 'Picked up and returned.'
            if damaged and note:
                summary = summary + ' ' + note
            event_id = uuid.uuid4().hex if damaged else ''
            event = _event(
                'picked_up', user, moment,
                customer_id=held_id,
                customer_name=held_name,
                customer_before_id=held_id,
                customer_before_name=held_name,
                location_before=held_name or 'Customer',
                location_after='Ora Glass',
                order_id=held_order,
                shipment_id=held_shipment,
                shipment_number=held_shipment_number,
                truck_plate=truck_plate or '',
                packing_number=row.get('packing_number') or '',
                scan_method=_scan_method(chosen.get(number)),
                note=summary,
                damaged=damaged,
                damage_note=note if damaged else '',
                photos=photos,
                event_id=event_id,
            )
            row.setdefault('history', []).append(event)
            racks[number] = row
            moved.append(number)
            if damaged:
                login, name, role = _actor(user)
                reports.append({
                    'status': REPORT_OPEN,
                    'rack': number,
                    'customer_id': held_id,
                    'customer_name': held_name,
                    'order_id': held_order,
                    'shipment_id': held_shipment,
                    'shipment_number': held_shipment_number,
                    'note': note,
                    'photos': photos,
                    'driver': login,
                    'driver_name': name,
                    'driver_role': role,
                    'at': event.get('at') or '',
                    'event_id': event_id,
                    'packing_number': row.get('packing_number') or '',
                    'invoice_id': None,
                    'invoice_number': '',
                    'closed_at': '',
                    'closed_by': '',
                })
        return {'moved': moved, 'skipped': skipped, 'reports': reports}

    result = _mutate(change)
    drafts = (result or {}).get('reports') or []
    if drafts:
        result['reports'] = _store_damage_reports(drafts)
    return result


def mark_returned(number, user, moment=None):
    """An admin takes one rack out of the customer's inventory."""
    number = str(number or '').strip()
    if not number:
        return None

    def change(racks):
        row = racks.get(number)
        if not isinstance(row, dict) or row.get('status') != AT_CUSTOMER:
            return None
        previous_id = row.get('customer_id') or ''
        previous_name = row.get('customer_name') or ''
        row['status'] = RETURNED
        row['customer_id'] = ''
        row['customer_name'] = ''
        row['since'] = ''
        row['truck_plate'] = ''
        row.setdefault('history', []).append(_event(
            'returned', user, moment,
            customer_id=previous_id,
            customer_name=previous_name,
            customer_before_id=previous_id,
            customer_before_name=previous_name,
            location_before=previous_name or 'Customer',
            location_after='Ora Glass',
            order_id=row.get('order_id'),
            shipment_number=row.get('shipment_number') or '',
            packing_number=row.get('packing_number') or '',
            note='Marked returned.',
        ))
        racks[number] = row
        return dict(row)

    return _mutate(change)


def clear_damage(number, user, moment=None, note='Cleared the damaged flag.', action='damage_cleared', customer_id='', customer_name=''):
    """Clear the damaged flag and log who did it. Pickup photos stay on the history event."""
    number = str(number or '').strip()
    if not number:
        return None

    def change(racks):
        row = racks.get(number)
        if not isinstance(row, dict):
            return None
        if row.get('status') != DAMAGED and not row.get('damaged') and action == 'damage_cleared':
            return None
        reported = {}
        for item in reversed(row.get('history') or []):
            if isinstance(item, dict) and item.get('damaged'):
                reported = item
                break
        if row.get('status') == DAMAGED:
            row['status'] = RETURNED
        row['damaged'] = False
        row['damage_note'] = ''
        row['damage_photos'] = []
        held_id = customer_id or reported.get('customer_id') or ''
        held_name = customer_name or reported.get('customer_name') or ''
        row.setdefault('history', []).append(_event(
            action, user, moment,
            customer_id=held_id,
            customer_name=held_name,
            customer_before_id=held_id,
            customer_before_name=held_name,
            location_before='Ora Glass',
            location_after='Ora Glass',
            packing_number=row.get('packing_number') or '',
            note=note,
        ))
        racks[number] = row
        return dict(row)

    return _mutate(change)


def _store_damage_reports(drafts):
    path = damage_reports_path()
    with lock_for(path):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        next_id = max((int(item.get('id') or 0) for item in rows if isinstance(item, dict)), default=0) + 1
        stored = []
        for draft in drafts:
            row = dict(draft)
            row['id'] = next_id
            next_id += 1
            row['status'] = REPORT_OPEN
            rows.append(row)
            stored.append(row)
        atomic_write_json(path, rows)
        return stored


def load_damage_reports():
    from storage import load_damage_reports as load_rows
    rows = load_rows()
    return [row for row in rows if isinstance(row, dict)]


def damage_report(report_id):
    try:
        wanted = int(report_id)
    except (TypeError, ValueError):
        return None
    return next((row for row in load_damage_reports() if row.get('id') == wanted), None)


def present_damage_report(row):
    shown = dict(row or {})
    shown['status_label'] = REPORT_LABELS.get(shown.get('status'), shown.get('status') or '')
    shown['at_label'] = pacific_label(shown.get('at'))
    shown['open'] = shown.get('status') == REPORT_OPEN
    shown['photos'] = [
        {'id': str(photo.get('id'))}
        for photo in (shown.get('photos') or [])
        if isinstance(photo, dict) and photo.get('id')
    ]
    return shown


def filter_damage_reports(rows, status='', customer_id='', rack='', start='', end=''):
    wanted_status = str(status or '').strip()
    wanted_customer = str(customer_id or '').strip()
    wanted_rack = rack_number_from_scan(rack) if rack else ''
    start_day = str(start or '').strip()[:10]
    end_day = str(end or '').strip()[:10]
    found = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if wanted_status and row.get('status') != wanted_status:
            continue
        if wanted_customer and row.get('customer_id') != wanted_customer:
            continue
        if wanted_rack and str(row.get('rack') or '') != wanted_rack:
            continue
        day = str(row.get('at') or '')[:10]
        if start_day and day and day < start_day:
            continue
        if end_day and day and day > end_day:
            continue
        found.append(present_damage_report(row))
    found.sort(key=lambda item: (str(item.get('at') or ''), item.get('id') or 0), reverse=True)
    return found


def open_damage_count():
    return sum(1 for row in load_damage_reports() if row.get('status') == REPORT_OPEN)


def close_damage_report(report_id, user, outcome, moment=None, invoice_id=None, invoice_number=''):
    """Close an open damage report and clear the rack's damaged flag."""
    if outcome not in {REPORT_REPAIRED, REPORT_CHARGED}:
        return None, 'Choose repaired or charge.'
    try:
        wanted = int(report_id)
    except (TypeError, ValueError):
        return None, 'That report was not found.'
    path = damage_reports_path()
    with lock_for(path):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        report = next((row for row in rows if isinstance(row, dict) and row.get('id') == wanted), None)
        if not report:
            return None, 'That report was not found.'
        if report.get('status') != REPORT_OPEN:
            return None, 'That report is already closed.'
        report['status'] = outcome
        report['closed_at'] = _stamp(moment)
        login, _name, _role = _actor(user)
        report['closed_by'] = login
        if outcome == REPORT_CHARGED:
            report['invoice_id'] = invoice_id
            report['invoice_number'] = invoice_number or ''
        atomic_write_json(path, rows)
    if outcome == REPORT_REPAIRED:
        note = 'Repaired, no cost.'
        action = 'damage_repaired'
    else:
        number = invoice_number or 'the repair invoice'
        note = f'Charged the customer. Invoice {number}.'
        action = 'damage_charged'
    clear_damage(
        report.get('rack'), user, moment,
        note=note, action=action,
        customer_id=report.get('customer_id') or '',
        customer_name=report.get('customer_name') or '',
    )
    return present_damage_report(report), None


def photo_root():
    folder = os.path.join(os.path.dirname(racks_path()), 'rack-photos')
    os.makedirs(folder, exist_ok=True)
    return folder


def _safe_rack_number(number):
    text = rack_number_from_scan(number)
    if text and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', text):
        return text
    return ''


def damage_photo_path(number, photo_id):
    rack = _safe_rack_number(number)
    token = str(photo_id or '')
    if not rack or not re.fullmatch(r'[0-9a-f]{32}', token):
        return ''
    root = os.path.realpath(photo_root())
    path = os.path.realpath(os.path.join(root, rack, token + '.jpg'))
    if not path.startswith(root + os.sep):
        return ''
    return path


def store_damage_photo(number, raw):
    """Save one damage photo as a JPEG no wider or taller than 1600px."""
    rack = _safe_rack_number(number)
    if not rack or not raw or len(raw) > 12_000_000:
        return None
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        image = ImageOps.exif_transpose(Image.open(BytesIO(raw))).convert('RGB')
    except Exception:
        return None
    image.thumbnail((MAX_DAMAGE_EDGE, MAX_DAMAGE_EDGE))
    import uuid
    photo_id = uuid.uuid4().hex
    folder = os.path.join(photo_root(), rack)
    os.makedirs(folder, exist_ok=True)
    image.save(os.path.join(folder, photo_id + '.jpg'), 'JPEG', quality=80, optimize=True)
    return {'id': photo_id}


def discard_damage_photo(number, photo_id):
    path = damage_photo_path(number, photo_id)
    if path and os.path.isfile(path):
        os.remove(path)


def load_rows():
    from storage import load_racks
    return load_racks()


def held_count(customer_id, racks=None):
    rows = load_rows() if racks is None else racks
    return sum(
        1 for row in rows.values()
        if isinstance(row, dict) and row.get('status') == AT_CUSTOMER and row.get('customer_id') == customer_id
    )


def _event_date(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _stay_days(start, end):
    if not start or not end:
        return 0
    return max(0, (end - start).days)


def _round_days(total, count):
    if not count:
        return None
    value = total / count
    if value == int(value):
        return int(value)
    return round(value, 1)


def pacific_label(stamp):
    """Show a stored UTC time in Pacific."""
    moment = _moment_from_clock(stamp)
    if not moment:
        text = str(stamp or '').strip()
        return text
    return moment.astimezone(PACIFIC).strftime('%Y-%m-%d %H:%M') + ' Pacific'


def role_label(role):
    text = str(role or '').strip()
    return _ROLE_LABELS.get(text, text)


def lifetime_stats(row, today=None):
    """Trips are a delivery followed by a return. A rack still out is one open trip."""
    today = today or date.today()
    history = [item for item in (row or {}).get('history') or [] if isinstance(item, dict)]
    history.sort(key=lambda item: str(item.get('at') or ''))
    stays = []
    start = None
    for item in history:
        action = item.get('action')
        when = _event_date(item.get('at'))
        if action == 'delivered' and when:
            if start:
                stays.append((start, when, True))
            start = when
        elif action in {'returned', 'picked_up'} and start and when:
            stays.append((start, when, True))
            start = None
    open_trip = (row or {}).get('status') == AT_CUSTOMER
    if open_trip:
        current_start = _event_date((row or {}).get('since')) or start
        if current_start:
            stays.append((current_start, today, False))
    measured = [( _stay_days(begin, end), closed) for begin, end, closed in stays]
    total = sum(days for days, _closed in measured)
    count = len(measured)
    scans = [
        item for item in history
        if item.get('action') == 'scanned' or item.get('scan_method')
    ]
    last = scans[-1] if scans else {}
    status = (row or {}).get('status')
    if status == AT_CUSTOMER:
        current_days = _days_since((row or {}).get('since'), today)
        current_location = (row or {}).get('customer_name') or 'Customer'
    else:
        current_days = None
        current_location = 'Ora Glass'
    return {
        'trips': count,
        'trip_open': bool(open_trip and count),
        'total_days': total,
        'avg_days': _round_days(total, count),
        'longest_days': max((days for days, _closed in measured), default=None),
        'current_days': current_days,
        'current_location': current_location,
        'last_scan_at': last.get('at') or '',
        'last_scan_label': pacific_label(last.get('at')) if last else '',
        'last_scan_by': last.get('user_name') or last.get('user') or '',
        'last_scan_role': role_label(last.get('user_role')),
        'last_scan_method': last.get('scan_method') or '',
    }


def alert_days(customer):
    """Days at a customer before a rack is overdue. Missing or invalid values stay at 14."""
    raw = OVERDUE_DAYS if customer is None else (customer or {}).get('rack_alert_days', OVERDUE_DAYS)
    if isinstance(raw, bool):
        return OVERDUE_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return OVERDUE_DAYS
    return days if days >= 1 else OVERDUE_DAYS


def present(row, today=None, alert_after=None):
    today = today or date.today()
    days = _days_since(row.get('since'), today) if row.get('status') == AT_CUSTOMER else None
    limit = OVERDUE_DAYS if alert_after is None else alert_after
    shown = dict(row)
    history = []
    for item in row.get('history') or []:
        if not isinstance(item, dict):
            continue
        copy = _complete_event(item)
        copy['at_label'] = pacific_label(copy.get('at'))
        copy['role_label'] = role_label(copy.get('user_role'))
        copy['photos'] = [
            {'id': str(photo.get('id'))}
            for photo in (copy.get('photos') or [])
            if isinstance(photo, dict) and photo.get('id')
        ]
        history.append(copy)
    shown['history'] = history
    shown['status_label'] = STATUS_LABELS.get(row.get('status'), row.get('status') or '')
    shown['damaged'] = bool(row.get('damaged')) or row.get('status') == DAMAGED
    shown['damage_note'] = row.get('damage_note') or ''
    shown['damage_photos'] = [
        {'id': str(photo.get('id'))}
        for photo in (row.get('damage_photos') or [])
        if isinstance(photo, dict) and photo.get('id')
    ] if shown['damaged'] else []
    shown['days'] = days
    shown['alert_after'] = limit
    shown['overdue'] = days is not None and days > limit
    shown.update(lifetime_stats(row, today))
    shown['current_days'] = days
    return shown


def visible_rows(racks, user, status='', customer_id='', number='', today=None, customers=None):
    """Staff see every rack. A customer sees the racks their company is holding."""
    if customers is None:
        from storage import load_customers
        customers = load_customers()
    role = (user or {}).get('role') or ''
    wanted = str(number or '').strip()
    overdue_only = status == 'overdue'
    damaged_only = status == 'damaged'
    rows = []
    for number, row in (racks or {}).items():
        if not isinstance(row, dict):
            continue
        shown_number = str(row.get('number') or number)
        if wanted and shown_number != wanted:
            continue
        if role == 'customer':
            if row.get('status') != AT_CUSTOMER or row.get('customer_id') != user.get('customer_id'):
                continue
        else:
            if overdue_only:
                pass
            elif damaged_only:
                if row.get('status') != DAMAGED and not row.get('damaged'):
                    continue
            elif status and row.get('status') != status:
                continue
            if customer_id and row.get('customer_id') != customer_id:
                continue
        company = (customers or {}).get(row.get('customer_id') or '') or {}
        shown = present(row, today, alert_after=alert_days(company))
        shown['alert_customer'] = bool(company.get('show_overdue_racks'))
        if overdue_only and not shown.get('overdue'):
            continue
        shown['number'] = shown_number
        rows.append(shown)
    rows.sort(key=lambda row: str(row.get('number')))
    return rows


def _report_path():
    return os.path.join(os.path.dirname(racks_path()), 'rack_weekly_overdue.json')


def _pacific_now(now=None):
    if now is None:
        return datetime.now(PACIFIC)
    if now.tzinfo is None:
        return now.replace(tzinfo=PACIFIC)
    return now.astimezone(PACIFIC)


def overdue_rows(racks, today=None, customers=None):
    """Racks past that customer's own alert. A company with no setting still uses 14 days."""
    found = []
    for row in visible_rows(racks, {'role': 'admin'}, status='overdue', today=today, customers=customers):
        found.append({
            'number': row.get('number') or '',
            'customer_id': row.get('customer_id') or '',
            'customer_name': row.get('customer_name') or '',
            'days': row.get('days'),
            'delivered_on': str(row.get('since') or '')[:10],
            'packing_number': row.get('packing_number') or '',
        })
    found.sort(key=lambda item: (-(item.get('days') or 0), item.get('number') or ''))
    return found


def damaged_rows(racks):
    """Racks a driver marked damaged. The customer is the one on that pickup."""
    found = []
    for number, row in (racks or {}).items():
        if not isinstance(row, dict):
            continue
        if row.get('status') != DAMAGED and not row.get('damaged'):
            continue
        reported = {}
        for item in reversed(row.get('history') or []):
            if isinstance(item, dict) and item.get('damaged'):
                reported = item
                break
        photos = row.get('damage_photos') or reported.get('photos') or []
        found.append({
            'number': str(row.get('number') or number),
            'customer_id': reported.get('customer_id') or row.get('customer_id') or '',
            'customer_name': reported.get('customer_name') or row.get('customer_name') or '',
            'note': row.get('damage_note') or reported.get('damage_note') or '',
            'reported_by': reported.get('user_name') or reported.get('user') or '',
            'at': reported.get('at') or '',
            'photos': len([photo for photo in photos if isinstance(photo, dict) and photo.get('id')]),
        })
    found.sort(key=lambda item: item.get('number') or '')
    return found


def weekly_overdue_due(now=None):
    """True after 7:00 AM Pacific on Monday, until this week's report is stored."""
    now = _pacific_now(now)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=WEEKLY_REPORT_HOUR, minute=0, second=0, microsecond=0,
    )
    week = monday.date().isoformat()
    if now < monday:
        return False, week
    guard = read_json(_report_path(), {})
    if not isinstance(guard, dict):
        guard = {}
    return guard.get('week') != week, week


def run_weekly_overdue_if_due(now=None):
    """Write this week's overdue racks. At most once. Nothing is emailed."""
    path = _report_path()
    with lock_for(path):
        due, week = weekly_overdue_due(now)
        if not due:
            return None
        rows = overdue_rows(load_rows())
        record = {
            'week': week,
            'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            'rows': rows,
            'damaged': damaged_rows(load_rows()),
            'emailed': False,
        }
        atomic_write_json(path, record)
        return record


def weekly_report_view():
    record = read_json(_report_path(), {})
    if not isinstance(record, dict):
        record = {}
    at = str(record.get('at') or '')
    return {
        'at': at,
        'at_label': pacific_label(at) if at else '',
        'week': record.get('week') or '',
        'rows': record.get('rows') or [],
        'damaged': record.get('damaged') or [],
        'ran': bool(record.get('week')),
        'schedule': 'Monday at 7:00 AM Pacific',
    }


def _weekly_overdue_loop():
    import logging
    import time
    log = logging.getLogger('ora_glass.racks')
    while True:
        try:
            run_weekly_overdue_if_due()
        except Exception:
            log.exception('Weekly overdue rack report failed.')
        time.sleep(60)


def start_weekly_overdue_report():
    """Start the Monday report. Production only, on the one web process, same as the QuickBooks import."""
    global _WEEKLY_STARTED
    if _WEEKLY_STARTED or os.environ.get('APP_ENV') != 'production':
        return None
    _WEEKLY_STARTED = True
    import threading
    threading.Thread(target=_weekly_overdue_loop, name='rack-weekly-overdue', daemon=True).start()
    return True


def labels_pdf(rows):
    """One QR label per rack. The code is the rack number."""
    import io
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape
    from documents import _styles

    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    story = []
    chosen = list(rows or [])
    if not chosen:
        story.append(Paragraph('No racks to print.', styles['small']))
    for row in chosen:
        number = str(row.get('number') or '')
        widget = QrCodeWidget(number or '-')
        bounds = widget.getBounds()
        width = max(bounds[2] - bounds[0], 1)
        height = max(bounds[3] - bounds[1], 1)
        size = 90
        drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
        drawing.add(widget)
        story.append(drawing)
        story.append(Paragraph(f'<font size="28"><b>{escape(number)}</b></font>', styles['title']))
        if row.get('name'):
            story.append(Paragraph(escape(str(row.get('name'))), styles['small']))
        story.append(Spacer(1, 18))
    doc.build(story)
    return buffer.getvalue()

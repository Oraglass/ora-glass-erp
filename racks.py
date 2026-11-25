"""Rack inventory. A rack number comes from the packing list, such as 1054.

A packing list records the rack as packed at Ora Glass. It joins the customer's
inventory when a shipment that contains it is marked delivered. It leaves that
inventory when a driver scans the empty rack, or an admin marks it returned.
"""

from datetime import date, datetime, timezone

from storage import atomic_write_json, lock_for, racks_path, read_json

OVERDUE_DAYS = 14
AT_ORA = 'at_ora'
PACKED = 'packed'
AT_CUSTOMER = 'at_customer'
RETURNED = 'returned'
STATUS_LABELS = {
    AT_ORA: 'At Ora Glass',
    PACKED: 'At Ora Glass / Packed',
    AT_CUSTOMER: 'At customer',
    RETURNED: 'Returned',
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


def _event(action, user, moment, **extra):
    row = {
        'at': _stamp(moment),
        'action': action,
        'user': (user or {}).get('email') or (user or {}).get('name') or '',
        'customer_id': '',
        'customer_name': '',
        'order_id': None,
        'shipment_id': None,
        'shipment_number': '',
        'truck_plate': '',
        'note': '',
    }
    row.update(extra)
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
    row.setdefault('history', []).append(_event(
        'delivered', user, moment,
        customer_id=row['customer_id'],
        customer_name=row['customer_name'],
        order_id=row['order_id'],
        shipment_number=row['shipment_number'],
        truck_plate=row['truck_plate'],
        note=f"Delivered on {row['shipment_number'] or 'delivery'}.",
    ))
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


def record_packing(order, number, packing_number, name, user, moment=None):
    """A packing list records the rack as packed at Ora Glass.

    A rack already in a customer's inventory stays there. It leaves only when a
    driver scans the pickup or an admin marks it returned.
    """
    number = str(number or '').strip()
    if not number:
        return None
    label = packing_number or number

    def change(racks):
        existing = racks.get(number)
        if existing and existing.get('status') == AT_CUSTOMER:
            return existing.get('status')
        row = existing or _blank(number, name)
        if name:
            row['name'] = name
        same = (
            existing is not None
            and row.get('status') == PACKED
            and row.get('order_id') == (order or {}).get('id')
            and str(row.get('packing_number') or '') == str(label)
        )
        row['status'] = PACKED
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
                order_id=row['order_id'],
                note=f'Packed on {label}.',
            ))
        racks[number] = row
        return row.get('status')

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


def backfill_from_orders(orders, user=None):
    """Create rack rows from packing lists, and from shipments already delivered."""
    def change(racks):
        created = []
        for order in orders or []:
            delivered = {}
            for shipment in _delivered_shipments(order):
                when = _delivery_day(order, shipment)
                for number, name in racks_for_shipment(order, shipment):
                    delivered[number] = (shipment, name, when)
            seen = set()
            for entry in (order or {}).get('packing_lists') or []:
                if entry.get('current') is False:
                    continue
                number = str(entry.get('rack') or '').strip()
                if not number or number in seen:
                    continue
                seen.add(number)
                shipment = delivered.get(number)
                existing = racks.get(number)
                if existing:
                    if shipment and existing.get('status') == PACKED:
                        ship, name, when = shipment
                        racks[number] = _apply_customer(
                            existing, order, ship, when, name or entry.get('rack_name') or '', user or {}, None,
                        )
                    continue
                row = _blank(number, entry.get('rack_name') or '')
                row['order_id'] = order.get('id')
                row['customer_id'] = order.get('customer_id') or ''
                row['customer_name'] = order.get('company') or ''
                row['packing_number'] = entry.get('number') or ''
                if shipment:
                    ship, name, when = shipment
                    if name:
                        row['name'] = name
                    row['status'] = AT_CUSTOMER
                    row['since'] = when
                    row['shipment_id'] = ship.get('id')
                    row['shipment_number'] = ship.get('number') or ''
                    row['truck_plate'] = ship.get('truck_plate') or ''
                    action = 'delivered'
                    note = f"Delivered on {row['shipment_number'] or 'delivery'}."
                else:
                    row['status'] = PACKED
                    action = 'packed'
                    note = f"Packed on {row['packing_number'] or 'a packing list'}."
                row['history'] = [_event(
                    action, user or {}, None,
                    customer_id=row['customer_id'],
                    customer_name=row['customer_name'],
                    order_id=row['order_id'],
                    shipment_number=row.get('shipment_number') or '',
                    truck_plate=row.get('truck_plate') or '',
                    note=note,
                )]
                racks[number] = row
                created.append(number)
            for number, (ship, name, when) in delivered.items():
                if number in racks:
                    continue
                row = _blank(number, name)
                row['status'] = AT_CUSTOMER
                row['customer_id'] = order.get('customer_id') or ''
                row['customer_name'] = order.get('company') or ''
                row['since'] = when
                row['order_id'] = order.get('id')
                row['shipment_id'] = ship.get('id')
                row['shipment_number'] = ship.get('number') or ''
                row['truck_plate'] = ship.get('truck_plate') or ''
                row['history'] = [_event(
                    'delivered', user or {}, None,
                    customer_id=row['customer_id'],
                    customer_name=row['customer_name'],
                    order_id=row['order_id'],
                    shipment_number=row['shipment_number'],
                    truck_plate=row['truck_plate'],
                    note=f"Delivered on {row['shipment_number'] or 'delivery'}.",
                )]
                racks[number] = row
                created.append(number)
        return created

    return _mutate(change)


def check_scan(racks, raw, customer_id, already=None):
    """Classify one scan before the driver confirms the pickup."""
    rack = rack_number_from_scan(raw)
    raw_text = '' if raw is None else str(raw)
    seen = {str(item).strip() for item in (already or [])}
    if not rack:
        return {'result': 'unknown', 'rack': '', 'raw': raw_text, 'message': 'That scan did not include a rack number.'}
    if rack in seen:
        return {'result': 'already', 'rack': rack, 'raw': raw_text, 'message': 'Already scanned.'}
    row = (racks or {}).get(rack)
    if not row:
        return {
            'result': 'unknown',
            'rack': rack,
            'raw': raw_text,
            'message': 'Unknown rack.',
        }
    if row.get('status') != AT_CUSTOMER or row.get('customer_id') != customer_id:
        where = row.get('customer_name') or STATUS_LABELS.get(row.get('status'), 'Ora Glass')
        return {
            'result': 'wrong_customer',
            'rack': rack,
            'raw': raw_text,
            'message': f'This rack is not at this customer. It is at {where}.',
        }
    return {'result': 'ok', 'rack': rack, 'raw': raw_text, 'message': 'OK', 'name': row.get('name') or ''}


def confirm_pickup(numbers, customer_id, customer_name, truck_plate, user, moment=None):
    """Bring the scanned racks back to Ora Glass."""
    wanted = []
    for number in numbers or []:
        cleaned = rack_number_from_scan(number)
        if cleaned and cleaned not in wanted:
            wanted.append(cleaned)

    def change(racks):
        moved = []
        skipped = []
        for number in wanted:
            verdict = check_scan(racks, number, customer_id, moved)
            if verdict['result'] != 'ok':
                skipped.append(verdict)
                continue
            row = racks[number]
            row['status'] = RETURNED
            row['customer_id'] = ''
            row['customer_name'] = ''
            row['since'] = ''
            row['order_id'] = None
            row['shipment_id'] = None
            row['shipment_number'] = ''
            row['truck_plate'] = ''
            row.setdefault('history', []).append(_event(
                'picked_up', user, moment,
                customer_id=customer_id,
                customer_name=customer_name,
                truck_plate=truck_plate or '',
                note='Picked up and returned.',
            ))
            racks[number] = row
            moved.append(number)
        return {'moved': moved, 'skipped': skipped}

    return _mutate(change)


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
            order_id=row.get('order_id'),
            shipment_number=row.get('shipment_number') or '',
            note='Marked returned.',
        ))
        racks[number] = row
        return dict(row)

    return _mutate(change)


def load_rows():
    from storage import load_racks
    return load_racks()


def held_count(customer_id, racks=None):
    rows = load_rows() if racks is None else racks
    return sum(
        1 for row in rows.values()
        if isinstance(row, dict) and row.get('status') == AT_CUSTOMER and row.get('customer_id') == customer_id
    )


def present(row, today=None):
    today = today or date.today()
    days = _days_since(row.get('since'), today) if row.get('status') == AT_CUSTOMER else None
    shown = dict(row)
    shown['status_label'] = STATUS_LABELS.get(row.get('status'), row.get('status') or '')
    shown['days'] = days
    shown['overdue'] = days is not None and days > OVERDUE_DAYS
    return shown


def visible_rows(racks, user, status='', customer_id='', number='', today=None):
    """Staff see every rack. A customer sees the racks their company is holding."""
    role = (user or {}).get('role') or ''
    wanted = str(number or '').strip()
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
            if status and row.get('status') != status:
                continue
            if customer_id and row.get('customer_id') != customer_id:
                continue
        shown = present(row, today)
        shown['number'] = shown_number
        rows.append(shown)
    rows.sort(key=lambda row: str(row.get('number')))
    return rows


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

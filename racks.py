"""Rack inventory. A rack number comes from the packing list, such as 1054.

Shipping packed pieces puts those racks at the customer. A driver pickup, or a
new packing list for a rack that is still marked out, brings it back to Ora Glass.
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


def receive_shipment(order, shipment, user, moment=None):
    """Move every rack on the shipped packing lists to the customer. Unknown racks are created."""
    pairs = racks_for_shipment(order, shipment)
    if not pairs:
        return []

    def change(racks):
        moved = []
        for number, name in pairs:
            row = racks.get(number) or _blank(number)
            if name:
                row['name'] = name
            row['status'] = AT_CUSTOMER
            row['customer_id'] = order.get('customer_id') or ''
            row['customer_name'] = order.get('company') or ''
            row['since'] = shipment.get('date') or _stamp(moment)[:10]
            row['order_id'] = order.get('id')
            row['shipment_id'] = shipment.get('id')
            row['shipment_number'] = shipment.get('number') or ''
            row['truck_plate'] = shipment.get('truck_plate') or ''
            row.setdefault('history', []).append(_event(
                'shipped', user, moment,
                customer_id=row['customer_id'],
                customer_name=row['customer_name'],
                order_id=row['order_id'],
                shipment_number=row['shipment_number'],
                truck_plate=row['truck_plate'],
                note=f"Shipped on {row['shipment_number'] or 'a shipment'}.",
            ))
            racks[number] = row
            moved.append(number)
        return moved

    return _mutate(change)


def _shipment_for_rack(order, number):
    """The latest shipment that sent this rack, if the order has one."""
    found = None
    for shipment in (order or {}).get('shipments') or []:
        numbers = {item[0] for item in racks_for_shipment(order, shipment)}
        if number in numbers:
            found = shipment
    return found


def record_packing(order, number, packing_number, name, user, moment=None):
    """A packing list puts the rack at Ora Glass, packed for this order."""
    number = str(number or '').strip()
    if not number:
        return None
    label = packing_number or number

    def change(racks):
        row = racks.get(number) or _blank(number, name)
        if name:
            row['name'] = name
        events = []
        if row.get('status') == AT_CUSTOMER and number in racks:
            previous = row.get('customer_name') or ''
            events.append(_event(
                'returned', user, moment,
                customer_name=previous,
                note=f'Returned, inferred from packing list {label}.',
            ))
        same = (
            number in racks
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
            events.append(_event(
                'packed', user, moment,
                customer_id=row['customer_id'],
                customer_name=row['customer_name'],
                order_id=row['order_id'],
                note=f'Packed on {label}.',
            ))
        if events:
            row.setdefault('history', []).extend(events)
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
    """Mark this order's packed racks as delivered to the customer."""
    shipments = (order or {}).get('shipments') or []
    latest = shipments[-1] if shipments else {}

    def change(racks):
        moved = []
        for number, row in list(racks.items()):
            if not isinstance(row, dict) or row.get('status') != PACKED:
                continue
            if row.get('order_id') != (order or {}).get('id'):
                continue
            shipment = _shipment_for_rack(order, number) or latest
            row['status'] = AT_CUSTOMER
            row['customer_id'] = (order or {}).get('customer_id') or row.get('customer_id') or ''
            row['customer_name'] = (order or {}).get('company') or row.get('customer_name') or ''
            row['since'] = (shipment or {}).get('date') or _stamp(moment)[:10]
            row['shipment_id'] = (shipment or {}).get('id')
            row['shipment_number'] = (shipment or {}).get('number') or ''
            row['truck_plate'] = (shipment or {}).get('truck_plate') or ''
            row.setdefault('history', []).append(_event(
                'shipped', user, moment,
                customer_id=row['customer_id'],
                customer_name=row['customer_name'],
                order_id=row['order_id'],
                shipment_number=row['shipment_number'],
                truck_plate=row['truck_plate'],
                note=f"Delivered on {row['shipment_number'] or 'delivery'}.",
            ))
            racks[number] = row
            moved.append(number)
        return moved

    return _mutate(change)


def backfill_from_orders(orders, user=None):
    """Create missing racks from packing lists already on orders."""
    def change(racks):
        created = []
        for order in orders or []:
            for entry in (order or {}).get('packing_lists') or []:
                if entry.get('current') is False:
                    continue
                number = str(entry.get('rack') or '').strip()
                if not number or number in racks:
                    continue
                shipment = _shipment_for_rack(order, number)
                row = _blank(number, entry.get('rack_name') or '')
                row['order_id'] = order.get('id')
                row['customer_id'] = order.get('customer_id') or ''
                row['customer_name'] = order.get('company') or ''
                row['packing_number'] = entry.get('number') or ''
                if shipment:
                    row['status'] = AT_CUSTOMER
                    row['since'] = shipment.get('date') or ''
                    row['shipment_id'] = shipment.get('id')
                    row['shipment_number'] = shipment.get('number') or ''
                    row['truck_plate'] = shipment.get('truck_plate') or ''
                    action = 'shipped'
                    note = f"Shipped on {row['shipment_number'] or 'a shipment'}."
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

"""Read a production packing-list PDF and match its glass rows to an order.

The production software prints each piece's quantity on its own line, then the
inch size, the floor code, the product name (which may wrap), and the size in
millimetres. Staff review the match before a shipment is saved.
"""

import io
import re
from datetime import date

import production

_HEADER = re.compile(r'QTY\s+WIDTH\s+HEIGHT\s+FLOOR', re.I)
_DELIVERY = re.compile(
    r'Sales Delivery:\s*(\S+)\s+Date:\s*(\d{1,2})/(\d{1,2})/(\d{4})',
    re.I,
)
_JOB = re.compile(r'Job Name:\s*(.+)', re.I)
_CUSTOMER = re.compile(r'Customer\s*:\s*(.+)', re.I)
_PROJECT = re.compile(r'Project\s*:\s*(.+)', re.I)
_PO = re.compile(r'(?:P\.?\s*O\.?|Purchase Order)\s*#?\s*:\s*(.+)', re.I)
_SERIAL_LINE = re.compile(r'(?:serial|s/?n|sn)\s*[:#]?\s*(\d+)\s*$', re.I)
_PCS = re.compile(r'Total Pcs\s*:\s*(\d+)', re.I)
_SQFT = re.compile(r'Total SQ/?FT\s*:\s*([\d.]+)', re.I)
_INCHES = re.compile(
    r'^(\d+)(?:\s+(\d+)\s*/\s*(\d+))?\s+(\d+)(?:\s+(\d+)\s*/\s*(\d+))?$'
)
_MM = re.compile(r'^(\d+(?:\.\d+)?)\s*mm\s+(\d+(?:\.\d+)?)\s*mm$', re.I)
_FLOOR = re.compile(r'^([A-Za-z]+\d+)\s*(?:/\s*(\d+))?$')
_QTY = re.compile(r'^\d+$')


def _inches(whole, numerator, denominator):
    value = float(whole)
    if numerator and denominator and float(denominator):
        value += float(numerator) / float(denominator)
    return value


def _floor_parts(text):
    match = _FLOOR.fullmatch(str(text or '').strip())
    if not match:
        return str(text or '').strip().upper(), None
    return match.group(1).upper(), match.group(2)


def floors_match(order_floor, packing_floor):
    """Sequence counts only when the order line itself has one."""
    order_code, order_seq = _floor_parts(order_floor)
    pack_code, pack_seq = _floor_parts(packing_floor)
    if not order_code or order_code != pack_code:
        return False
    if order_seq is not None:
        return order_seq == pack_seq
    return True


def sizes_match(order_w, order_h, pack_w, pack_h, tolerance=1):
    def close(left, right):
        return abs(float(left) - float(right)) <= tolerance
    direct = close(order_w, pack_w) and close(order_h, pack_h)
    swapped = close(order_w, pack_h) and close(order_h, pack_w)
    return direct or swapped


def _norm_name(text):
    return re.sub(r'[^a-z0-9+]+', '', str(text or '').casefold())


def products_match(order_id, order_name, packing_name):
    if order_id and str(order_id) == str(packing_name or '').strip():
        return True
    left = _norm_name(order_name)
    right = _norm_name(packing_name)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def pdf_lines(payload):
    import pdfplumber
    lines = []
    with pdfplumber.open(io.BytesIO(payload)) as document:
        for page in document.pages:
            text = page.extract_text(layout=True) or page.extract_text() or ''
            lines.extend(text.splitlines())
    return lines


_ID_QTY = re.compile(r'^\((\d+)\)\s+(\d+)\b')
_INCH_PREFIX = re.compile(
    r'^(?P<w>\d+(?:\s+\d+\s*/\s*\d+)?)\s+(?P<h>\d+(?:\s+\d+\s*/\s*\d+)?)(?:\s|$)'
)
_MM_FIND = re.compile(r'(\d+(?:\.\d+)?)\s*mm\s+(\d+(?:\.\d+)?)\s*mm', re.I)
_SOLD_TO = re.compile(r'Sold\s+To:\s*(.+?)(?:\s{2,}|$)', re.I)
_RACK_VALUE = re.compile(r'^(\d{3,})\s+[A-Za-z]\d')
_FRACTION = re.compile(r'^(\d+)(?:\s+(\d+)\s*/\s*(\d+))?$')


def _fraction_inches(text):
    match = _FRACTION.fullmatch(str(text or '').strip())
    if not match:
        return None
    return _inches(match.group(1), match.group(2), match.group(3))


def _parse_id_qty_lines(cleaned):
    """Production lists print the serial as (1) and the quantity beside it.

    Width and height sit on the neighbouring lines. The wrapped IGU description
    is kept for reference and is not used to match an order.
    """
    header = {
        'number': '', 'date': '', 'job': '', 'customer': '', 'project': '', 'po': '',
        'rack': '', 'total_pcs': None, 'total_sqft': None,
    }
    for line in cleaned:
        delivery = _DELIVERY.search(line)
        if delivery and not header['number']:
            month, day, year = int(delivery.group(2)), int(delivery.group(3)), int(delivery.group(4))
            header['number'] = delivery.group(1)
            header['date'] = date(year, month, day).isoformat()
        sold = _SOLD_TO.search(line)
        if sold and not header['customer']:
            header['customer'] = sold.group(1).strip()
        job = _JOB.search(line)
        if job:
            header['job'] = job.group(1).strip()
        project = _PROJECT.search(line)
        if project:
            header['project'] = project.group(1).strip()
        customer = _CUSTOMER.search(line)
        if customer and not header['customer']:
            header['customer'] = customer.group(1).strip()
        pcs = _PCS.search(line)
        if pcs:
            header['total_pcs'] = int(pcs.group(1))
        sqft = _SQFT.search(line)
        if sqft:
            header['total_sqft'] = float(sqft.group(1))
        rack_value = _RACK_VALUE.match(line)
        if rack_value and not header['rack']:
            header['rack'] = rack_value.group(1)
    anchors = [(index, int(match.group(1)), int(match.group(2)))
               for index, line in enumerate(cleaned)
               for match in [_ID_QTY.match(line)] if match]
    rows = []
    for position, (index, serial, qty) in enumerate(anchors):
        previous = anchors[position - 1][0] if position else -1
        following = anchors[position + 1][0] if position + 1 < len(anchors) else len(cleaned)
        width_in = height_in = None
        for look in range(index - 1, previous, -1):
            found = _INCH_PREFIX.match(cleaned[look])
            if not found:
                continue
            width_in = _fraction_inches(found.group('w'))
            height_in = _fraction_inches(found.group('h'))
            if width_in and height_in:
                break
        width_mm = round(width_in * 25.4, 2) if width_in else None
        height_mm = round(height_in * 25.4, 2) if height_in else None
        for look in range(index + 1, following):
            millimetres = _MM_FIND.search(cleaned[look])
            if not millimetres:
                continue
            width_mm = float(millimetres.group(1))
            height_mm = float(millimetres.group(2))
            break
        rows.append({
            'serial': serial,
            'qty': qty,
            'floor': '',
            'description': '',
            'width_in': None if width_in is None else round(width_in, 4),
            'height_in': None if height_in is None else round(height_in, 4),
            'width_mm': width_mm,
            'height_mm': height_mm,
        })
    parsed_pcs = sum(row['qty'] for row in rows)
    warnings = []
    if header['total_pcs'] is not None and header['total_pcs'] != parsed_pcs:
        warnings.append(
            f"The packing list says {header['total_pcs']} pieces but the rows add up to {parsed_pcs}."
        )
    return {
        'number': header['number'],
        'date': header['date'],
        'job': header['job'],
        'customer': header['customer'],
        'project': header['project'] or header['job'],
        'po': header['po'],
        'rack': header['rack'],
        'total_pcs': header['total_pcs'],
        'total_sqft': header['total_sqft'],
        'parsed_pcs': parsed_pcs,
        'parsed_sqft': None,
        'rows': rows,
        'warnings': warnings,
    }


def parse_packing_lines(lines):
    """Turn packing-list text lines into a header and glass rows."""
    cleaned = [str(line or '').strip() for line in lines if str(line or '').strip()]
    if any(_ID_QTY.match(line) for line in cleaned):
        return _parse_id_qty_lines(cleaned)
    header = {
        'number': '',
        'date': '',
        'job': '',
        'customer': '',
        'project': '',
        'po': '',
        'rack': '',
        'total_pcs': None,
        'total_sqft': None,
    }
    rows = []
    index = 0
    serial_mode = False
    pending_serial = None
    while index < len(cleaned):
        line = cleaned[index]
        if _HEADER.search(line):
            serial_mode = 'serial' in line.casefold()
            index += 1
            continue
        delivery = _DELIVERY.search(line)
        if delivery:
            month, day, year = int(delivery.group(2)), int(delivery.group(3)), int(delivery.group(4))
            header['number'] = delivery.group(1)
            header['date'] = date(year, month, day).isoformat()
            index += 1
            continue
        job = _JOB.search(line)
        if job:
            header['job'] = job.group(1).strip()
            index += 1
            continue
        customer = _CUSTOMER.search(line)
        if customer:
            header['customer'] = customer.group(1).strip()
            index += 1
            continue
        project = _PROJECT.search(line)
        if project:
            header['project'] = project.group(1).strip()
            index += 1
            continue
        po = _PO.search(line)
        if po:
            header['po'] = po.group(1).strip()
            index += 1
            continue
        serial_label = _SERIAL_LINE.fullmatch(line)
        if serial_label:
            pending_serial = int(serial_label.group(1))
            index += 1
            continue
        pcs = _PCS.search(line)
        if pcs:
            header['total_pcs'] = int(pcs.group(1))
            index += 1
            continue
        sqft = _SQFT.search(line)
        if sqft:
            header['total_sqft'] = float(sqft.group(1))
            index += 1
            continue
        if 'rackcode' in line.casefold():
            header['rack'] = line.strip()
            index += 1
            continue
        if not _QTY.fullmatch(line):
            index += 1
            continue
        serial = pending_serial
        pending_serial = None
        qty = int(line)
        index += 1
        if serial_mode and serial is None and index < len(cleaned) and _QTY.fullmatch(cleaned[index]):
            serial = qty
            qty = int(cleaned[index])
            index += 1
        if index >= len(cleaned):
            break
        inch = _INCHES.fullmatch(cleaned[index])
        if not inch:
            continue
        width_in = _inches(inch.group(1), inch.group(2), inch.group(3))
        height_in = _inches(inch.group(4), inch.group(5), inch.group(6))
        index += 1
        floor = ''
        if index < len(cleaned) and _FLOOR.fullmatch(cleaned[index]):
            floor = re.sub(r'\s+', ' ', cleaned[index]).strip()
            index += 1
        description = []
        width_mm = round(width_in * 25.4, 2)
        height_mm = round(height_in * 25.4, 2)
        while index < len(cleaned):
            current = cleaned[index]
            if _HEADER.search(current) or _QTY.fullmatch(current):
                break
            millimetres = _MM.fullmatch(current)
            if millimetres:
                width_mm = float(millimetres.group(1))
                height_mm = float(millimetres.group(2))
                index += 1
                break
            description.append(current)
            index += 1
        rows.append({
            'serial': serial,
            'qty': qty,
            'floor': floor,
            'description': ' '.join(description).strip(),
            'width_in': round(width_in, 4),
            'height_in': round(height_in, 4),
            'width_mm': width_mm,
            'height_mm': height_mm,
        })
    parsed_pcs = sum(row['qty'] for row in rows)
    parsed_sqft = 0.0
    for row in rows:
        area = (row['width_mm'] / 25.4) * (row['height_mm'] / 25.4) / 144
        parsed_sqft += area * row['qty']
    warnings = []
    if header['total_pcs'] is not None and header['total_pcs'] != parsed_pcs:
        warnings.append(
            f"The packing list says {header['total_pcs']} pieces but the rows add up to {parsed_pcs}."
        )
    if header['total_sqft'] is not None and abs(header['total_sqft'] - parsed_sqft) > 0.5:
        warnings.append(
            f"The packing list says {header['total_sqft']} sq ft but the rows add up to {round(parsed_sqft, 2)}."
        )
    return {
        'number': header['number'],
        'date': header['date'],
        'job': header['job'],
        'customer': header['customer'],
        'project': header['project'] or header['job'],
        'po': header['po'],
        'rack': header['rack'],
        'total_pcs': header['total_pcs'],
        'total_sqft': header['total_sqft'],
        'parsed_pcs': parsed_pcs,
        'parsed_sqft': round(parsed_sqft, 2),
        'rows': rows,
        'warnings': warnings,
    }


def parse_packing_pdf(payload):
    return parse_packing_lines(pdf_lines(payload))


def match_packing(order, parsed):
    """Compare packing rows with the order's production lines."""
    from documents import piece_size_text
    items = (order or {}).get('items') or []
    candidates = []
    for index, item in enumerate(items):
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        if spec.get('width') in (None, '') or spec.get('height') in (None, ''):
            continue
        if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        exported = production.line_export(item, serial=len(candidates) + 1)
        from shipments import remaining_qty
        candidates.append({
            'index': index,
            'serial': exported['Serial Number'],
            'floor_code': exported['Floor Code'],
            'product_id': exported['Product ID'],
            'name': exported['Product Name'],
            'width_mm': exported['Width(mm)'],
            'height_mm': exported['Height(mm)'],
            'width_in': exported['Width(In)'],
            'height_in': exported['Height(In)'],
            'unit_price': exported['Unit Price'],
            'remarks': exported['Remarks'],
            'remaining': remaining_qty(order, index),
            'ordered': int(item.get('pcs') or 0),
        })
    used = {row['index']: 0 for row in candidates}
    matched_rows = []
    unmatched = []
    for packing_row in parsed.get('rows') or []:
        found = None
        for candidate in candidates:
            if not floors_match(candidate['floor_code'], packing_row['floor']):
                continue
            if not sizes_match(
                candidate['width_mm'], candidate['height_mm'],
                packing_row['width_mm'], packing_row['height_mm'],
            ):
                continue
            if not products_match(candidate['product_id'], candidate['name'], packing_row['description']):
                continue
            found = candidate
            break
        if not found:
            unmatched.append(packing_row)
            continue
        used[found['index']] += packing_row['qty']
        matched_rows.append({
            'index': found['index'],
            'serial': found['serial'],
            'floor_code': packing_row['floor'],
            'product_id': found['product_id'],
            'name': found['name'],
            'description': packing_row['description'],
            'qty': packing_row['qty'],
            'width_mm': packing_row['width_mm'],
            'height_mm': packing_row['height_mm'],
            'size': piece_size_text(packing_row['width_mm'], packing_row['height_mm'], 'mm'),
        })
    lines = []
    review = []
    warnings = list(parsed.get('warnings') or [])
    for candidate in candidates:
        qty = used[candidate['index']]
        if not qty:
            continue
        over = qty > candidate['remaining']
        if over:
            warnings.append(
                f"{candidate['name']} asks to ship {qty} but only {candidate['remaining']} are left."
            )
        lines.append({'index': candidate['index'], 'qty': qty})
        review.append({
            'index': candidate['index'],
            'serial': candidate['serial'],
            'floor_code': candidate['floor_code'],
            'product_id': candidate['product_id'],
            'name': candidate['name'],
            'size': piece_size_text(candidate['width_mm'], candidate['height_mm'], 'mm'),
            'qty': qty,
            'ordered': candidate['ordered'],
            'already_shipped': candidate['ordered'] - candidate['remaining'],
            'remaining': candidate['remaining'],
            'bo_after': max(0, candidate['remaining'] - qty),
            'over': over,
        })
    return {
        'number': parsed.get('number') or '',
        'date': parsed.get('date') or '',
        'rack': parsed.get('rack') or '',
        'job': parsed.get('job') or '',
        'warnings': warnings,
        'matched': review,
        'matched_rows': matched_rows,
        'unmatched': [
            dict(row, size=piece_size_text(row.get('width_mm'), row.get('height_mm'), 'mm'))
            if isinstance(row, dict) else row
            for row in unmatched
        ],
        'lines': lines,
        'header_pcs': parsed.get('total_pcs'),
        'header_sqft': parsed.get('total_sqft'),
        'parsed_pcs': parsed.get('parsed_pcs'),
        'parsed_sqft': parsed.get('parsed_sqft'),
    }


def _fold_name(text):
    cleaned = re.sub(r'\b(inc|ltd|llc|corp|corporation|company|co)\b', ' ', str(text or '').casefold())
    return re.sub(r'[^a-z0-9]+', '', cleaned)


def _fold_code(text):
    return re.sub(r'[^a-z0-9]+', '', str(text or '').casefold())


def _names_match(left, right):
    """A short production name such as HighQ matches HighQ Developments Inc."""
    first, second = _fold_name(left), _fold_name(right)
    if not first or not second or min(len(first), len(second)) < 3:
        return False
    if first == second or first in second or second in first:
        return True
    return first.startswith(second) or second.startswith(first)


def list_key(parsed):
    """Identity of one rack. A later upload with the same key replaces it."""
    number = str((parsed or {}).get('number') or '').strip()
    rack = str((parsed or {}).get('rack') or '').strip()
    return number.casefold() or rack.casefold()


def recognized_customer_ids(parsed, orders):
    """Company ids the packing-list customer name matches. Empty when it matches nobody."""
    name = (parsed or {}).get('customer') or ''
    if not str(name).strip():
        return []
    found = []
    for order in orders or []:
        customer_id = order.get('customer_id')
        if not customer_id or customer_id in found:
            continue
        if _names_match(name, order.get('company')):
            found.append(customer_id)
    return found


def orders_for_customer(customer_id, orders):
    """Orders staff may attach a packing list to. Delivered orders are closed."""
    rows = []
    for order in orders or []:
        if order.get('customer_id') != customer_id:
            continue
        if str(order.get('status') or '').lower() == 'delivered':
            continue
        rows.append(order)
    return rows


def candidate_orders(parsed, orders):
    """Orders whose customer and project agree. PO is checked only when both sides have one."""
    project = _fold_code((parsed or {}).get('project') or (parsed or {}).get('job'))
    customer = _fold_name((parsed or {}).get('customer'))
    po = _fold_code((parsed or {}).get('po'))
    if not project:
        return []
    hits = []
    for order in orders or []:
        if _fold_code(order.get('project')) != project:
            continue
        if customer and not _names_match((parsed or {}).get('customer'), order.get('company')):
            continue
        order_po = _fold_code(order.get('po') or order.get('customer_po'))
        if po and order_po and po != order_po:
            continue
        hits.append(order)
    return hits


def serial_catalog(order):
    """Production serials for one order. The serial is the Excel serial, not an IGU number."""
    from documents import piece_size_text
    catalog = []
    serial = 1
    for index, item in enumerate((order or {}).get('items') or []):
        if not isinstance(item, dict):
            continue
        if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        if spec.get('width') in (None, '') or spec.get('height') in (None, ''):
            continue
        exported = production.line_export(item, serial=serial)
        try:
            ordered = int(item.get('pcs') or 0)
        except (TypeError, ValueError):
            ordered = 0
        catalog.append({
            'serial': exported['Serial Number'],
            'index': index,
            'ordered': ordered,
            'floor': exported['Floor Code'],
            'product': exported['Product Name'],
            'size': piece_size_text(spec.get('width'), spec.get('height'), spec.get('unit')),
        })
        serial += 1
    return catalog


def classify_rows(order, parsed):
    """Keep rows whose production serial is on the order. The description is never checked."""
    catalog = {row['serial']: row for row in serial_catalog(order)}
    matched = []
    unmatched = []
    for row in (parsed or {}).get('rows') or []:
        try:
            serial = int(row.get('serial'))
        except (TypeError, ValueError):
            unmatched.append({
                'serial': None,
                'qty': row.get('qty') or 0,
                'description': row.get('description') or '',
                'reason': 'This row has no production serial number.',
            })
            continue
        if serial not in catalog:
            unmatched.append({
                'serial': serial,
                'qty': row.get('qty') or 0,
                'description': row.get('description') or '',
                'reason': f'Serial {serial} is not on this order.',
            })
            continue
        try:
            qty = int(row.get('qty') or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        matched.append({'serial': serial, 'qty': qty})
    combined = {}
    for row in matched:
        combined[row['serial']] = combined.get(row['serial'], 0) + row['qty']
    counted = []
    for serial, qty in combined.items():
        ordered = catalog[serial]['ordered']
        kept = min(qty, ordered)
        if kept > 0:
            counted.append({'serial': serial, 'qty': kept})
        if qty > ordered:
            unmatched.append({
                'serial': serial,
                'qty': qty - ordered,
                'description': '',
                'reason': f'Serial {serial} only has {ordered} on this order. {qty - ordered} were not counted.',
            })
    return counted, unmatched


def packing_progress(order):
    """Packed quantity is the sum of the latest list for each rack. BO is ordered minus packed."""
    catalog = serial_catalog(order)
    packed = {row['serial']: 0 for row in catalog}
    for entry in (order or {}).get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        for line in entry.get('lines') or []:
            try:
                serial = int(line.get('serial'))
                qty = int(line.get('qty') or 0)
            except (TypeError, ValueError):
                continue
            if serial in packed and qty > 0:
                packed[serial] += qty
    rows = []
    ordered_total = packed_total = bo_total = 0
    for item in catalog:
        got = min(packed[item['serial']], item['ordered'])
        bo = max(0, item['ordered'] - got)
        rows.append(dict(item, packed=got, bo=bo))
        ordered_total += item['ordered']
        packed_total += got
        bo_total += bo
    if packed_total <= 0:
        status = ''
    elif ordered_total > 0 and bo_total <= 0:
        status = 'packed'
    else:
        status = 'partial'
    label = {'packed': 'Packed', 'partial': 'Partially packed'}.get(status, '')
    return {
        'status': status,
        'status_label': label,
        'serials': rows,
        'totals': {'ordered': ordered_total, 'packed': packed_total, 'bo': bo_total},
    }


_COLUMN_ALIASES = {
    'serial': ('serial number', 'serial', 'sn', 's/n'),
    'qty': ('quantity', 'qty', 'pcs', 'pieces', 'packed', 'packed qty'),
    'customer': ('customer', 'company'),
    'project': ('project', 'job', 'job name'),
    'po': ('po', 'p.o.', 'po#', 'purchase order'),
    'floor': ('floor', 'floor code'),
    'rack': ('rack', 'rack code', 'rackcode'),
    'date': ('date', 'packing date'),
    'number': ('packing list', 'packing list number', 'sales delivery', 'delivery', 'number'),
    'description': ('description', 'item description', 'product', 'igu'),
}


def _header_map(headers):
    found = {}
    for index, header in enumerate(headers):
        label = re.sub(r'\s+', ' ', str(header or '').strip().casefold())
        for key, names in _COLUMN_ALIASES.items():
            if key in found:
                continue
            if label in names:
                found[key] = index
    return found


def _table_matrix(filename, payload):
    name = str(filename or '').lower()
    if name.endswith('.csv'):
        import csv
        text = payload.decode('utf-8-sig', errors='replace')
        return [row for row in csv.reader(io.StringIO(text)) if any(str(cell or '').strip() for cell in row)]
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheet = book.active
    matrix = []
    for row in sheet.iter_rows(values_only=True):
        values = ['' if cell is None else cell for cell in row]
        if any(str(cell or '').strip() for cell in values):
            matrix.append(values)
    book.close()
    return matrix


def _cell(row, index):
    if index is None or index >= len(row):
        return ''
    value = row[index]
    if hasattr(value, 'isoformat'):
        return value.isoformat()[:10]
    return str(value or '').strip()


def parse_packing_table(filename, payload):
    """Read a spreadsheet packing list. Rows are grouped into one list per rack."""
    matrix = _table_matrix(filename, payload)
    empty = {
        'number': '', 'date': '', 'job': '', 'customer': '', 'project': '', 'po': '',
        'rack': '', 'rows': [], 'warnings': ['No glass rows were found in that packing list.'],
        'total_pcs': None, 'total_sqft': None, 'parsed_pcs': 0, 'parsed_sqft': None,
    }
    if len(matrix) < 2:
        return [empty]
    headers = _header_map(matrix[0])
    if 'serial' not in headers or 'qty' not in headers:
        empty['warnings'] = ['The spreadsheet needs a Serial Number column and a Quantity column.']
        return [empty]
    groups = {}
    order = []
    for raw in matrix[1:]:
        try:
            serial = int(float(_cell(raw, headers['serial'])))
            qty = int(float(_cell(raw, headers['qty'])))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        customer = _cell(raw, headers.get('customer'))
        project = _cell(raw, headers.get('project'))
        po = _cell(raw, headers.get('po'))
        number = _cell(raw, headers.get('number'))
        rack = _cell(raw, headers.get('rack'))
        packed_on = _cell(raw, headers.get('date'))
        if len(packed_on) > 10:
            packed_on = packed_on[:10]
        key = (customer, project, po, number, rack, packed_on)
        if key not in groups:
            groups[key] = {
                'number': number,
                'date': packed_on,
                'job': project,
                'customer': customer,
                'project': project,
                'po': po,
                'rack': rack,
                'rows': [],
                'warnings': [],
                'total_pcs': None,
                'total_sqft': None,
                'parsed_sqft': None,
            }
            order.append(key)
        groups[key]['rows'].append({
            'serial': serial,
            'qty': qty,
            'floor': _cell(raw, headers.get('floor')),
            'description': _cell(raw, headers.get('description')),
            'width_in': None,
            'height_in': None,
            'width_mm': None,
            'height_mm': None,
        })
    documents = []
    for key in order:
        document = groups[key]
        document['parsed_pcs'] = sum(row['qty'] for row in document['rows'])
        documents.append(document)
    return documents or [empty]


def parse_packing_upload(filename, payload):
    """PDF uses the production packing-list layout. CSV and Excel use a serial column."""
    name = str(filename or '').lower()
    if name.endswith('.csv') or name.endswith('.xlsx') or name.endswith('.xlsm'):
        return parse_packing_table(filename, payload)
    if name.endswith('.pdf') or (payload or b'')[:4] == b'%PDF':
        return [parse_packing_pdf(payload)]
    parsed = parse_packing_lines(str(payload or b'', 'utf-8', errors='replace').splitlines())
    return [parsed]

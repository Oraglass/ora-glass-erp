"""Read a production packing-list PDF and match its glass rows to an order.

The production software prints each piece's quantity on its own line, then the
inch size, the floor code, the product name (which may wrap), and the size in
millimetres. Staff review the match before a shipment is saved.
"""

import io
import re
from datetime import date

import production

# Customs description for glass Ora Glass Inc bills to Ora Glass (Canada) Inc.
INTERCOMPANY_HTS = '7008.00.00.00'
INTERCOMPANY_ORIGIN = 'USA'

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
_WEIGHT_WORD = r'(?:Approx(?:imate)?|Appx|Appr)'
_WEIGHT = re.compile(
    rf'(?:Total\s*)?Weight\b[^:\n]{{0,80}}:\s*(?:{_WEIGHT_WORD}\.?\s*)?([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)?',
    re.I,
)
_WEIGHT_OPEN = re.compile(
    rf'(?:Total\s*)?Weight\b[^:\n]{{0,80}}:\s*(?:{_WEIGHT_WORD}\.?\s*)?$',
    re.I,
)
_PRINTED_WEIGHT = re.compile(
    rf'(?:{_WEIGHT_WORD}\.?\s*)(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+)?',
    re.I,
)
_UNIT_WEIGHT = re.compile(
    r'(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<unit>lbs?|kgs?|pounds?|kilos?|kilograms?)\b',
    re.I,
)
# One kilogram as printed on a packing list, in pounds.
_LB_PER_KG = 2.2046226218
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


_ID_QTY = re.compile(r'^\((\d+)\)\s+(\d+)\b')
_INCH_PREFIX = re.compile(
    r'^(?P<w>\d+(?:\s+\d+\s*/\s*\d+)?)\s+(?P<h>\d+(?:\s+\d+\s*/\s*\d+)?)(?:\s|$)'
)
_MM_FIND = re.compile(r'(\d+(?:\.\d+)?)\s*mm\s+(\d+(?:\.\d+)?)\s*mm', re.I)
_SOLD_TO = re.compile(r'Sold\s+To:\s*(.+?)(?:\s{2,}|$)', re.I)
_RACK_VALUE = re.compile(r'^(\d{3,})\s+[A-Za-z]\d')
_FRACTION = re.compile(r'^(\d+)(?:\s+(\d+)\s*/\s*(\d+))?$')


def _weight_unit(text):
    unit = re.sub(r'[^A-Za-z]', '', str(text or '')).upper()
    if unit in {'LB', 'LBS', 'POUND', 'POUNDS'}:
        return 'LBS'
    if unit in {'KG', 'KGS', 'KILO', 'KILOS', 'KILOGRAM', 'KILOGRAMS'}:
        return 'KG'
    return unit


def _display_weight_unit(unit):
    """Pounds print as lb, matching the packing-list customs line."""
    normalized = _weight_unit(unit)
    if normalized == 'LBS':
        return 'lb'
    if normalized == 'KG':
        return 'kg'
    return str(unit or '').strip().lower()


def _normalize_measure_line(line):
    return (
        str(line or '')
        .replace('\u00a0', ' ')
        .replace('\u3000', ' ')
        .replace('：', ':')
        .replace('，', ',')
    )


def _store_weight(header, amount, unit):
    header['weight'] = float(str(amount).replace(',', ''))
    header['weight_unit'] = _weight_unit(unit or 'LBS')


def _weight_after_label(text):
    """A printed amount on a line that follows 'Total Weight :'.

    A PDF extract often puts the rack on that same line, so the line is not
    only the amount: '5016 A1900*... Appx. 1200 lb'.
    """
    printed = _PRINTED_WEIGHT.search(text)
    if printed:
        unit = printed.group('unit') or ''
        if not unit or _weight_unit(unit) in {'LBS', 'KG'}:
            return printed.group('amount'), unit
    measured = _UNIT_WEIGHT.search(text)
    if measured:
        return measured.group('amount'), measured.group('unit')
    return None


def _feed_weight(line, header, pending):
    """Read Total Weight from this line, or from the value printed just after it.

    The HighQ packing list puts 'Total Weight :' in the totals column and
    'Approx. 171 LBS' (or 'Appx. 1200 lb') beside or just under it. Layout
    extraction often leaves the label on one line and the amount on the next,
    on the same line as the rack. Returns (consumed, still_pending).
    """
    if header.get('weight') is not None:
        return False, False
    text = _normalize_measure_line(line).strip()
    found = _WEIGHT.search(text)
    if found:
        _store_weight(header, found.group(1), found.group(2))
        return False, False
    if pending:
        later = _weight_after_label(text)
        if later:
            _store_weight(header, later[0], later[1])
            return True, False
        if _DELIVERY.search(text) or pending >= 12:
            return False, False
        return False, pending + 1
    if _WEIGHT_OPEN.search(text):
        return False, 1
    return False, False


def weight_pounds(amount, unit):
    """The printed weight in pounds. Kilograms are converted. Pounds stay as printed."""
    number = float(amount)
    if _weight_unit(unit) == 'KG':
        return number * _LB_PER_KG
    return number


def _note_missing_weight(header, warnings):
    """A list with glass rows and no printed weight is flagged, not estimated."""
    if header.get('weight') is not None:
        return False
    warnings.append('Weight not found.')
    return True


def measures_by_number(lines):
    """Weight, piece count, and square feet printed on each Sales Delivery."""
    found = {}
    current = ''
    pending = False
    for raw in lines or []:
        line = str(raw or '').strip()
        if not line:
            continue
        delivery = _DELIVERY.search(line)
        if delivery:
            current = delivery.group(1)
            pending = False
        if not current:
            continue
        bucket = found.setdefault(current, {
            'weight': None, 'weight_unit': '', 'total_pcs': None, 'total_sqft': None,
        })
        _consumed, pending = _feed_weight(line, bucket, pending)
        pcs = _PCS.search(line)
        if pcs and bucket.get('total_pcs') is None:
            bucket['total_pcs'] = int(pcs.group(1))
        sqft = _SQFT.search(line)
        if sqft and bucket.get('total_sqft') is None:
            bucket['total_sqft'] = float(sqft.group(1))
    return {
        number: row for number, row in found.items()
        if number and (
            row.get('weight') is not None
            or row.get('total_pcs') is not None
            or row.get('total_sqft') is not None
        )
    }


def weights_by_number(lines):
    """Weight printed on each Sales Delivery in one packing-list file."""
    found = {}
    current = ''
    pending = False
    for raw in lines or []:
        line = str(raw or '').strip()
        if not line:
            continue
        delivery = _DELIVERY.search(line)
        if delivery:
            current = delivery.group(1)
            pending = False
        header = found.setdefault(current, {'weight': None, 'weight_unit': ''})
        _consumed, pending = _feed_weight(line, header, pending)
    return {
        number: {'weight': row['weight'], 'weight_unit': row.get('weight_unit') or ''}
        for number, row in found.items()
        if number and row.get('weight') is not None
    }


def _note_list_header(line, header):
    """Rack numbers printed on the packing list."""
    rack_value = _RACK_VALUE.match(line.strip())
    if not rack_value:
        return
    number = rack_value.group(1)
    racks = header.setdefault('racks', [])
    if number not in racks:
        racks.append(number)
    if not header.get('rack'):
        header['rack'] = number
        parts = line.strip().split(None, 1)
        header['rack_name'] = parts[1].strip() if len(parts) > 1 else ''


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
        'rack': '', 'rack_name': '', 'racks': [], 'weight': None, 'weight_unit': '',
        'total_pcs': None, 'total_sqft': None,
    }
    pending_weight = False
    for line in cleaned:
        _consumed, pending_weight = _feed_weight(line, header, pending_weight)
        _note_list_header(line, header)
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
        floor = ''
        for look in range(index + 1, following):
            if _FLOOR.fullmatch(cleaned[look]):
                floor = re.sub(r'\s+', ' ', cleaned[look]).strip()
                break
        rows.append({
            'serial': serial,
            'qty': qty,
            'floor': floor,
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
    missing_weight = bool(rows) and _note_missing_weight(header, warnings)
    return {
        'number': header['number'],
        'date': header['date'],
        'job': header['job'],
        'customer': header['customer'],
        'project': header['project'] or header['job'],
        'po': header['po'],
        'rack': header['rack'],
        'rack_name': header.get('rack_name') or '',
        'racks': list(header.get('racks') or []),
        'weight': header.get('weight'),
        'weight_unit': header.get('weight_unit') or '',
        'weight_missing': missing_weight,
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
        'rack_name': '',
        'racks': [],
        'weight': None,
        'weight_unit': '',
        'total_pcs': None,
        'total_sqft': None,
    }
    rows = []
    index = 0
    serial_mode = False
    pending_serial = None
    pending_weight = False
    while index < len(cleaned):
        line = cleaned[index]
        consumed, pending_weight = _feed_weight(line, header, pending_weight)
        if consumed:
            index += 1
            continue
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
        before = (header.get('rack'), header.get('weight'), list(header.get('racks') or []))
        _note_list_header(line, header)
        if (header.get('rack'), header.get('weight'), list(header.get('racks') or [])) != before:
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
    missing_weight = bool(rows) and _note_missing_weight(header, warnings)
    return {
        'number': header['number'],
        'date': header['date'],
        'job': header['job'],
        'customer': header['customer'],
        'project': header['project'] or header['job'],
        'po': header['po'],
        'rack': header['rack'],
        'rack_name': header.get('rack_name') or '',
        'racks': list(header.get('racks') or []),
        'weight': header.get('weight'),
        'weight_unit': header.get('weight_unit') or '',
        'weight_missing': missing_weight,
        'total_pcs': header['total_pcs'],
        'total_sqft': header['total_sqft'],
        'parsed_pcs': parsed_pcs,
        'parsed_sqft': round(parsed_sqft, 2),
        'rows': rows,
        'warnings': warnings,
    }


def _lines_from_words(page):
    """Group PDF words into rows, the same way pdftotext -layout does. No external binary."""
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False) or []
    rows = []
    for word in words:
        top = float(word.get('top') or 0)
        placed = None
        for row in rows:
            if abs(row['top'] - top) <= 2:
                placed = row
                break
        if placed is None:
            placed = {'top': top, 'words': []}
            rows.append(placed)
        placed['words'].append(word)
    lines = []
    for row in sorted(rows, key=lambda item: item['top']):
        ordered = sorted(row['words'], key=lambda item: float(item.get('x0') or 0))
        parts = []
        last_x1 = None
        for word in ordered:
            x0 = float(word.get('x0') or 0)
            if last_x1 is not None and x0 - last_x1 > 8:
                parts.append('  ')
            parts.append(str(word.get('text') or ''))
            last_x1 = float(word.get('x1') or x0)
        text = ''.join(parts).strip()
        if text:
            lines.append(text)
    return lines


def _measure_score(lines):
    text = '\n'.join(str(line or '') for line in lines or [])
    score = 0
    if _WEIGHT.search(text) or _WEIGHT_OPEN.search(text):
        score += 4
    if _PCS.search(text):
        score += 2
    if _SQFT.search(text):
        score += 2
    if _DELIVERY.search(text):
        score += 1
    return score


def _best_page_lines(candidates):
    best = []
    best_score = -1
    for lines in candidates:
        cleaned = [str(line) for line in lines or [] if str(line or '').strip()]
        score = _measure_score(cleaned)
        if score > best_score:
            best = cleaned
            best_score = score
    return best


def pdf_lines(payload):
    """Text lines from a packing-list PDF.

    pdfplumber reads the file itself. Render does not have the pdftotext
    binary, so this never shells out. The word rows match pdftotext -layout
    closely enough to keep Total Pcs, Total SQ/FT, and Total Weight together.
    """
    import pdfplumber
    collected = []
    with pdfplumber.open(io.BytesIO(payload)) as document:
        for page in document.pages:
            candidates = []
            try:
                candidates.append(_lines_from_words(page))
            except Exception:
                pass
            try:
                candidates.append((page.extract_text() or '').splitlines())
            except Exception:
                pass
            try:
                candidates.append((page.extract_text(layout=True) or '').splitlines())
            except Exception:
                pass
            collected.extend(_best_page_lines(candidates))
    return collected


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


def match_customer_ids(name, customers):
    """Customer ids whose company name matches the packing list, even with no ERP order yet."""
    found = []
    for customer_id, customer in (customers or {}).items():
        if _names_match(name, (customer or {}).get('name')):
            found.append(customer_id)
    return found


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
        if str(order.get('status') or '').lower() in {'delivered', 'voided'}:
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
        if str(order.get('status') or '').lower() == 'voided':
            continue
        if _fold_code(order.get('project')) != project:
            continue
        if customer and not _names_match((parsed or {}).get('customer'), order.get('company')):
            continue
        order_po = _fold_code(order.get('po') or order.get('customer_po'))
        if po and order_po and po != order_po:
            continue
        hits.append(order)
    return hits


def _legacy_catalog(order):
    """A pre-ERP job has no quote lines. The packing list itself is the piece list."""
    from documents import piece_size_text
    found = {}
    for entry in (order or {}).get('packing_lists') or []:
        if not entry.get('current', True):
            continue
        for line in entry.get('lines') or []:
            try:
                serial = int(line.get('serial'))
                qty = int(line.get('qty') or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            if line.get('width_mm') not in (None, '') and line.get('height_mm') not in (None, ''):
                size = piece_size_text(line.get('width_mm'), line.get('height_mm'), 'mm')
            elif line.get('width_in') not in (None, '') and line.get('height_in') not in (None, ''):
                size = piece_size_text(line.get('width_in'), line.get('height_in'), 'in')
            else:
                size = ''
            found[serial] = {
                'serial': serial,
                'index': serial,
                'ordered': qty,
                'floor': line.get('floor') or '',
                'product': line.get('description') or 'Glass',
                'size': size,
            }
    return [found[key] for key in sorted(found)]


def _serial_fully_packed(order, serial):
    for row in packing_progress(order)['serials']:
        if row['serial'] == serial:
            return row['ordered'] > 0 and row['bo'] <= 0
    return False


def _packing_rows(parsed):
    rows = []
    for row in (parsed or {}).get('rows') or []:
        try:
            serial = int(row.get('serial'))
            qty = int(row.get('qty') or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0:
            rows.append({'serial': serial, 'floor': str(row.get('floor') or '').strip()})
    return rows


def _catalog_map(order):
    return {row['serial']: row for row in serial_catalog(order)}


def _covers_serials(order, rows):
    catalog = _catalog_map(order)
    return all(row['serial'] in catalog for row in rows)


def _floors_fit(order, rows):
    """True when every packing floor matches that remake serial. None when the list has no floor."""
    catalog = _catalog_map(order)
    given = matched = 0
    for row in rows:
        if not row['floor']:
            continue
        given += 1
        info = catalog.get(row['serial']) or {}
        if floors_match(info.get('floor') or '', row['floor']):
            matched += 1
    if given == 0:
        return None
    return matched == given


def choose_packing_order(parsed, hits, orders=None):
    """Pick one order when the customer and project match more than one.

    A remake starts its own serials at 1, so those numbers also exist on the
    original order. Match a remake list to the remake, and use the floor note
    to choose among open remakes. Do not treat the remake's serials as the
    original order's serials.
    """
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    rows = _packing_rows(parsed)
    if not rows:
        return None
    covered = [order for order in hits if _covers_serials(order, rows)]
    if len(covered) == 1:
        return covered[0]
    if len(covered) < 2:
        return None

    def remake_open(order):
        return bool(order.get('remake')) and str(order.get('status') or '').lower() not in {'delivered', 'voided'}

    remakes = [order for order in covered if remake_open(order)]
    originals = [order for order in covered if not order.get('remake')]
    floor_remakes = [order for order in remakes if _floors_fit(order, rows) is True]
    if len(floor_remakes) == 1:
        return floor_remakes[0]
    if len(floor_remakes) > 1:
        return None
    floor_originals = [order for order in originals if _floors_fit(order, rows) is True]
    if len(floor_originals) == 1 and not floor_remakes:
        return floor_originals[0]
    if any(_floors_fit(order, rows) is not None for order in covered):
        return None
    open_remakes = [
        order for order in remakes
        if any(not _serial_fully_packed(order, row['serial']) for row in rows)
    ]
    originals_open = [
        order for order in originals
        if any(not _serial_fully_packed(order, row['serial']) for row in rows)
    ]
    if len(open_remakes) == 1 and not originals_open:
        return open_remakes[0]
    return None


def serial_catalog(order):
    """Production serials for one order. The serial is the Excel serial, not an IGU number."""
    if (order or {}).get('legacy'):
        return _legacy_catalog(order)
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
        entry = {
            'serial': exported['Serial Number'],
            'index': index,
            'ordered': ordered,
            'floor': exported['Floor Code'],
            'product': exported['Product Name'],
            'size': piece_size_text(spec.get('width'), spec.get('height'), spec.get('unit')),
        }
        if item.get('source_serial') not in (None, ''):
            try:
                entry['source_serial'] = int(item.get('source_serial'))
            except (TypeError, ValueError):
                pass
        catalog.append(entry)
        serial += 1
    return catalog


def classify_rows(order, parsed):
    """Keep rows whose production serial is on the order. The description is never checked.

    A legacy job has no ERP lines. Every numbered packing row is kept, and that
    quantity is the whole job.
    """
    if (order or {}).get('legacy'):
        matched = []
        unmatched = []
        for row in (parsed or {}).get('rows') or []:
            try:
                serial = int(row.get('serial'))
                qty = int(row.get('qty') or 0)
            except (TypeError, ValueError):
                unmatched.append({
                    'serial': None,
                    'qty': row.get('qty') or 0,
                    'description': row.get('description') or '',
                    'reason': 'This row has no production serial number.',
                })
                continue
            if qty <= 0:
                continue
            matched.append({
                'serial': serial,
                'qty': qty,
                'floor': row.get('floor') or '',
                'description': row.get('description') or '',
                'width_in': row.get('width_in'),
                'height_in': row.get('height_in'),
                'width_mm': row.get('width_mm'),
                'height_mm': row.get('height_mm'),
            })
        return matched, unmatched
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


def intercompany_customs():
    """HTS code and country of origin printed on every intercompany invoice."""
    return {'hts': INTERCOMPANY_HTS, 'origin': INTERCOMPANY_ORIGIN}


def racks_on_list(entry):
    """Rack numbers printed on one packing list."""
    found = []
    for number in (entry or {}).get('racks') or []:
        text = str(number or '').strip()
        if text and text not in found:
            found.append(text)
    single = str((entry or {}).get('rack') or '').strip()
    if single and single not in found:
        found.insert(0, single)
    for line in (entry or {}).get('lines') or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get('rack') or '').strip()
        if text and text not in found:
            found.append(text)
    return found


def format_rack_numbers(numbers):
    """Label each rack so a packing-list line reads 'Rack 5016' rather than a bare number."""
    labels = [str(number).strip() for number in (numbers or []) if str(number or '').strip()]
    if not labels:
        return '-'
    return ', '.join(f'Rack {number}' for number in labels)


def format_quantity(amount):
    """A printed piece count or square-foot total, without a trailing zero."""
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return ''
    if abs(number - round(number)) < 0.005:
        return str(int(round(number)))
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def format_weight(amount, unit=''):
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return ''
    if abs(number - round(number)) < 0.005:
        shown = str(int(round(number)))
    else:
        shown = f'{number:.2f}'.rstrip('0').rstrip('.')
    label = _display_weight_unit(unit)
    return f'{shown} {label}'.strip()


def _serial_qty(rows):
    totals = {}
    for line in rows or []:
        if not isinstance(line, dict) or line.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            continue
        try:
            serial = int(line.get('serial'))
        except (TypeError, ValueError):
            continue
        raw = line.get('qty')
        if raw in (None, ''):
            raw = line.get('pcs')
        try:
            qty = int(raw or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            totals[serial] = totals.get(serial, 0) + qty
    return totals


def _printed_number(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_packing(entries):
    """Racks, printed piece count, square feet, and weight for each packing list."""
    racks = []
    lists = []
    total_lb = 0.0
    total_pcs = 0
    total_sqft = 0.0
    found_weight = False
    found_pcs = False
    found_sqft = False
    for entry in entries or []:
        numbers = racks_on_list(entry)
        for number in numbers:
            if number not in racks:
                racks.append(number)
        unit = str(entry.get('weight_unit') or '').strip()
        amount = _printed_number(entry.get('weight'))
        shown_weight = None
        weight_text = 'weight not found'
        missing = True
        if amount is not None:
            missing = False
            found_weight = True
            shown_weight = int(round(amount)) if abs(amount - round(amount)) < 0.005 else round(amount, 2)
            pounds = weight_pounds(amount, unit)
            total_lb = round(total_lb + pounds, 2)
            shown_lb = int(round(pounds)) if abs(pounds - round(pounds)) < 0.005 else round(pounds, 2)
            weight_text = format_weight(shown_lb, 'LBS') or 'weight not found'
            if not weight_text or weight_text == 'weight not found':
                missing = True
        pcs = _printed_number(entry.get('total_pcs'))
        sqft = _printed_number(entry.get('total_sqft'))
        if pcs is None:
            pcs_text = 'pcs not found'
            shown_pcs = None
        else:
            found_pcs = True
            shown_pcs = int(round(pcs))
            total_pcs += shown_pcs
            pcs_text = f'{shown_pcs} pcs'
        if sqft is None:
            sqft_text = 'sq ft not found'
            shown_sqft = None
        else:
            found_sqft = True
            shown_sqft = round(sqft, 2)
            total_sqft = round(total_sqft + shown_sqft, 2)
            sqft_text = f'{format_quantity(shown_sqft)} sq ft'
        racks_label = format_rack_numbers(numbers)
        number = entry.get('number') or 'Packing list'
        line = f'{number}: {racks_label} · {pcs_text} · {sqft_text} · {weight_text}'
        lists.append({
            'id': entry.get('id'),
            'number': entry.get('number') or '',
            'racks': numbers,
            'racks_label': racks_label,
            'pcs': shown_pcs,
            'sqft': shown_sqft,
            'pcs_text': pcs_text,
            'sqft_text': sqft_text,
            'weight': shown_weight,
            'weight_unit': unit,
            'weight_text': weight_text,
            'weight_missing': missing,
            'line': line,
        })
    if found_weight:
        weight_label = format_weight(
            int(round(total_lb)) if abs(total_lb - round(total_lb)) < 0.005 else round(total_lb, 2),
            'LBS',
        )
    else:
        weight_label = 'weight not found' if lists else ''
    summary = {
        'rack_count': len(racks),
        'racks': racks,
        'lists': lists,
        'pcs_label': str(total_pcs) if found_pcs else ('pcs not found' if lists else ''),
        'sqft_label': format_quantity(total_sqft) if found_sqft else ('sq ft not found' if lists else ''),
        'weight_label': weight_label,
        'weight_missing': any(row.get('weight_missing') for row in lists),
    }
    summary['shipment'] = shipment_lines(summary)
    return summary


def shipment_lines(summary):
    """Packing-list lines, then the totals. Page and PDF both use this."""
    if not summary or not summary.get('lists'):
        return {
            'rack_line': '', 'list_lines': [], 'pcs_line': '', 'sqft_line': '',
            'weight_line': '', 'weight_missing': False,
        }
    return {
        'list_lines': [row.get('line') for row in summary.get('lists') or [] if row.get('line')],
        'rack_line': f"Total racks: {int(summary.get('rack_count') or 0)}",
        'pcs_line': f"Total pcs: {summary['pcs_label']}" if summary.get('pcs_label') else '',
        'sqft_line': f"Total sq ft: {summary['sqft_label']}" if summary.get('sqft_label') else '',
        'weight_line': f"Total weight: {summary['weight_label']}" if summary.get('weight_label') else '',
        'weight_missing': bool(summary.get('weight_missing')),
    }


def packing_for_intercompany(order, invoice, invoices):
    """Packing lists whose pieces this invoice billed.

    Earlier invoices of the same kind on the order take their pieces first, so
    a later invoice does not repeat those lists. A Canada customer invoice and
    the intercompany invoice are allocated separately, so the same pieces show
    the same lists on both.
    """
    pool = []
    for entry in (order or {}).get('packing_lists') or []:
        if entry.get('current') is False:
            continue
        pool.append({'entry': entry, 'left': _serial_qty(entry.get('lines'))})
    kind = (invoice or {}).get('kind') or 'intercompany'
    ordered = [
        row for row in (invoices or [])
        if row.get('order_id') == (order or {}).get('id')
        and (row.get('kind') or 'customer') == kind
        and not row.get('voided')
    ]
    ordered.sort(key=lambda row: (str(row.get('invoice_date') or ''), row.get('id') or 0))
    if invoice and all(row.get('id') != invoice.get('id') for row in ordered):
        ordered.append(invoice)
    chosen = []
    target = (invoice or {}).get('id')
    for document in ordered:
        need = _serial_qty(document.get('items'))
        if not need:
            used = [slot['entry'] for slot in pool] if document.get('id') == target else []
        else:
            used = []
            for slot in pool:
                took = False
                for serial, left in list(slot['left'].items()):
                    want = need.get(serial, 0)
                    if want <= 0 or left <= 0:
                        continue
                    take = min(want, left)
                    slot['left'][serial] = left - take
                    need[serial] = want - take
                    took = True
                if took:
                    used.append(slot['entry'])
        if document.get('id') == target:
            chosen = used
            break
    return summarize_packing(chosen)


def parse_packing_upload(filename, payload):
    """PDF uses the production packing-list layout. CSV and Excel use a serial column."""
    name = str(filename or '').lower()
    if name.endswith('.csv') or name.endswith('.xlsx') or name.endswith('.xlsm'):
        return parse_packing_table(filename, payload)
    if name.endswith('.pdf') or (payload or b'')[:4] == b'%PDF':
        return [parse_packing_pdf(payload)]
    parsed = parse_packing_lines(str(payload or b'', 'utf-8', errors='replace').splitlines())
    return [parsed]

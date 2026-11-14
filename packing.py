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


def parse_packing_lines(lines):
    """Turn packing-list text lines into a header and glass rows."""
    cleaned = [str(line or '').strip() for line in lines if str(line or '').strip()]
    header = {
        'number': '',
        'date': '',
        'job': '',
        'rack': '',
        'total_pcs': None,
        'total_sqft': None,
    }
    rows = []
    index = 0
    while index < len(cleaned):
        line = cleaned[index]
        if _HEADER.search(line):
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
        qty = int(line)
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
        'unmatched': unmatched,
        'lines': lines,
        'header_pcs': parsed.get('total_pcs'),
        'header_sqft': parsed.get('total_sqft'),
        'parsed_pcs': parsed.get('parsed_pcs'),
        'parsed_sqft': parsed.get('parsed_sqft'),
    }

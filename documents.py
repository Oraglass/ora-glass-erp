"""Quote and order confirmation PDFs in the production-software layout."""

import math
import os
import re
from datetime import date, datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from copy_notes import strip_copy_history
from production import dimensions_mm_in, floor_code, lookup_product, makeup_from_spec
from quoting import DRAWING_NOTE, area_sqf

COMPANY_ADDRESS = '2312 Leatherwood Pl,<br/>Lynden, WA 98264,USA'
TERMS = 'Ora Glass Inc Terms And Conditions of Sale apply.'
GREY = colors.HexColor('#D9D9D9')
PAGE_WIDTH = 7.7 * inch


def currency_prefix(currency):
    return 'C$' if str(currency or 'usd').lower() == 'cad' else 'US$'


def money_text(currency, amount):
    return f'{currency_prefix(currency)} {float(amount or 0):.2f}'


def format_date(value):
    text = str(value or '')[:10]
    try:
        return datetime.strptime(text, '%Y-%m-%d').strftime('%m/%d/%Y')
    except ValueError:
        if text:
            return text
        return date.today().strftime('%m/%d/%Y')


def _number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _pieces(item):
    try:
        pieces = int((item or {}).get('pcs') or 0)
    except (TypeError, ValueError):
        return 0
    return pieces if pieces > 0 else 0


def quote_form_fraction(millimetres):
    """Nearest 1/16 inch, the same rounding the quote form uses in generateIGUName."""
    inches = float(millimetres) / 25.4
    whole = math.floor(inches)
    numer = int(math.floor((inches - whole) * 16 + 0.5))
    if numer >= 16:
        whole += numer // 16
        numer = numer % 16
    if numer <= 0:
        return str(int(whole))
    divisor = math.gcd(numer, 16)
    numer //= divisor
    denom = 16 // divisor
    if whole:
        return f'{int(whole)} {numer}/{denom}'
    return f'{numer}/{denom}'


def inch_fraction(inches):
    """Nearest 1/32 inch, reduced, such as 49 1/4 or 77 23/32."""
    number = _number(inches)
    sign = '-' if number < 0 else ''
    number = abs(number)
    whole = math.floor(number + 1e-9)
    numer = int(round((number - whole) * 32))
    if numer >= 32:
        whole += numer // 32
        numer = numer % 32
    if numer <= 0:
        return f'{sign}{whole}'
    divisor = math.gcd(numer, 32)
    numer //= divisor
    denom = 32 // divisor
    if whole:
        return f'{sign}{whole} {numer}/{denom}'
    return f'{sign}{numer}/{denom}'


def _display_mm(value):
    """At most two decimal places. A whole millimetre stays a whole number."""
    number = round(_number(value), 2)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def _plain_measure(value):
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f'{number:.4f}'.rstrip('0').rstrip('.')


def drawing_caption(item):
    """First line identifies the quote row. The IGU makeup is the second line."""
    name = str((item or {}).get('name') or '')
    thickness, spec = '', name
    if ' - ' in name:
        thickness, spec = name.split(' - ', 1)
    bits = [f"Line {(item or {}).get('line_number') or ''}"]
    if thickness:
        bits.append(thickness.strip())
    spec_row = (item or {}).get('spec') if isinstance((item or {}).get('spec'), dict) else {}
    width, height = spec_row.get('width'), spec_row.get('height')
    if width not in (None, '') and height not in (None, ''):
        try:
            bits.append(f'{_plain_measure(width)} x {_plain_measure(height)}')
        except (TypeError, ValueError):
            bits.append(f'{width} x {height}')
    pcs = (item or {}).get('pcs')
    if pcs not in (None, '', '-'):
        try:
            bits.append(f'Qty {_plain_measure(pcs)}')
        except (TypeError, ValueError):
            bits.append(f'Qty {pcs}')
    floor = (item or {}).get('floor') or (item or {}).get('mark') or ''
    if floor:
        bits.append(f'Floor {floor}')
    if (item or {}).get('irregular_shape'):
        bits.append('Irregular')
    return ' · '.join(bits), spec.strip()


def piece_size_text(width, height, unit=None):
    """Piece size in inches and millimetres. 25.4 mm is one inch."""
    if width in (None, '') or height in (None, ''):
        return ''
    try:
        width_mm, height_mm, width_in, height_in = dimensions_mm_in(width, height, unit)
    except (TypeError, ValueError):
        return ''
    inches = f'{inch_fraction(width_in)} x {inch_fraction(height_in)} in'
    millimetres = f'{_display_mm(width_mm)} x {_display_mm(height_mm)} mm'
    return f'{inches} ({millimetres})'


def dimension_lines(width, height, unit):
    """Inch fraction on the first line and whole millimetres on the second."""
    if width in (None, '') or height in (None, ''):
        return '', ''
    try:
        width_mm, height_mm, width_in, height_in = dimensions_mm_in(width, height, unit)
    except (TypeError, ValueError):
        return '', ''
    wide = f'{inch_fraction(width_in)}<br/>{_display_mm(width_mm)} mm'
    high = f'{inch_fraction(height_in)}<br/>{_display_mm(height_mm)} mm'
    return wide, high


_MM_THICKNESS = re.compile(r'(\d+(?:\.\d+)?)\s*mm', re.IGNORECASE)
_INCH_LAYER = re.compile(r'(\d+(?:\.\d+)?)\s*(?:inch(?:es)?|in|"|″|”)', re.IGNORECASE)
_LAMINATED = re.compile(r'lam|\bpvb\b|\bsgp\b|interlayer', re.IGNORECASE)
_LEADING_THICKNESS = re.compile(r'^\s*(\d+(?:\.\d+)?)')
_PIECE_SIZE_SUFFIX = re.compile(
    r'\s+-\s+\d+(?:\.\d+)?\s+x\s+\d+(?:\.\d+)?\s*(?:mm|in)\b\s*$',
    re.IGNORECASE,
)


def _round_mm(value):
    number = round(float(value), 2)
    if abs(number - round(number)) < 1e-9:
        return float(int(round(number)))
    return number


def component_thickness_mm(name):
    """Millimetres in one lite or spacer.

    A monolithic name uses its one millimetre number. A laminated lite is the
    sum of every ply and the interlayer, whether those are written in
    millimetres or as an inch layer such as 0.030".
    """
    text = str(name or '').strip()
    if not text or text.lower() == 'n/a':
        return 0.0
    millimetres = [float(value) for value in _MM_THICKNESS.findall(text)]
    laminated = bool(_LAMINATED.search(text)) or len(millimetres) > 1
    inches = 0.0
    if laminated:
        inches = sum(float(value) * 25.4 for value in _INCH_LAYER.findall(text))
    if laminated and (millimetres or inches):
        return _round_mm(sum(millimetres) + inches)
    if millimetres:
        return _round_mm(millimetres[0])
    return 0.0


def overall_thickness_mm(spec):
    """Glass plus spacer millimetres, the overall unit size in the build name.

    Each lite contributes its full thickness. Laminated glass counts every ply
    and the interlayer, not only the first number in the product name.
    """
    from production import normalize_igu_spec
    spec = normalize_igu_spec(spec)
    total = 0.0
    found = False
    for index in (1, 2, 3):
        glass = spec.get(f'glass{index}') or ''
        if not glass or str(glass).strip().lower() == 'n/a':
            continue
        glass_mm = component_thickness_mm(glass)
        if glass_mm <= 0:
            continue
        found = True
        total += glass_mm
        if index < 3:
            spacer = spec.get(f'spacer{index}') or ''
            if spacer and str(spacer).strip().lower() != 'n/a':
                total += component_thickness_mm(spacer)
    if found:
        return _round_mm(total)
    makeup = makeup_from_spec(spec)
    if not makeup:
        return 0.0
    parsed = 0.0
    for part in makeup.split('+'):
        match = re.match(r'\s*(\d+(?:\.\d+)?)mm', part, re.IGNORECASE)
        if match:
            parsed += float(match.group(1))
    return _round_mm(parsed)


def build_label(spec):
    """Full IGU name: 32 (1 1/4") - 4mmSolarban60 VT Tempered +24mm..."""
    return _igu_label(spec)


def igu_name_from_spec(spec):
    """IGU name the quote calculator builds: overall millimetres, inch fraction, then the lites."""
    return _igu_label(spec)


def name_needs_igu_refresh(item):
    """A stored name is stale when its thickness or its Tempered spacing disagrees with the spec."""
    if name_thickness_mismatch(item):
        return True
    if not isinstance(item, dict) or item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
        return False
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    label = igu_name_from_spec(spec) if spec else ''
    if not label or item.get('name') == label:
        return False
    current = str(item.get('name') or '')
    return 'Tempered+' in current or 'Tempered +' in current or 'Tempered+' in label or 'Tempered +' in label


def name_thickness_mismatch(item):
    """True when a stored name leads with a thickness that is not the spec sum."""
    if not isinstance(item, dict) or item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
        return False
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    label = igu_name_from_spec(spec) if spec else ''
    if not label:
        return False
    current = _PIECE_SIZE_SUFFIX.sub('', str(item.get('name') or '')).strip()
    match = _LEADING_THICKNESS.match(current)
    if not match:
        return False
    return abs(float(match.group(1)) - overall_thickness_mm(spec)) > 0.01


def sync_line_name(item):
    """Replace a line name with the name its spec builds. A piece size is not part of that name."""
    if not isinstance(item, dict) or item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
        return False
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    label = igu_name_from_spec(spec) if spec else ''
    if not label or item.get('name') == label:
        return False
    item['name'] = label
    return True


def sync_item_names(items, only_mismatch=False):
    """Rebuild IGU names from their specs. A following surcharge note uses the new name."""
    changed = False
    previous = None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('kind') == 'irregular_surcharge':
            if previous:
                expected = f"Irregular shape surcharge – {previous.get('name') or 'Glass'}"
                if item.get('name') != expected:
                    item['name'] = expected
                    if item.get('description'):
                        item['description'] = expected
                    changed = True
            continue
        if item.get('kind') == 'dwg_creation':
            continue
        if only_mismatch and not name_needs_igu_refresh(item):
            previous = item
            continue
        if sync_line_name(item):
            changed = True
        previous = item
    return changed


def glass_thickness_mm(spec):
    """Sum of the lite thicknesses. Spacers are not glass. Laminated lites include every ply."""
    from production import normalize_igu_spec
    spec = normalize_igu_spec(spec)
    total = 0.0
    for index in (1, 2, 3):
        name = str((spec or {}).get(f'glass{index}') or '')
        if not name or name.strip().lower() == 'n/a':
            continue
        total += component_thickness_mm(name)
    return _round_mm(total)


def _format_overall_mm(total):
    number = _round_mm(total)
    if float(number).is_integer():
        return str(int(number))
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def _igu_label(spec):
    makeup = makeup_from_spec(spec)
    if not makeup:
        return ''
    total = overall_thickness_mm(spec)
    if total <= 0:
        return makeup
    return f'{_format_overall_mm(total)} ({quote_form_fraction(total)}") - {makeup}'


def real_area(item):
    """Real square feet for the whole line. Charged area is not used for weight."""
    pieces = _pieces(item) or 1
    if item.get('real_sqf') not in (None, ''):
        return _number(item.get('real_sqf')) * pieces
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    if spec and spec.get('width') not in (None, '') and spec.get('height') not in (None, ''):
        return area_sqf(spec.get('width'), spec.get('height'), spec.get('unit')) * pieces
    return _number(item.get('sqf')) * pieces


def line_weight(item, factor):
    if is_charge_line(item):
        return 0.0
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    thickness = glass_thickness_mm(spec)
    rate = _number(factor)
    if thickness <= 0 or rate <= 0:
        return 0.0
    return real_area(item) * thickness * rate


def charged_sqf(item):
    """Charged square feet for the line: pieces times the charged area of one piece."""
    pieces = _pieces(item)
    if pieces < 1:
        pieces = 1 if item.get('sqf') not in (None, '') else 0
    return _number(item.get('sqf')) * pieces


def describe_items(items):
    """Show the full build name on screen. Surcharge lines use that same name."""
    shown = []
    last = ''
    for item in items or []:
        if not isinstance(item, dict):
            shown.append(item)
            continue
        row = dict(item)
        kind = row.get('kind')
        if kind == 'irregular_surcharge' and last:
            row['name'] = f'Irregular shape surcharge – {last}'
        elif kind not in {'irregular_surcharge', 'dwg_creation'}:
            spec = row.get('spec') if isinstance(row.get('spec'), dict) else None
            label = build_label(spec) if spec else ''
            if label:
                row['name'] = label
                last = label
            else:
                last = ''
        spec = row.get('spec') if isinstance(row.get('spec'), dict) else None
        if spec and spec.get('width') not in (None, '') and spec.get('height') not in (None, ''):
            row['size_label'] = piece_size_text(spec.get('width'), spec.get('height'), spec.get('unit'))
        shown.append(row)
    return shown


def line_description(item):
    """One component per line. The overall thickness leads the first line."""
    if item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
        return escape(str(item.get('name') or ''))
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    makeup = makeup_from_spec(spec) if spec else ''
    if not makeup:
        text = escape(str(item.get('name') or ''))
    else:
        parts = [part.strip() for part in makeup.split('+') if part.strip()]
        product_id = ''
        if spec:
            product_id = str(lookup_product(spec, '').get('product_id') or '').strip()
        prefix = ''
        label = build_label(spec)
        if label.endswith(makeup):
            prefix = label[: -len(makeup)]
        lines = []
        last = len(parts) - 1
        for index, part in enumerate(parts):
            if index == 0:
                shown = prefix + (f'{product_id} - ' if product_id else '') + part
            else:
                shown = f'+{part}'
            lines.append(escape(shown) + (' /' if index < last else ''))
        text = '<br/>'.join(lines)
    remark = strip_copy_history(item.get('remarks')).strip()
    if remark:
        text += '<br/>' + escape(remark)
    return text


def _styles():
    base = getSampleStyleSheet()['Normal']
    return {
        'title': ParagraphStyle('DocTitle', parent=base, fontName='Helvetica-Bold', fontSize=16, leading=18),
        'small': ParagraphStyle('DocSmall', parent=base, fontName='Helvetica', fontSize=8, leading=10),
        'meta': ParagraphStyle('DocMeta', parent=base, fontName='Helvetica', fontSize=8, leading=10),
        'cell': ParagraphStyle('DocCell', parent=base, fontName='Helvetica', fontSize=6.5, leading=8),
        'desc': ParagraphStyle('DocDesc', parent=base, fontName='Helvetica', fontSize=6, leading=7.5),
        'head': ParagraphStyle('DocHead', parent=base, fontName='Helvetica-Bold', fontSize=6, leading=7.5, alignment=TA_CENTER),
        'num': ParagraphStyle('DocNum', parent=base, fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_RIGHT),
        'center': ParagraphStyle('DocCenter', parent=base, fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_CENTER),
        'floor': ParagraphStyle('DocFloor', parent=base, fontName='Helvetica', fontSize=6.5, leading=8, alignment=TA_CENTER),
        'foot': ParagraphStyle('DocFoot', parent=base, fontName='Helvetica', fontSize=8, leading=11),
        'sum_label': ParagraphStyle('SumLabel', parent=base, fontName='Helvetica', fontSize=8, leading=11),
        'sum_amount': ParagraphStyle('SumAmount', parent=base, fontName='Helvetica', fontSize=8, leading=11),
    }


def _logo(style):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logo.png')
    if os.path.exists(path):
        return Image(path, width=1.15 * inch, height=0.72 * inch)
    return Paragraph('<font color="#C41E3A" size="16"><b>ORA</b></font><br/>GLASS', style)


def _header(document, styles):
    kind = document.get('kind') or 'quote'
    if kind == 'order':
        title = 'Order Confirmation'
        try:
            revision = int(document.get('revision') or 0)
        except (TypeError, ValueError):
            revision = 0
        if revision > 0:
            title = f'Order Confirmation Revision {revision}'
        number_label = 'Order'
    else:
        title = 'Quotation'
        number_label = 'Quotation'
    identifier = document.get('id')
    identifier_text = '' if identifier in (None, '') else escape(str(identifier))
    shown_date = format_date(document.get('date'))
    meta = '<br/>'.join([
        f'<b>{number_label}:</b> {identifier_text}&nbsp;&nbsp;&nbsp;<b>Date:</b> {escape(shown_date)}',
        f'<b>Customer P.O.#:</b> {escape(str(document.get("po") or ""))}',
        f'<b>Quote By:</b> {escape(str(document.get("quote_by") or ""))}&nbsp;&nbsp;&nbsp;<b>Pmt Mode:</b> {escape(str(document.get("payment_mode") or ""))}',
        f'<b>Update By:</b> {escape(str(document.get("updated_by") or ""))}',
        f'<b>Net:</b> {escape(str(document.get("payment_terms") or ""))}',
        f'<b>Dlv Method:</b> {escape(str(document.get("delivery_method") or ""))}',
    ])
    title_text = f'{escape(title)}<br/><font size="8">{header_address_block(document)}</font>'
    header = Table(
        [[_logo(styles['small']), Paragraph(title_text, styles['title']), Paragraph(meta, styles['meta'])]],
        colWidths=[1.25 * inch, 3.05 * inch, 3.4 * inch],
    )
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    sold = [f'<b>Sold To:</b> {escape(str(document.get("company") or ""))}']
    customer_tax = _customer_tax_id(document)
    if customer_tax:
        sold.append(customer_tax)
    sold.extend(escape(str(line)) for line in document.get('sold_to') or [] if str(line).strip())
    job = [f'<b>Job Name:</b> {escape(str(document.get("project") or ""))}']
    job.extend(escape(str(line)) for line in document.get('job_address') or [] if str(line).strip())
    parties = Table(
        [[Paragraph('<br/>'.join(sold), styles['small']), Paragraph('<br/>'.join(job), styles['small'])]],
        colWidths=[3.85 * inch, 3.85 * inch],
    )
    parties.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('LINEAFTER', (0, 0), (0, 0), 0.4, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    remarks = strip_copy_history(document.get('remarks')).strip()
    remark_line = f'<b>Remarks:</b> {escape(remarks)}' if remarks else '<b>Remarks:</b>'
    return [header, Spacer(1, 6), parties, Spacer(1, 4), Paragraph(remark_line, styles['small']), Spacer(1, 6)]


def is_charge_line(item):
    """A fee or surcharge, not a piece of glass."""
    if not isinstance(item, dict):
        return False
    if item.get('kind') in {'irregular_surcharge', 'dwg_creation', 'fee', 'surcharge', 'charge'}:
        return True
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
    has_glass = bool(spec and spec.get('width') not in (None, '')) or item.get('sqf') not in (None, '', 0, '0')
    return not has_glass and item.get('pcs') in (None, '', 0, '0')


def layout_lines(items):
    """Glass in order, with each charge directly under the line it belongs to."""
    glass = []
    glass_by_id = {}
    for item in items or []:
        if not isinstance(item, dict) or is_charge_line(item):
            continue
        glass.append(item)
        if item.get('line_id'):
            glass_by_id[str(item.get('line_id'))] = len(glass) - 1
    under = {index: [] for index in range(len(glass))}
    tail = []
    last = None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if not is_charge_line(item):
            if item.get('line_id') and str(item.get('line_id')) in glass_by_id:
                last = glass_by_id[str(item.get('line_id'))]
            else:
                try:
                    last = glass.index(item)
                except ValueError:
                    last = last
            continue
        parent = None
        if item.get('drawing_for'):
            parent = glass_by_id.get(str(item.get('drawing_for')))
        elif item.get('kind') == 'irregular_surcharge':
            parent = last
        if parent is None:
            tail.append(item)
        else:
            under[parent].append(item)
    ordered = []
    for index, item in enumerate(glass):
        ordered.append(item)
        ordered.extend(under.get(index) or [])
    ordered.extend(tail)
    return ordered


def charge_label(item, currency):
    """Short note for a surcharge that belongs under its glass line."""
    kind = (item or {}).get('kind')
    label = 'DWG drawing creation' if kind == 'dwg_creation' else 'Irregular shape surcharge'
    code = 'CAD' if str(currency or '').lower() == 'cad' else 'USD'
    return f'{label} +{code} ${_line_total(item):.2f}'


def _line_total(item):
    if item.get('total') not in (None, ''):
        return _number(item.get('total'))
    if item.get('price') not in (None, ''):
        return _number(item.get('price'))
    return _number(item.get('price_pc')) * (_pieces(item) or 1)


def _size_table(items, currency, styles):
    prefix = currency_prefix(currency)
    header = [
        '', 'QTY', 'WIDTH', 'HEIGHT', 'FLOOR #', 'ITEM DESCRIPTION',
        'SQF', f'{prefix}/SQF', f'{prefix}/PC', 'TOTAL',
    ]
    rows = [[Paragraph(label, styles['head']) for label in header]]
    total_sqf = 0.0
    total_pcs = 0
    total_money = 0.0
    number = 0
    body = []
    for item in layout_lines(items):
        if is_charge_line(item) and item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            total_money += _line_total(item)
            if body:
                body[-1]['notes'].append(charge_label(item, currency))
            continue
        if is_charge_line(item):
            total_money += _line_total(item)
            body.append({
                'number': '',
                'pieces': '',
                'wide': '',
                'high': '',
                'floor': '',
                'desc': escape(str(item.get('name') or '')),
                'notes': [],
                'area': '',
                'price_sqf': '',
                'price_pc': '',
                'amount': f'{_line_total(item):.2f}',
            })
            continue
        pieces = _pieces(item)
        shown_pieces = pieces or (0 if item.get('sqf') in (None, '') else pieces)
        if not shown_pieces and item.get('sqf') not in (None, ''):
            shown_pieces = 1
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        has_size = spec.get('width') not in (None, '') and spec.get('height') not in (None, '')
        wide, high = dimension_lines(spec.get('width'), spec.get('height'), spec.get('unit')) if has_size else ('', '')
        area = charged_sqf(item) if has_size or item.get('sqf') not in (None, '', 0, '0') else 0.0
        price_sqf = item.get('price_sqf')
        price_pc = item.get('price_pc')
        amount = _line_total(item)
        number += 1
        total_pcs += shown_pieces
        total_sqf += area
        total_money += amount
        floor = escape(floor_code(item)).replace(' ', '&nbsp;')
        body.append({
            'number': str(number),
            'pieces': str(shown_pieces or ''),
            'wide': wide,
            'high': high,
            'floor': floor,
            'desc': line_description(item),
            'notes': [],
            'area': '' if not area else f'{area:.2f}',
            'price_sqf': '' if price_sqf in (None, '', 0, '0') else f'{_number(price_sqf):.2f}',
            'price_pc': '' if price_pc in (None, '') else f'{_number(price_pc):.2f}',
            'amount': f'{amount:.2f}',
        })
    for entry in body:
        desc = entry['desc']
        if entry['notes']:
            desc += '<br/>' + '<br/>'.join(escape(note) for note in entry['notes'])
        rows.append([
            Paragraph(entry['number'], styles['center']),
            Paragraph(entry['pieces'], styles['center']),
            Paragraph(entry['wide'], styles['center']),
            Paragraph(entry['high'], styles['center']),
            Paragraph(entry['floor'], styles['floor']),
            Paragraph(desc, styles['desc']),
            Paragraph(entry['area'], styles['num']),
            Paragraph(entry['price_sqf'], styles['num']),
            Paragraph(entry['price_pc'], styles['num']),
            Paragraph(entry['amount'], styles['num']),
        ])
    widths = [
        0.38 * inch, 0.38 * inch, 0.68 * inch, 0.68 * inch, 0.95 * inch,
        1.75 * inch, 0.55 * inch, 0.72 * inch, 0.68 * inch, 0.93 * inch,
    ]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(_grid())
    return table, total_pcs, total_sqf, total_money


def _sqf_table(items, currency, styles):
    label = 'CAD/SQF' if str(currency or '').lower() == 'cad' else 'USD/SQF'
    rows = [[
        Paragraph('', styles['head']),
        Paragraph('ITEM DESCRIPTION', styles['head']),
        Paragraph(label, styles['head']),
    ]]
    total_money = 0.0
    total_pcs = 0
    number = 0
    body = []
    for item in layout_lines(items):
        amount = _line_total(item)
        total_money += amount
        if is_charge_line(item) and item.get('kind') in {'irregular_surcharge', 'dwg_creation'}:
            if body:
                body[-1]['notes'].append(charge_label(item, currency))
            continue
        if is_charge_line(item):
            body.append({
                'number': '',
                'desc': escape(str(item.get('name') or '')),
                'notes': [],
                'amount': f'{amount:.2f}',
            })
            continue
        number += 1
        total_pcs += _pieces(item) or 1
        body.append({'number': str(number), 'desc': line_description(item), 'notes': [], 'amount': f'{amount:.2f}'})
    for entry in body:
        desc = entry['desc']
        if entry['notes']:
            desc += '<br/>' + '<br/>'.join(escape(note) for note in entry['notes'])
        rows.append([
            Paragraph(entry['number'], styles['center']),
            Paragraph(desc, styles['cell']),
            Paragraph(entry['amount'], styles['num']),
        ])
    rows.append([
        Paragraph('', styles['cell']),
        Paragraph('<b>TOTAL</b>', styles['foot']),
        Paragraph(f'<b>{total_money:.2f}</b>', styles['num']),
    ])
    table = Table(rows, colWidths=[0.45 * inch, 6.0 * inch, 1.25 * inch], repeatRows=1)
    table.setStyle(_grid())
    return table, total_pcs, 0.0, total_money


def _grid():
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), GREY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.black),
        ('LINEABOVE', (0, 0), (-1, 0), 0.6, colors.black),
        ('BOX', (0, 0), (-1, -1), 0.6, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ])


def canada_bill(document):
    """CAD documents are billed by Ora Glass (Canada) Inc. USD documents, including intercompany, are Ora Glass Inc."""
    if str((document or {}).get('kind') or '') == 'intercompany':
        return False
    issuer = str((document or {}).get('issuer') or '')
    if issuer == 'us':
        return False
    if issuer == 'ca':
        return True
    return str((document or {}).get('currency') or 'usd').lower() == 'cad'


def header_address_block(document):
    """Company name, then the Lynden address, then the GST number on Canada bills.

    Canada invoices set omit_issuer_address so the header is only the company name and GST number.
    US invoices, quotes, and order confirmations keep the Lynden address.
    """
    name = 'Ora Glass (Canada) Inc.' if canada_bill(document) else 'Ora Glass Inc.'
    block = escape(name)
    if not (canada_bill(document) and (document or {}).get('omit_issuer_address')):
        block += '<br/>' + COMPANY_ADDRESS
    if canada_bill(document):
        gst = str((document or {}).get('seller_gst_number') or '').strip()
        if gst:
            block += f'<br/>GST# {escape(gst)}'
    return block


def invoice_party(invoice):
    """Customer block shared by the invoice page and the PDF. Empty lines are left out."""
    rows = []
    name = str((invoice or {}).get('company') or '').strip()
    if name:
        rows.append({'key': 'customer', 'text': name})
    for line in (invoice or {}).get('customer_address') or []:
        text = str(line or '').strip()
        if text:
            rows.append({'key': 'address', 'text': text})
    gst = str((invoice or {}).get('customer_gst_number') or '').strip()
    if gst:
        rows.append({'key': 'gst', 'text': f'GST# {gst}'})
    pst = str((invoice or {}).get('pst_number') or '').strip()
    if pst:
        rows.append({'key': 'pst', 'text': f'PST: {pst}'})
    qst = str((invoice or {}).get('qst_number') or '').strip()
    if qst:
        rows.append({'key': 'qst', 'text': f'QST: {qst}'})
    project = str((invoice or {}).get('project') or '').strip()
    if project and project != '-':
        rows.append({'key': 'project', 'text': f'Project: {project}'})
    bits = []
    order_id = (invoice or {}).get('order_id')
    quote_id = (invoice or {}).get('quote_id')
    if order_id not in (None, ''):
        bits.append(f'Order: {order_id}')
    if quote_id not in (None, ''):
        bits.append(f'Quote: {quote_id}')
    if bits:
        rows.append({'key': 'order', 'text': '   '.join(bits)})
    return rows


def tax_line_label(line, document):
    """Tax row in the invoice totals. GST and HST carry the seller's GST number."""
    name = str((line or {}).get('name') or 'Tax').strip() or 'Tax'
    rate = str((line or {}).get('rate') or '').strip()
    label = f'{name} {rate}%'.strip()
    kind = name.upper()
    number = ''
    if kind in {'GST', 'HST'}:
        gst = str((document or {}).get('seller_gst_number') or '').strip()
        if gst:
            number = f'GST# {gst}'
    elif kind in {'PST', 'RST'}:
        pst = str((document or {}).get('pst_number') or '').strip()
        if pst:
            number = f'PST# {pst}'
    elif kind == 'QST':
        qst = str((document or {}).get('qst_number') or '').strip()
        if qst:
            number = f'QST# {qst}'
    if number:
        return f'{label} ({number})'
    return label


def invoice_summary(invoice):
    """Totals shared by the invoice page and the PDF, tax sitting above the grand total."""
    from billing import money

    def amount(value):
        return money(value)

    discount = (invoice or {}).get('discount') or {}
    if discount.get('type') == 'percent':
        discount_label = f"{amount(discount.get('value')):.2f}%"
    else:
        discount_label = 'fixed'
    subtotal_label = 'Tax-free subtotal' if (invoice or {}).get('tax_free') else 'IGU subtotal'
    rows = [
        {'label': subtotal_label, 'amount': amount((invoice or {}).get('subtotal') or (invoice or {}).get('igu_subtotal'))},
        {'label': f'Discount ({discount_label})', 'amount': -amount((invoice or {}).get('discount_amount'))},
    ]
    extra_discount = amount((invoice or {}).get('additional_discount_amount'))
    if extra_discount:
        try:
            extra_percent = float((invoice or {}).get('additional_discount_percent') or 0)
        except (TypeError, ValueError):
            extra_percent = 0
        rows.append({
            'label': f'Additional discount ({extra_percent:g}%)',
            'amount': -extra_discount,
        })
    igu_label = 'Total before extras' if (invoice or {}).get('tax_free') else 'IGU total'
    rows.append({'label': igu_label, 'amount': amount((invoice or {}).get('igu_total'))})
    for charge in (invoice or {}).get('extra_charges') or []:
        if not isinstance(charge, dict):
            continue
        rows.append({'label': charge.get('label') or 'Extra', 'amount': amount(charge.get('amount'))})
    for line in (invoice or {}).get('tax_lines') or []:
        if not isinstance(line, dict):
            continue
        rows.append({
            'label': line.get('label') or tax_line_label(line, invoice),
            'amount': amount(line.get('amount')),
        })
    rows.append({'label': 'Grand total', 'amount': amount((invoice or {}).get('total'))})
    if (invoice or {}).get('doc_type') == 'credit_note':
        rows.append({'label': 'Applied to invoices', 'amount': amount((invoice or {}).get('applied'))})
        rows.append({'label': 'Unapplied credit', 'amount': amount((invoice or {}).get('balance'))})
    else:
        rows.append({'label': 'Payments applied', 'amount': amount((invoice or {}).get('applied'))})
        rows.append({'label': 'Balance due', 'amount': amount((invoice or {}).get('balance'))})
    return rows


def _customer_tax_id(document):
    """The customer's PST or QST, printed with Sold To on Canada bills only."""
    if not canada_bill(document):
        return ''
    province = str(document.get('ship_province') or '').strip().upper()
    qst = str(document.get('qst_number') or '').strip()
    pst = str(document.get('pst_number') or '').strip()
    if province == 'QC' and qst:
        return f'<b>QST:</b> {escape(qst)}'
    if pst and province in {'BC', 'SK', 'MB'}:
        return f'<b>PST:</b> {escape(pst)}'
    return ''


def _footer(document, total_pcs, total_sqf, subtotal, weight, styles):
    currency = document.get('currency') or 'usd'
    discount = _number(document.get('additional_discount_amount'))
    if not discount:
        percent = _number(document.get('additional_discount_percent'))
        if percent:
            discount = round(subtotal * percent / 100.0, 2)
    taxable = round(subtotal - discount, 2)
    supplied = [row for row in (document.get('tax_lines') or []) if isinstance(row, dict)]
    tax_rows = []
    tax_value = 0.0
    if supplied:
        import tax_numbers
        for row in supplied:
            amount = tax_numbers.tax_amount(taxable, row.get('rate'))
            tax_value += amount
            rate = str(row.get('rate') or '')
            label = f"{row.get('name') or 'Tax'} {rate}%"
            tax_rows.append((label, money_text(currency, amount)))
        tax_value = round(tax_value, 2)
    else:
        tax_amount = document.get('tax_amount')
        if tax_amount not in (None, ''):
            tax_text = money_text(currency, tax_amount)
            tax_value = _number(tax_amount)
        else:
            label = str(document.get('tax_label') or '').strip()
            tax_text = label or 'AS APPLICABLE'
            tax_value = 0.0
        if tax_text == 'AS APPLICABLE':
            tax_text = '- AS APPLICABLE'
        tax_rows.append(('Tax:', tax_text))
    grand = round(taxable + tax_value, 2)
    summary = Paragraph(
        f'Total Pcs: {total_pcs}&nbsp;&nbsp;&nbsp;&nbsp;Total SQ/FT : {total_sqf:.2f}'
        f'&nbsp;&nbsp;&nbsp;&nbsp;Total Weight: Appx. {weight:.2f} LBS',
        styles['foot'],
    )
    signature = Paragraph(
        'Accept Signature: ____________________<br/>Date: ____________________<br/>Name: ____________________',
        styles['foot'],
    )
    rows = [
        [Paragraph('Sub-Total:', styles['sum_label']), Paragraph(money_text(currency, subtotal), styles['sum_amount'])],
    ]
    if discount > 0:
        rows.append([
            Paragraph('Discount:', styles['sum_label']),
            Paragraph(money_text(currency, discount), styles['sum_amount']),
        ])
    for label, amount in tax_rows:
        shown = label if str(label).endswith(':') else f'{label}:'
        rows.append([
            Paragraph(escape(shown), styles['sum_label']),
            Paragraph(escape(str(amount)), styles['sum_amount']),
        ])
    rows.append([
        Paragraph('<b>Total:</b>', styles['sum_label']),
        Paragraph(f'<b>{money_text(currency, grand)}</b>', styles['sum_amount']),
    ])
    totals = Table(rows, colWidths=[1.35 * inch, 1.9 * inch])
    totals.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (0, -1), 8),
        ('RIGHTPADDING', (1, 0), (1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    totals.hAlign = 'RIGHT'
    block = Table([[signature, totals]], colWidths=[4.4 * inch, 3.3 * inch])
    block.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
    ]))
    return [
        Spacer(1, 8),
        summary,
        Spacer(1, 8),
        block,
        Spacer(1, 12),
        Paragraph(TERMS, styles['small']),
    ]


def is_size_document(document, items):
    declared = str(document.get('type') or '').lower()
    if declared == 'size':
        return True
    if declared == 'sqf':
        return False
    for item in items:
        spec = item.get('spec') if isinstance(item, dict) and isinstance(item.get('spec'), dict) else None
        if spec and spec.get('width') not in (None, ''):
            return True
        if isinstance(item, dict) and item.get('sqf') not in (None, '', 0, '0'):
            return True
    return False


def build_pdf(document):
    """Portrait letter PDF. The table header repeats when the lines continue onto another page."""
    styles = _styles()
    items = [item for item in (document.get('items') or []) if isinstance(item, dict)]
    for extra in document.get('extra_charges') or []:
        if isinstance(extra, dict) and str(extra.get('label') or '').strip():
            items.append({'name': extra.get('label'), 'total': extra.get('amount'), 'pcs': ''})
    remarks = strip_copy_history(document.get('remarks')).strip()
    if any(item.get('irregular_shape') or item.get('irregular') for item in items) and DRAWING_NOTE not in remarks:
        remarks = (remarks + '\n' + DRAWING_NOTE).strip()
    prepared = dict(document)
    prepared['remarks'] = remarks
    size = is_size_document(prepared, items)
    if size:
        table, total_pcs, total_sqf, subtotal = _size_table(items, prepared.get('currency'), styles)
    else:
        table, total_pcs, total_sqf, subtotal = _sqf_table(items, prepared.get('currency'), styles)
    weight = 0.0
    factor = _number(prepared.get('weight_factor'), 0.512)
    if size:
        weight = sum(line_weight(item, factor) for item in items if not is_charge_line(item))
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=0.38 * inch, bottomMargin=0.4 * inch,
        title=prepared.get('project') or 'ORA Glass',
    )
    story = _header(prepared, styles)
    story.append(table)
    story.extend(_footer(prepared, total_pcs, total_sqf, subtotal, weight, styles))
    doc.build(story)
    return buffer.getvalue()

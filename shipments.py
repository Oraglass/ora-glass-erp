"""Shipment quantities and what is still open to ship or invoice.

A shipment never takes more than the remaining quantity, and a quantity is
invoiced at most once. Orders with no shipments keep the existing full invoice.
"""

from billing import money
import production

SURCHARGE = 'irregular_surcharge'
DRAWING = 'dwg_creation'


def is_glass_line(item):
    if not isinstance(item, dict) or item.get('kind') in {SURCHARGE, DRAWING}:
        return False
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
    return spec.get('width') not in (None, '') and spec.get('height') not in (None, '')


def _pcs(item):
    raw = item.get('pcs')
    if raw in (None, '', 0):
        raw = item.get('qty')
    try:
        count = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def glass_rows(order):
    if (order or {}).get('legacy'):
        import packing
        return [{
            'index': row['index'],
            'serial': row['serial'],
            'name': row['product'] or 'Glass',
            'pcs': row['ordered'],
            'sqf_each': 0,
            'price_pc': 0,
            'price_sqf': 0,
            'mark': row['floor'] or '',
            'product_id': '',
            'remarks': '',
            'spec': {},
        } for row in packing.serial_catalog(order)]
    rows = []
    for index, item in enumerate((order or {}).get('items') or []):
        if not is_glass_line(item):
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        rows.append({
            'index': index,
            'serial': len(rows) + 1,
            'name': item.get('name') or 'Glass',
            'pcs': _pcs(item),
            'sqf_each': money(item.get('sqf')),
            'price_pc': money(item.get('price_pc') if item.get('price_pc') not in (None, '') else item.get('unit_price')),
            'price_sqf': money(item.get('price_sqf')),
            'mark': item.get('mark') or item.get('floor') or item.get('floor_code') or item.get('location') or '',
            'product_id': item.get('product_id') or item.get('production_id') or '',
            'remarks': item.get('remarks') or '',
            'spec': spec,
        })
    return rows


def _linked_charges(items):
    """Surcharge and drawing charges follow the glass line they belong to."""
    links = {}
    last = None
    ids = {}
    for index, item in enumerate(items or []):
        if is_glass_line(item):
            last = index
            if item.get('line_id'):
                ids[str(item.get('line_id'))] = index
            continue
        if item.get('kind') == SURCHARGE and last is not None:
            links.setdefault(last, []).append(index)
        elif item.get('kind') == DRAWING:
            parent = ids.get(str(item.get('drawing_for') or ''))
            if parent is None:
                parent = last
            if parent is not None:
                links.setdefault(parent, []).append(index)
    return links


def shipped_qty(order, index):
    total = 0
    for shipment in (order or {}).get('shipments') or []:
        for line in shipment.get('lines') or []:
            if line.get('index') == index:
                total += _pcs(line)
    return total


def invoiced_qty(order, index):
    total = 0
    for shipment in (order or {}).get('shipments') or []:
        if not shipment.get('invoiced'):
            continue
        for line in shipment.get('lines') or []:
            if line.get('index') == index:
                total += _pcs(line)
    return total


def remaining_qty(order, index):
    if (order or {}).get('legacy'):
        ordered = next((row['pcs'] for row in glass_rows(order) if row['index'] == index), 0)
        return max(0, ordered - shipped_qty(order, index))
    items = (order or {}).get('items') or []
    item = items[index] if 0 <= index < len(items) else None
    if not item:
        return 0
    return max(0, _pcs(item) - shipped_qty(order, index))


def is_open_windows(customer):
    customer = customer or {}
    name = str(customer.get('name') or '').strip().casefold()
    return customer.get('id') == 'open-windows' or name in {'open windows', 'open windows inc'}


def billing_mode(order, customer, canadian):
    """How this order is invoiced once staff record shipments.

    Every Canadian customer, including Open Windows, is invoiced the same way:
    Ora Glass Inc bills Ora Glass (Canada) Inc in USD on each shipment, and
    Ora Glass (Canada) Inc bills the customer in CAD when the order is fully shipped.
    """
    if canadian or is_open_windows(customer):
        return 'canada'
    if (order or {}).get('partial_invoicing'):
        return 'us_partial'
    return 'us_full'


def parse_quantities(raw, order):
    """Map line index to a positive ship quantity. Rejects more than what is left."""
    if not isinstance(raw, list) or not raw:
        return None, 'Enter a quantity for at least one line.'
    chosen = {}
    items = (order or {}).get('items') or []
    legacy = {row['index']: row for row in glass_rows(order)} if (order or {}).get('legacy') else {}
    for row in raw:
        if not isinstance(row, dict):
            return None, 'Each shipped line needs a quantity.'
        try:
            index = int(row.get('index'))
            qty = int(row.get('qty') or 0)
        except (TypeError, ValueError):
            return None, 'Quantity must be a whole number.'
        if qty <= 0:
            continue
        if (order or {}).get('legacy'):
            if index not in legacy:
                return None, 'That line is not on this order.'
        elif index < 0 or index >= len(items) or not is_glass_line(items[index]):
            return None, 'That line is not on this order.'
        if index in chosen:
            return None, 'A line can only be entered once on a shipment.'
        left = remaining_qty(order, index)
        if qty > left:
            name = legacy[index]['name'] if (order or {}).get('legacy') else (items[index].get('name') or 'Glass')
            return None, f'Only {left} of {name} are left to ship.'
        chosen[index] = qty
    if not chosen:
        return None, 'Enter a quantity for at least one line.'
    return chosen, None


def shipment_lines(order, quantities):
    if (order or {}).get('legacy'):
        catalog = {row['index']: row for row in glass_rows(order)}
        built = []
        pcs = 0
        for index, qty in sorted(quantities.items()):
            info = catalog.get(index) or {}
            built.append({
                'name': info.get('name') or 'Glass',
                'description': info.get('name') or 'Glass',
                'spec': {},
                'pcs': qty,
                'qty': qty,
                'sqf': 0,
                'price_sqf': 0,
                'price_pc': 0,
                'unit_price': 0,
                'total': 0,
                'source_index': index,
                'serial': info.get('serial'),
            })
            pcs += qty
        return built, pcs, 0.0
    items = order.get('items') or []
    links = _linked_charges(items)
    built = []
    pcs = 0
    sqf = 0.0
    serials = {row['index']: row['serial'] for row in glass_rows(order)}
    for index, qty in sorted(quantities.items()):
        source = items[index]
        each = money(source.get('price_pc') if source.get('price_pc') not in (None, '') else source.get('unit_price'))
        area = money(source.get('sqf'))
        total = round(each * qty, 2)
        built.append({
            'name': source.get('name') or 'Glass',
            'description': source.get('name') or 'Glass',
            'spec': source.get('spec'),
            'pcs': qty,
            'qty': qty,
            'sqf': round(area * qty, 2),
            'price_sqf': money(source.get('price_sqf')),
            'price_pc': each,
            'unit_price': each,
            'total': total,
            'source_index': index,
            'serial': serials.get(index),
        })
        pcs += qty
        sqf += area * qty
        ordered = _pcs(source) or qty
        ratio = qty / ordered
        for charge_index in links.get(index, []):
            charge = items[charge_index]
            amount = round(money(charge.get('total')) * ratio, 2)
            if amount <= 0:
                continue
            built.append({
                'kind': charge.get('kind'),
                'name': charge.get('name') or 'Charge',
                'description': charge.get('description') or charge.get('name') or 'Charge',
                'pcs': '',
                'qty': '',
                'sqf': 0,
                'price_sqf': 0,
                'price_pc': amount,
                'unit_price': amount,
                'total': amount,
                'source_index': charge_index,
            })
    return built, pcs, round(sqf, 2)


def shipment_progress(order):
    """Remaining glass, value, and a short shipping label."""
    items = (order or {}).get('items') or []
    lines = []
    ordered_pcs = shipped_pcs = invoiced_pcs = 0
    ordered_value = shipped_value = invoiced_value = 0.0
    remaining_sqf = 0.0
    for row in glass_rows(order):
        index = row['index']
        shipped = shipped_qty(order, index)
        invoiced = invoiced_qty(order, index)
        left = max(0, row['pcs'] - shipped)
        left_invoice = max(0, row['pcs'] - invoiced)
        value = round(row['price_pc'] * row['pcs'], 2)
        ordered_pcs += row['pcs']
        shipped_pcs += shipped
        invoiced_pcs += invoiced
        ordered_value += value
        shipped_value += row['price_pc'] * shipped
        invoiced_value += row['price_pc'] * invoiced
        remaining_sqf += row['sqf_each'] * left
        if (order or {}).get('legacy') and (left or left_invoice):
            import packing
            info = next((item for item in packing.serial_catalog(order) if item['index'] == index), {})
            lines.append({
                'index': index,
                'serial': row['serial'],
                'floor_code': info.get('floor') or '',
                'size': info.get('size') or '',
                'product_id': '',
                'name': info.get('product') or row['name'],
                'width_mm': None,
                'height_mm': None,
                'width_in': None,
                'height_in': None,
                'ordered': row['pcs'],
                'shipped': shipped,
                'invoiced': invoiced,
                'remaining': left,
                'bo_qty': left,
                'unit_price': 0,
                'remarks': '',
            })
            continue
        if left or left_invoice:
            import currency_rules
            rate, _effective = currency_rules.rate_on((order or {}).get('date'))
            exported = production.line_export(
                items[index], quantity=left, serial=row['serial'],
                currency=(order or {}).get('currency'), fx_rate=rate,
            )
            from documents import piece_size_text
            line_spec = items[index].get('spec') if isinstance(items[index].get('spec'), dict) else {}
            lines.append({
                'index': index,
                'serial': exported['Serial Number'],
                'floor_code': exported['Floor Code'],
                'size': piece_size_text(line_spec.get('width'), line_spec.get('height'), line_spec.get('unit')),
                'product_id': exported['Product ID'],
                'name': exported['Product Name'],
                'width_mm': exported['Width(mm)'],
                'height_mm': exported['Height(mm)'],
                'width_in': exported['Width(In)'],
                'height_in': exported['Height(In)'],
                'ordered': row['pcs'],
                'shipped': shipped,
                'invoiced': invoiced,
                'remaining': left,
                'bo_qty': exported['Quantity'],
                'unit_price': exported['Unit Price'],
                'remarks': exported['Remarks'],
            })
    fully_shipped = ordered_pcs > 0 and shipped_pcs >= ordered_pcs
    fully_invoiced = ordered_pcs > 0 and invoiced_pcs >= ordered_pcs
    if (order or {}).get('legacy') and fully_shipped:
        label = 'Shipped'
    elif fully_shipped and fully_invoiced:
        label = 'Fully shipped & invoiced'
    elif shipped_pcs > 0:
        label = 'Partially shipped'
    else:
        label = 'Not shipped'
    return {
        'lines': lines,
        'ordered_pcs': ordered_pcs,
        'shipped_pcs': shipped_pcs,
        'invoiced_pcs': invoiced_pcs,
        'remaining_pcs': max(0, ordered_pcs - shipped_pcs),
        'remaining_sqf': round(remaining_sqf, 2),
        'remaining_value': round(max(0.0, ordered_value - shipped_value), 2),
        'ordered_value': round(ordered_value, 2),
        'invoiced_value': round(invoiced_value, 2),
        'fully_shipped': fully_shipped,
        'fully_invoiced': fully_invoiced,
        'label': label,
        'open': ordered_pcs > 0 and not (fully_shipped and fully_invoiced),
    }


BACKORDER_HEADERS = ('Order', 'Customer') + tuple(
    'BO quantity' if name == 'Quantity' else name for name in production.PRODUCTION_HEADERS
) + ('Packed', 'Shipped', 'Not packed', 'Ready to ship', 'Size')


def packed_quantities_to_ship(order):
    """Packed pieces that have not shipped yet. Empty when the order has no packing lists."""
    import packing
    if not (order or {}).get('packing_lists'):
        return {}
    chosen = {}
    for row in packing.packing_progress(order)['serials']:
        left = max(0, int(row.get('packed') or 0) - shipped_qty(order, row['index']))
        left = min(left, remaining_qty(order, row['index']))
        if left > 0:
            chosen[row['index']] = left
    return chosen


def shipment_pdf_bytes(order, shipment):
    """A packing slip for one shipment. Goods leave Ora Glass Inc, so the slip has no GST."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table
    from xml.sax.saxutils import escape
    from documents import _styles, header_address_block, piece_size_text
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.55 * inch, bottomMargin=0.5 * inch)
    story = [
        Paragraph('Shipment / Packing slip<br/>' + header_address_block({'issuer': 'us', 'kind': 'shipment', 'currency': 'usd'}), styles['title']),
        Spacer(1, 8),
        Paragraph(
            f"<b>Shipment:</b> {escape(shipment.get('number') or '')}"
            f"&nbsp;&nbsp;<b>Date:</b> {escape(shipment.get('date') or '')}"
            f"&nbsp;&nbsp;<b>Rack:</b> {escape(shipment.get('rack') or '')}"
            + (f"&nbsp;&nbsp;<b>Truck:</b> {escape(shipment.get('truck_plate') or '')}" if shipment.get('truck_plate') else ''),
            styles['small'],
        ),
        Paragraph(
            f"<b>Sold To:</b> {escape(order.get('company') or '')}"
            f"&nbsp;&nbsp;<b>Job:</b> {escape(order.get('project') or '')}",
            styles['small'],
        ),
        Spacer(1, 8),
    ]
    catalog = {}
    try:
        import packing
        catalog = {row['index']: row for row in packing.serial_catalog(order)}
    except Exception:
        catalog = {}
    data = [['Serial', 'Floor', 'Product', 'Size', 'Pcs']]
    for line in shipment.get('lines') or []:
        info = catalog.get(line.get('index')) or {}
        items = order.get('items') or []
        index = line.get('index')
        item = items[index] if isinstance(index, int) and 0 <= index < len(items) else {}
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        size = info.get('size') or piece_size_text(spec.get('width'), spec.get('height'), spec.get('unit'))
        data.append([
            str(line.get('serial') or info.get('serial') or ''),
            info.get('floor') or item.get('floor') or '',
            info.get('product') or item.get('name') or '',
            size,
            str(line.get('qty') or line.get('pcs') or ''),
        ])
    table = Table(data, colWidths=[0.7 * inch, 0.9 * inch, 2.4 * inch, 2.4 * inch, 0.7 * inch])
    story.append(table)
    if shipment.get('notes'):
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<b>Notes:</b> {escape(shipment.get('notes') or '')}", styles['small']))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"<b>Total pcs:</b> {escape(str(shipment.get('pcs') or ''))}", styles['small']))
    doc.build(story)
    return buffer.getvalue()


def _slip_bytes(title, order, sections, totals):
    """A letter PDF in the same header style as the shipment packing slip."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table
    from xml.sax.saxutils import escape
    from documents import _styles, header_address_block
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=0.55 * inch, rightMargin=0.55 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    story = [
        Paragraph(escape(title) + '<br/>' + header_address_block({'issuer': 'us', 'kind': 'shipment', 'currency': 'usd'}), styles['title']),
        Spacer(1, 8),
        Paragraph(
            f"<b>Customer:</b> {escape(order.get('company') or '')}"
            f"&nbsp;&nbsp;<b>Project:</b> {escape(order.get('project') or '')}"
            f"&nbsp;&nbsp;<b>PO:</b> {escape(order.get('po') or order.get('customer_po') or '')}",
            styles['small'],
        ),
        Spacer(1, 8),
    ]
    story.extend(sections)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"<b>Total pcs:</b> {totals.get('pcs', 0)}"
        f"&nbsp;&nbsp;<b>Total sqft:</b> {totals.get('sqft', 0):.2f}"
        f"&nbsp;&nbsp;<b>Total weight:</b> Appx. {totals.get('weight', 0):.2f} LBS",
        styles['small'],
    ))
    doc.build(story)
    return buffer.getvalue()


def _line_measures(order, index, qty):
    from documents import line_weight, real_area
    items = (order or {}).get('items') or []
    item = dict(items[index]) if index < len(items) and isinstance(items[index], dict) else {}
    item['pcs'] = qty
    sqft = real_area(item) if qty else 0.0
    weight = line_weight(item, 0.512) if qty else 0.0
    return round(sqft, 2), round(weight, 2)


def packing_lists_pdf_bytes(order):
    """Latest packing lists for one order, in the packing-slip layout."""
    import packing
    from reportlab.platypus import Paragraph, Spacer, Table
    from xml.sax.saxutils import escape
    from documents import _styles
    styles = _styles()
    catalog = {row['serial']: row for row in packing.serial_catalog(order)}
    sections = []
    pcs = sqft = weight = 0.0
    lists = [row for row in (order or {}).get('packing_lists') or [] if row.get('current', True)]
    if not lists:
        sections.append(Paragraph('No packing lists are on this order.', styles['small']))
    for entry in lists:
        sections.append(Paragraph(
            f"<b>Packing list:</b> {escape(entry.get('number') or '')}"
            f"&nbsp;&nbsp;<b>Date:</b> {escape(entry.get('date') or '')}"
            f"&nbsp;&nbsp;<b>Rack:</b> {escape(entry.get('rack') or '')}",
            styles['small'],
        ))
        data = [['Serial', 'Floor', 'Product', 'Size', 'Pcs packed']]
        for line in entry.get('lines') or []:
            info = catalog.get(int(line.get('serial'))) if str(line.get('serial') or '').isdigit() else None
            if not info:
                continue
            qty = int(line.get('qty') or 0)
            line_sqft, line_weight_lbs = _line_measures(order, info['index'], qty)
            pcs += qty
            sqft += line_sqft
            weight += line_weight_lbs
            data.append([
                str(info['serial']), info.get('floor') or '', info.get('product') or '',
                info.get('size') or '', str(qty),
            ])
        sections.append(Table(data, colWidths=[0.7 * 72, 0.8 * 72, 2.3 * 72, 2.5 * 72, 0.9 * 72]))
        sections.append(Spacer(1, 8))
    return _slip_bytes('Packing List', order, sections, {'pcs': int(pcs), 'sqft': sqft, 'weight': weight})


def backorder_list_pdf_bytes(order):
    """Back-ordered pieces only, in the packing-slip layout."""
    import packing
    from reportlab.platypus import Table
    progress = packing.packing_progress(order)
    data = [['Serial', 'Floor', 'Product', 'Size', 'Ordered', 'Packed', 'Shipped', 'BO']]
    pcs = sqft = weight = 0.0
    for row in progress['serials']:
        if int(row.get('bo') or 0) <= 0:
            continue
        shipped = shipped_qty(order, row['index'])
        line_sqft, line_weight_lbs = _line_measures(order, row['index'], int(row['bo']))
        pcs += int(row['bo'])
        sqft += line_sqft
        weight += line_weight_lbs
        data.append([
            str(row['serial']), row.get('floor') or '', row.get('product') or '', row.get('size') or '',
            str(row.get('ordered') or 0), str(row.get('packed') or 0), str(shipped), str(row.get('bo') or 0),
        ])
    if len(data) == 1:
        data.append(['', '', 'No back ordered pieces', '', '', '', '', ''])
    table = Table(data, colWidths=[0.6 * 72, 0.7 * 72, 1.8 * 72, 2.1 * 72, 0.7 * 72, 0.7 * 72, 0.7 * 72, 0.5 * 72])
    return _slip_bytes('Back Ordered List', order, [table], {'pcs': int(pcs), 'sqft': sqft, 'weight': weight})


def open_order_lines(order):
    """Pieces still to pack or still to ship. Packed quantity never exceeds what the order has."""
    import packing
    catalog = packing.serial_catalog(order)
    packed_rows = {}
    if (order or {}).get('packing_lists'):
        packed_rows = {row['serial']: row for row in packing.packing_progress(order)['serials']}
    lines = []
    items = (order or {}).get('items') or []
    for info in catalog:
        shipped = shipped_qty(order, info['index'])
        ordered = info['ordered']
        if packed_rows:
            packed = int(packed_rows.get(info['serial'], {}).get('packed') or 0)
            not_packed = int(packed_rows.get(info['serial'], {}).get('bo') or 0)
        else:
            packed = 0
            not_packed = max(0, ordered - shipped)
        ready = max(0, packed - shipped) if packed_rows else 0
        remaining_ship = max(0, ordered - shipped)
        if packed_rows:
            if ready <= 0 and not_packed <= 0 and remaining_ship <= 0:
                continue
        elif remaining_ship <= 0:
            continue
        item = items[info['index']] if info['index'] < len(items) else {}
        import currency_rules
        rate, _effective = currency_rules.rate_on((order or {}).get('date'))
        exported = production.line_export(
            item, quantity=remaining_ship, serial=info['serial'],
            currency=(order or {}).get('currency'), fx_rate=rate,
        )
        lines.append({
            'index': info['index'],
            'serial': info['serial'],
            'floor_code': info['floor'],
            'size': info['size'],
            'product_id': exported['Product ID'],
            'name': info['product'] or exported['Product Name'],
            'width_mm': exported['Width(mm)'],
            'height_mm': exported['Height(mm)'],
            'width_in': exported['Width(In)'],
            'height_in': exported['Height(In)'],
            'ordered': ordered,
            'packed': packed,
            'shipped': shipped,
            'not_packed': not_packed,
            'ready_to_ship': ready,
            'remaining': remaining_ship,
            'bo_qty': remaining_ship,
            'unit_price': exported['Unit Price'],
            'remarks': exported['Remarks'],
        })
    return lines


def backorder_table(orders):
    """One row per back-ordered line, kept together by order."""
    table = []
    for order in orders or []:
        for line in order.get('lines') or []:
            if not line.get('bo_qty') and not line.get('remaining'):
                continue
            table.append([
                order.get('id'),
                order.get('company') or '',
                line.get('serial'),
                line.get('floor_code') or '',
                line.get('product_id') or '',
                line.get('name') or '',
                line.get('unit_price'),
                line.get('width_mm'),
                line.get('height_mm'),
                line.get('width_in'),
                line.get('height_in'),
                line.get('bo_qty') if line.get('bo_qty') is not None else line.get('remaining'),
                line.get('remarks') or '',
                line.get('packed') or 0,
                line.get('shipped') or 0,
                line.get('not_packed') if line.get('not_packed') is not None else '',
                line.get('ready_to_ship') or 0,
                line.get('size') or '',
            ])
    return table


def backorder_workbook(orders):
    from io import BytesIO
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = 'Backorders'
    sheet.append(list(BACKORDER_HEADERS))
    for row in backorder_table(orders):
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def backorder_pdf(orders):
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    styles = getSampleStyleSheet()
    small = styles['Normal']
    small.fontSize = 7
    small.leading = 8
    header = list(BACKORDER_HEADERS)
    data = [header]
    for row in backorder_table(orders):
        data.append([Paragraph(str(cell if cell is not None else ''), small) for cell in row])
    if len(data) == 1:
        data.append([Paragraph('Nothing is waiting to ship.', small)] + [''] * (len(header) - 1))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=0.35 * inch, rightMargin=0.35 * inch, topMargin=0.4 * inch, bottomMargin=0.4 * inch)
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C41E3A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    doc.build([Paragraph('Open shipments / backorders', styles['Title']), Spacer(1, 8), table])
    return buffer.getvalue()

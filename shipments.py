"""Shipment quantities and what is still open to ship or invoice.

A shipment never takes more than the remaining quantity, and a quantity is
invoiced at most once. Orders with no shipments keep the existing full invoice.
"""

from billing import money

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
    rows = []
    for index, item in enumerate((order or {}).get('items') or []):
        if not is_glass_line(item):
            continue
        rows.append({
            'index': index,
            'name': item.get('name') or 'Glass',
            'pcs': _pcs(item),
            'sqf_each': money(item.get('sqf')),
            'price_pc': money(item.get('price_pc') if item.get('price_pc') not in (None, '') else item.get('unit_price')),
            'price_sqf': money(item.get('price_sqf')),
            'mark': item.get('mark') or item.get('floor') or item.get('floor_code') or '',
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
    item = ((order or {}).get('items') or [None])[index] if index < len((order or {}).get('items') or []) else None
    if not item:
        return 0
    return max(0, _pcs(item) - shipped_qty(order, index))


def is_open_windows(customer):
    customer = customer or {}
    name = str(customer.get('name') or '').strip().casefold()
    return customer.get('id') == 'open-windows' or name in {'open windows', 'open windows inc'}


def billing_mode(order, customer, canadian):
    """How this order is invoiced once staff record shipments."""
    if is_open_windows(customer):
        return 'open_windows'
    if canadian:
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
        if index < 0 or index >= len(items) or not is_glass_line(items[index]):
            return None, 'That line is not on this order.'
        if index in chosen:
            return None, 'A line can only be entered once on a shipment.'
        left = remaining_qty(order, index)
        if qty > left:
            name = items[index].get('name') or 'Glass'
            return None, f'Only {left} of {name} are left to ship.'
        chosen[index] = qty
    if not chosen:
        return None, 'Enter a quantity for at least one line.'
    return chosen, None


def shipment_lines(order, quantities):
    items = order.get('items') or []
    links = _linked_charges(items)
    built = []
    pcs = 0
    sqf = 0.0
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
                'pcs': 1,
                'qty': 1,
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
        if left or left_invoice:
            lines.append({
                'index': index,
                'name': row['name'],
                'ordered': row['pcs'],
                'shipped': shipped,
                'invoiced': invoiced,
                'remaining': left,
                'sqf_remaining': round(row['sqf_each'] * left, 2),
                'value_remaining': round(row['price_pc'] * left, 2),
            })
    fully_shipped = ordered_pcs > 0 and shipped_pcs >= ordered_pcs
    fully_invoiced = ordered_pcs > 0 and invoiced_pcs >= ordered_pcs
    if fully_shipped and fully_invoiced:
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

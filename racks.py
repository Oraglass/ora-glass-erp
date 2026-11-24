"""Look up a rack from the text on its existing QR label.

Those labels contain only the rack number, for example 1054. Scanning adds
whitespace, so the number is the scanned text with the ends trimmed.
"""


def rack_number_from_scan(text):
    """The rack number is the scanned text, without surrounding whitespace."""
    return str(text if text is not None else '').strip()


def lookup_rack(orders, raw):
    """Match the scanned rack number to the current packing lists."""
    import packing

    rack = rack_number_from_scan(raw)
    raw_text = '' if raw is None else str(raw)
    matches = []
    if rack:
        for order in orders or []:
            catalog = None
            for entry in order.get('packing_lists') or []:
                if not entry.get('current', True):
                    continue
                if str(entry.get('rack') or '').strip() != rack:
                    continue
                if catalog is None:
                    catalog = {row['serial']: row for row in packing.serial_catalog(order)}
                lines = []
                for line in entry.get('lines') or []:
                    try:
                        serial = int(line.get('serial'))
                    except (TypeError, ValueError):
                        continue
                    info = catalog.get(serial) or {}
                    lines.append({
                        'serial': serial,
                        'qty': line.get('qty') or 0,
                        'floor': info.get('floor') or '',
                        'product': info.get('product') or '',
                        'size': info.get('size') or '',
                    })
                matches.append({
                    'order_id': order.get('id'),
                    'company': order.get('company') or '',
                    'project': order.get('project') or '',
                    'number': entry.get('number') or '',
                    'date': entry.get('date') or '',
                    'rack': rack,
                    'lines': lines,
                })
    return {
        'rack': rack,
        'raw': raw_text,
        'found': bool(matches),
        'matches': matches,
    }

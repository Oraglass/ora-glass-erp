"""Production-software product IDs and the order Excel export.

Product IDs come from the MAC_IGU sheet, the last tab of pricing_base.xlsx.
A blank ID is normal: the production software assigns one after import.
"""

import copy
import math
import os
import re

from openpyxl import Workbook, load_workbook

from quoting import DWG_KIND, SURCHARGE_KIND, load_pricing_workbook
from storage import (
    atomic_write_json,
    data_dir,
    lock_for,
    read_json,
    resolve_pricing_path,
)

PRODUCTION_HEADERS = (
    'Serial Number',
    'Floor Code',
    'Product ID',
    'Product Name',
    'Unit Price',
    'Width(mm)',
    'Height(mm)',
    'Width(In)',
    'Height(In)',
    'Quantity',
    'Remarks',
)

MM_PER_INCH = 25.4


def production_catalog_path():
    return os.path.join(data_dir(), 'production_products.json')


def export_settings_path():
    return os.path.join(data_dir(), 'production_export_settings.json')


PRODUCTION_TABS = (
    {'id': 'ids', 'label': 'Production IDs'},
    {'id': 'excel', 'label': 'Production Excel export'},
)


def load_export_settings():
    raw = read_json(export_settings_path(), {})
    if not isinstance(raw, dict):
        raw = {}
    flag = raw.get('generate_on_confirm', True)
    if isinstance(flag, str):
        flag = flag.strip().lower() not in {'0', 'false', 'off', 'no'}
    return {'generate_on_confirm': bool(flag)}


def save_export_settings(generate_on_confirm):
    path = export_settings_path()
    with lock_for(path):
        atomic_write_json(path, {'generate_on_confirm': bool(generate_on_confirm)})
    return load_export_settings()


def _six(value):
    """Six decimal places, cut off rather than rounded, matching 1000 mm -> 39.370078."""
    return math.trunc(float(value) * 1_000_000) / 1_000_000


def dimensions_mm_in(width, height, unit):
    """Both millimetres and inches, whichever unit the line was entered in."""
    wide = float(width)
    high = float(height)
    if str(unit or 'in').lower() == 'mm':
        width_mm, height_mm = wide, high
    else:
        width_mm, height_mm = wide * MM_PER_INCH, high * MM_PER_INCH
    return (
        round(width_mm, 6),
        round(height_mm, 6),
        _six(width_mm / MM_PER_INCH),
        _six(height_mm / MM_PER_INCH),
    )


def _collapse(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def glass_token(name, tempered=False):
    """ERP glass name to the production makeup token. Jumbo and Large share the lite name."""
    text = _collapse(name).replace(' Jumbo', '').replace(' Large', '').replace(' / sqf', '')
    match = re.search(r'(\d+)mm', text)
    if not match:
        return ''
    rest = _collapse(re.sub(r'\d+mm', '', text))
    token = f'{match.group(1)}mm{rest}'
    if tempered:
        token += ' Tempered'
    return token


def spacer_token(name, sealant):
    match = re.search(r'(\d+)mm', str(name or ''))
    if not match:
        return ''
    kind = 'Technoform' if 'techno' in str(name).lower() else 'SuperSpacer'
    seal = 'Silicone' if sealant and 'silicone' in str(sealant).lower() else 'Polysulfide'
    return f'{match.group(1)}mm{kind} with Argon({seal})'


def makeup_from_spec(spec):
    """Canonical IGU makeup. Empty when the line is not a build-up we can name."""
    spec = spec or {}
    parts = []
    for index in (1, 2, 3):
        glass = spec.get(f'glass{index}') or ''
        if not glass or glass == 'n/a':
            continue
        token = glass_token(glass, spec.get(f'temper{index}') == 'yes')
        if not token:
            return ''
        parts.append(token)
        spacer = spec.get(f'spacer{index}') or ''
        if spacer and spacer != 'n/a':
            spacer_name = spacer_token(spacer, spec.get(f'sealant{index}'))
            if not spacer_name:
                return ''
            parts.append(spacer_name)
    return '+'.join(parts)


def makeup_from_product_name(product_name):
    """Drop the overall thickness prefix from a MAC_IGU product name."""
    text = _collapse(product_name)
    text = re.sub(r'^\d+\s*-\s*', '', text)
    text = text.replace(' +', '+')
    return _collapse(text)


def _pricing_names(path):
    try:
        rows = load_pricing_workbook(path)
    except (OSError, ValueError):
        return [], []
    glass, spacers = [], []
    for item in rows:
        name = item.get('name') or ''
        lowered = name.lower()
        if 'spacer' in lowered or 'technoform' in lowered:
            spacers.append(name)
        elif 'sealant' in lowered or lowered in {'n/a', ''}:
            continue
        elif 'mm' in lowered:
            glass.append(name)
    return glass, spacers


def _token_indexes(glass_names, spacer_names):
    glass = {}
    for name in glass_names:
        for tempered in (False, True):
            token = glass_token(name, tempered)
            if token:
                glass.setdefault(token.casefold(), name)
    spacers = set()
    for name in spacer_names:
        for seal in ('Silicone', 'Polysulfide'):
            token = spacer_token(name, seal)
            if token:
                spacers.add(token.casefold())
    return glass, spacers


def read_product_sheet(path=None):
    """Rows from the workbook's last tab, or from an uploaded workbook with the same columns."""
    chosen = path or resolve_pricing_path()
    if not chosen or not os.path.isfile(chosen):
        return []
    workbook = load_workbook(chosen, data_only=True, read_only=True)
    try:
        sheet = workbook[workbook.sheetnames[-1]]
        rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return []
    header = [str(cell or '').strip() for cell in rows[0]]
    def column(name):
        for index, label in enumerate(header):
            if label.casefold() == name.casefold():
                return index
        return None
    id_col = column('Product ID')
    name_col = column('Product Name')
    if id_col is None or name_col is None:
        return []
    parsed = []
    for row in rows[1:]:
        if not row or name_col >= len(row):
            continue
        product_name = str(row[name_col] or '').strip()
        if not product_name:
            continue
        raw_id = row[id_col] if id_col < len(row) else ''
        product_id = '' if raw_id in (None, '') else str(raw_id).strip()
        if product_id.endswith('.0') and product_id[:-2].isdigit():
            product_id = product_id[:-2]
        parsed.append({'product_id': product_id, 'product_name': product_name})
    return parsed


_SEED_CACHE = {}


def seed_products(path=None):
    """Map MAC_IGU rows onto ERP IGU makeups. Unmatched rows stay in the list for the report."""
    chosen = path or resolve_pricing_path()
    stamp = os.path.getmtime(chosen) if chosen and os.path.isfile(chosen) else None
    cached = _SEED_CACHE.get((chosen, stamp))
    if cached and path is None:
        return cached
    glass_names, spacer_names = _pricing_names(chosen) if chosen else ([], [])
    glass_index, spacer_index = _token_indexes(glass_names, spacer_names)
    products = {}
    unmatched = []
    matched = 0
    for row in read_product_sheet(chosen):
        makeup = makeup_from_product_name(row['product_name'])
        missing = []
        for part in makeup.split('+'):
            token = part.strip()
            if not token:
                continue
            folded = token.casefold()
            if 'with argon' in folded:
                if folded not in spacer_index:
                    missing.append(token)
            elif folded not in glass_index:
                missing.append(token)
        record = {
            'kind': 'igu',
            'key': makeup.casefold(),
            'label': makeup,
            'product_id': row['product_id'],
            'product_name': row['product_name'],
            'erp_name': makeup,
            'matched': not missing,
            'missing': missing,
        }
        if missing:
            unmatched.append({
                'product_id': row['product_id'],
                'product_name': row['product_name'],
                'missing': missing,
            })
            continue
        matched += 1
        products.setdefault(record['key'], record)
    report = {'matched': matched, 'unmatched': unmatched, 'sheet_rows': matched + len(unmatched)}
    if path is None:
        _SEED_CACHE[(chosen, stamp)] = (products, report)
    return products, report


def _saved_overrides():
    raw = read_json(production_catalog_path(), {})
    if not isinstance(raw, dict):
        return {}
    rows = raw.get('products') if isinstance(raw.get('products'), dict) else raw
    if not isinstance(rows, dict):
        return {}
    return rows


def production_catalog():
    """Workbook seed plus every IGU configuration a quote or order has used.

    Admin values, including a blank ID, replace the seed. Usage (first date and
    order ids) always comes from the saved registry.
    """
    seeded = copy.deepcopy(seed_products()[0])
    for key, saved in _saved_overrides().items():
        if not isinstance(saved, dict):
            continue
        current = seeded.get(key) or {
            'kind': 'igu',
            'key': key,
            'label': saved.get('label') or saved.get('product_name') or key,
            'erp_name': saved.get('label') or '',
            'product_id': '',
            'product_name': saved.get('product_name') or saved.get('label') or '',
            'matched': False,
            'missing': [],
        }
        if 'product_id' in saved:
            current['product_id'] = str(saved.get('product_id') or '').strip()
        if saved.get('product_name'):
            current['product_name'] = str(saved.get('product_name')).strip()
        current['first_used'] = str(saved.get('first_used') or '')
        current['order_ids'] = [item for item in (saved.get('order_ids') or []) if item not in (None, '')]
        seeded[key] = current
    for record in seeded.values():
        record.setdefault('first_used', '')
        record.setdefault('order_ids', [])
    return seeded


def seed_report():
    _products, report = seed_products()
    return report


def save_production_overrides(updates):
    """Persist ID and name edits without dropping first-used dates or order counts."""
    path = production_catalog_path()
    with lock_for(path):
        current = _saved_overrides()
        catalog = production_catalog()
        for key, fields in (updates or {}).items():
            if not key or not isinstance(fields, dict):
                continue
            existing = catalog.get(key) or {}
            row = dict(current.get(key) or {})
            if 'product_id' in fields:
                row['product_id'] = str(fields.get('product_id') or '').strip()[:40]
            if fields.get('product_name'):
                row['product_name'] = str(fields.get('product_name')).strip()[:300]
            row.setdefault('label', existing.get('label') or '')
            row.setdefault('first_used', existing.get('first_used') or '')
            row.setdefault('order_ids', list(existing.get('order_ids') or []))
            current[key] = row
        atomic_write_json(path, {'products': current})
    return production_catalog()


def registry_rows():
    """IGU types for the admin page. Configurations with no ID come first."""
    rows = []
    for key, record in production_catalog().items():
        if record.get('kind') == 'glass':
            continue
        product_id = str(record.get('product_id') or '').strip()
        rows.append({
            'key': key,
            'product_name': record.get('product_name') or record.get('label') or '',
            'product_id': product_id,
            'first_used': record.get('first_used') or '',
            'order_count': len(record.get('order_ids') or []),
            'missing': not product_id,
        })
    rows.sort(key=lambda row: (not row['missing'], row['product_name'].casefold()))
    return rows


def remember_configurations(items, used_on, order_id=None):
    """Record every IGU configuration on a quote or order. A new one starts with a blank ID."""
    path = production_catalog_path()
    with lock_for(path):
        saved = _saved_overrides()
        seeded, _report = seed_products()
        changed = False
        for item in items or []:
            if not isinstance(item, dict) or item.get('kind') in {SURCHARGE_KIND, DWG_KIND}:
                continue
            spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
            makeup = makeup_from_spec(spec)
            if not makeup:
                continue
            key = makeup.casefold()
            row = dict(saved.get(key) or {})
            if used_on and not row.get('first_used'):
                row['first_used'] = str(used_on)[:10]
                changed = True
            if key not in seeded and not row.get('product_name'):
                row['product_name'] = str(item.get('name') or makeup).strip()[:300]
                row['label'] = makeup
                changed = True
            elif 'label' not in row:
                row['label'] = makeup
            if order_id not in (None, ''):
                order_ids = [entry for entry in (row.get('order_ids') or [])]
                if order_id not in order_ids and str(order_id) not in {str(entry) for entry in order_ids}:
                    order_ids.append(order_id)
                    row['order_ids'] = order_ids
                    changed = True
            if row != saved.get(key):
                saved[key] = row
                changed = True
        if changed:
            atomic_write_json(path, {'products': saved})
    return production_catalog()


def _names_for(record):
    names = set()
    for field in ('product_name', 'erp_name', 'label'):
        text = str(record.get(field) or '').strip()
        if text:
            names.add(text)
    return names


def apply_product_upload(rows, replace=False):
    """Fill blank IDs where the product name matches exactly. Existing IDs stay unless replace is set."""
    catalog = production_catalog()
    by_name = {}
    for key, record in catalog.items():
        for name in _names_for(record):
            by_name.setdefault(name, []).append(key)
    matched = []
    unmatched = []
    kept = []
    updates = {}
    for row in rows or []:
        name = str(row.get('product_name') or '').strip()
        product_id = str(row.get('product_id') or '').strip()
        if not name or not product_id:
            continue
        keys = by_name.get(name) or []
        if not keys:
            unmatched.append(name)
            continue
        for key in keys:
            current = str(catalog[key].get('product_id') or '').strip()
            if current and not replace:
                kept.append({'name': name, 'product_id': current})
                continue
            updates[key] = {
                'product_id': product_id,
                'product_name': catalog[key].get('product_name') or name,
            }
            matched.append({'name': name, 'product_id': product_id, 'replaced': bool(current)})
    if updates:
        save_production_overrides(updates)
    return {'matched': matched, 'unmatched': unmatched, 'kept': kept}


def lookup_product(spec, fallback_name=''):
    """ID and name for one line at export time. A missing ID stays blank."""
    catalog = production_catalog()
    makeup = makeup_from_spec(spec)
    record = catalog.get(makeup.casefold()) if makeup else None
    name = ''
    product_id = ''
    if record:
        product_id = str(record.get('product_id') or '').strip()
        name = str(record.get('product_name') or '').strip()
    if not name:
        name = str(fallback_name or '').strip()
    return {'product_id': product_id, 'product_name': name, 'known': record is not None}


def floor_code(item):
    for key in ('floor_code', 'floor', 'location', 'mark'):
        text = str((item or {}).get(key) or '').strip()
        if text:
            return text
    return ''


def export_lines(items):
    """One production row per glass line. Surcharge and drawing charges are not units."""
    rows = []
    serial = 1
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('kind') in {SURCHARGE_KIND, DWG_KIND}:
            continue
        spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
        if spec.get('width') in (None, '') or spec.get('height') in (None, ''):
            continue
        found = lookup_product(spec, item.get('name') or '')
        width_mm, height_mm, width_in, height_in = dimensions_mm_in(
            spec.get('width'), spec.get('height'), spec.get('unit'),
        )
        try:
            unit_price = float(item.get('price_sqf'))
        except (TypeError, ValueError):
            unit_price = None
        try:
            quantity = int(item.get('pcs') or 0)
        except (TypeError, ValueError):
            quantity = 0
        rows.append({
            'Serial Number': serial,
            'Floor Code': floor_code(item),
            'Product ID': found['product_id'],
            'Product Name': found['product_name'],
            'Unit Price': unit_price,
            'Width(mm)': width_mm,
            'Height(mm)': height_mm,
            'Width(In)': width_in,
            'Height(In)': height_in,
            'Quantity': quantity,
            'Remarks': str(item.get('remarks') or '').strip(),
        })
        serial += 1
    return rows


def workbook_bytes(items):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Production'
    sheet.append(list(PRODUCTION_HEADERS))
    for row in export_lines(items):
        values = []
        for header in PRODUCTION_HEADERS:
            value = row.get(header)
            if header == 'Product ID' and value:
                values.append(int(value) if str(value).isdigit() else value)
            else:
                values.append(value if value is not None else '')
        sheet.append(values)
    from io import BytesIO
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def present_order_line(item):
    """Copy a line and attach the registry ID. A blank ID is a note, not an error."""
    row = dict(item or {})
    if row.get('kind') in {SURCHARGE_KIND, DWG_KIND}:
        return row
    spec = row.get('spec') if isinstance(row.get('spec'), dict) else {}
    if spec.get('width') in (None, '') or spec.get('height') in (None, ''):
        return row
    found = lookup_product(spec, row.get('name') or '')
    row['production_id'] = found['product_id']
    row['production_name'] = found['product_name']
    row['production_note'] = '' if found['product_id'] else 'No production ID yet'
    return row


def write_order_workbook(order):
    """Save the file generated when an order is confirmed. A blank product ID does not stop this."""
    folder = os.path.join(data_dir(), 'production_exports')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{order.get('id')}.xlsx")
    payload = workbook_bytes(order.get('items') or [])
    with open(path, 'wb') as handle:
        handle.write(payload)
    return path

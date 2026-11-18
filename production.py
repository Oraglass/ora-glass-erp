"""Production-software product IDs and the order Excel export.

Product IDs come from the MAC_IGU sheet, the last tab of pricing_base.xlsx.
A blank ID is normal: the production software assigns one after import.
"""

import copy
import math
import os
import re

from openpyxl import Workbook, load_workbook

from copy_notes import strip_copy_history
from quoting import DWG_KIND, SURCHARGE_KIND
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


# Leading overall size from the quote calculator: 42 (1 5/8"), 22 (15/16"), 24 (2").
_INCH_PREFIX = re.compile(
    r'''^\s*(\d+)\s*\(\s*
        (?:
            \d+\s+\d+\s*/\s*\d+
            |
            \d+\s*/\s*\d+
            |
            \d+
        )
        \s*(?:"|″|”)\s*\)\s*-\s*''',
    re.VERBOSE,
)
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]+')


def strip_inch_overall(name):
    """Drop the inch overall thickness. 42 (1 5/8") - makeup becomes 42 - makeup."""
    text = _collapse(str(name or '').replace('\r', '\n').replace('\n', ' '))
    return _INCH_PREFIX.sub(r'\1 - ', text, count=1)


# Piece size appended by the quote page: " - 615 x 2230 mm" or " - 20 x 30 in".
_SIZE_SUFFIX = re.compile(
    r'\s+-\s+\d+(?:\.\d+)?\s+x\s+\d+(?:\.\d+)?\s*(?:mm|in)\b\s*$',
    re.IGNORECASE,
)


def strip_size_suffix(name):
    """Drop a trailing piece size. The build does not include the width and height."""
    text = _collapse(str(name or '').replace('\r', '\n').replace('\n', ' '))
    return _SIZE_SUFFIX.sub('', text).strip()


def makeup_from_product_name(product_name):
    """Drop the overall thickness, the inch part, and any piece size from a product name."""
    text = strip_size_suffix(product_name)
    text = strip_inch_overall(text)
    text = text.replace(' / ', '+')
    text = re.sub(r'\s*\+\s*', '+', text)
    text = re.sub(r'^\d+\s*-\s*', '', text)
    return _collapse(text)


def _overall_mm(makeup):
    total = 0.0
    for part in str(makeup or '').split('+'):
        match = re.match(r'\s*(\d+(?:\.\d+)?)mm', part, re.IGNORECASE)
        if match:
            total += float(match.group(1))
    if total <= 0:
        return ''
    if float(total).is_integer():
        return str(int(total))
    return str(total)


def canonical_build(name):
    """Registry identity for a line description: the build, with no inch part and no piece size.

    Returns (makeup key, production name). The production name is `24 - makeup`.
    """
    text = strip_size_suffix(name)
    text = strip_inch_overall(text)
    makeup = makeup_from_product_name(text)
    if not makeup:
        return '', ''
    match = re.match(r'^(\d+(?:\.\d+)?)\s*-\s+', text)
    if match:
        mm = match.group(1)
        if mm.endswith('.0'):
            mm = mm[:-2]
    else:
        mm = _overall_mm(makeup)
    normalized = f'{mm} - {makeup}' if mm else makeup
    return makeup.casefold(), normalized


def production_name_for_makeup(makeup, seeded=None):
    """Production-software name. A workbook row keeps that sheet's text."""
    key = _collapse(makeup).casefold()
    if not key:
        return ''
    if seeded is None:
        seeded = seed_products()[0]
    seed = seeded.get(key) or {}
    sheet_name = str(seed.get('product_name') or '').strip()
    if sheet_name:
        return sheet_name
    mm = _overall_mm(makeup)
    return f'{mm} - {makeup}' if mm else makeup


def _filename_part(value):
    text = _ILLEGAL_FILENAME.sub('-', str(value or ''))
    text = _collapse(text).strip(' .-')
    return text[:80]


def production_filename(order):
    """Customer.Project.PO.xlsx. An empty PO uses the ERP order number."""
    order = order or {}
    company = _filename_part(order.get('company') or 'Customer')
    project = _filename_part(order.get('project') or 'Order')
    po = _filename_part(order.get('po') or order.get('customer_po') or '')
    if not po:
        po = _filename_part(order.get('id'))
    parts = [part for part in (company, project, po) if part]
    stem = '.'.join(parts) or f"order-{order.get('id') or 'export'}"
    return stem + '.xlsx'


def content_disposition(filename):
    """Attachment header with an ASCII filename and an RFC 5987 filename*."""
    from urllib.parse import quote
    ascii_name = filename.encode('ascii', 'ignore').decode('ascii')
    ascii_name = _ILLEGAL_FILENAME.sub('-', ascii_name).replace('"', '').strip() or 'production.xlsx'
    encoded = quote(filename, safe='')
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _pricing_names(path=None):
    """ERP glass and spacer names, including aliases and priced Technoform widths."""
    import pricing_catalog
    rows = pricing_catalog.pricing_rows()
    glass, spacers = [], []
    for item in rows:
        for name in [item.get('name') or ''] + list(item.get('aliases') or []):
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
        if saved.get('id_conflict'):
            current['id_conflict'] = True
            current['conflict_ids'] = [str(item) for item in (saved.get('conflict_ids') or []) if str(item).strip()]
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
            'id_conflict': bool(record.get('id_conflict')),
            'conflict_ids': list(record.get('conflict_ids') or []),
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
            wanted = production_name_for_makeup(makeup, seeded)
            if key in seeded:
                if row.get('product_name') and row.get('product_name') != wanted:
                    row.pop('product_name', None)
                    changed = True
            elif row.get('product_name') != wanted:
                row['product_name'] = wanted[:300]
                changed = True
            if row.get('label') != makeup:
                row['label'] = makeup
                changed = True
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


def _upload_plan(rows, replace=False):
    """Which upload rows would set an ID. Nothing is written."""
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
    return {'matched': matched, 'unmatched': unmatched, 'kept': kept, 'updates': updates}


def apply_product_upload(rows, replace=False):
    """Fill blank IDs where the product name matches exactly. Existing IDs stay unless replace is set."""
    plan = _upload_plan(rows, replace=replace)
    if plan['updates']:
        save_production_overrides(plan['updates'])
    return {'matched': plan['matched'], 'unmatched': plan['unmatched'], 'kept': plan['kept']}


def id_change_plan(updates):
    """IDs that would change, and any Product ID shared by two different names."""
    catalog = production_catalog()
    changes = []
    owners = {}
    for key, record in catalog.items():
        product_id = str(record.get('product_id') or '').strip()
        name = str(record.get('product_name') or record.get('label') or key).strip()
        if product_id:
            owners.setdefault(product_id, {})[key] = name
    for key, fields in (updates or {}).items():
        if not key or not isinstance(fields, dict):
            continue
        existing = catalog.get(key) or {}
        old = str(existing.get('product_id') or '').strip()
        new = str(fields.get('product_id') or '').strip()[:40]
        name = str(fields.get('product_name') or existing.get('product_name') or existing.get('label') or key).strip()
        for names in owners.values():
            names.pop(key, None)
        if new:
            owners.setdefault(new, {})[key] = name
        if old == new:
            continue
        changes.append({
            'key': key,
            'product_name': name,
            'old_id': old,
            'new_id': new,
            'order_count': len(existing.get('order_ids') or []),
            'replaces_existing': bool(old) or bool(existing.get('order_ids')),
        })
    duplicates = []
    for product_id, names in owners.items():
        distinct = sorted({name for name in names.values() if name})
        if len(names) > 1 and len(distinct) > 1:
            duplicates.append({'product_id': product_id, 'names': distinct})
    duplicates.sort(key=lambda row: row['product_id'])
    return {'changes': changes, 'duplicates': duplicates}


def _audit_path():
    return os.path.join(data_dir(), 'production_id_audit.json')


def load_id_audit():
    rows = read_json(_audit_path(), [])
    return rows if isinstance(rows, list) else []


def append_id_audit(entries):
    """Append-only record of Product ID changes. Nothing here is edited or deleted."""
    if not entries:
        return load_id_audit()
    path = _audit_path()
    with lock_for(path):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        rows.extend(entries)
        atomic_write_json(path, rows)
    return rows


def _pending_path():
    return os.path.join(data_dir(), 'production_id_pending.json')


def save_pending_upload(token, rows, replace):
    path = _pending_path()
    with lock_for(path):
        pending = read_json(path, {})
        if not isinstance(pending, dict):
            pending = {}
        pending[token] = {'rows': rows, 'replace': bool(replace)}
        atomic_write_json(path, pending)
    return token


def load_pending_upload(token):
    pending = read_json(_pending_path(), {})
    if not isinstance(pending, dict):
        return None
    found = pending.get(token)
    return found if isinstance(found, dict) else None


def drop_pending_upload(token):
    path = _pending_path()
    with lock_for(path):
        pending = read_json(path, {})
        if isinstance(pending, dict) and token in pending:
            pending.pop(token, None)
            atomic_write_json(path, pending)


def _registry_hit(record):
    """A registry row counts only when it has a production Product ID and that ID is not in conflict."""
    if not record or record.get('id_conflict'):
        return None
    product_id = str(record.get('product_id') or '').strip()
    product_name = str(record.get('product_name') or '').strip()
    if not product_id or not product_name:
        return None
    return {'product_id': product_id, 'product_name': product_name}


def registry_for_makeup(makeup):
    """Exact registry row for one IGU makeup. The name is the sheet text, unchanged."""
    key = _collapse(makeup).casefold()
    if not key:
        return None
    return _registry_hit(production_catalog().get(key))


def registry_for_name(name):
    """Match a quote or PDF description to the registry after the inch prefix is removed."""
    return registry_for_makeup(makeup_from_product_name(name))


def lookup_product(spec, fallback_name=''):
    """ID and name for one line at export time. A missing ID stays blank."""
    catalog = production_catalog()
    makeup = makeup_from_spec(spec)
    record = catalog.get(makeup.casefold()) if makeup else None
    name = ''
    product_id = ''
    if record:
        name = str(record.get('product_name') or '').strip()
        if not record.get('id_conflict'):
            product_id = str(record.get('product_id') or '').strip()
    if not name:
        name = production_name_for_makeup(makeup) if makeup else ''
    if not name:
        _key, name = canonical_build(fallback_name)
    return {'product_id': product_id, 'product_name': name, 'known': record is not None}


def excel_identity(item):
    """Production Excel name and ID. A match uses the registry text exactly."""
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
    found = lookup_product(spec, item.get('name') or '')
    if makeup_from_spec(spec) and found.get('product_name'):
        return {
            'product_id': found['product_id'],
            'product_name': found['product_name'],
            'matched': bool(found['product_id']),
        }
    hit = registry_for_name(item.get('name') or '')
    if hit:
        return {
            'product_id': hit['product_id'],
            'product_name': hit['product_name'],
            'matched': True,
        }
    _key, fallback = canonical_build(item.get('name') or found.get('product_name') or '')
    if not fallback:
        fallback = strip_size_suffix(strip_inch_overall(item.get('name') or ''))
    return {'product_id': '', 'product_name': fallback, 'matched': False}


def floor_code(item):
    """The floor where the piece is installed. Older lines called this a mark or location."""
    for key in ('floor', 'mark', 'location', 'floor_code'):
        text = str((item or {}).get(key) or '').strip()
        if text:
            return text
    return ''


def line_export(item, quantity=None, serial=None, currency=None, fx_rate=None):
    """One production-Excel row. Unit Price is always USD per square foot."""
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
    identity = excel_identity(item)
    width_mm, height_mm, width_in, height_in = dimensions_mm_in(
        spec.get('width'), spec.get('height'), spec.get('unit'),
    )
    try:
        unit_price = float(item.get('price_sqf'))
    except (TypeError, ValueError):
        unit_price = None
    if unit_price is not None and str(currency or '').lower() == 'cad' and fx_rate:
        unit_price = round(unit_price / float(fx_rate), 4)
    if quantity is None:
        try:
            quantity = int(item.get('pcs') or 0)
        except (TypeError, ValueError):
            quantity = 0
    return {
        'Serial Number': serial,
        'Floor Code': floor_code(item),
        'Product ID': identity['product_id'],
        'Product Name': identity['product_name'],
        'Unit Price': unit_price,
        'Width(mm)': width_mm,
        'Height(mm)': height_mm,
        'Width(In)': width_in,
        'Height(In)': height_in,
        'Quantity': quantity,
        'Remarks': strip_copy_history(item.get('remarks')).strip(),
    }


def export_lines(items, currency=None, fx_rate=None):
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
        rows.append(line_export(item, serial=serial, currency=currency, fx_rate=fx_rate))
        serial += 1
    return rows


def workbook_bytes(items, currency=None, fx_rate=None):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Production'
    sheet.append(list(PRODUCTION_HEADERS))
    for row in export_lines(items, currency=currency, fx_rate=fx_rate):
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


def _usage(rows):
    order_ids = []
    seen = set()
    first = ''
    for row in rows:
        for order_id in row.get('order_ids') or []:
            if order_id in (None, '') or str(order_id) in seen:
                continue
            seen.add(str(order_id))
            order_ids.append(order_id)
        used = str(row.get('first_used') or '')[:10]
        if used and (not first or used < first):
            first = used
    return first, order_ids


def _product_ids(rows):
    found = []
    for row in rows:
        product_id = str(row.get('product_id') or '').strip()
        if product_id and product_id not in found:
            found.append(product_id)
    return found


def migrate_registry_names():
    """Rewrite stored registry rows to the production build name.

    A line description such as '24 (15/16") - makeup - 615 x 2230 mm' becomes
    '24 - makeup'. Rows that collapse to the same build are merged. One
    non-empty Product ID is kept. Two different IDs are both kept and flagged.
    Order ids move with the build, and orders whose lines use that build are
    counted on it. A second run changes nothing.
    """
    import logging
    from storage import load_orders

    logger = logging.getLogger('ora_glass.production')
    path = production_catalog_path()
    with lock_for(path):
        saved = _saved_overrides()
        if not saved:
            return {'renamed': [], 'merged': [], 'conflicts': []}
        seeded = seed_products()[0]
        groups = {}
        order = []
        for key, row in saved.items():
            if not isinstance(row, dict):
                continue
            source = row.get('product_name') or row.get('label') or key
            canonical, normalized = canonical_build(source)
            if not canonical:
                canonical, normalized = str(key), strip_size_suffix(strip_inch_overall(source))
            if canonical not in groups:
                order.append(canonical)
                groups[canonical] = []
            groups[canonical].append((key, row, normalized))
        new_saved = {}
        renamed = []
        merged = []
        conflicts = []
        for canonical in order:
            entries = groups[canonical]
            seed = seeded.get(canonical) or {}
            sheet_name = str(seed.get('product_name') or '').strip()
            normalized = sheet_name or entries[0][2]
            makeup = str(seed.get('label') or '') or makeup_from_product_name(normalized)
            ids = _product_ids(row for _key, row, _name in entries)
            first, order_ids = _usage(row for _key, row, _name in entries)
            before = [str(row.get('product_name') or key) for key, row, _name in entries]
            explicit_blank = any(
                key == canonical and 'product_id' in row and not str(row.get('product_id') or '').strip()
                for key, row, _name in entries
            )
            if len(ids) > 1:
                conflicts.append({
                    'product_name': normalized,
                    'ids': ids,
                    'from': before,
                    'order_ids': order_ids,
                })
                for product_id in ids:
                    owned = [row for _key, row, _name in entries if str(row.get('product_id') or '').strip() == product_id]
                    owned_first, owned_orders = _usage(owned or [row for _key, row, _name in entries])
                    storage_key = canonical if product_id == ids[0] else f'{canonical}\x1f{product_id}'
                    new_saved[storage_key] = {
                        'label': makeup,
                        'product_name': normalized,
                        'product_id': product_id,
                        'first_used': owned_first,
                        'order_ids': owned_orders,
                        'id_conflict': True,
                        'conflict_ids': ids,
                    }
                continue
            row = {'label': makeup, 'first_used': first, 'order_ids': order_ids}
            if ids:
                row['product_id'] = ids[0]
            elif explicit_blank:
                row['product_id'] = ''
            if not sheet_name:
                row['product_name'] = normalized
            elif ids and ids[0] != str(seed.get('product_id') or '').strip():
                row['product_name'] = normalized
            new_saved[canonical] = row
            changed_name = any(name != normalized for name in before)
            if len(entries) > 1:
                merged.append({
                    'product_name': normalized,
                    'product_id': ids[0] if ids else '',
                    'from': before,
                    'order_ids': order_ids,
                })
            elif changed_name or any(key != canonical for key, _row, _name in entries):
                renamed.append({
                    'before': before[0],
                    'after': normalized,
                    'product_id': ids[0] if ids else '',
                    'order_ids': order_ids,
                })
        conflict_keys = {key for key, row in new_saved.items() if row.get('id_conflict')}
        for order_row in load_orders():
            order_id = order_row.get('id')
            if order_id in (None, ''):
                continue
            for item in order_row.get('items') or []:
                if not isinstance(item, dict) or item.get('kind') in {SURCHARGE_KIND, DWG_KIND}:
                    continue
                spec = item.get('spec') if isinstance(item.get('spec'), dict) else None
                makeup = makeup_from_spec(spec) if spec else ''
                if not makeup:
                    makeup = makeup_from_product_name(item.get('name') or '')
                key = makeup.casefold()
                if not key or key not in new_saved or key in conflict_keys or new_saved[key].get('id_conflict'):
                    continue
                current = new_saved[key]
                linked = list(current.get('order_ids') or [])
                if order_id not in linked and str(order_id) not in {str(entry) for entry in linked}:
                    linked.append(order_id)
                    current['order_ids'] = linked
                    if not current.get('first_used'):
                        current['first_used'] = str(order_row.get('date') or '')[:10]
        if new_saved != saved:
            atomic_write_json(path, {'products': new_saved})
            logger.info(
                'Production ID names normalized: %s renamed, %s merged, %s ID conflicts.',
                len(renamed), len(merged), len(conflicts),
            )
            for row in renamed + merged:
                logger.info('Production build %s from %s', row.get('after') or row.get('product_name'), row.get('before') or row.get('from'))
            for row in conflicts:
                logger.warning('Production ID conflict for %s: %s', row['product_name'], ', '.join(row['ids']))
        return {'renamed': renamed, 'merged': merged, 'conflicts': conflicts}


def write_order_workbook(order):
    """Save the file generated when an order is confirmed. A blank product ID does not stop this."""
    import currency_rules
    rate, _effective = currency_rules.rate_on((order or {}).get('date'))
    folder = os.path.join(data_dir(), 'production_exports')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, production_filename(order))
    payload = workbook_bytes(order.get('items') or [], (order or {}).get('currency'), rate)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return path

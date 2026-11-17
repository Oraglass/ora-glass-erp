"""Editable pricing catalog seeded from pricing_base.xlsx.

The sell formula keeps using each row's stored base price. Seeding copies the
base the workbook loader already uses, so a new catalog prices the same as the
workbook. Editing a raw input recomputes that base for later calculations.
Saved quotes and orders are not rewritten.
"""

import os
import re
from datetime import datetime, timezone

from quoting import TEMPER_PRICES, classify, load_pricing, load_pricing_workbook
from storage import atomic_write_json, data_dir, lock_for, read_json

TABS = (
    {'id': 'markups', 'label': 'Tier markups'},
    {'id': 'currency', 'label': 'Currency'},
    {'id': 'glass', 'label': 'Glass'},
    {'id': 'components', 'label': 'Spacers and sealants'},
    {'id': 'formula', 'label': 'Formula / other costs'},
    {'id': 'prices', 'label': 'Price list'},
)
TAB_IDS = {item['id'] for item in TABS}

# These are the constants the calculator already used. Defaults reproduce today's prices.
FORMULA_DEFAULTS = (
    {
        'key': 'tier5_multiplier',
        'value': 2,
        'description': 'Tier 5 multiplies the Tier 2 sell price of each glass, spacer, and sealant. Temper add-ons are not multiplied. 2 keeps Tier 5 at twice Tier 2.',
    },
    {
        'key': 'unknown_temper_addon',
        'value': 0.65,
        'description': 'Tempering add-on per sq ft when the glass name does not end in a thickness from the temper table. Used on the Glass tab temper add-ons.',
    },
    {
        'key': 'triple_glaze_factor',
        'value': 1,
        'description': 'Multiplier on the third lite only, after its markup and tier scale. The workbook has no triple-glaze factor. 1 prices a third lite as a normal glass line.',
    },
    {
        'key': 'spacer_sample_sizes',
        'value': '2x5,2x6,3x5',
        'description': 'IGU sizes in feet. A spacer $/sqf before waste is the average of (perimeter feet × $ per foot) / area over these sizes. The spacer sheet uses 2×5, 2×6, and 3×5. Changing this recomputes spacer bases on the next save.',
    },
    {
        'key': 'technoform_foot_factor',
        'value': 0,
        'description': 'Spacer sheet cell B51. Technoform $ per foot is the listed $ per foot times this factor. The cell is empty in the workbook, so the factor is 0 and those bases stay $0 until it is changed.',
    },
    {
        'key': 'sealant_igu_width_mm',
        'value': 800,
        'description': 'Sample IGU width on the sealant sheet. Used with the length, spacer width, and seal depth to turn a price per set into $ per sq ft when a sealant row is saved.',
    },
    {
        'key': 'sealant_igu_length_mm',
        'value': 1800,
        'description': 'Sample IGU length on the sealant sheet, in millimetres.',
    },
    {
        'key': 'sealant_depth_mm',
        'value': 6,
        'description': 'Seal depth on the sealant sheet, in millimetres. The sheet calls this the 6mm sealing depth.',
    },
    {
        'key': 'sealant_liters_per_set',
        'value': 210.36,
        'description': 'Litres in one set of sealant (polysulfide column). Silicone uses the same set volume and its own price per set.',
    },
    {
        'key': 'sealant_sqm_to_sqf',
        'value': 10.76,
        'description': 'The sealant sheet multiplies square metres per set by this to get square feet per set.',
    },
    {
        'key': 'gas_fill_per_sqf',
        'value': 0,
        'description': 'Added once per sq ft, not marked up. Argon is only in production product names. The workbook has no gas-fill price. 0 leaves the sell price unchanged.',
    },
    {
        'key': 'labour_per_sqf',
        'value': 0,
        'description': 'Labour added once per sq ft, not marked up. The workbook has no labour rate. 0 leaves the sell price unchanged.',
    },
    {
        'key': 'overhead_per_sqf',
        'value': 0,
        'description': 'Overhead added once per sq ft, not marked up. The workbook has no overhead rate. 0 leaves the sell price unchanged.',
    },
    {
        'key': 'minimum_sqf',
        'value': 0,
        'description': 'A line smaller than this many square feet is billed as this many square feet. 0 means there is no minimum size. The workbook has no minimum size.',
    },
    {
        'key': 'minimum_charge',
        'value': 0,
        'description': 'Minimum line total in the quote currency, after the per-sqf price. 0 means there is no minimum charge. The workbook has no minimum charge.',
    },
)

EXCEL_TEMPER = {'4mm': 0.50, '5mm': 0.60, '6mm': 0.65, '8mm': 0.95, '10mm': 1.00, '12mm': 1.20}

GAPS = (
    'Workbook temper prices are 4mm 0.50, 5mm 0.60, 6mm 0.65, 8mm 0.95, 10mm 1.00, 12mm 1.20. The calculator uses the code table (4mm 0.50, 5mm 0.55, 6mm 0.65, 8mm 0.75, 10mm 0.85, 12mm 0.95). Switching would change prices, so the Glass tab edits the code table. The workbook numbers are shown beside it and are not used.',
    'Desicant YES is $0.10 per sq ft in the workbook. The sell formula does not add it. It is on Spacers and sealants with In price set to no.',
    'Butyl / primary sealant and corner keys are not in the workbook. Placeholder rows start at $0 and In price no.',
    'Technoform $ per foot is listed, but spacer cell B51 is empty, so the $ per sq ft is $0. Those rows stay out of the price list, which is how the workbook loader already worked.',
    'The second SKN176 and SKN176II rows share a name with the row above them. The loader kept the first row. The copies are on the Glass tab with In price no.',
    'Quote sheet markups (glass 30/40/60 and spacer 20/40/60) are that sample quote, not a second price list. Live markups are the Tier markups tab. Tier 4 at 75% and Tier 5 as a multiple of Tier 2 are ERP rules.',
    'Quote cell B1 (USD/CAD 1.40) is not a pricing constant. CAD uses the ERP exchange rate on the Currency tab.',
    'The MAC_IGU tab is the production ID list, not a price input.',
    'There is no gas-fill, labour, overhead, minimum size, or minimum charge input in the workbook. Those settings start at 0.',
    'There is no triple-glaze multiplier. A third lite is another glass line. The factor starts at 1.',
    'The Quote sheet thickness parser is not a price input.',
)


def catalog_path():
    return os.path.join(data_dir(), 'pricing_catalog.json')


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blank():
    return {'materials': [], 'temper': [], 'formula': [], 'history': [], 'source': ''}


def load_catalog():
    path = catalog_path()
    raw = read_json(path, None)
    if not isinstance(raw, dict) or not raw.get('materials'):
        seeded = seed_catalog()
        _write(seeded)
        return seeded
    return _normalize(raw)


def _write(catalog):
    path = catalog_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock_for(path):
        atomic_write_json(path, catalog)


def save_catalog(catalog, user, changes):
    """Persist and append history rows. `changes` is a list of dicts without at/user."""
    catalog = _normalize(catalog)
    for change in changes or []:
        catalog['history'].append({
            'at': _now(),
            'user': user or '',
            'tab': str(change.get('tab') or ''),
            'item': str(change.get('item') or ''),
            'field': str(change.get('field') or ''),
            'old': _text(change.get('old')),
            'new': _text(change.get('new')),
        })
    catalog['history'] = catalog['history'][-500:]
    _write(catalog)
    return catalog


def _text(value):
    if value is None:
        return ''
    if isinstance(value, float):
        return _num_text(value)
    return str(value)


def _num_text(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ''
    if number == int(number) and abs(number) < 1e12:
        return str(int(number))
    return f'{number:.10f}'.rstrip('0').rstrip('.')


def _num(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _yes(value):
    return str(value or '').strip().lower() in {'yes', 'y', 'true', '1'}


def _normalize(raw):
    catalog = _blank()
    catalog['source'] = str(raw.get('source') or '')
    materials = []
    for row in raw.get('materials') or []:
        if not isinstance(row, dict) or not row.get('id') or not row.get('name'):
            continue
        materials.append({
            'id': str(row.get('id')),
            'name': str(row.get('name')).strip(),
            'kind': str(row.get('kind') or 'glass'),
            'thickness': str(row.get('thickness') or ''),
            'sheet_width': _num(row.get('sheet_width')),
            'sheet_height': _num(row.get('sheet_height')),
            'raw_price': _num(row.get('raw_price')),
            'tariff_percent': _num(row.get('tariff_percent')),
            'waste_percent': _num(row.get('waste_percent')),
            'base_price': _num(row.get('base_price')),
            'price_per_ft': _num(row.get('price_per_ft')),
            'foot_factor': _num(row.get('foot_factor'), 1),
            'price_per_set': _num(row.get('price_per_set')),
            'spacer_width_mm': _num(row.get('spacer_width_mm')),
            'temperable': bool(row.get('temperable')),
            'annealed': bool(row.get('annealed')),
            'active': bool(row.get('active', True)),
            'in_price': bool(row.get('in_price')),
            'note': str(row.get('note') or ''),
        })
    catalog['materials'] = materials
    temper = []
    seen = set()
    for row in raw.get('temper') or []:
        if not isinstance(row, dict):
            continue
        thickness = str(row.get('thickness') or '').strip()
        if not thickness or thickness in seen:
            continue
        seen.add(thickness)
        temper.append({
            'thickness': thickness,
            'addon': _num(row.get('addon')),
            'workbook': _num(row.get('workbook')),
        })
    if not temper:
        temper = [
            {'thickness': key, 'addon': value, 'workbook': EXCEL_TEMPER.get(key, 0)}
            for key, value in TEMPER_PRICES.items()
        ]
    catalog['temper'] = temper
    stored = {row.get('key'): row for row in (raw.get('formula') or []) if isinstance(row, dict)}
    formula = []
    for item in FORMULA_DEFAULTS:
        current = stored.get(item['key']) or {}
        value = current.get('value', item['value'])
        formula.append({'key': item['key'], 'value': value, 'description': item['description']})
    catalog['formula'] = formula
    history = []
    for row in raw.get('history') or []:
        if isinstance(row, dict):
            history.append({
                'at': str(row.get('at') or ''),
                'user': str(row.get('user') or ''),
                'tab': str(row.get('tab') or ''),
                'item': str(row.get('item') or ''),
                'field': str(row.get('field') or ''),
                'old': str(row.get('old') or ''),
                'new': str(row.get('new') or ''),
            })
    catalog['history'] = history[-500:]
    return catalog


def formula_map(catalog=None):
    catalog = catalog or load_catalog()
    return {row['key']: row['value'] for row in catalog['formula']}


def formula_number(key, fallback=0.0, catalog=None):
    return _num(formula_map(catalog).get(key), fallback)


def history_for(tab, catalog=None, limit=40):
    catalog = catalog or load_catalog()
    rows = [row for row in catalog['history'] if row.get('tab') == tab]
    return list(reversed(rows[-limit:]))


def temper_prices(catalog=None):
    catalog = catalog or load_catalog()
    prices = dict(TEMPER_PRICES)
    for row in catalog['temper']:
        prices[row['thickness']] = _num(row['addon'])
    return prices


def pricing_rows(catalog=None):
    """Rows the sell formula looks up by name. Same bases as the workbook loader after seeding."""
    catalog = catalog or load_catalog()
    rows = []
    seen = set()
    for item in catalog['materials']:
        if item['kind'] not in {'glass', 'spacer', 'sealant'}:
            continue
        if not item['active'] or not item['in_price']:
            continue
        if item['name'] in seen:
            continue
        seen.add(item['name'])
        width = item['sheet_width']
        height = item['sheet_height']
        rows.append({
            'name': item['name'],
            'thickness': item['thickness'],
            'sheet_size': f'{_num_text(width)}"x{_num_text(height)}"' if width and height else '',
            'base_price': item['base_price'],
            'temperable': item['temperable'],
            'annealed': item['annealed'],
        })
    return rows


def material_lists(catalog=None):
    return classify(pricing_rows(catalog))


def extra_per_sqf(catalog=None):
    catalog = catalog or load_catalog()
    total = 0.0
    for item in catalog['materials']:
        if item['kind'] in {'desiccant', 'butyl', 'corner'} and item['active'] and item['in_price']:
            total += item['base_price']
    for key in ('gas_fill_per_sqf', 'labour_per_sqf', 'overhead_per_sqf'):
        total += formula_number(key, 0, catalog)
    return total


def calc_options(catalog=None):
    catalog = catalog or load_catalog()
    return {
        'pricing': pricing_rows(catalog),
        'temper_prices': temper_prices(catalog),
        'tier5_scale': formula_number('tier5_multiplier', 2, catalog),
        'unknown_temper': formula_number('unknown_temper_addon', 0.65, catalog),
        'extra_per_sqf': extra_per_sqf(catalog),
        'triple_factor': formula_number('triple_glaze_factor', 1, catalog),
        'minimum_sqf': formula_number('minimum_sqf', 0, catalog),
        'minimum_charge': formula_number('minimum_charge', 0, catalog),
    }


def known_names(catalog=None):
    catalog = catalog or load_catalog()
    return {item['name'] for item in catalog['materials'] if item['active'] and item['in_price']}


def _slug(name, index):
    token = re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-') or 'row'
    return f'{token}-{index}'


def _apply_tariff_waste(raw, tariff, waste):
    gross = float(raw) * (1 + float(tariff) / 100.0)
    return gross * (1 + float(waste) / 100.0)


def _sample_pairs(catalog):
    text = str(formula_map(catalog).get('spacer_sample_sizes') or '2x5,2x6,3x5')
    pairs = []
    for part in text.split(','):
        piece = part.strip().lower().replace('×', 'x')
        if 'x' not in piece:
            continue
        left, right = piece.split('x', 1)
        width = _num(left, 0)
        height = _num(right, 0)
        if width > 0 and height > 0:
            pairs.append((width, height))
    return pairs or [(2, 5), (2, 6), (3, 5)]


def spacer_raw_sqf(price_per_ft, foot_factor, catalog):
    unit = float(price_per_ft) * float(foot_factor)
    costs = []
    for width, height in _sample_pairs(catalog):
        area = width * height
        perimeter = 2 * width + 2 * height
        costs.append(unit * (perimeter / area))
    return sum(costs) / len(costs)


def sealant_raw_sqf(price_per_set, spacer_width_mm, catalog):
    width = formula_number('sealant_igu_width_mm', 800, catalog) / 1000.0
    length = formula_number('sealant_igu_length_mm', 1800, catalog) / 1000.0
    depth = formula_number('sealant_depth_mm', 6, catalog) / 1000.0
    spacer = float(spacer_width_mm) / 1000.0
    liters = formula_number('sealant_liters_per_set', 210.36, catalog) / 1000.0
    factor = formula_number('sealant_sqm_to_sqf', 10.76, catalog)
    volume = (2 * width + 2 * length) * spacer * depth
    if volume <= 0 or factor <= 0:
        return 0.0
    units = liters / volume
    sqf = units * width * length * factor
    if sqf <= 0:
        return 0.0
    return float(price_per_set) / sqf


def glass_base(raw, tariff, waste):
    return _apply_tariff_waste(raw, tariff, waste)


def _changed(old, new):
    if isinstance(old, float) or isinstance(new, float):
        return abs(_num(old) - _num(new)) > 1e-9
    return old != new


def seed_catalog():
    pricing, source = load_pricing()
    priced = {item['name']: item['base_price'] for item in pricing}
    materials = _materials_from_workbook(source, priced) if source else []
    if not materials:
        materials = [_fallback_material(item, index) for index, item in enumerate(pricing, start=1)]
    materials.extend(_placeholder_components(materials))
    catalog = _normalize({
        'source': source or 'built-in fallback',
        'materials': materials,
        'temper': [
            {'thickness': key, 'addon': value, 'workbook': EXCEL_TEMPER.get(key, 0)}
            for key, value in TEMPER_PRICES.items()
        ],
        'formula': [{'key': item['key'], 'value': item['value']} for item in FORMULA_DEFAULTS],
        'history': [{
            'at': _now(),
            'user': 'system',
            'tab': 'formula',
            'item': 'Pricing catalog',
            'field': 'seed',
            'old': '',
            'new': source or 'built-in fallback',
        }],
    })
    return catalog


def _placeholder_components(materials):
    kinds = {item['kind'] for item in materials}
    extras = []
    if 'butyl' not in kinds:
        extras.append(_empty_component('butyl-primary', 'Butyl / primary sealant', 'butyl'))
    if 'corner' not in kinds:
        extras.append(_empty_component('corner-keys', 'Corner keys', 'corner'))
    return extras


def _empty_component(row_id, name, kind):
    return {
        'id': row_id,
        'name': name,
        'kind': kind,
        'raw_price': 0,
        'base_price': 0,
        'active': True,
        'in_price': False,
        'note': 'Not in the pricing workbook. In price is off, so the sell price does not include it.',
    }


def _fallback_material(item, index):
    name = item['name']
    lowered = name.lower()
    if 'spacer' in lowered or 'technoform' in lowered:
        kind = 'spacer'
    elif 'sealant' in lowered:
        kind = 'sealant'
    else:
        kind = 'glass'
    return {
        'id': _slug(name, index),
        'name': name,
        'kind': kind,
        'thickness': '',
        'base_price': item.get('base_price') or 0,
        'raw_price': item.get('base_price') or 0,
        'temperable': bool(item.get('temperable')),
        'annealed': bool(item.get('annealed')),
        'active': True,
        'in_price': True,
        'note': 'Seeded from the built-in list because the workbook was not opened.',
    }


def _materials_from_workbook(path, priced):
    import warnings
    import openpyxl

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Data Validation extension is not supported')
        values = openpyxl.load_workbook(path, data_only=True)
        formulas = openpyxl.load_workbook(path, data_only=False)
    sheet = values['pricelist']
    formula_sheet = formulas['pricelist']
    spacer_values = values['spacer']
    sealant_values = values['sealant']
    materials = []
    used_priced = set()
    index = 0
    for offset, row in enumerate(sheet.iter_rows(min_row=2, values_only=True)):
        excel_row = offset + 2
        name = str(row[0]).strip() if row and row[0] else ''
        if not name or name == 'n/a':
            continue
        lowered = name.lower()
        formula_e = formula_sheet.cell(excel_row, 5).value
        if lowered.startswith('tempering'):
            continue
        index += 1
        if 'desic' in lowered:
            materials.append({
                'id': _slug(name, index),
                'name': name,
                'kind': 'desiccant',
                'raw_price': _num(row[4]),
                'tariff_percent': _num(row[5]),
                'waste_percent': _num(row[7]),
                'base_price': _num(row[8]) if row[8] not in (None, '') else _num(row[4]),
                'active': True,
                'in_price': False,
                'note': 'Workbook component. The sell formula does not add it while In price is no.',
            })
            continue
        kind = 'glass'
        if 'spacer' in lowered or 'technoform' in lowered:
            kind = 'spacer'
        elif 'sealant' in lowered:
            kind = 'sealant'
        names = [name]
        if '180/270' in name:
            names = [name.replace('180/270', '180'), name.replace('180/270', '270')]
        for split_name in names:
            index += 1 if split_name != names[0] else 0
            in_price = split_name in priced and split_name not in used_priced
            if in_price:
                used_priced.add(split_name)
                base = priced[split_name]
            else:
                base = _num(row[8]) if row[8] not in (None, '') else 0
            item = {
                'id': _slug(split_name, index),
                'name': split_name,
                'kind': kind,
                'thickness': str(row[1] or ''),
                'sheet_width': _num(row[2]),
                'sheet_height': _num(row[3]),
                'raw_price': _num(row[4]) if not isinstance(row[4], str) else 0,
                'tariff_percent': _num(row[5]),
                'waste_percent': _num(row[7]),
                'base_price': base,
                'temperable': _yes(row[9]) if row[9] not in (None, '') else True,
                'annealed': _yes(row[10]) if row[10] not in (None, '') else True,
                'active': True,
                'in_price': in_price,
                'note': '' if in_price else 'Not in the current sell price. The workbook loader skipped this row.',
            }
            if kind == 'spacer':
                _attach_spacer(item, formula_e, spacer_values)
            elif kind == 'sealant':
                _attach_sealant(item, formula_e, sealant_values)
            elif kind == 'glass' and isinstance(row[4], (int, float)):
                item['raw_price'] = _num(row[4])
            materials.append(item)
    return materials


_CELL = re.compile(r'=spacer!([A-Z]+)(\d+)', re.IGNORECASE)
_SEALANT_CELL = re.compile(r'=sealant!([A-Z]+)(\d+)', re.IGNORECASE)


def _attach_spacer(item, formula, spacer_sheet):
    match = _CELL.search(str(formula or ''))
    if not match:
        item['foot_factor'] = 1
        return
    row_number = int(match.group(2))
    price = _num(spacer_sheet.cell(row_number, 3).value)
    used = _num(spacer_sheet.cell(row_number, 4).value)
    item['price_per_ft'] = price
    item['foot_factor'] = 0 if price and used == 0 else (1 if not price else (used / price if price else 1))
    item['raw_price'] = _num(spacer_sheet.cell(row_number, 11).value)


def _attach_sealant(item, formula, sealant_sheet):
    match = _SEALANT_CELL.search(str(formula or ''))
    if not match:
        return
    row_number = int(match.group(2))
    column = match.group(1).upper()
    price_column = 10 if column == 'K' else 12
    item['price_per_set'] = _num(sealant_sheet.cell(row_number, price_column).value)
    item['spacer_width_mm'] = _num(sealant_sheet.cell(row_number - 1, 4).value)
    item['raw_price'] = _num(sealant_sheet.cell(row_number, 11 if column == 'K' else 13).value)


def update_materials(posted_rows, user, tab):
    """Apply posted rows. Recompute a base only when a price input changed."""
    catalog = load_catalog()
    by_id = {item['id']: item for item in catalog['materials']}
    changes = []
    for posted in posted_rows:
        current = by_id.get(posted.get('id'))
        if current is None:
            if not posted.get('name'):
                continue
            current = {
                'id': _slug(posted['name'], len(catalog['materials']) + 1),
                'name': posted['name'],
                'kind': posted.get('kind') or 'glass',
                'thickness': '',
                'sheet_width': 0,
                'sheet_height': 0,
                'raw_price': 0,
                'tariff_percent': 0,
                'waste_percent': 0,
                'base_price': 0,
                'price_per_ft': 0,
                'foot_factor': 1,
                'price_per_set': 0,
                'spacer_width_mm': 0,
                'temperable': True,
                'annealed': True,
                'active': True,
                'in_price': True,
                'note': 'Added in the ERP.',
            }
            catalog['materials'].append(current)
            by_id[current['id']] = current
            changes.append({'tab': tab, 'item': current['name'], 'field': 'added', 'old': '', 'new': current['name']})
        _apply_row(current, posted, catalog, changes, tab)
    return save_catalog(catalog, user, changes)


def _apply_row(current, posted, catalog, changes, tab):
    price_inputs = ('raw_price', 'tariff_percent', 'waste_percent', 'price_per_ft', 'foot_factor', 'price_per_set', 'spacer_width_mm')
    flags = ('temperable', 'annealed', 'active', 'in_price')
    text_fields = ('name', 'thickness', 'note')
    number_fields = ('sheet_width', 'sheet_height') + price_inputs
    before_price = {key: current.get(key) for key in price_inputs}
    for key in text_fields:
        if key not in posted:
            continue
        new = str(posted.get(key) or '').strip()
        if key == 'name' and not new:
            continue
        if _changed(current.get(key) or '', new):
            changes.append({'tab': tab, 'item': current['name'], 'field': key, 'old': current.get(key) or '', 'new': new})
            current[key] = new
    for key in number_fields:
        if key not in posted:
            continue
        new = _num(posted.get(key))
        if _changed(current.get(key) or 0, new):
            changes.append({'tab': tab, 'item': current['name'], 'field': key, 'old': current.get(key) or 0, 'new': new})
            current[key] = new
    for key in flags:
        if key not in posted:
            continue
        new = bool(posted.get(key))
        if current.get(key) != new:
            changes.append({'tab': tab, 'item': current['name'], 'field': key, 'old': current.get(key), 'new': new})
            current[key] = new
    if any(_changed(before_price.get(key) or 0, current.get(key) or 0) for key in price_inputs):
        _recompute_base(current, catalog, changes, tab)


def _recompute_base(current, catalog, changes, tab):
    kind = current.get('kind')
    if kind == 'glass' or kind in {'desiccant', 'butyl', 'corner'}:
        new_base = glass_base(current.get('raw_price') or 0, current.get('tariff_percent') or 0, current.get('waste_percent') or 0)
    elif kind == 'spacer':
        factor = current.get('foot_factor')
        if 'technoform' in current['name'].lower():
            factor = formula_number('technoform_foot_factor', 0, catalog)
            current['foot_factor'] = factor
        raw = spacer_raw_sqf(current.get('price_per_ft') or 0, factor, catalog)
        current['raw_price'] = raw
        new_base = _apply_tariff_waste(raw, current.get('tariff_percent') or 0, current.get('waste_percent') or 0)
    elif kind == 'sealant':
        raw = sealant_raw_sqf(current.get('price_per_set') or 0, current.get('spacer_width_mm') or 0, catalog)
        current['raw_price'] = raw
        new_base = _apply_tariff_waste(raw, current.get('tariff_percent') or 0, current.get('waste_percent') or 0)
    else:
        return
    if _changed(current.get('base_price') or 0, new_base):
        changes.append({'tab': tab, 'item': current['name'], 'field': 'base_price', 'old': current.get('base_price') or 0, 'new': new_base})
        current['base_price'] = new_base


def update_temper(rows, user):
    catalog = load_catalog()
    by_thickness = {item['thickness']: item for item in catalog['temper']}
    changes = []
    for posted in rows:
        thickness = str(posted.get('thickness') or '').strip()
        if not thickness:
            continue
        current = by_thickness.get(thickness)
        if current is None:
            current = {'thickness': thickness, 'addon': 0, 'workbook': 0}
            catalog['temper'].append(current)
            by_thickness[thickness] = current
        new = _num(posted.get('addon'))
        if _changed(current.get('addon') or 0, new):
            changes.append({'tab': 'glass', 'item': f'Temper {thickness}', 'field': 'addon', 'old': current.get('addon') or 0, 'new': new})
            current['addon'] = new
    return save_catalog(catalog, user, changes)


def update_formula(posted, user):
    catalog = load_catalog()
    changes = []
    current = formula_map(catalog)
    for row in catalog['formula']:
        if row['key'] not in posted:
            continue
        old = current.get(row['key'])
        new_raw = posted.get(row['key'])
        if row['key'] == 'spacer_sample_sizes':
            new = str(new_raw or '').strip()
            if new != str(old or ''):
                changes.append({'tab': 'formula', 'item': row['key'], 'field': 'value', 'old': old, 'new': new})
                row['value'] = new
            continue
        new = _num(new_raw)
        if _changed(old, new):
            changes.append({'tab': 'formula', 'item': row['key'], 'field': 'value', 'old': old, 'new': new})
            row['value'] = new
    if any(change['item'] in {'spacer_sample_sizes', 'technoform_foot_factor'} for change in changes):
        for item in catalog['materials']:
            if item['kind'] == 'spacer' and item.get('price_per_ft'):
                _recompute_base(item, catalog, changes, 'components')
    if any(change['item'].startswith('sealant_') for change in changes):
        for item in catalog['materials']:
            if item['kind'] == 'sealant' and item.get('price_per_set'):
                _recompute_base(item, catalog, changes, 'components')
    return save_catalog(catalog, user, changes)


def record_change(tab, item, field, old, new, user):
    if not _changed(old, new):
        return load_catalog()
    return save_catalog(load_catalog(), user, [{
        'tab': tab, 'item': item, 'field': field, 'old': old, 'new': new,
    }])


def price_list(tiers, rate):
    """Every priced glass, tempered lite, component, and same-glass double IGU."""
    from quoting import calculate_price_sqf, suggest_sealant

    catalog = load_catalog()
    options = calc_options(catalog)
    glass, spacers, sealants = classify(options['pricing'])
    rows = []

    def prices(spec, label, kind):
        values = {}
        for tier in ('T1', 'T2', 'T3', 'T4', 'T5'):
            usd = calculate_price_sqf(spec, tier, options['pricing'], tiers, temper_prices=options['temper_prices'],
                                      tier5_scale=options['tier5_scale'], unknown_temper=options['unknown_temper'],
                                      extra_per_sqf=options['extra_per_sqf'], triple_factor=options['triple_factor'])
            values[tier] = {'usd': usd, 'cad': round(usd * rate, 2)}
        rows.append({'label': label, 'kind': kind, 'prices': values})

    for item in glass:
        spec = {'glass1': item['name'], 'temper1': 'n/a'}
        prices(spec, item['name'], 'Glass')
        if item.get('temperable', True):
            prices({**spec, 'temper1': 'yes'}, f"{item['name']} tempered", 'Tempered glass')
    for item in spacers:
        prices({'spacer1': item['name']}, item['name'], 'Spacer')
    for item in sealants:
        prices({'sealant1': item['name']}, item['name'], 'Sealant')
    for lite in glass:
        for spacer in spacers:
            sealant = suggest_sealant(spacer['name'], sealants).get('selected') or ''
            spec = {
                'glass1': lite['name'], 'temper1': 'n/a',
                'spacer1': spacer['name'], 'sealant1': sealant,
                'glass2': lite['name'], 'temper2': 'n/a',
            }
            label = f"{lite['name']} / {spacer['name']} / {sealant or 'no sealant'} / {lite['name']}"
            prices(spec, label, 'Double IGU')
    return rows

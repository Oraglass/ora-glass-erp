"""IGU price calculation and the pricing workbook loader."""

import logging
import os
import warnings

from storage import resolve_pricing_path

logger = logging.getLogger('ora_glass.quoting')

TEMPER_PRICES = {'4mm': 0.50, '5mm': 0.55, '6mm': 0.65, '8mm': 0.75, '10mm': 0.85, '12mm': 0.95}

# Used only when pricing_base.xlsx cannot be read. Numbers mirror the historical fallback.
FALLBACK_PRICING = [
    {'name': 'Clear Glass 4mm', 'sheet_size': '130"x84"', 'base_price': 1.12, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 5mm', 'sheet_size': '144"x84"', 'base_price': 1.44, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm', 'sheet_size': '144"x84"', 'base_price': 1.67, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 6mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 2.39, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm', 'sheet_size': '144"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 8mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 3.70, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm', 'sheet_size': '144"x84"', 'base_price': 3.01, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 10mm Jumbo', 'sheet_size': '144"x100"', 'base_price': 4.40, 'temperable': True, 'annealed': True},
    {'name': 'Clear Glass 12mm', 'sheet_size': '144"x84"', 'base_price': 3.44, 'temperable': True, 'annealed': True},
    {'name': 'Bronze 4mm', 'sheet_size': '130"x84"', 'base_price': 2.22, 'temperable': True, 'annealed': True},
    {'name': 'Bronze 6mm', 'sheet_size': '130"x84"', 'base_price': 3.49, 'temperable': True, 'annealed': True},
    {'name': 'Sandblast 4mm', 'sheet_size': '130"x84"', 'base_price': 1.84, 'temperable': True, 'annealed': True},
    {'name': 'Sandblast 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': True},
    {'name': 'SolarCoat 4mm', 'sheet_size': '130"x84"', 'base_price': 2.18, 'temperable': True, 'annealed': True},
    {'name': 'SolarCoat 6mm', 'sheet_size': '130"x84"', 'base_price': 3.28, 'temperable': True, 'annealed': True},
    {'name': 'Solarban60 4mm', 'sheet_size': '130"x84"', 'base_price': 2.22, 'temperable': True, 'annealed': False},
    {'name': 'Solarban60 6mm', 'sheet_size': '130"x84"', 'base_price': 3.85, 'temperable': True, 'annealed': False},
    {'name': 'Solarban60 VT 4mm', 'sheet_size': '130"x84"', 'base_price': 2.50, 'temperable': True, 'annealed': False},
    {'name': 'Solarban60 VT 6mm', 'sheet_size': '130"x84"', 'base_price': 4.27, 'temperable': True, 'annealed': False},
    {'name': 'LoE 180 4mm', 'sheet_size': '130"x84"', 'base_price': 1.57, 'temperable': True, 'annealed': False},
    {'name': 'LoE 180 6mm', 'sheet_size': '130"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': False},
    {'name': 'LoE 270 4mm', 'sheet_size': '130"x84"', 'base_price': 1.57, 'temperable': True, 'annealed': False},
    {'name': 'LoE 270 6mm', 'sheet_size': '130"x84"', 'base_price': 2.38, 'temperable': True, 'annealed': False},
    {'name': 'LoE 366 4mm', 'sheet_size': '130"x84"', 'base_price': 2.12, 'temperable': True, 'annealed': False},
    {'name': 'LoE 366 6mm', 'sheet_size': '130"x84"', 'base_price': 2.95, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-180 4mm', 'sheet_size': '130"x84"', 'base_price': 2.21, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-180 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-270 4mm', 'sheet_size': '130"x84"', 'base_price': 2.21, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-270 6mm', 'sheet_size': '130"x84"', 'base_price': 2.88, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-366 4mm', 'sheet_size': '130"x84"', 'base_price': 3.00, 'temperable': True, 'annealed': False},
    {'name': 'LoE Q-366 6mm', 'sheet_size': '130"x84"', 'base_price': 3.64, 'temperable': True, 'annealed': False},
    {'name': 'LoE i89 4mm', 'sheet_size': '130"x84"', 'base_price': 3.36, 'temperable': True, 'annealed': False},
    {'name': 'LoE i89 6mm', 'sheet_size': '130"x84"', 'base_price': 3.85, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm 4mm', 'sheet_size': '130"x84"', 'base_price': 2.75, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm 6mm', 'sheet_size': '130"x84"', 'base_price': 3.81, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm II EZP 4mm', 'sheet_size': '130"x84"', 'base_price': 2.95, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm II EZP 6mm', 'sheet_size': '130"x84"', 'base_price': 3.81, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm Lux 4mm', 'sheet_size': '130"x84"', 'base_price': 2.61, 'temperable': True, 'annealed': False},
    {'name': 'Planitherm Lux 6mm', 'sheet_size': '130"x84"', 'base_price': 3.51, 'temperable': True, 'annealed': False},
    {'name': 'SKN176 4mm', 'sheet_size': '130"x84"', 'base_price': 3.35, 'temperable': True, 'annealed': False},
    {'name': 'SKN176 6mm', 'sheet_size': '130"x84"', 'base_price': 4.50, 'temperable': True, 'annealed': False},
    {'name': 'SKN176II EZP 4mm', 'sheet_size': '130"x84"', 'base_price': 3.48, 'temperable': True, 'annealed': False},
    {'name': 'SKN176II EZP 6mm', 'sheet_size': '130"x84"', 'base_price': 4.63, 'temperable': True, 'annealed': False},
    {'name': 'Superspacer 10mm / sqf', 'sheet_size': '', 'base_price': 0.73, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 11mm / sqf', 'sheet_size': '', 'base_price': 0.75, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 12mm / sqf', 'sheet_size': '', 'base_price': 0.80, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 14mm / sqf', 'sheet_size': '', 'base_price': 0.85, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 16mm / sqf', 'sheet_size': '', 'base_price': 0.90, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 18mm / sqf', 'sheet_size': '', 'base_price': 0.95, 'temperable': False, 'annealed': False},
    {'name': 'Superspacer 20mm / sqf', 'sheet_size': '', 'base_price': 1.00, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 11mm / sqf', 'sheet_size': '', 'base_price': 0.45, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 12mm / sqf', 'sheet_size': '', 'base_price': 0.47, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 14mm / sqf', 'sheet_size': '', 'base_price': 0.51, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 16mm / sqf', 'sheet_size': '', 'base_price': 0.55, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 18mm / sqf', 'sheet_size': '', 'base_price': 0.59, 'temperable': False, 'annealed': False},
    {'name': 'Technoform 20mm / sqf', 'sheet_size': '', 'base_price': 0.63, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 10mm Polyurethane', 'sheet_size': '', 'base_price': 0.15, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 12mm Polyurethane', 'sheet_size': '', 'base_price': 0.17, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 16mm Polyurethane', 'sheet_size': '', 'base_price': 0.20, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 18mm Polyurethane', 'sheet_size': '', 'base_price': 0.22, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 20mm Polyurethane', 'sheet_size': '', 'base_price': 0.24, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 10mm Silicone', 'sheet_size': '', 'base_price': 0.18, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 12mm Silicone', 'sheet_size': '', 'base_price': 0.20, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 16mm Silicone', 'sheet_size': '', 'base_price': 0.23, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 18mm Silicone', 'sheet_size': '', 'base_price': 0.25, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 20mm Silicone', 'sheet_size': '', 'base_price': 0.27, 'temperable': False, 'annealed': False},
]


def resolve_effective_tier(user, customers, valid_tiers):
    """User override, otherwise the customer company tier, otherwise T2."""
    valid = set(valid_tiers)
    user = user or {}
    override = user.get('tier_override') or ''
    if override in valid:
        return override
    customer = (customers or {}).get(user.get('customer_id') or '') or {}
    tier = customer.get('tier') or ''
    if tier in valid:
        return tier
    if 'T2' in valid:
        return 'T2'
    if not valid_tiers:
        return 'T2'
    return next(iter(valid_tiers))


def _markup(cfg, key, fallback):
    try:
        value = float((cfg or {}).get(key))
    except (TypeError, ValueError):
        return fallback
    if value < 0:
        return fallback
    return value


def calculate_price_sqf(data, tier, pricing, tiers, temper_prices=None):
    """Per-square-foot sell price. Sealants use the spacer markup, matching the existing engine."""
    temper_prices = TEMPER_PRICES if temper_prices is None else temper_prices
    cfg = (tiers or {}).get(tier) or (tiers or {}).get('T2') or {}
    glass_markup = _markup(cfg, 'glass_markup', 0.40)
    spacer_markup = _markup(cfg, 'spacer_markup', 0.40)
    total = 0.0
    for key in ('glass1', 'glass2', 'glass3'):
        name = data.get(key) or ''
        if not name or name == 'n/a':
            continue
        item = next((row for row in pricing if row['name'] == name), None)
        if not item:
            continue
        total += round(float(item['base_price']) * (1 + glass_markup), 2)
        if data.get(key.replace('glass', 'temper')) == 'yes':
            thick = name.split()[-1] if name else ''
            total += temper_prices.get(thick, 0.65)
    for key in ('spacer1', 'spacer2', 'sealant1', 'sealant2'):
        name = data.get(key) or ''
        if not name or name == 'n/a':
            continue
        item = next((row for row in pricing if row['name'] == name), None)
        if not item:
            continue
        total += round(float(item['base_price']) * (1 + spacer_markup), 2)
    return round(total, 2)


def classify(pricing):
    glass, spacers, sealants = [], [], []
    for item in pricing:
        name = item['name'].lower()
        if 'spacer' in name or 'technoform' in name:
            spacers.append(item)
        elif 'sealant' in name:
            sealants.append(item)
        elif 'mm' in name and 'tempering' not in name and 'desic' not in name:
            glass.append(item)
    return glass, spacers, sealants


def load_pricing_workbook(path):
    import openpyxl

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='Data Validation extension is not supported')
        workbook = openpyxl.load_workbook(path, data_only=True)
    if 'pricelist' not in workbook.sheetnames:
        raise ValueError('pricelist sheet not found')
    sheet = workbook['pricelist']
    pricing = []
    seen = set()

    def add(item):
        if item['name'] in seen:
            logger.warning('Skipping duplicate price row %s', item['name'])
            return
        seen.add(item['name'])
        pricing.append(item)

    for row in sheet.iter_rows(min_row=2, values_only=True):
        if not row or not row[0] or not row[8]:
            continue
        name = str(row[0]).strip()
        lowered = name.lower()
        # Tempering and desiccant are add-ons, not lites. Temper prices stay in TEMPER_PRICES.
        if lowered.startswith('tempering') or 'desic' in lowered:
            continue
        item = {
            'thickness': str(row[1]) if row[1] else '',
            'sheet_size': f'{row[2]}"x{row[3]}"' if row[2] and row[3] else '',
            'base_price': float(row[8]) if row[8] else 0,
            'temperable': True,
            'annealed': True,
        }
        if '180/270' in name:
            add({**item, 'name': name.replace('180/270', '180')})
            add({**item, 'name': name.replace('180/270', '270')})
        else:
            add({**item, 'name': name})
    if not pricing:
        raise ValueError('pricelist was empty')
    return pricing


def load_pricing(path=None):
    path = path or resolve_pricing_path()
    try:
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(path or 'pricing_base.xlsx')
        pricing = load_pricing_workbook(path)
        logger.info('Loaded %s pricing rows from %s', len(pricing), path)
        return pricing, path
    except Exception:
        logger.exception('Pricing load error for %s; using built-in fallback', path)
        return [dict(item) for item in FALLBACK_PRICING], None

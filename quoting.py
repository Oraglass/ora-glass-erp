"""IGU price calculation and the pricing workbook loader."""

import logging
import os
import re
import warnings

from storage import TIER_ORDER, pricing_search_paths, resolve_pricing_path

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
    # Sealant prices are copied from pricing_base.xlsx. Widths match the workbook
    # (10, 11, 12, 14, 16, 18, 20, 24, 26). Technoform prices below are the old
    # fallback only; the workbook prices those rows at 0 and they are not added here.
    {'name': 'Sealant - 10mm Polyurethane', 'sheet_size': '', 'base_price': 0.14831243227223856, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 11mm Polyurethane', 'sheet_size': '', 'base_price': 0.16243742582197554, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 12mm Polyurethane', 'sheet_size': '', 'base_price': 0.16949992259684407, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 14mm Polyurethane', 'sheet_size': '', 'base_price': 0.19774990969631806, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 16mm Polyurethane', 'sheet_size': '', 'base_price': 0.2259998967957921, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 18mm Polyurethane', 'sheet_size': '', 'base_price': 0.2542498838952661, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 20mm Polyurethane', 'sheet_size': '', 'base_price': 0.28249987099474017, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 24mm Polyurethane', 'sheet_size': '', 'base_price': 0.33899984519368814, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 26mm Polyurethane', 'sheet_size': '', 'base_price': 0.3672498322931621, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 10mm Silicone', 'sheet_size': '', 'base_price': 0.1795788450989665, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 11mm Silicone', 'sheet_size': '', 'base_price': 0.19668159225124895, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 12mm Silicone', 'sheet_size': '', 'base_price': 0.20523296582739026, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 14mm Silicone', 'sheet_size': '', 'base_price': 0.23943846013195527, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 16mm Silicone', 'sheet_size': '', 'base_price': 0.27364395443652034, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 18mm Silicone', 'sheet_size': '', 'base_price': 0.30784944874108533, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 20mm Silicone', 'sheet_size': '', 'base_price': 0.3420549430456505, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 24mm Silicone', 'sheet_size': '', 'base_price': 0.4104659316547805, 'temperable': False, 'annealed': False},
    {'name': 'Sealant - 26mm Silicone', 'sheet_size': '', 'base_price': 0.4446714259593455, 'temperable': False, 'annealed': False},
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


def _ordered_tier_ids(ids, valid_tiers):
    valid = set(valid_tiers)
    chosen = []
    for tid in ids:
        if tid in valid and tid not in chosen:
            chosen.append(tid)
    ordered = [tid for tid in TIER_ORDER if tid in chosen]
    ordered.extend(tid for tid in chosen if tid not in ordered)
    return ordered


def allowed_tier_ids(user, customers, valid_tiers):
    """Tiers this user may quote at.

    A missing allowed_tiers list is the legacy set: company tier and override.
    An explicit list is used as stored. The company tier is not added to it.
    """
    user = user or {}
    customers = customers or {}
    customer = customers.get(user.get('customer_id') or '') or {}
    company_tier = customer.get('tier') or ''
    override = user.get('tier_override') or ''
    stored = user.get('allowed_tiers', None)
    if stored is None:
        base = [company_tier, override]
        if not _ordered_tier_ids(base, valid_tiers):
            base.append(resolve_effective_tier(user, customers, valid_tiers))
    else:
        base = []
        if isinstance(stored, (list, tuple)):
            base.extend(str(item or '').strip() for item in stored)
    return _ordered_tier_ids(base, valid_tiers)


def default_quote_tier(user, customers, valid_tiers):
    """Tier used when the request does not choose one. Never a tier the user is not allowed."""
    user = user or {}
    if user.get('role') == 'customer':
        allowed = allowed_tier_ids(user, customers, valid_tiers)
        allowed_set = set(allowed)
        override = user.get('tier_override') or ''
        if override in allowed_set:
            return override
        customer = (customers or {}).get(user.get('customer_id') or '') or {}
        company_tier = customer.get('tier') or ''
        if company_tier in allowed_set:
            return company_tier
        if allowed:
            return allowed[0]
        return ''
    return resolve_effective_tier(user, customers, valid_tiers)


def tier_for_request(user, requested, customers, valid_tiers):
    """Tier to price and store.

    Customers may only use an allowed tier. A requested tier outside that list
    is refused (None) instead of priced. Staff may use any known tier.
    """
    user = user or {}
    requested = str(requested or '').strip()
    if user.get('role') == 'customer':
        allowed = set(allowed_tier_ids(user, customers, valid_tiers))
        if requested:
            if requested in allowed:
                return requested
            return None
        chosen = default_quote_tier(user, customers, valid_tiers)
        if chosen in allowed:
            return chosen
        return None
    if requested in set(valid_tiers):
        return requested
    return resolve_effective_tier(user, customers, valid_tiers)


def _markup(cfg, key, fallback):
    try:
        value = float((cfg or {}).get(key))
    except (TypeError, ValueError):
        return fallback
    if value < 0:
        return fallback
    return value


_WIDTH_MM = re.compile(r'(\d+(?:\.\d+)?)\s*mm', re.IGNORECASE)


def component_width_mm(name):
    """Spacer or sealant width. Accepts '14mm', '14 mm', '14MM', and extra spaces."""
    if not name:
        return None
    match = _WIDTH_MM.search(str(name))
    if not match:
        return None
    width = float(match.group(1))
    if width != int(width):
        return None
    return int(width)


def _sealant_names(sealants):
    names = []
    for item in sealants or []:
        name = item.get('name') if isinstance(item, dict) else item
        if name:
            names.append(str(name))
    return names


def _prefer_polyurethane(options):
    """Default material is polyurethane, then silicone, matching the quote screen."""
    for name in options:
        if 'poly' in name.lower():
            return name
    return options[0] if options else None


def suggest_sealant(spacer_name, sealants):
    """Choose the sealant for a spacer.

    Exact width wins. Polyurethane is selected when that width has more than one material.
    Technoform with no exact width uses a sealant 1mm smaller, then 1mm larger.
    Other spacers never use that ±1mm rule: the sealant stays unselected.
    """
    names = _sealant_names(sealants)
    width = component_width_mm(spacer_name)
    if width is None:
        return {
            'selected': None,
            'options': names,
            'message': 'Could not read a spacer width. Choose a sealant.',
            'width': None,
            'match': 'none',
        }
    by_width = {}
    for name in names:
        sealant_width = component_width_mm(name)
        if sealant_width is None:
            continue
        by_width.setdefault(sealant_width, []).append(name)
    technoform = 'techno' in str(spacer_name).lower()
    chosen_width = None
    match = 'none'
    if width in by_width:
        chosen_width = width
        match = 'exact'
    elif technoform and (width - 1) in by_width:
        # Both neighbors can exist. The smaller one is the consistent choice.
        chosen_width = width - 1
        match = 'technoform_narrower'
    elif technoform and (width + 1) in by_width:
        chosen_width = width + 1
        match = 'technoform_wider'
    if chosen_width is None:
        return {
            'selected': None,
            'options': names,
            'message': f'No {width}mm sealant found. Choose a sealant.',
            'width': width,
            'match': 'none',
        }
    options = by_width[chosen_width]
    message = ''
    if match != 'exact':
        message = f'No {width}mm sealant. Using {chosen_width}mm.'
    return {
        'selected': _prefer_polyurethane(options),
        'options': options,
        'message': message,
        'width': width,
        'match': match,
    }


def sealant_match_map(spacers, sealants):
    """Spacer name to the sealant suggestion used by both quote calculators."""
    mapping = {}
    for item in spacers or []:
        name = item.get('name') if isinstance(item, dict) else item
        if name:
            mapping[str(name)] = suggest_sealant(name, sealants)
    return mapping


CAD_RATE = 1.4


def area_sqf(width, height, unit):
    """Square feet from a width and height. Millimetres use the same divisor as the size calculator."""
    try:
        wide = float(width)
        high = float(height)
    except (TypeError, ValueError):
        return 0.0
    if wide <= 0 or high <= 0:
        return 0.0
    if str(unit or 'in').lower() == 'mm':
        return (wide * high) / 92903.04
    return (wide * high) / 144


def reprice_size_line(spec, pcs, tier, currency, pricing, tiers):
    """Price one per-size line in the quote currency. CAD applies the 1.40 rate once."""
    usd = calculate_price_sqf(spec, tier, pricing, tiers)
    rate = CAD_RATE if str(currency or 'usd').lower() == 'cad' else 1.0
    sqf = area_sqf((spec or {}).get('width'), (spec or {}).get('height'), (spec or {}).get('unit'))
    price_sqf = round(usd * rate, 2)
    price_pc = round(usd * sqf * rate, 2)
    try:
        pieces = int(pcs or 1)
    except (TypeError, ValueError):
        pieces = 1
    if pieces < 1:
        pieces = 1
    return {
        'sqf': round(sqf, 2),
        'price_sqf': price_sqf,
        'price_pc': price_pc,
        'pcs': pieces,
        'total': round(price_pc * pieces, 2),
    }


def calculate_price_sqf(data, tier, pricing, tiers, temper_prices=None):
    """Per-square-foot sell price. Sealants use the spacer markup, matching the existing engine.

    Tier 5 is exactly twice the Tier 2 price of each glass, spacer, and sealant line.
    Temper add-ons stay the same flat amount at every tier.
    """
    temper_prices = TEMPER_PRICES if temper_prices is None else temper_prices
    source = 'T2' if tier == 'T5' else tier
    scale = 2 if tier == 'T5' else 1
    cfg = (tiers or {}).get(source) or (tiers or {}).get('T2') or {}
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
        total += round(float(item['base_price']) * (1 + glass_markup), 2) * scale
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
        total += round(float(item['base_price']) * (1 + spacer_markup), 2) * scale
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


def _announce_pricing(message):
    """Print as well as log. Render captured the old print() line, not the logger."""
    print(message, flush=True)
    logger.warning(message)


def load_pricing(path=None):
    if path:
        candidates = [os.path.abspath(path)]
    else:
        candidates = pricing_search_paths()
    chosen = next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
    try:
        if not chosen:
            raise FileNotFoundError('pricing_base.xlsx not found')
        pricing = load_pricing_workbook(chosen)
        _announce_pricing(f'Pricing loaded: {len(pricing)} items from {chosen}')
        return pricing, chosen
    except Exception:
        logger.exception('Pricing load error for %s; using built-in fallback', chosen or candidates)
        fallback = [dict(item) for item in FALLBACK_PRICING]
        tried = ', '.join(candidates) if candidates else '(none)'
        _announce_pricing(
            f'Pricing loaded: {len(fallback)} items from built-in fallback. '
            f'Workbook not found or unreadable. Tried: {tried}'
        )
        return fallback, None

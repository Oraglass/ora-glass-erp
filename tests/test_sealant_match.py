from quoting import FALLBACK_PRICING, classify, component_width_mm, suggest_sealant
from app import SEALANTS, SPACERS


LIVE_WIDTHS = (10, 12, 16, 18, 20)
LIVE_SEALANTS = [
    name
    for width in LIVE_WIDTHS
    for name in (f'Sealant - {width}mm Polyurethane', f'Sealant - {width}mm Silicone')
]


def test_width_parsing_accepts_catalog_formats():
    assert component_width_mm('Superspacer 14mm / sqf') == 14
    assert component_width_mm('Technoform 11mm / sqf') == 11
    assert component_width_mm('Sealant -  14mm Polyurethane') == 14
    assert component_width_mm('Sealant - 10mm Silicone') == 10
    assert component_width_mm('Sealant - 14 mm Polyurethane') == 14
    assert component_width_mm('Sealant - 14MM Silicone') == 14
    assert component_width_mm('') is None


def test_exact_width_selects_polyurethane():
    choice = suggest_sealant('Superspacer 14mm / sqf', [
        'Sealant -  14mm Silicone',
        'Sealant -  14mm Polyurethane',
        'Sealant -  12mm Polyurethane',
    ])
    assert choice['match'] == 'exact'
    assert choice['selected'] == 'Sealant -  14mm Polyurethane'
    assert choice['options'] == ['Sealant -  14mm Silicone', 'Sealant -  14mm Polyurethane']
    assert choice['message'] == ''


def test_technoform_prefers_one_millimetre_smaller_then_larger():
    both = suggest_sealant('Technoform 11mm / sqf', LIVE_SEALANTS)
    assert both['match'] == 'technoform_narrower'
    assert both['selected'] == 'Sealant - 10mm Polyurethane'
    assert both['options'] == ['Sealant - 10mm Polyurethane', 'Sealant - 10mm Silicone']
    assert '12mm' not in both['selected']

    wider_only = suggest_sealant('Technoform 15mm / sqf', LIVE_SEALANTS)
    assert wider_only['match'] == 'technoform_wider'
    assert wider_only['selected'] == 'Sealant - 16mm Polyurethane'

    exact = suggest_sealant('Technoform 12mm / sqf', LIVE_SEALANTS + ['Sealant - 11mm Polyurethane', 'Sealant - 13mm Polyurethane'])
    assert exact['match'] == 'exact'
    assert exact['selected'] == 'Sealant - 12mm Polyurethane'


def test_superspacer_without_exact_width_is_not_auto_selected():
    choice = suggest_sealant('Superspacer 14mm / sqf', LIVE_SEALANTS)
    assert choice['selected'] is None
    assert choice['match'] == 'none'
    assert choice['options'] == LIVE_SEALANTS
    assert 'No 14mm sealant found' in choice['message']
    assert '12mm' not in (choice['selected'] or '')
    assert '16mm' not in (choice['selected'] or '')


def test_workbook_and_fallback_gaps():
    workbook_gaps = [
        item['name'] for item in SPACERS
        if suggest_sealant(item['name'], SEALANTS)['selected'] is None
    ]
    _glass, fallback_spacers, fallback_sealants = classify(FALLBACK_PRICING)
    fallback_gaps = [
        item['name'] for item in fallback_spacers
        if suggest_sealant(item['name'], fallback_sealants)['selected'] is None
    ]
    assert workbook_gaps == []
    assert fallback_gaps == []
    assert suggest_sealant('Superspacer 14mm / sqf', fallback_sealants)['selected'] == 'Sealant - 14mm Polyurethane'
    assert suggest_sealant('Technoform 11mm / sqf', fallback_sealants)['match'] == 'exact'
    assert suggest_sealant('Technoform 11mm / sqf', fallback_sealants)['selected'] == 'Sealant - 11mm Polyurethane'

"""Tempered lites keep the MAC_IGU space before a following spacer."""

import documents
import production
from orgadata_import import parse_igu_description
from storage import load_quotes, save_quotes
from tests.conftest import login, write_user
from tests.test_irregular_shape import _line
from tests.test_edit_quote import _draft


def _spec(glass1, temper1, spacer, glass2, temper2, glass3='', temper3='n/a', spacer2='', sealant2=''):
    return {
        'glass1': glass1, 'temper1': temper1,
        'spacer1': spacer, 'sealant1': 'Polysulfide',
        'glass2': glass2, 'temper2': temper2,
        'spacer2': spacer2, 'sealant2': sealant2,
        'glass3': glass3, 'temper3': temper3,
        'width': 24, 'height': 36, 'unit': 'in',
    }


def test_mac_igu_names_match_the_erp_and_excel_uses_the_sheet_text():
    rows = production.read_product_sheet()
    assert len(rows) == 169
    matched = 0
    for row in rows:
        makeup = production.makeup_from_product_name(row['product_name'])
        assert production.production_name_for_makeup(makeup) == row['product_name']
        matched += 1
    assert matched == 169

    double = _spec('Solarban60 VT 6mm', 'yes', 'Superspacer 16mm / sqf', 'Clear Glass 6mm', 'yes')
    triple = _spec(
        'Solarban60 VT 6mm', 'yes', 'Superspacer 14mm / sqf',
        'SolarCoat 4mm', 'yes', 'Clear Glass 4mm', 'yes',
        'Superspacer 14mm / sqf', 'Polysulfide',
    )
    plain = _spec('Clear Glass 6mm', 'yes', 'Superspacer 16mm / sqf', 'Clear Glass 6mm', 'yes')
    double_row = production.excel_identity({'name': 'stale Tempered+ name', 'spec': double})
    triple_row = production.excel_identity({'name': 'stale Tempered+ name', 'spec': triple})
    id7 = production.excel_identity({'name': 'stale Tempered + name', 'spec': plain})
    assert double_row['product_id'] == '70'
    assert double_row['product_name'] == (
        '28 - 6mmSolarban60 VT Tempered +16mmSuperSpacer with Argon(Polysulfide)+6mmClear Glass Tempered'
    )
    assert triple_row['product_id'] == '74'
    assert triple_row['product_name'] == (
        '42 - 6mmSolarban60 VT Tempered +14mmSuperSpacer with Argon(Polysulfide)'
        '+4mmSolarCoat Tempered +14mmSuperSpacer with Argon(Polysulfide)+4mmClear Glass Tempered'
    )
    assert id7['product_id'] == '7'
    assert id7['product_name'] == (
        '28 - 6mmClear Glass Tempered+16mmSuperSpacer with Argon(Polysulfide)+6mmClear Glass Tempered'
    )
    assert 'Tempered +' not in id7['product_name']
    spaced = production.registry_for_name(double_row['product_name'].replace('Tempered +', 'Tempered+'))
    exact = production.registry_for_name(double_row['product_name'])
    assert spaced['product_id'] == exact['product_id'] == '70'
    parsed_tight = parse_igu_description(double_row['product_name'].replace('Tempered +', 'Tempered+'))
    parsed_spaced = parse_igu_description(double_row['product_name'])
    assert parsed_tight == parsed_spaced
    assert documents.build_label(double).split(' - ', 1)[1].startswith('6mmSolarban60 VT Tempered +')


def test_an_annealed_lite_keeps_no_space_before_the_spacer():
    """Only Tempered gets a space. 4mmSolarCoat+20mm stays tight."""
    annealed = _spec('SolarCoat 4mm', 'n/a', 'Superspacer 20mm / sqf', 'Clear Glass 4mm', 'n/a')
    makeup = production.makeup_from_spec(annealed)
    assert makeup.startswith('4mmSolarCoat+20mm')
    assert ' +' not in makeup
    label = documents.build_label(annealed)
    assert '4mmSolarCoat+20mm' in label
    assert ' +' not in label
    listed = _spec('Solarban60 4mm', 'n/a', 'Superspacer 20mm / sqf', 'SolarCoat 4mm', 'n/a')
    row = production.excel_identity({'name': 'annealed', 'spec': listed})
    assert row['product_id'] == '12'
    assert row['product_name'] == (
        '28 - 4mmSolarban60+20mmSuperSpacer with Argon(Polysulfide)+4mmSolarCoat'
    )
    assert ' +' not in row['product_name']


def test_opening_a_quote_rewrites_tempered_spacing_without_touching_price(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    quote_id = _draft(client, [_line(pcs=1, irregular=False)])['id']
    quotes = load_quotes()
    quote = next(row for row in quotes if row['id'] == quote_id)
    glass = next(item for item in quote['items'] if item.get('spec'))
    glass['spec'] = _spec('Solarban60 VT 6mm', 'yes', 'Superspacer 16mm / sqf', 'Clear Glass 6mm', 'yes')
    glass['name'] = (
        '28 (1 1/8") - 6mmSolarban60 VT Tempered+16mmSuperSpacer with Argon(Polysulfide)+6mmClear Glass Tempered'
    )
    price = glass['total']
    number = quote.get('number') or quote['id']
    save_quotes(quotes)
    opened = client.get(f'/api/quotes/{quote_id}').get_json()
    shown = next(item for item in opened['items'] if item.get('spec'))
    assert 'Tempered +16mm' in shown['name']
    assert shown['total'] == price
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    assert (stored.get('number') or stored['id']) == number
    saved = next(item for item in stored['items'] if item.get('spec'))
    assert saved['total'] == price
    assert 'Tempered +16mm' in saved['name']

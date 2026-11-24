"""Excel product names that describe a full IGU are priced as an IGU."""

import io

from orgadata_import import match_igu_description, parse_igu_description
from quoting import suggest_sealant
from tests.conftest import login, write_user
from tests.test_orgadata_import import _open_windows

EXACT = '28 - 4mmSolarban60+20mmSuperSpacer with Argon(Polysulfide)+4mmSolarCoat'
TECHNO = '28 - 4mmSolarban60+16mmTechnoform with Argon(Polysulfide)+4mmSolarCoat'
NO_GAS = '28 - 4mmSolarban60+20mmSuperSpacer+4mmSolarCoat'
TRIPLE = (
    '42 - 4mmSolarban60+12mmSuperSpacer with Argon(Polysulfide)'
    '+4mmClear Glass+12mmTechnoform with Argon(Polysulfide)+4mmSolarCoat'
)
UNKNOWN = '28 - 4mmNotARealGlass+20mmSuperSpacer with Argon(Polysulfide)+4mmSolarCoat'


def _names():
    import pricing_catalog
    glass, spacers, sealants = pricing_catalog.material_lists()
    return [row['name'] for row in glass], [row['name'] for row in spacers], [row['name'] for row in sealants]


def test_exact_open_windows_string_and_variants_match_the_catalog():
    glass, spacers, sealants = _names()
    parsed = parse_igu_description(EXACT)
    assert parsed['argon'] is True
    assert parsed['lites'] == [
        {'thickness_mm': 4, 'source': 'Solarban60'},
        {'thickness_mm': 4, 'source': 'SolarCoat'},
    ]
    assert parsed['spacers'][0]['source'] == 'SuperSpacer'
    assert parsed['spacers'][0]['gap_mm'] == 20
    assert parsed['spacers'][0]['sealant_note'] == 'Polysulfide'
    matched = match_igu_description(EXACT, glass, spacers)
    assert matched['matched'] is True
    assert matched['lites'][0]['product'] == 'Solarban60 4mm'
    assert matched['lites'][1]['product'] == 'SolarCoat 4mm'
    assert matched['spacers'][0]['product'] == 'Superspacer 20mm / sqf'
    assert 'Polyurethane' in (suggest_sealant('Superspacer 20mm / sqf', sealants)['selected'] or '')

    techno = match_igu_description(TECHNO, glass, spacers)
    assert techno['spacers'][0]['product'] == 'Technoform 16mm / sqf'
    assert techno['argon'] is True

    plain_gas = parse_igu_description(NO_GAS)
    assert plain_gas['argon'] is False
    assert plain_gas['spacers'][0]['source'] == 'SuperSpacer'
    assert match_igu_description(NO_GAS, glass, spacers)['matched'] is True

    triple = match_igu_description(TRIPLE, glass, spacers)
    assert [lite['product'] for lite in triple['lites']] == [
        'Solarban60 4mm', 'Clear Glass 4mm', 'SolarCoat 4mm',
    ]
    assert [spacer['product'] for spacer in triple['spacers']] == [
        'Superspacer 12mm / sqf', 'Technoform 12mm / sqf',
    ]
    assert triple['argon'] is True

    missed = match_igu_description(UNKNOWN, glass, spacers)
    assert missed['matched'] is False
    assert missed['lites'][0]['product'] == ''
    assert missed['lites'][1]['product'] == 'SolarCoat 4mm'
    assert any('NotARealGlass' in warning for warning in missed['warnings'])
    assert parse_igu_description('Solarban60 4mm') is None
    assert parse_igu_description('Their Clear') is None


def _csv(*rows):
    lines = ['Width,Height,Qty,Product name,Mark']
    for width, height, qty, glass, mark in rows:
        lines.append(f'{width},{height},{qty},"{glass}",{mark}')
    return '\n'.join(lines).encode()


def test_import_preview_prices_the_igu_and_flags_an_unmatched_lite(client):
    write_user('admin@test.com', 'admin')
    _open_windows()
    login(client, 'admin@test.com')
    page = client.get('/quotes')
    assert b'data-field="glass2"' in page.data
    assert b'>Outer</div>' in page.data
    assert b'data-field="argon"' in page.data
    saved = client.post('/api/order-templates', json={
        'customer_id': 'open-windows',
        'name': 'Open Windows product name',
        'kind': 'excel',
        'unit': 'in',
        'header_row': 1,
        'columns': {
            'width': 'Width', 'height': 'Height', 'qty': 'Qty',
            'glass': 'Product name', 'mark': 'Mark',
        },
    })
    assert saved.status_code == 200, saved.get_json()
    uploaded = client.post('/api/order-imports', data={
        'customer_id': 'open-windows',
        'currency': 'cad',
        'template_id': saved.get_json()['id'],
        'file': (io.BytesIO(_csv(
            (24, 36, 2, EXACT, 'W1'),
            (24, 36, 2, 'Clear Glass 6mm', 'W2'),
            (24, 36, 1, UNKNOWN, 'W3'),
        )), 'open-windows.csv'),
    })
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['status'] == 'needs_mapping'
    igu, single, missed = body['lines']
    assert igu['igu'] is True
    assert igu['argon'] is True
    assert igu['glass_product'] == 'Solarban60 4mm'
    assert igu['glass2_product'] == 'SolarCoat 4mm'
    assert igu['spacer_product'] == 'Superspacer 20mm / sqf'
    assert 'Polyurethane' in (igu.get('sealant') or '')
    assert igu['errors'] == []
    assert igu['total'] > single['total']
    assert single['igu'] is False
    assert single['glass_product'] == 'Clear Glass 6mm'
    assert single['glass2_product'] == ''
    assert single['spacer_product'] == ''
    assert missed['igu'] is True
    assert missed['glass_product'] == ''
    assert missed['total'] is None
    assert any('NotARealGlass' in error for error in missed['errors'])
    assert EXACT not in body['unmatched_glass']

    created = client.post(f"/api/order-imports/{body['id']}/quote", json={
        'currency': 'cad',
        'tier': body['tier'],
        'lines': [{
            'mark': igu['mark'],
            'width': igu['width'],
            'height': igu['height'],
            'qty': igu['qty'],
            'glass_name': igu['glass_name'],
            'glass_product': igu['glass_product'],
            'glass2_product': igu['glass2_product'],
            'spacer_product': igu['spacer_product'],
            'argon': True,
            'igu': True,
        }],
    })
    assert created.status_code == 200, created.get_json()
    quote_id = created.get_json()['id']
    quote = next(row for row in client.get('/api/quotes').get_json() if row['id'] == quote_id)
    spec = quote['items'][0]['spec']
    assert spec['glass1'] == 'Solarban60 4mm'
    assert spec['glass2'] == 'SolarCoat 4mm'
    assert spec['spacer1'] == 'Superspacer 20mm / sqf'
    assert spec['argon'] is True
    assert 'Polyurethane' in spec['sealant1']
    assert 'Solarban60' in quote['items'][0]['name']
    assert 'SolarCoat' in quote['items'][0]['name']
    assert quote['items'][0]['total'] == igu['total']

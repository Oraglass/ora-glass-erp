"""Pricing catalog stays on the workbook prices until an administrator edits a row."""

from quoting import calculate_price_sqf, load_pricing
from storage import load_quotes, load_tiers
from tests.conftest import login, write_user


def _specs():
    return [
        {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'},
        {'glass1': 'Clear Glass 6mm', 'temper1': 'yes'},
        {'glass1': 'Bronze 4mm', 'temper1': 'n/a'},
        {'glass1': 'LoE 180 4mm', 'temper1': 'n/a', 'spacer1': 'Superspacer 12mm / sqf',
         'sealant1': 'Sealant -  12mm Polyurethane', 'glass2': 'Bronze 6mm', 'temper2': 'yes'},
        {'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'spacer1': 'Superspacer 10mm / sqf',
         'sealant1': 'Sealant -  10mm Polyurethane', 'glass2': 'Clear Glass 4mm', 'temper2': 'n/a',
         'spacer2': 'Superspacer 14mm / sqf', 'sealant2': 'Sealant -  14mm Silicone',
         'glass3': 'Clear Glass 6mm', 'temper3': 'n/a'},
    ]


def test_seeded_catalog_matches_the_workbook_prices(data_dir):
    import pricing_catalog
    workbook, _source = load_pricing()
    options = pricing_catalog.calc_options()
    tiers = load_tiers()
    for spec in _specs():
        for tier in ('T1', 'T2', 'T3', 'T4', 'T5'):
            before = calculate_price_sqf(spec, tier, workbook, tiers)
            after = calculate_price_sqf(
                spec, tier, options['pricing'], tiers,
                temper_prices=options['temper_prices'],
                tier5_scale=options['tier5_scale'],
                unknown_temper=options['unknown_temper'],
                extra_per_sqf=options['extra_per_sqf'],
                triple_factor=options['triple_factor'],
            )
            assert before == after, (spec, tier, before, after)
    assert options['extra_per_sqf'] == 0
    assert options['tier5_scale'] == 2
    assert options['temper_prices']['5mm'] == 0.55
    assert options['temper_prices']['12mm'] == 0.95


def test_price_tabs_history_and_saved_quotes_keep_old_prices(client, data_dir):
    import pricing_catalog
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    before = client.post('/api/calculate', json={'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Clear Glass 6mm', 'price_sqf': before['price_sqf'], 'price': before['price_sqf'], 'total': 100}],
        'currency': 'usd',
        'project': 'Keep this price',
        'type': 'sqf',
        'tier': 'T2',
    })
    assert saved.status_code == 200
    quote_id = saved.get_json()['id']

    page = client.get('/pricing')
    assert page.status_code == 200
    assert b'Tier markups' in page.data
    assert b'Glass' in page.data
    assert b'Spacers and sealants' in page.data
    assert b'href="/currency-settings"' not in page.data
    prices = client.get('/pricing?tab=prices')
    assert prices.status_code == 200
    assert b'Clear Glass 4mm' in prices.data
    assert b'T5 CAD' in prices.data
    assert b'Double IGU' in prices.data
    assert b'Bronze 6mm' in prices.data
    glass = client.get('/pricing?tab=glass')
    assert b'Tempering add-on' in glass.data
    assert b'0.55' in glass.data
    components = client.get('/pricing?tab=components')
    assert b'Desicant YES' in components.data
    assert b'Butyl / primary sealant' in components.data
    assert b'Corner keys' in components.data
    formula = client.get('/pricing?tab=formula')
    assert b'tier5_multiplier' in formula.data
    assert b'gas_fill_per_sqf' in formula.data

    catalog = pricing_catalog.load_catalog()
    row = next(item for item in catalog['materials'] if item['name'] == 'Clear Glass 6mm' and item['in_price'])
    row['raw_price'] = 9
    pricing_catalog._recompute_base(row, catalog, [], 'glass')
    pricing_catalog.save_catalog(catalog, 'admin@oraglass.us', [{
        'tab': 'glass', 'item': 'Clear Glass 6mm', 'field': 'raw_price', 'old': 1, 'new': 9,
    }])
    after = client.post('/api/calculate', json={'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    assert after['price_sqf'] != before['price_sqf']
    stored = next(item for item in load_quotes() if item['id'] == quote_id)
    assert stored['items'][0]['price_sqf'] == before['price_sqf']
    history = client.get('/pricing?tab=glass')
    assert b'Clear Glass 6mm' in history.data
    assert b'admin@oraglass.us' in history.data
    assert b'raw_price' in history.data


def test_new_glass_can_be_priced_and_deactivated(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    saved = client.post('/pricing?tab=glass', data={
        'action': 'glass',
        'new_name': 'Test Glass 6mm',
        'new_thickness': '6mm',
        'new_sheet_width': '100',
        'new_sheet_height': '80',
        'new_raw_price': '2',
        'new_tariff_percent': '0',
        'new_waste_percent': '0',
        'new_temperable': 'yes',
        'new_annealed': 'yes',
    })
    assert saved.status_code == 302
    quotes = client.get('/quotes')
    assert b'Test Glass 6mm' in quotes.data
    priced = client.post('/api/calculate', json={'glass1': 'Test Glass 6mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    assert priced['price_sqf'] == round(2 * 1.4, 2)

    import pricing_catalog
    catalog = pricing_catalog.load_catalog()
    added = next(item for item in catalog['materials'] if item['name'] == 'Test Glass 6mm')
    added['in_price'] = False
    pricing_catalog.save_catalog(catalog, 'admin@oraglass.us', [{
        'tab': 'glass', 'item': 'Test Glass 6mm', 'field': 'in_price', 'old': True, 'new': False,
    }])
    removed = client.post('/api/calculate', json={'glass1': 'Test Glass 6mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    assert removed['price_sqf'] == 0


def test_markup_edit_is_logged(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    response = client.post('/pricing', data={
        'action': 'markups',
        'label_T1': 'Tier 1', 'glass_T1': '30', 'spacer_T1': '20',
        'label_T2': 'Tier 2', 'glass_T2': '50', 'spacer_T2': '40',
        'label_T3': 'Tier 3', 'glass_T3': '60', 'spacer_T3': '60',
        'label_T4': 'Tier 4', 'glass_T4': '75', 'spacer_T4': '75',
        'irregular_shape_surcharge': '55',
        'dwg_creation_fee': '50',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'glass_markup' in response.data
    assert b'admin@oraglass.us' in response.data
    assert b'Irregular shape surcharge' in response.data

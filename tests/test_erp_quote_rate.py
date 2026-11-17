"""CAD quote prices use the ERP exchange rate stored on the quote."""

import logging

import currency_rules
from app import PRICING
from quoting import reprice_size_line
from storage import load_orders, load_quotes
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item
from tests.test_quote_export import pdf_text


def test_missing_erp_rate_is_seeded_from_the_old_factor(data_dir, caplog):
    with caplog.at_level(logging.INFO, logger='ora_glass.currency'):
        rate, effective = currency_rules.rate_on('2026-11-17')
    assert rate == 1.4
    assert effective == '2000-01-01'
    assert 'Seeded the ERP exchange rate' in caplog.text
    again, _day = currency_rules.rate_on('2026-11-17')
    assert again == 1.4


def test_cad_quote_stores_the_rate_and_ignores_a_later_change(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'kiu@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['maple'].update({
        'country': 'Canada', 'credit_limit': 100000, 'credit_approved': True, 'credit_currency': 'cad',
    })
    save_customers(customers)
    currency_rules.record_rate(1.25, '2020-01-01', 'admin@oraglass.us')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'USD/CAD 1.2500' in page.data
    item = _size_item()
    item['spec'] = dict(SPEC)
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'cad', 'project': 'Rate', 'type': 'size', 'tier': 'T2',
        'customer_id': 'maple',
    })
    assert saved.status_code == 200, saved.get_json()
    quote_id = saved.get_json()['id']
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    assert stored['fx_rate'] == 1.25
    assert stored['currency'] == 'cad'
    expected = reprice_size_line(dict(SPEC), item.get('pcs') or 1, 'T2', 'cad', PRICING, __import__('storage').load_tiers(), 1.25)
    usd = reprice_size_line(dict(SPEC), item.get('pcs') or 1, 'T2', 'usd', PRICING, __import__('storage').load_tiers())
    assert expected['price_sqf'] == round(usd['price_sqf'] * 1.25, 2)
    assert expected['price_sqf'] != round(usd['price_sqf'] * 1.4, 2)
    currency_rules.record_rate(2, '2026-01-01', 'admin@oraglass.us')
    copied = client.post(f'/api/quotes/{quote_id}/copy', json={})
    assert copied.status_code == 200, copied.get_json()
    copy = next(row for row in load_quotes() if row['id'] == copied.get_json()['id'])
    assert copy['fx_rate'] == 1.25
    assert copy['currency'] == 'cad'
    copy_glass = next(row for row in copy['items'] if row.get('spec'))
    assert copy_glass['price_sqf'] == expected['price_sqf']
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'maple'})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    assert order['fx_rate'] == 1.25
    assert order['items'][0]['price_sqf'] == expected['price_sqf']
    history = client.get('/api/quotes').get_json()
    listed = next(row for row in history if row['id'] == quote_id)
    assert listed['fx_rate'] == 1.25
    pdf = client.post('/api/export-quote', json={
        'items': stored['items'], 'currency': 'cad', 'project': 'Rate', 'fx_rate': 1.25,
    })
    assert b'1.2500' in pdf_text(pdf.data)

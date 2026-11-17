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
    history_page = client.get('/quote-history')
    assert b'USD/CAD' in history_page.data
    order_page = client.get(f"/orders/{order['id']}")
    assert b'USD/CAD' in order_page.data
    order_json = client.get('/api/orders').get_json()
    listed_order = next(row for row in order_json if row['id'] == order['id'])
    assert listed_order['fx_rate'] == 1.25
    pdf = client.post('/api/export-quote', json={
        'items': stored['items'], 'currency': 'cad', 'project': 'Rate', 'fx_rate': 1.25,
    })
    quote_pdf = pdf_text(pdf.data)
    assert b'1.2500' not in quote_pdf
    assert b'USD/CAD' not in quote_pdf
    order_pdf = pdf_text(client.get(f"/api/orders/{order['id']}/confirmation.pdf").data)
    assert b'1.2500' not in order_pdf
    assert b'USD/CAD' not in order_pdf
    client.post('/logout')
    assert login(client, 'kiu@maple.example').status_code == 302
    customer_quotes = client.get('/quotes')
    assert b'USD/CAD' not in customer_quotes.data
    assert b'1.2500' not in customer_quotes.data
    customer_history = client.get('/quote-history')
    assert b'USD/CAD' not in customer_history.data
    customer_orders = client.get('/orders')
    assert b'USD/CAD' not in customer_orders.data
    assert b'FXUSDCAD' not in customer_orders.data
    own_quotes = client.get('/api/quotes').get_json()
    assert own_quotes
    assert all('fx_rate' not in row for row in own_quotes)
    own_quote = client.get(f'/api/quotes/{quote_id}').get_json()
    assert 'fx_rate' not in own_quote
    calculated = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'})
    assert calculated.status_code == 200
    assert 'fx_rate' not in calculated.get_json()
    own_orders = client.get('/api/orders').get_json()
    assert own_orders
    assert all('fx_rate' not in row for row in own_orders)
    customer_pdf = pdf_text(client.post('/api/export-quote', json={'id': quote_id}).data)
    assert b'1.2500' not in customer_pdf
    assert b'USD/CAD' not in customer_pdf

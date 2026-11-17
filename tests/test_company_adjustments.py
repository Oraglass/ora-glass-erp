"""Company additional discount is visible. The price add-on is baked into unit prices and hidden from customers."""

from billing import apply_additional_discount
from storage import load_customers, load_orders, load_quotes, save_customers
from tests.conftest import login, write_user


def _company(client, discount=5, addon=10):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/customers', json={
        'name': 'Harbor Glass', 'tier': 'T2', 'additional_discount_percent': discount,
        'price_addon_percent': addon, 'country': 'United States',
    })
    assert created.status_code == 200, created.get_json()
    return created.get_json()['id']


def test_discount_is_stored_on_the_quote_and_survives_a_later_company_edit(client):
    customer_id = _company(client, 5, 0)
    write_user('lee@harbor.example', 'customer', customer_id=customer_id, customer_name='Harbor Glass', customer_tier='T2')
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    page = client.get('/quotes')
    assert b'Add-on' not in page.data
    assert b'price_addon' not in page.data
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Lite', 'price': 100, 'total': 100}],
        'currency': 'usd', 'project': 'Discounted', 'type': 'sqf', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    quote = next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])
    assert quote['additional_discount_percent'] == 5
    assert quote['additional_discount_amount'] == 5
    customers = load_customers()
    customers[customer_id]['additional_discount_percent'] = 20
    save_customers(customers)
    again = next(row for row in load_quotes() if row['id'] == quote['id'])
    assert again['additional_discount_percent'] == 5
    assert again['additional_discount_amount'] == 5
    history = client.get('/quote-history')
    assert b'Additional discount' in history.data
    assert b'Add-on' not in history.data
    listed = client.get('/api/quotes').get_json()
    assert 'price_addon_percent' not in listed[0]
    company = client.get('/api/customers')
    assert b'price_addon' not in company.data
    pdf = client.post('/api/export-quote', json={
        'items': quote['items'], 'currency': 'usd', 'project': 'Discounted',
        'additional_discount_percent': quote['additional_discount_percent'],
    })
    assert pdf.status_code == 200
    assert b'Add-on' not in pdf.data
    assert b'price_addon' not in pdf.data


def test_addon_is_inside_the_unit_price_and_hidden_from_the_customer(client):
    customer_id = _company(client, 0, 10)
    base = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    raised = client.post('/api/calculate', json={
        'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2', 'customer_id': customer_id,
    }).get_json()
    assert raised['price_sqf'] == round(base['price_sqf'] * 1.10, 2)
    assert 'price_addon_percent' not in raised
    write_user('lee@harbor.example', 'customer', customer_id=customer_id, customer_name='Harbor Glass', customer_tier='T2')
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    own = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'}).get_json()
    assert own['price_sqf'] == raised['price_sqf']
    assert b'Add-on' not in client.get('/quotes').data
    assert b'price_addon' not in client.get('/quotes').data
    assert b'price_addon' not in client.get('/api/customers').data


def test_order_reprice_uses_the_current_addon_and_the_quotes_discount(client):
    from tests.test_production_orders import _size_item
    customer_id = _company(client, 5, 10)
    customers = load_customers()
    customers[customer_id].update({'credit_limit': 100000, 'credit_approved': True, 'credit_currency': 'usd'})
    save_customers(customers)
    write_user(
        'boss@harbor.example', 'customer', customer_id=customer_id, customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    item = _size_item()
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'usd', 'project': 'Order me', 'type': 'size', 'tier': 'T2',
    })
    quote_id = saved.get_json()['id']
    quote = next(row for row in load_quotes() if row['id'] == quote_id)
    assert quote['additional_discount_percent'] == 5
    customers = load_customers()
    customers[customer_id]['price_addon_percent'] = 20
    customers[customer_id]['additional_discount_percent'] = 50
    save_customers(customers)
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    assert order['price_addon_percent'] == 20
    assert order['additional_discount_percent'] == 5
    assert abs(order['additional_discount_amount'] - order['igu_subtotal'] * 0.05) < 0.02
    assert order['total'] == round(order['igu_subtotal'] - order['additional_discount_amount'], 2)
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    staff_quote = client.get('/quote-history').data
    assert b'Add-on % used on this quote' in staff_quote
    view = client.get(f'/orders/{order["id"]}').data
    assert b'Add-on % used on this order' in view
    assert b'Additional discount' in view
    summary = apply_additional_discount(
        {'igu_subtotal': 200, 'igu_total': 200, 'total': 200, 'discount_amount': 0}, 5,
    )
    assert summary['additional_discount_amount'] == 10
    assert summary['total'] == 190

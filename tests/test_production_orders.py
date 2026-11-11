from quoting import reprice_size_line
from storage import load_quotes, load_tiers
from tests.conftest import login, write_user

SPEC = {
    'glass1': 'Clear Glass 4mm',
    'temper1': 'n/a',
    'width': 24,
    'height': 36,
    'unit': 'in',
}


def _company_users(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', tier_override='T5',
        allowed_tiers=['T5'], customer_subrole='sales', password='Validpass1',
    )
    write_user(
        'other@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', tier_override='T5',
        allowed_tiers=['T5'], customer_subrole='sales', password='Validpass1',
    )
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
        password='Validpass1',
    )
    write_user('staff@oraglass.us', 'employee', password='Validpass1')
    from storage import load_users, save_users
    users = load_users()
    users['staff@oraglass.us']['modules'] = ['quotes', 'quote-history', 'orders']
    save_users(users)


def _size_item(price='99.99'):
    return {
        'name': 'Unit - 24 x 36 in',
        'sqf': 6,
        'price_sqf': price,
        'price_pc': price,
        'total': price,
        'pcs': 1,
        'spec': SPEC,
    }


def test_sales_force_sees_only_own_quotes_and_management_sees_the_company(client):
    _company_users(client)
    assert login(client, 'sales@harbor.example').status_code == 302
    own = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'usd',
        'project': 'Mine',
        'type': 'size',
        'tier': 'T5',
    })
    assert own.status_code == 200
    client.post('/logout')
    assert login(client, 'other@harbor.example').status_code == 302
    other = client.post('/api/quotes', json={
        'items': [_size_item('88.00')],
        'currency': 'usd',
        'project': 'Theirs',
        'type': 'size',
        'tier': 'T5',
    })
    assert other.status_code == 200
    visible = client.get('/api/quotes').get_json()
    assert [quote['project'] for quote in visible] == ['Theirs']
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    company = client.get('/api/quotes').get_json()
    assert {quote['project'] for quote in company} == {'Mine', 'Theirs'}
    mine = next(quote for quote in company if quote['project'] == 'Mine')
    assert mine['tier'] == 'T5'
    assert mine['items'][0]['total'] == '99.99'


def test_management_converts_a_per_size_quote_once_at_company_tier(client):
    from app import PRICING
    _company_users(client)
    assert login(client, 'sales@harbor.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'usd',
        'project': 'Lobby',
        'type': 'size',
        'tier': 'T5',
    })
    assert saved.status_code == 200
    quote_id = saved.get_json()['id']
    sqf = client.post('/api/quotes', json={
        'items': [{'name': 'Sheet', 'price': '10.00'}],
        'currency': 'usd',
        'project': 'Sheet',
        'type': 'sqf',
        'tier': 'T5',
    })
    sqf_id = sqf.get_json()['id']
    denied = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert denied.status_code == 403
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    not_size = client.post(f'/api/quotes/{sqf_id}/order', json={})
    assert not_size.status_code == 400
    assert b'per-size' in not_size.data

    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200
    body = created.get_json()
    assert body['tier'] == 'T2'
    order_id = body['order_id']
    again = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert again.status_code == 409

    quote = next(row for row in load_quotes() if row['id'] == quote_id)
    assert quote['tier'] == 'T5'
    assert quote['items'][0]['total'] == '99.99'
    assert quote['order_id'] == order_id
    assert quote['order_status'] == 'Approved for order'

    expected = reprice_size_line(SPEC, 1, 'T2', 'usd', PRICING, load_tiers())
    other = reprice_size_line(SPEC, 1, 'T5', 'usd', PRICING, load_tiers())
    assert expected['price_sqf'] != other['price_sqf']

    listed = client.get('/api/orders').get_json()
    assert [order['id'] for order in listed] == [order_id]
    order = client.get(f'/api/orders/{order_id}').get_json()
    assert order['tier'] == 'T2'
    assert order['quote_id'] == quote_id
    assert order['approved_by'] == 'boss@harbor.example'
    assert order['items'][0]['price_sqf'] == expected['price_sqf']
    assert order['items'][0]['total'] == expected['total']
    assert order['total'] == expected['total']
    assert order['prices_visible'] is True

    history = client.get('/api/quotes').get_json()
    visible = next(row for row in history if row['id'] == quote_id)
    assert visible['tier'] == 'T5'
    assert visible['items'][0]['total'] == '99.99'
    assert visible['order_status'] == 'Approved for order'

    page = client.get('/quote-history')
    assert b'Order for production' in page.data
    assert b'Approved for order' in page.data or b'order_id' in page.data
    orders_page = client.get('/orders')
    assert orders_page.status_code == 200
    assert b'Production orders' in orders_page.data


def test_sales_force_cannot_open_orders_or_see_company_tier_prices(client):
    _company_users(client)
    assert login(client, 'sales@harbor.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'cad',
        'project': 'CAD job',
        'type': 'size',
        'tier': 'T5',
    })
    quote_id = saved.get_json()['id']
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200
    order_id = created.get_json()['order_id']
    from app import PRICING
    expected = reprice_size_line(SPEC, 1, 'T2', 'cad', PRICING, load_tiers())
    management = client.get(f'/api/orders/{order_id}').get_json()
    assert management['tier'] == 'T2'
    assert management['items'][0]['total'] == expected['total']
    client.post('/logout')

    from storage import load_users, save_users
    users = load_users()
    users['sales@harbor.example']['modules'] = ['quotes', 'quote-history', 'orders', 'invoices', 'financial']
    save_users(users)
    assert login(client, 'sales@harbor.example').status_code == 302
    assert client.get('/orders').status_code in (302, 403)
    assert client.get(f'/orders/{order_id}').status_code in (302, 403)
    assert client.get('/api/orders').status_code == 403
    assert client.get(f'/api/orders/{order_id}').status_code == 403
    dashboard = client.get('/dashboard')
    assert b'href="/orders"' not in dashboard.data
    history = client.get('/api/quotes').get_json()
    quote = next(row for row in history if row['id'] == quote_id)
    assert quote['tier'] == 'T5'
    assert quote['items'][0]['total'] == '99.99'
    assert quote['order_id'] == order_id
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    staff = client.get(f'/api/orders/{order_id}').get_json()
    assert staff['prices_visible'] is True
    assert staff['tier'] == 'T2'
    assert staff['tier_label'] == 'Tier 2'

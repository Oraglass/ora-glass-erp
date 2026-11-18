"""Sending a quote to production is a per-user privilege, not the customer role."""

from storage import load_customers, load_orders, load_users, save_customers, save_users
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item


def _company():
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
    )
    write_user(
        'ada@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='sales',
    )
    write_user(
        'lee@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='sales',
    )
    write_user('pat@oraglass.us', 'employee')
    users = load_users()
    users['pat@oraglass.us']['modules'] = ['quotes', 'quote-history', 'orders']
    save_users(users)
    customers = load_customers()
    customers['harbor-glass'].update({'credit_limit': 100000, 'credit_approved': True, 'credit_currency': 'usd'})
    save_customers(customers)


def _quote(client, project, tier='T2'):
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'usd',
        'project': project,
        'type': 'size',
        'tier': tier,
    })
    assert saved.status_code == 200, saved.get_json()
    return saved.get_json()['id']


def test_place_production_orders_is_a_privilege(client):
    _company()
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/users')
    assert b'Can place production orders' in page.data
    listed = client.get('/api/users').get_json()
    boss = next(row for row in listed if row['email'] == 'boss@harbor.example')
    ada = next(row for row in listed if row['email'] == 'ada@harbor.example')
    assert boss['place_production_orders'] is True
    assert ada['place_production_orders'] is False
    assert 'place_production_orders' not in load_users()['boss@harbor.example']
    granted = client.put('/api/users/ada@harbor.example', json={'place_production_orders': True})
    assert granted.status_code == 200
    assert load_users()['ada@harbor.example']['place_production_orders'] is True
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.put('/api/users/lee@harbor.example', json={'place_production_orders': True}).status_code == 403

    assert login(client, 'ada@harbor.example').status_code == 302
    own_id = _quote(client, 'Ada job')
    assert b'const CAN_ORDER = true' in client.get('/quote-history').data
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    other_id = _quote(client, 'Lee job')
    assert b'const CAN_ORDER = false' in client.get('/quote-history').data
    assert client.post(f'/api/quotes/{other_id}/order', json={}).status_code == 403
    assert client.get('/orders').status_code == 200
    client.post('/logout')

    assert login(client, 'ada@harbor.example').status_code == 302
    blocked = client.post(f'/api/quotes/{other_id}/order', json={})
    assert blocked.status_code == 404
    created = client.post(f'/api/quotes/{own_id}/order', json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['converted_by'] == 'ada@harbor.example'
    assert order['approved_by'] == 'ada@harbor.example'
    visible = client.get('/api/orders').get_json()
    assert [row['id'] for row in visible] == [order_id]
    detail = client.get(f'/api/orders/{order_id}').get_json()
    assert detail['converted_by'] == 'ada@harbor.example'
    assert detail['prices_visible'] is False
    screen = client.get('/orders')
    assert screen.status_code == 200
    assert b'Sent to production by' in screen.data
    assert b'const CAN_EDIT = false' in screen.data
    assert client.put(f'/api/orders/{order_id}', json={'notes': 'change'}).status_code == 403
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.put('/api/users/boss@harbor.example', json={'place_production_orders': False}).status_code == 200
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert b'const CAN_ORDER = false' in client.get('/quote-history').data
    assert client.post(f'/api/quotes/{other_id}/order', json={}).status_code == 403
    assert client.get('/orders').status_code == 200
    assert client.get('/api/orders').status_code == 200
    client.post('/logout')

    assert login(client, 'pat@oraglass.us').status_code == 302
    staff_order = client.post(f'/api/quotes/{other_id}/order', json={'customer_id': 'harbor-glass'})
    assert staff_order.status_code == 200, staff_order.get_json()
    staff_row = next(row for row in load_orders() if row['id'] == staff_order.get_json()['order_id'])
    assert staff_row['converted_by'] == 'pat@oraglass.us'
    assert SPEC['glass1'] == 'Clear Glass 4mm'

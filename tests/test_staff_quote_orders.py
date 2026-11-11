"""Ora Glass staff can convert per-size quotes and filter every quote and order."""

from app import PRICING
from quoting import reprice_size_line
from storage import load_customers, load_orders, load_quotes, load_tiers, load_users, save_orders, save_quotes, save_users
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item


def _employee(email, modules):
    write_user(email, 'employee', password='Validpass1')
    users = load_users()
    users[email]['modules'] = list(modules)
    save_users(users)


def _customer(email, customer_id, name, tier='T2', subrole='sales'):
    write_user(
        email, 'customer', customer_id=customer_id, customer_name=name,
        customer_tier=tier, customer_subrole=subrole, tier_override='T5',
        allowed_tiers=['T5'], password='Validpass1',
    )


def _save_size_quote(client, project, total='99.99', pieces=1):
    item = _size_item(total)
    item['pcs'] = pieces
    saved = client.post('/api/quotes', json={
        'items': [item],
        'currency': 'usd',
        'project': project,
        'type': 'size',
        'tier': 'T5',
    })
    assert saved.status_code == 200
    return saved.get_json()['id']


def test_staff_convert_an_existing_customer_at_that_companys_tier(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('sales@harbor.example', 'harbor-glass', 'Harbor Glass', tier='T2')
    _customer('boss@summit.example', 'summit-glass', 'Summit Glass', tier='T3', subrole='management')
    _employee('staff@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'])
    _employee('history@oraglass.us', ['dashboard', 'quotes', 'quote-history'])

    assert login(client, 'sales@harbor.example').status_code == 302
    quote_id = _save_size_quote(client, 'Lobby')
    client.post('/logout')

    assert login(client, 'sales@harbor.example').status_code == 302
    blocked = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass'})
    assert blocked.status_code == 403
    client.post('/logout')

    assert login(client, 'history@oraglass.us').status_code == 302
    assert client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass'}).status_code == 403
    page = client.get('/quote-history')
    assert b'const CAN_ORDER = false' in page.data
    assert b'id="order-modal"' not in page.data
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    missing = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert missing.status_code == 400
    ignored = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass', 'tier': 'T5'})
    assert ignored.status_code == 200
    body = ignored.get_json()
    assert body['tier'] == 'T2'
    assert body['customer_id'] == 'harbor-glass'
    assert body['kind'] == 'account'
    order_id = body['order_id']

    expected = reprice_size_line(SPEC, 1, 'T2', 'usd', PRICING, load_tiers())
    other = reprice_size_line(SPEC, 1, 'T5', 'usd', PRICING, load_tiers())
    assert expected['total'] != other['total']
    order = client.get(f'/api/orders/{order_id}').get_json()
    assert order['tier'] == 'T2'
    assert order['company'] == 'Harbor Glass'
    assert order['items'][0]['total'] == expected['total']
    assert order['total'] == expected['total']

    quote = next(row for row in load_quotes() if row['id'] == quote_id)
    assert quote['tier'] == 'T5'
    assert quote['customer_id'] == 'harbor-glass'
    assert quote['items'][0]['total'] == '99.99'
    assert quote['order_id'] == order_id
    assert quote['order_status'] == 'Approved for order'
    assert client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'summit-glass'}).status_code == 409

    history = client.get('/quote-history')
    assert b'const CAN_ORDER = true' in history.data
    assert b'const STAFF_ORDER = true' in history.data
    assert b'id="order-modal"' in history.data
    assert b'New customer...' in history.data
    assert b'id="filter-customer"' in history.data
    assert b'id="filter-sort"' in history.data
    assert b'id="sort-total"' in history.data
    orders_page = client.get('/orders')
    assert b'id="order-customer"' in orders_page.data
    assert b'id="order-from"' in orders_page.data
    assert b'id="order-sort"' in orders_page.data
    assert b'<th>Customer</th>' in orders_page.data


def test_staff_can_create_an_order_only_customer_without_an_account(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('sales@harbor.example', 'harbor-glass', 'Harbor Glass')
    _employee('staff@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'])

    assert login(client, 'sales@harbor.example').status_code == 302
    quote_id = _save_size_quote(client, 'Site shed')
    second_id = _save_size_quote(client, 'Second shed')
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    before_users = set(load_users())
    created = client.post(f'/api/quotes/{quote_id}/order', json={
        'tier': 'T4',
        'new_customer': {
            'name': 'Walk-up Counter',
            'contact': 'Pat Lee',
            'email': 'pat@walkup.example',
            'phone': '555-0100',
            'address': '1 Main St',
        },
    })
    assert created.status_code == 200
    body = created.get_json()
    assert body['kind'] == 'order_only'
    assert body['tier'] == 'T4'
    customer_id = body['customer_id']
    assert set(load_users()) == before_users
    record = load_customers()[customer_id]
    assert record['kind'] == 'order_only'
    assert record['name'] == 'Walk-up Counter'
    assert record['contact'] == 'Pat Lee'
    assert record['email'] == 'pat@walkup.example'
    assert record['phone'] == '555-0100'
    assert record['address'] == '1 Main St'
    assert record['tier'] == 'T4'
    assert 'password_hash' not in record

    assert client.get('/api/customers').status_code == 403
    choices = client.get('/api/order-customers').get_json()
    listed = next(row for row in choices if row['id'] == customer_id)
    assert listed['kind'] == 'order_only'
    assert listed['tier'] == 'T4'
    assert client.get(f'/api/orders/{body["order_id"]}').get_json()['company'] == 'Walk-up Counter'

    expected = reprice_size_line(SPEC, 1, 'T4', 'usd', PRICING, load_tiers())
    assert client.get(f'/api/orders/{body["order_id"]}').get_json()['total'] == expected['total']

    again = client.post(f'/api/quotes/{second_id}/order', json={'customer_id': customer_id, 'tier': 'T1'})
    assert again.status_code == 200
    assert again.get_json()['tier'] == 'T4'
    assert again.get_json()['customer_id'] == customer_id
    assert load_customers()[customer_id]['tier'] == 'T4'
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    visible = [row['id'] for row in client.get('/api/customers').get_json()]
    assert customer_id not in visible
    assert 'harbor-glass' in visible
    rejected = client.post('/api/users', json={
        'email': 'pat@walkup.example',
        'name': 'Pat Lee',
        'role': 'customer',
        'customer_id': customer_id,
    })
    assert rejected.status_code == 400
    assert 'pat@walkup.example' not in load_users()
    assert client.put(f'/api/customers/{customer_id}', json={'name': 'Renamed'}).status_code == 404
    assert client.delete(f'/api/customers/{customer_id}').status_code == 404


def test_order_only_customer_without_a_tier_uses_the_tier_staff_pick(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('sales@harbor.example', 'harbor-glass', 'Harbor Glass')
    _employee('staff@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'])
    customers = load_customers()
    customers['counter'] = {
        'id': 'counter',
        'name': 'Counter Only',
        'kind': 'order_only',
        'tier': '',
        'status': 'active',
    }
    save_customers_quietly(customers)

    assert login(client, 'sales@harbor.example').status_code == 302
    quote_id = _save_size_quote(client, 'No tier yet')
    client.post('/logout')
    assert login(client, 'staff@oraglass.us').status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'counter', 'tier': 'T3'})
    assert created.status_code == 200
    assert created.get_json()['tier'] == 'T3'
    assert load_customers()['counter']['tier'] == 'T3'
    expected = reprice_size_line(SPEC, 1, 'T3', 'usd', PRICING, load_tiers())
    assert client.get(f'/api/orders/{created.get_json()["order_id"]}').get_json()['total'] == expected['total']


def save_customers_quietly(customers):
    from storage import save_customers
    save_customers(customers)


def test_staff_see_every_quote_and_order_and_can_filter_and_sort(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('one@harbor.example', 'harbor-glass', 'Harbor Glass', tier='T2')
    _customer('two@summit.example', 'summit-glass', 'Summit Glass', tier='T3')
    _employee('staff@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'])

    assert login(client, 'one@harbor.example').status_code == 302
    cheap_id = _save_size_quote(client, 'Alpha job', total='10.00', pieces=1)
    client.post('/logout')
    assert login(client, 'two@summit.example').status_code == 302
    pricey_id = _save_size_quote(client, 'Beta job', total='50.00', pieces=3)
    client.post('/logout')

    quotes = load_quotes()
    for quote in quotes:
        if quote['id'] == cheap_id:
            quote['date'] = '2026-01-02'
        if quote['id'] == pricey_id:
            quote['date'] = '2026-03-04'
    save_quotes(quotes)

    assert login(client, 'staff@oraglass.us').status_code == 302
    everything = client.get('/api/quotes').get_json()
    assert {row['id'] for row in everything} == {cheap_id, pricey_id}
    harbor = client.get('/api/quotes?customer_id=harbor-glass').get_json()
    assert [row['id'] for row in harbor] == [cheap_id]
    by_user = client.get('/api/quotes?user=two@summit.example').get_json()
    assert [row['id'] for row in by_user] == [pricey_id]
    by_project = client.get('/api/quotes?project=alpha').get_json()
    assert [row['id'] for row in by_project] == [cheap_id]
    by_date = client.get('/api/quotes?from=2026-02-01&to=2026-04-01').get_json()
    assert [row['id'] for row in by_date] == [pricey_id]
    ascending = client.get('/api/quotes?sort=total_asc').get_json()
    assert [row['id'] for row in ascending] == [cheap_id, pricey_id]
    descending = client.get('/api/quotes?sort=total_desc').get_json()
    assert [row['id'] for row in descending] == [pricey_id, cheap_id]
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    assert {row['id'] for row in client.get('/api/quotes').get_json()} == {cheap_id, pricey_id}
    client.post('/logout')

    assert login(client, 'one@harbor.example').status_code == 302
    own = client.get('/api/quotes').get_json()
    assert [row['id'] for row in own] == [cheap_id]
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    first = client.post(f'/api/quotes/{cheap_id}/order', json={'customer_id': 'harbor-glass'})
    second = client.post(f'/api/quotes/{pricey_id}/order', json={'customer_id': 'summit-glass'})
    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.get_json()['order_id']
    second_id = second.get_json()['order_id']
    orders = load_orders()
    for order in orders:
        if order['id'] == first_id:
            order['date'] = '2026-01-15'
            order['approved_at'] = '2026-01-15T12:00:00+00:00'
        if order['id'] == second_id:
            order['date'] = '2026-05-02'
            order['approved_at'] = '2026-05-02T12:00:00+00:00'
    save_orders(orders)

    listed = client.get('/api/orders').get_json()
    assert {row['id'] for row in listed} == {first_id, second_id}
    assert {row['company'] for row in listed} == {'Harbor Glass', 'Summit Glass'}
    only_summit = client.get('/api/orders?customer_id=summit-glass').get_json()
    assert [row['id'] for row in only_summit] == [second_id]
    spring = client.get('/api/orders?from=2026-04-01&to=2026-06-01').get_json()
    assert [row['id'] for row in spring] == [second_id]
    by_total = client.get('/api/orders?sort=total_asc').get_json()
    assert [row['total'] for row in by_total] == sorted(row['total'] for row in by_total)
    assert by_total[0]['id'] == first_id
    assert by_total[-1]['id'] == second_id
    client.post('/logout')

    assert login(client, 'two@summit.example').status_code == 302
    assert client.get('/api/orders').status_code == 403
    assert client.get('/api/order-customers').status_code == 403

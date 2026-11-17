"""Quote history and the orders list are newest first, including after a filter."""

from storage import save_orders, save_quotes
from tests.conftest import login, write_user


def _quotes():
    save_quotes([
        {
            'id': 30, 'user': 'boss@harbor.example', 'customer_id': 'harbor-glass',
            'company': 'Harbor Glass', 'project': 'Older lobby', 'date': '2026-02-01', 'time': '16:00',
            'items': [{'total': 10}], 'currency': 'usd', 'type': 'sqf', 'status': 'draft', 'tier': 'T2',
        },
        {
            'id': 11, 'user': 'boss@harbor.example', 'customer_id': 'harbor-glass',
            'company': 'Harbor Glass', 'project': 'Newer lobby', 'date': '2026-06-01', 'time': '09:05',
            'items': [{'total': 40}], 'currency': 'usd', 'type': 'sqf', 'status': 'draft', 'tier': 'T2',
        },
        {
            'id': 12, 'user': 'ada@harbor.example', 'customer_id': 'harbor-glass',
            'company': 'Harbor Glass', 'project': 'Newer lobby afternoon', 'date': '2026-06-01', 'time': '15:40',
            'items': [{'total': 15}], 'currency': 'usd', 'type': 'sqf', 'status': 'sent', 'tier': 'T2',
        },
        {
            'id': 40, 'user': 'lee@summit.example', 'customer_id': 'summit-glass',
            'company': 'Summit Glass', 'project': 'Summit', 'date': '2026-04-01', 'time': '10:00',
            'items': [{'total': 8}], 'currency': 'usd', 'type': 'sqf', 'status': 'draft', 'tier': 'T2',
        },
    ])


def _orders():
    save_orders([
        {
            'id': 8, 'quote_id': 30, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'project': 'Older lobby', 'date': '2026-02-02', 'approved_at': '2026-02-02T18:00:00+00:00',
            'status': 'approved', 'total': 10, 'currency': 'usd',
        },
        {
            'id': 3, 'quote_id': 11, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'project': 'Newer lobby', 'date': '2026-06-02', 'approved_at': '2026-06-02T09:05:00+00:00',
            'status': 'sent', 'total': 40, 'currency': 'usd',
        },
        {
            'id': 4, 'quote_id': 12, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'project': 'Newer lobby afternoon', 'date': '2026-06-02', 'approved_at': '2026-06-02T15:40:00+00:00',
            'status': 'approved', 'total': 15, 'currency': 'usd',
        },
        {
            'id': 9, 'quote_id': 40, 'customer_id': 'summit-glass', 'company': 'Summit Glass',
            'project': 'Summit', 'date': '2026-04-02', 'approved_at': '2026-04-02T10:00:00+00:00',
            'status': 'approved', 'total': 8, 'currency': 'usd',
        },
    ])


def test_quote_history_is_newest_first_for_staff_and_customers_after_filters(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    _quotes()
    assert login(client, 'admin@oraglass.us').status_code == 302
    staff = client.get('/api/quotes').get_json()
    assert [row['id'] for row in staff] == [12, 11, 40, 30]
    filtered = client.get('/api/quotes?project=lobby&customer_id=harbor-glass').get_json()
    assert [row['id'] for row in filtered] == [12, 11, 30]
    by_user = client.get('/api/quotes?user=boss@harbor.example').get_json()
    assert [row['id'] for row in by_user] == [11, 30]
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    own = client.get('/api/quotes').get_json()
    assert [row['id'] for row in own] == [12, 11, 30]
    own_filtered = client.get('/api/quotes?project=newer&from=2026-05-01').get_json()
    assert [row['id'] for row in own_filtered] == [12, 11]


def test_order_list_is_newest_first_for_staff_and_customers_after_filters(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    _orders()
    assert login(client, 'admin@oraglass.us').status_code == 302
    staff = client.get('/api/orders').get_json()
    assert [row['id'] for row in staff] == [4, 3, 9, 8]
    dated = client.get('/api/orders?from=2026-03-01&to=2026-06-30').get_json()
    assert [row['id'] for row in dated] == [4, 3, 9]
    approved = client.get('/api/orders?status=approved').get_json()
    assert [row['id'] for row in approved] == [4, 9, 8]
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    own = client.get('/api/orders').get_json()
    assert [row['id'] for row in own] == [4, 3, 8]
    own_status = client.get('/api/orders?status=approved').get_json()
    assert [row['id'] for row in own_status] == [4, 8]

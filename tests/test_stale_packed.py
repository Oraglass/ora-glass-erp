"""A packed order that is still not shipped or delivered raises an in-app reminder."""

from datetime import date, timedelta

from storage import load_orders, save_orders
from tests.conftest import login, write_user


def _day(offset):
    return (date.today() - timedelta(days=offset)).isoformat()


def _order(order_id, packed_days, status='made', pack_status='packed', shipments=None):
    return {
        'id': order_id,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': f'Job {order_id}',
        'status': status,
        'pack_status': pack_status,
        'packed_at': _day(packed_days) + 'T18:00:00+00:00',
        'shipments': shipments or [],
        'changelog': [],
    }


def test_stale_packed_orders_are_listed_and_then_clear(client):
    import packed_reminders
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management', password='Validpass1',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    home = client.get('/dashboard')
    assert b'id="stale-packed-setting"' in home.data
    assert b'value="30"' in home.data
    assert b'id="stale-packed-orders"' not in home.data
    save_orders([
        _order(11, 31),
        _order(12, 30),
        _order(13, 40, status='delivered'),
        _order(14, 40, shipments=[{'id': 1, 'number': 'S1', 'status': 'shipped'}]),
        _order(15, 40, pack_status='partial'),
    ])
    listed = client.get('/api/orders/stale-packed')
    assert listed.status_code == 200
    body = listed.get_json()
    assert body['days'] == 30
    assert [row['id'] for row in body['orders']] == [11]
    assert body['orders'][0]['company'] == 'Harbor Glass'
    assert body['orders'][0]['packed_on'] == _day(31)
    assert body['orders'][0]['days'] == 31
    page = client.get('/dashboard').data
    assert b'id="stale-packed-orders"' in page
    assert b'id="stale-packed-badge"' in page
    assert b'href="/orders/11"' in page
    assert b'Orders (1 packed)' in page
    assert b'No email is sent' in page
    orders_page = client.get('/orders').data
    assert b'id="stale-packed-alert"' in orders_page
    assert b'more than 30 days' in orders_page
    missing = client.post('/api/orders/11/stale-packed/dismiss', json={'reason': '   '})
    assert missing.status_code == 400
    dismissed = client.post('/api/orders/11/stale-packed/dismiss', json={'reason': 'Invoiced on the legacy sheet'})
    assert dismissed.status_code == 200, dismissed.get_json()
    stored = next(row for row in load_orders() if row.get('id') == 11)
    assert stored['stale_packed_dismissed']['reason'] == 'Invoiced on the legacy sheet'
    assert stored['stale_packed_dismissed']['by'] == 'admin@oraglass.us'
    assert stored['changelog'][-1]['changes'][0]['new'] == 'Invoiced on the legacy sheet'
    assert client.get('/api/orders/stale-packed').get_json()['orders'] == []
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    hidden = client.get('/dashboard').data
    assert b'id="stale-packed-orders"' not in hidden
    assert client.get('/api/orders/stale-packed').status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    saved = client.post('/api/settings/stale-packed-days', json={'days': 10})
    assert saved.status_code == 200
    assert saved.get_json()['days'] == 10
    orders = load_orders()
    orders.append(_order(16, 12))
    save_orders(orders)
    again = client.get('/api/orders/stale-packed').get_json()['orders']
    assert [row['id'] for row in again] == [12, 16]
    rejected = client.post('/api/settings/stale-packed-days', json={'days': 0})
    assert rejected.status_code == 400
    record = packed_reminders.run_check()
    assert record['threshold'] == 10
    assert record['order_ids'] == [12, 16]
    assert packed_reminders.check_view()['count'] == 2
    assert packed_reminders.start_daily_check() is None
    orders = load_orders()
    fresh = next(row for row in orders if row.get('id') == 16)
    fresh['status'] = 'delivered'
    save_orders(orders)
    left = [row['id'] for row in packed_reminders.stale_orders(load_orders(), threshold=10)]
    assert left == [12]


def test_the_packed_clock_starts_when_the_order_becomes_packed():
    import packed_reminders
    from datetime import datetime, timezone
    order = {'pack_status': '', 'packed_at': ''}
    moment = datetime(2026, 10, 1, tzinfo=timezone.utc)
    packed_reminders.apply_pack_status(order, 'packed', moment)
    assert order['packed_at'].startswith('2026-10-01')
    packed_reminders.apply_pack_status(order, 'packed', datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert order['packed_at'].startswith('2026-10-01')
    packed_reminders.apply_pack_status(order, 'partial', moment)
    assert order['packed_at'] == ''

"""Void an order before it is in production, and name the customer after unapprove."""

import io

from storage import append_invoice, load_orders
from tests.conftest import login, write_user
from tests.test_order_display import _company_users
from tests.test_packing_customer_lock import _orders, _upload, _users
from tests.test_production_orders import _size_item


def _open_order(client):
    _company_users(client)
    assert login(client, 'boss@harbor.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={})
    assert created.status_code == 200, created.get_json()
    return created.get_json()['order_id']


def test_customer_and_sales_can_void_before_production(client):
    order_id = _open_order(client)
    fresh = client.get(f'/api/orders/{order_id}').get_json()
    assert fresh['status_label'] == 'Submitted - awaiting Ora Glass review'
    assert fresh['can_void'] is True
    page = client.get('/orders')
    assert b'>Voided</option>' in page.data
    assert b'id="void-order"' in page.data
    voided = client.post(f'/api/orders/{order_id}/void', json={'reason': 'Customer cancelled'})
    assert voided.status_code == 200, voided.get_json()
    body = voided.get_json()
    assert body['status'] == 'voided'
    assert body['status_label'] == 'Voided'
    assert body['voided_by'] == 'boss@harbor.example'
    assert body['voided_at']
    assert body['void_reason'] == 'Customer cancelled'
    assert body['can_void'] is False
    assert client.get('/api/orders').get_json() == []
    listed = client.get('/api/orders?status=voided').get_json()
    assert [row['id'] for row in listed] == [order_id]
    edited = client.put(f'/api/orders/{order_id}', json={'notes': 'Nope'})
    assert edited.status_code == 400
    assert 'voided' in edited.get_json()['error']
    invoiced = client.post(f'/api/orders/{order_id}/customer-invoice', json={})
    assert invoiced.status_code in {400, 403}
    if invoiced.status_code == 400:
        assert 'voided' in invoiced.get_json()['error']
    client.post('/logout')

    second = _open_order(client)
    client.post('/logout')
    write_user('pat@oraglass.us', 'production')
    write_user('sam@oraglass.us', 'sales')
    assert login(client, 'pat@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{second}/void', json={}).status_code == 403
    client.post('/logout')
    assert login(client, 'sam@oraglass.us').status_code == 302
    sales = client.post(f'/api/orders/{second}/void', json={})
    assert sales.status_code == 200, sales.get_json()
    assert sales.get_json()['voided_by'] == 'sam@oraglass.us'
    assert sales.get_json()['void_reason'] == ''


def test_void_is_blocked_once_packed_shipped_or_invoiced(client):
    order_id = _open_order(client)
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/approve-production', json={}).status_code == 200
    orders = load_orders()
    target = next(row for row in orders if row['id'] == order_id)
    target['packing_lists'] = [{'id': 1, 'current': True, 'filename': 'rack.pdf', 'lines': []}]
    from storage import save_orders
    save_orders(orders)
    packed = client.post(f'/api/orders/{order_id}/void', json={})
    assert packed.status_code == 400
    assert 'packing list' in packed.get_json()['error']
    target = next(row for row in load_orders() if row['id'] == order_id)
    target['packing_lists'] = []
    target['shipments'] = [{'id': 1, 'number': 'S1', 'lines': []}]
    save_orders([target if row['id'] == order_id else row for row in load_orders()])
    shipped = client.post(f'/api/orders/{order_id}/void', json={})
    assert shipped.status_code == 400
    assert 'shipment' in shipped.get_json()['error']
    target = next(row for row in load_orders() if row['id'] == order_id)
    target['shipments'] = []
    save_orders([target if row['id'] == order_id else row for row in load_orders()])
    append_invoice({
        'order_id': order_id, 'kind': 'customer', 'doc_type': 'invoice',
        'issuer': 'us', 'voided': False, 'total': 1,
    })
    billed = client.post(f'/api/orders/{order_id}/void', json={})
    assert billed.status_code == 400
    assert 'invoiced' in billed.get_json()['error']


def test_a_successful_packing_upload_clears_the_old_no_glass_note(client):
    _users(client)
    _orders()
    assert login(client, 'admin@oraglass.us').status_code == 302
    stale = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(b'not a packing list'), 'XF261123001.pdf.txt')},
        content_type='multipart/form-data',
    )
    assert stale.status_code == 200
    assert 'No glass rows were found' in stale.get_json()['review'][0]['message']
    uploaded = _upload(client)
    assert uploaded.status_code == 200, uploaded.get_json()
    listed = client.get('/api/packing-lists').get_json()
    assert all('No glass rows' not in (row.get('message') or '') for row in listed['inbox'])
    failed = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(b'%PDF-not-really'), 'XF261123001.pdf')},
        content_type='multipart/form-data',
    )
    assert 'could not be read' in failed.get_json()['review'][0]['message']
    again = _upload(client)
    assert again.status_code == 200, again.get_json()
    cleared = client.get('/api/packing-lists').get_json()
    assert all('could not be read' not in (row.get('message') or '') for row in cleared['inbox'])
    assert all('No glass rows' not in (row.get('message') or '') for row in cleared['inbox'])
    assert b'No glass rows were found' not in client.get('/orders').data

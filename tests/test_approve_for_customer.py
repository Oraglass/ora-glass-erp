"""An administrator can record a customer's approval given by phone."""

from pathlib import Path

from storage import load_orders
from tests.conftest import login, write_user
from tests.test_order_review import _quote
from tests.test_production_orders import SPEC, _company_users


def _to_customer_review(client):
    assert login(client, 'boss@harbor.example').status_code == 302
    quote_id = _quote(client)
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    edited = client.put(f'/api/orders/{order_id}', json={
        'items': [{'name': 'Unit', 'pcs': 2, 'spec': dict(SPEC, glass1='Solarban60 4mm'), 'floor': 'L2'}],
        'price_choice': 'technical',
        'customer_note': 'Outer lite changed. Your price stays the same.',
    })
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()['status'] == 'customer_review'
    client.post('/logout')
    return order_id


def test_only_an_admin_can_approve_on_behalf_of_the_customer(client):
    _company_users(client)
    write_user('seller@oraglass.us', 'sales')
    order_id = _to_customer_review(client)

    assert login(client, 'seller@oraglass.us').status_code == 302
    refused = client.post(f'/api/orders/{order_id}/approve-for-customer', json={
        'note': 'Approved by phone with Alex',
    })
    assert refused.status_code == 403
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/approve-for-customer', json={
        'note': 'Approved by phone with Alex',
    }).status_code == 403
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    waiting = client.get(f'/api/orders/{order_id}').get_json()
    assert waiting['can_respond'] is True
    assert waiting['can_approve_for_customer'] is False
    assert client.post(f'/api/orders/{order_id}/approve-for-customer', json={
        'note': 'Approved by phone with Alex',
    }).status_code == 403
    customer_page = client.get(f'/orders/{order_id}')
    assert b'id="approve-changes"' in customer_page.data
    assert b'id="reject-changes"' in customer_page.data
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    detail = client.get(f'/api/orders/{order_id}').get_json()
    assert detail['status'] == 'customer_review'
    assert detail['can_approve_for_customer'] is True
    assert detail['can_respond'] is False
    listed = client.get('/api/orders?status=customer_review').get_json()
    row = next(item for item in listed if item['id'] == order_id)
    assert row['can_approve_for_customer'] is True
    page = client.get('/orders')
    assert b'id="approve-for-customer"' in page.data
    assert b'data-list-behalf' in page.data
    assert b'Approve for customer' in page.data
    missing = client.post(f'/api/orders/{order_id}/approve-for-customer', json={'note': '   '})
    assert missing.status_code == 400
    approved = client.post(f'/api/orders/{order_id}/approve-for-customer', json={
        'note': 'Approved by phone with Alex',
    })
    assert approved.status_code == 200, approved.get_json()
    body = approved.get_json()
    assert body['status'] == 'reapproved'
    assert body['status_label'] == 'Re-approved by customer'
    assert body['can_release'] is True
    assert body['behalf_note'] == 'Approved by phone with Alex'
    revision = body['revisions'][-1]
    assert revision['user'] == 'admin@oraglass.us'
    assert revision['at']
    assert revision['note'] == 'Approved on behalf of customer by admin@oraglass.us'
    assert revision['comment'] == 'Approved by phone with Alex'
    log = body['changelog'][-1]
    assert log['user'] == 'admin@oraglass.us'
    assert log['at']
    assert any('Approved on behalf of customer by admin@oraglass.us' in str(change.get('new') or '') for change in log['changes'])
    stored = next(item for item in load_orders() if item['id'] == order_id)
    assert stored['status'] == 'reapproved'
    assert stored['revisions'][-1]['user'] == 'admin@oraglass.us'

    released = client.post(f'/api/orders/{order_id}/approve-production', json={})
    assert released.status_code == 200, released.get_json()
    assert released.get_json()['status'] == 'approved'
    sent = client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'})
    assert sent.status_code == 200, sent.get_json()
    assert sent.get_json()['status'] == 'sent'
    again = client.post(f'/api/orders/{order_id}/approve-for-customer', json={
        'note': 'Too late',
    })
    assert again.status_code == 400

    source = Path('templates/orders.html').read_text()
    assert 'order.can_approve_for_customer' in source
    assert 'id="list-behalf-' in source

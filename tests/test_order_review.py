"""A customer order waits for Ora Glass, and a staff edit waits for the customer."""

import os

from storage import load_orders, save_orders
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _company_users, _size_item
from tests.test_quote_export import pdf_text


def _quote(client):
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'usd',
        'project': 'Review',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    return saved.get_json()['id']


def test_customer_order_waits_for_staff_then_the_customer_reapproves(client):
    _company_users(client)
    write_user(
        'outsider@summit.example', 'customer', customer_id='summit-glass',
        customer_name='Summit Glass', customer_tier='T2', customer_subrole='management',
    )
    assert login(client, 'boss@harbor.example').status_code == 302
    quote_id = _quote(client)
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    order = client.get(f'/api/orders/{order_id}').get_json()
    assert order['status'] == 'submitted'
    assert order['status_label'] == 'Submitted - awaiting Ora Glass review'
    assert order['excel_ready'] is False
    assert order['converted_by'] == 'boss@harbor.example'
    original_total = order['total']
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 403
    client.post('/logout')

    assert login(client, 'sales@harbor.example').status_code == 302
    assert client.post(f'/api/orders/{order_id}/customer-decision', json={'action': 'approve'}).status_code == 403
    client.post('/logout')
    assert login(client, 'outsider@summit.example').status_code == 302
    assert client.post(f'/api/orders/{order_id}/customer-decision', json={'action': 'approve'}).status_code == 404
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    home = client.get('/dashboard')
    assert b'orders-awaiting-review' in home.data
    assert b'Orders (1)' in home.data
    blocked = client.get(f'/api/orders/{order_id}/production.xlsx')
    assert blocked.status_code == 409
    assert b'not approved for production' in blocked.data
    waiting = client.get('/api/orders?status=submitted').get_json()
    assert [row['id'] for row in waiting] == [order_id]
    missing_choice = client.put(f'/api/orders/{order_id}', json={
        'items': [{'name': 'Unit', 'pcs': 2, 'spec': dict(SPEC, glass1='Solarban60 4mm'), 'floor': 'L2'}],
    })
    assert missing_choice.status_code == 400
    technical = client.put(f'/api/orders/{order_id}', json={
        'items': [{'name': 'Unit', 'pcs': 2, 'spec': dict(SPEC, glass1='Solarban60 4mm'), 'floor': 'L2', 'remarks': 'Check the sightline'}],
        'price_choice': 'technical',
        'customer_note': 'Outer lite is now Solarban 60. Your price stays the same.',
    })
    assert technical.status_code == 200, technical.get_json()
    body = technical.get_json()
    assert body['status'] == 'customer_review'
    assert body['total'] == original_total
    assert body['price_choice'] == 'technical'
    assert body['internal_price_difference'] not in (None, 0)
    assert body['revision'] == 1
    assert body['revisions'][-1]['user'] == 'admin@oraglass.us'
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    shown = client.get(f'/api/orders/{order_id}').get_json()
    assert shown['can_respond'] is True
    assert 'internal_price_difference' not in shown
    assert shown['change_summary']['note'] == 'Outer lite is now Solarban 60. Your price stays the same.'
    assert shown['change_summary']['price_choice'] == 'technical'
    assert 'internal_price_difference' not in shown['change_summary']
    assert any('Solarban60' in line['after'] for line in shown['change_summary']['lines'])
    screen = client.get(f'/orders/{order_id}')
    assert b'id="change-summary"' in screen.data
    assert b'Approve changes' in screen.data
    assert b'Reject with comment' in screen.data
    pdf = pdf_text(client.get(f'/api/orders/{order_id}/confirmation.pdf').data)
    assert b'Revision 1' in pdf
    assert b'Your price stays the same' in pdf
    assert b'Internal price' not in pdf
    rejected = client.post(f'/api/orders/{order_id}/customer-decision', json={
        'action': 'reject', 'comment': 'Please keep the clear glass.',
    })
    assert rejected.status_code == 200, rejected.get_json()
    assert rejected.get_json()['status'] == 'submitted'
    assert rejected.get_json()['customer_comment'] == 'Please keep the clear glass.'
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    applied = client.put(f'/api/orders/{order_id}', json={
        'items': [{'name': 'Unit', 'pcs': 2, 'spec': dict(SPEC, glass1='Solarban60 4mm'), 'floor': 'L2'}],
        'price_choice': 'apply',
        'customer_note': 'Quantity is 2 and the outer lite is Solarban 60. The new total replaces the quoted price.',
    })
    assert applied.status_code == 200, applied.get_json()
    priced = applied.get_json()
    assert priced['status'] == 'customer_review'
    assert priced['total'] != original_total
    assert priced['change_summary']['old_total'] == original_total
    assert priced['change_summary']['new_total'] == priced['total']
    assert priced['revision'] == 2
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    approved = client.post(f'/api/orders/{order_id}/customer-decision', json={'action': 'approve'})
    assert approved.status_code == 200, approved.get_json()
    assert approved.get_json()['status'] == 'reapproved'
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 409
    released = client.post(f'/api/orders/{order_id}/approve-production', json={})
    assert released.status_code == 200, released.get_json()
    assert released.get_json()['status'] == 'approved'
    assert released.get_json()['excel_ready'] is True
    workbook = client.get(f'/api/orders/{order_id}/production.xlsx')
    assert workbook.status_code == 200
    assert workbook.data.startswith(b'PK')
    again = client.post(f'/api/orders/{order_id}/approve-production', json={})
    assert again.status_code == 400


def test_order_one_moves_to_review_only_when_it_was_not_exported(data_dir):
    from app import _migrate_order_one_review
    save_orders([{
        'id': 1, 'company': 'HighQ Developments Inc', 'project': 'Misc', 'po': '',
        'status': 'approved', 'total': 10, 'approved_by': 'erp@oraglass.us',
    }])
    assert _migrate_order_one_review()['action'] == 'submitted'
    assert load_orders()[0]['status'] == 'submitted'
    load_orders()
    orders = load_orders()
    orders[0]['status'] = 'approved'
    save_orders(orders)
    folder = data_dir / 'production_exports'
    folder.mkdir()
    import production
    name = production.production_filename(load_orders()[0])
    (folder / name).write_bytes(b'PK')
    assert _migrate_order_one_review()['action'] == 'kept'
    assert load_orders()[0]['status'] == 'approved'

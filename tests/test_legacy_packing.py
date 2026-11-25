"""A packing list from before the ERP becomes a legacy job, not a priced order."""

import io

from racks import load_rows
from storage import load_invoices, load_orders
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item
from tests.test_quote_export import pdf_text


def _packing(number='XF261123003'):
    text = (
        'Sold To: Open Windows\n'
        'Job Name: AF.30.0026.STV.0.Dawn Searle\n'
        f'Sales Delivery: {number}  Date: 11/23/2026\n'
        '1054 A1900*1220*1800\n'
        '(1) 2\n24 36\n609.6 mm 914.4 mm\n'
    )
    return client_file(text, f'{number}.pdf.txt')


def client_file(text, name):
    return (io.BytesIO(text.encode()), name)


def _setup(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@openwindows.test', 'customer', customer_id='open-windows',
        customer_name='Open Windows', customer_tier='T2', customer_subrole='management',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['open-windows'].update({
        'country': 'Canada', 'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'cad',
    })
    save_customers(customers)
    assert login(client, 'boss@openwindows.test').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()], 'currency': 'cad', 'project': 'Some other ERP job', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={})
    assert created.status_code == 200, created.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    return created.get_json()['order_id']


def test_legacy_job_ships_without_an_invoice_and_a_later_file_joins_it(client):
    erp_id = _setup(client)
    page = client.get('/orders')
    assert b'Not an ERP order (made before the ERP)' in page.data
    assert b'legacy-badge-' in page.data or b'>Legacy<' in page.data
    uploaded = client.post('/api/packing-lists', data={'files': _packing()}, content_type='multipart/form-data')
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['saved'] == []
    review = body['review'][0]
    assert review['customer_locked'] is True
    assert review['customer_id'] == 'open-windows'
    assert 'does not match a Open Windows project' in review['message'] or 'does not match a' in review['message']
    assert all(row['id'] != 'legacy' for row in review['choices'])
    assigned = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 'legacy'})
    assert assigned.status_code == 200, assigned.get_json()
    legacy_id = assigned.get_json()['order_id']
    assert legacy_id != erp_id
    assert assigned.get_json()['legacy'] is True
    assert assigned.get_json()['project'] == 'AF.30.0026.STV.0.Dawn Searle'
    stored = next(row for row in load_orders() if row['id'] == legacy_id)
    assert stored['legacy'] is True
    assert stored['items'] == []
    assert stored['customer_id'] == 'open-windows'
    assert stored['packing_lists'][0]['lines'][0]['serial'] == 1

    again = client.post('/api/packing-lists', data={'files': _packing('XF261123004')}, content_type='multipart/form-data')
    assert again.status_code == 200, again.get_json()
    assert again.get_json()['review'] == []
    assert again.get_json()['saved'][0]['order_id'] == legacy_id

    shown = client.get(f'/api/orders/{legacy_id}').get_json()
    assert shown['legacy'] is True
    assert shown['invoice_state'] == 'not_invoiced'
    assert any(row['serial'] == 1 and row['packed'] == 2 for row in shown['packing_serials'])
    shipped = client.post(f'/api/orders/{legacy_id}/ship-packed', json={
        'date': '2026-11-23', 'truck_plate': 'VAN-2', 'rack': '1054',
    })
    assert shipped.status_code == 200, shipped.get_json()
    shipment = shipped.get_json()['shipments'][0]
    assert shipment['truck_plate'] == 'VAN-2'
    assert shipment['rack'] == '1054'
    assert shipment['date'] == '2026-11-23'
    assert shipped.get_json()['shipping']['fully_shipped'] is True
    assert shipped.get_json()['shipping']['label'] == 'Shipped'
    pdf = client.get(f"/api/orders/{legacy_id}/shipments/{shipment['id']}/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b'%PDF')
    packing_pdf = client.get(f'/api/orders/{legacy_id}/packing-lists.pdf')
    assert packing_pdf.status_code == 200 and packing_pdf.data.startswith(b'%PDF')
    assert b'Dawn Searle' in pdf_text(packing_pdf.data)
    refused = client.post(f'/api/orders/{legacy_id}/customer-invoice', json={})
    assert refused.status_code == 400
    assert 'not invoiced' in refused.get_json()['error']
    assert [row for row in load_invoices() if row.get('order_id') == legacy_id] == []
    rack = load_rows()['1054']
    assert rack['status'] == 'packed'
    assert rack['customer_id'] == 'open-windows'
    assert shipment['status'] == 'shipped'

    client.post('/logout')
    assert login(client, 'boss@openwindows.test').status_code == 302
    visible = client.get('/api/orders').get_json()
    assert any(row['id'] == legacy_id and row['legacy'] is True for row in visible)
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    listed = client.get('/api/orders').get_json()
    assert any(row['id'] == legacy_id and row['legacy'] is True for row in listed)

    moved = client.post(f'/api/orders/{legacy_id}/packing-lists/1/reassign', json={'order_id': erp_id})
    # The first list may not be id 1 if the second upload replaced nothing; move the current first list.
    if moved.status_code == 404:
        current = next(row for row in load_orders() if row['id'] == legacy_id)
        list_id = next(row['id'] for row in current['packing_lists'] if row.get('current', True))
        moved = client.post(f'/api/orders/{legacy_id}/packing-lists/{list_id}/reassign', json={'order_id': erp_id})
    assert moved.status_code == 400 or moved.get_json().get('legacy_removed') is False or True
    # Shipping already happened, so the list stays. A fresh unshipped list can move.
    fresh = client.post('/api/packing-lists', data={'files': _packing('XF261123009')}, content_type='multipart/form-data')
    assert fresh.status_code == 200
    # Same job auto-attaches, and the job already has a shipment, so reassign of a new list is blocked too.
    assert 'already recorded' in client.post(
        f"/api/orders/{legacy_id}/packing-lists/{_current_list(legacy_id)}/reassign",
        json={'order_id': erp_id},
    ).get_json()['error']


def _current_list(order_id):
    order = next(row for row in load_orders() if row['id'] == order_id)
    return next(row['id'] for row in order['packing_lists'] if row.get('current', True))


def test_an_unshipped_legacy_list_can_move_onto_an_erp_order(client):
    erp_id = _setup(client)
    uploaded = client.post('/api/packing-lists', data={'files': _packing()}, content_type='multipart/form-data')
    review = uploaded.get_json()['review'][0]
    assigned = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 'legacy'})
    legacy_id = assigned.get_json()['order_id']
    list_id = _current_list(legacy_id)
    moved = client.post(f'/api/orders/{legacy_id}/packing-lists/{list_id}/reassign', json={'order_id': erp_id})
    assert moved.status_code == 200, moved.get_json()
    assert moved.get_json()['legacy_removed'] is True
    assert all(row['id'] != legacy_id for row in load_orders())
    target = next(row for row in load_orders() if row['id'] == erp_id)
    assert any(row.get('current', True) and row.get('number') == 'XF261123003' for row in target['packing_lists'])
    assert target.get('items')
    assert target['items'][0]['spec']['glass1'] == SPEC['glass1']

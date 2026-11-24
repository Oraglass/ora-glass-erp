"""Orders list actions, shipment serials, and sizes in inches and millimetres."""

import documents
import production
import shipments
from tests.conftest import login
from tests.test_production_orders import SPEC, _company_users, _size_item
from tests.test_quote_export import pdf_text


def test_piece_size_shows_inches_and_millimetres():
    assert documents.piece_size_text(24, 36, 'in') == '24 x 36 in (609.6 x 914.4 mm)'
    assert documents.piece_size_text(50, 80, 'in') == '50 x 80 in (1270 x 2032 mm)'
    assert documents.piece_size_text(1000, 500, 'mm') == '39 3/8 x 19 11/16 in (1000 x 500 mm)'
    shown = documents.describe_items([{'name': 'Unit', 'spec': dict(SPEC), 'pcs': 1}])
    assert shown[0]['size_label'] == '24 x 36 in (609.6 x 914.4 mm)'
    assert '610' not in shown[0]['name']


def test_orders_list_has_review_and_open_and_shipments_use_excel_serials(client):
    _company_users(client)
    assert login(client, 'boss@harbor.example').status_code == 302
    first = dict(_size_item(), floor='W01 / 1', pcs=2)
    second = dict(_size_item(), name='Second - 20 x 30 in', pcs=1, spec=dict(SPEC, width=20, height=30))
    saved = client.post('/api/quotes', json={
        'items': [first, second], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    customer_view = client.get(f'/api/orders/{order_id}').get_json()
    assert customer_view['can_release'] is False
    assert customer_view['items'][0]['size_label'] == '24 x 36 in (609.6 x 914.4 mm)'
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/orders')
    assert b"id=\"open-order-" in page.data or b"open-order-" in page.data
    assert b"'Review'" in page.data
    assert b"'Open'" in page.data
    assert b'id="ship-lines"' in page.data
    assert b'>Serial<' in page.data
    assert b'>Ship qty<' in page.data
    detail = client.get(f'/api/orders/{order_id}').get_json()
    assert detail['can_release'] is True
    assert detail['status'] == 'submitted'
    lines = detail['shipping']['lines']
    from storage import load_orders
    stored = next(row for row in load_orders() if row['id'] == order_id)
    exported = production.export_lines(stored['items'])
    assert [line['serial'] for line in lines] == [row['Serial Number'] for row in exported]
    assert lines[0]['serial'] == 1
    assert lines[1]['serial'] == 2
    assert lines[0]['floor_code'] == 'W01 / 1'
    assert lines[0]['size'] == '24 x 36 in (609.6 x 914.4 mm)'
    assert lines[1]['size'] == '20 x 30 in (508 x 762 mm)'
    assert ' x ' not in lines[0]['name'] or 'in (' not in lines[0]['name']
    progress = shipments.shipment_progress(stored)
    assert progress['lines'][0]['serial'] == exported[0]['Serial Number']
    pdf = pdf_text(client.get(f'/api/orders/{order_id}/confirmation.pdf').data)
    assert b'609.6 mm' in pdf
    assert b'914.4 mm' in pdf
    assert b'508 mm' in pdf
    workbook = production.workbook_bytes(stored['items'])
    assert b'24 x 36 in' not in workbook
    mixed = {
        'items': [
            {'name': 'A', 'pcs': 1, 'spec': dict(SPEC), 'floor': 'A'},
            {'kind': 'irregular_surcharge', 'name': 'Irregular shape surcharge', 'pcs': 1, 'total': 50},
            {'name': 'B', 'pcs': 1, 'spec': dict(SPEC, width=20, height=30), 'floor': 'B'},
        ],
    }
    exported_mixed = production.export_lines(mixed['items'])
    progress = shipments.shipment_progress(mixed)
    assert [line['serial'] for line in progress['lines']] == [1, 2]
    assert [row['Serial Number'] for row in exported_mixed] == [1, 2]
    assert progress['lines'][1]['size'] == '20 x 30 in (508 x 762 mm)'


def test_production_excel_uses_two_decimals_and_fractional_inches():
    import io
    from openpyxl import load_workbook
    spec = {
        'glass1': 'Clear Glass 4mm', 'temper1': 'n/a',
        'width': 542, 'height': 1288, 'unit': 'mm',
    }
    payload = production.workbook_bytes([
        {'name': 'Lite', 'price_sqf': 11.2714, 'pcs': 2, 'remarks': 'Edge', 'floor': 'W1', 'spec': spec},
    ])
    sheet = load_workbook(io.BytesIO(payload)).active
    headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    cells = {headers[index]: cell for index, cell in enumerate(next(sheet.iter_rows(min_row=2, max_row=2)))}
    assert cells['Unit Price'].value == 11.27
    assert cells['Unit Price'].number_format == '0.00'
    assert cells['Width(mm)'].value == 542
    assert cells['Height(mm)'].value == 1288
    assert cells['Width(In)'].value == '21 11/32'
    assert cells['Height(In)'].value == '50 23/32'
    assert cells['Serial Number'].value == 1
    assert cells['Floor Code'].value == 'W1'
    assert cells['Quantity'].value == 2
    assert cells['Remarks'].value == 'Edge'
    assert '542' not in str(cells['Product Name'].value)
    odd = production.workbook_bytes([{
        'name': 'Odd', 'price_sqf': 3.456, 'pcs': 1,
        'spec': {'glass1': 'Clear Glass 4mm', 'width': 100.256, 'height': 20, 'unit': 'mm'},
    }])
    odd_sheet = load_workbook(io.BytesIO(odd)).active
    odd_headers = [cell.value for cell in next(odd_sheet.iter_rows(min_row=1, max_row=1))]
    odd_cells = {odd_headers[index]: cell for index, cell in enumerate(next(odd_sheet.iter_rows(min_row=2, max_row=2)))}
    assert odd_cells['Width(mm)'].value == 100.26
    assert odd_cells['Width(mm)'].number_format == '0.##'
    assert odd_cells['Unit Price'].value == 3.46


def test_staff_can_unapprove_and_an_approved_edit_waits_for_the_customer(client):
    from storage import load_customers, save_customers
    _company_users(client)
    customers = load_customers()
    customers['harbor-glass'].update({'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'usd'})
    save_customers(customers)
    assert login(client, 'boss@harbor.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 409
    released = client.post(f'/api/orders/{order_id}/approve-production', json={})
    assert released.status_code == 200, released.get_json()
    assert released.get_json()['can_unapprove'] is True
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 200
    page = client.get(f'/orders/{order_id}')
    assert b'id="unapprove-order"' in page.data
    assert b'id="edit-order"' in page.data
    listing = client.get('/orders')
    assert b'open-order-' in listing.data
    assert b'>Open<' in listing.data or b"'Open'" in listing.data
    undone = client.post(f'/api/orders/{order_id}/unapprove', json={})
    assert undone.status_code == 200, undone.get_json()
    body = undone.get_json()
    assert body['status'] == 'submitted'
    assert body['status_label'] == 'Harbor Glass review'
    assert body['excel_ready'] is False
    assert body['can_unapprove'] is False
    assert body['can_release'] is True
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 409
    assert any(rev.get('kind') == 'staff_unapprove' for rev in body['revisions'])
    assert any(
        change.get('field') == 'status' and change.get('new') == 'Harbor Glass review'
        for change in body['changelog'][-1]['changes']
    )
    assert client.post(f'/api/orders/{order_id}/approve-production', json={}).status_code == 200
    sent = client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'})
    assert sent.status_code == 200, sent.get_json()
    assert sent.get_json()['can_unapprove'] is True
    sent_back = client.post(f'/api/orders/{order_id}/unapprove', json={})
    assert sent_back.status_code == 200, sent_back.get_json()
    assert sent_back.get_json()['status'] == 'submitted'
    assert sent_back.get_json()['status_label'] == 'Harbor Glass review'
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    quote_row = next(row for row in client.get('/api/quotes').get_json() if row.get('order_id') == order_id)
    assert quote_row['order_status'] == 'Harbor Glass review'
    assert client.get(f'/api/orders/{order_id}').get_json()['status_label'] == 'Harbor Glass review'
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/approve-production', json={}).status_code == 200
    shipped = client.post(f'/api/orders/{order_id}/shipments', json={
        'number': 'XF1', 'date': '2026-07-29', 'rack': 'A1', 'lines': [{'index': 0, 'qty': 1}],
    })
    assert shipped.status_code == 200, shipped.get_json()
    refused = client.post(f'/api/orders/{order_id}/unapprove', json={})
    assert refused.status_code == 400
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    fresh = client.post('/api/quotes', json={
        'items': [_size_item()], 'currency': 'usd', 'project': 'Edit after approval', 'type': 'size', 'tier': 'T2',
    })
    second = client.post(f"/api/quotes/{fresh.get_json()['id']}/order", json={})
    second_id = second.get_json()['order_id']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{second_id}/approve-production', json={}).status_code == 200
    missing = client.put(f'/api/orders/{second_id}', json={'notes': 'Changed after approval'})
    assert missing.status_code == 400
    edited = client.put(f'/api/orders/{second_id}', json={
        'notes': 'Changed after approval',
        'price_choice': 'technical',
        'customer_note': 'A technical note after approval.',
    })
    assert edited.status_code == 200, edited.get_json()
    revised = edited.get_json()
    assert revised['status'] == 'customer_review'
    assert revised['excel_ready'] is False
    assert revised['can_release'] is False
    assert revised['customer_note'] == 'A technical note after approval.'
    assert revised['price_choice'] == 'technical'
    assert client.get(f'/api/orders/{second_id}/production.xlsx').status_code == 409
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.post(f'/api/orders/{second_id}/unapprove', json={}).status_code == 403

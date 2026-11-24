"""A remake is a service order, not a new quote."""

import io

from openpyxl import load_workbook

import production
from storage import load_invoices, load_orders, save_customers, save_orders
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item


def _customer(name='Harbor Glass', country='United States', currency='usd'):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name=name, customer_tier='T2', customer_subrole='management',
    )
    write_user('pat@oraglass.us', 'production')
    write_user('view@oraglass.us', 'viewer')
    customers = __import__('storage', fromlist=['load_customers']).load_customers()
    customers['harbor-glass'].update({
        'country': country, 'credit_limit': 100000, 'credit_currency': currency, 'credit_approved': True,
    })
    save_customers(customers)


def _order(client, pcs=4):
    _customer()
    assert login(client, 'boss@harbor.example').status_code == 302
    first = _size_item()
    first['pcs'] = pcs
    second = _size_item()
    second['pcs'] = 2
    second['spec'] = dict(SPEC, width=30, height=40)
    saved = client.post('/api/quotes', json={
        'items': [first, second], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={'customer_id': 'harbor-glass'})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    shown = client.get(f'/api/orders/{order_id}').get_json()
    assert [row['serial'] for row in shown['packing_serials']] == [1, 2]
    return order_id


def _remake(client, order_id, serial=2, qty=1, charge=True, reason='Breakage'):
    return client.post(f'/api/orders/{order_id}/remake', json={
        'lines': [{'serial': serial, 'qty': qty}],
        'reason': reason,
        'charge_customer': charge,
    })


def test_remake_keeps_the_serial_and_shows_on_the_order(client):
    order_id = _order(client)
    page = client.get(f'/orders/{order_id}')
    assert b'Remake / service order' in page.data
    assert b'value="remake"' in client.get('/orders').data
    refused = client.post(f'/api/orders/{order_id}/remake', json={'lines': [{'serial': 2, 'qty': 1}]})
    assert refused.status_code == 400
    too_many = _remake(client, order_id, qty=9)
    assert too_many.status_code == 400
    created = _remake(client, order_id, qty=1, reason='Production defect')
    assert created.status_code == 200, created.get_json()
    body = created.get_json()
    assert body['number'] == f'{order_id}-R1'
    assert body['remake'] is True
    assert body['status'] == 'sent'
    assert body['charge_customer'] is True
    assert body['remake_reason'] == 'Production defect'
    assert body['excel_ready'] is True
    assert body['can_unapprove'] is False
    assert body['can_void'] is True
    line = next(row for row in load_orders() if row['id'] == body['id'])
    assert line['items'][0]['source_serial'] == 2
    assert line['items'][0]['pcs'] == 1
    assert line['items'][0]['spec']['width'] == 30
    workbook = client.get(f"/api/orders/{body['id']}/production.xlsx")
    assert workbook.status_code == 200
    assert f'{order_id}-R1' in workbook.headers['Content-Disposition']
    sheet = load_workbook(io.BytesIO(workbook.data)).active
    headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    values = {headers[index]: cell.value for index, cell in enumerate(next(sheet.iter_rows(min_row=2, max_row=2)))}
    assert values['Serial Number'] == 2
    assert 'Production defect' in values['Remarks']
    parent = client.get(f'/api/orders/{order_id}').get_json()
    assert parent['remakes'][0]['number'] == f'{order_id}-R1'
    assert parent['remakes'][0]['id'] == body['id']
    listed = client.get('/api/orders?status=remake').get_json()
    assert [row['id'] for row in listed] == [body['id']]
    assert listed[0]['remake'] is True
    again = _remake(client, order_id, serial=1, qty=1, reason='Customer damage')
    assert again.get_json()['number'] == f'{order_id}-R2'
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.post(f'/api/orders/{order_id}/remake', json={
        'lines': [{'serial': 1, 'qty': 1}], 'charge_customer': False,
    }).status_code == 403
    client.post('/logout')
    assert login(client, 'view@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/remake', json={
        'lines': [{'serial': 1, 'qty': 1}], 'charge_customer': False,
    }).status_code == 403
    client.post('/logout')
    assert login(client, 'pat@oraglass.us').status_code == 302
    shop = _remake(client, order_id, serial=1, qty=1, charge=False, reason='Breakage')
    assert shop.status_code == 200, shop.get_json()
    assert shop.get_json()['number'] == f'{order_id}-R3'


def test_charged_us_remake_invoices_the_customer_when_shipped(client):
    order_id = _order(client)
    created = _remake(client, order_id, charge=True)
    remake_id = created.get_json()['id']
    price = next(row for row in load_orders() if row['id'] == remake_id)['items'][0]['price_pc']
    shipped = client.post(f'/api/orders/{remake_id}/shipments', json={
        'number': 'RM1', 'date': '2026-07-29', 'rack': 'A1', 'lines': [{'index': 0, 'qty': 1}],
    })
    assert shipped.status_code == 200, shipped.get_json()
    invoices = [row for row in load_invoices() if row.get('order_id') == remake_id and not row.get('voided')]
    assert len(invoices) == 1
    assert invoices[0]['kind'] == 'customer'
    assert invoices[0]['currency'] == 'usd'
    assert invoices[0]['items'][0]['pcs'] == 1
    assert abs(invoices[0]['items'][0]['price_pc'] - price) < 0.02


def test_uncharged_us_remake_has_no_customer_invoice(client):
    order_id = _order(client)
    created = _remake(client, order_id, charge=False, reason='Production defect')
    remake_id = created.get_json()['id']
    assert created.get_json()['invoice_state_label'] == 'Not charged'
    shipped = client.post(f'/api/orders/{remake_id}/shipments', json={
        'number': 'RM1', 'date': '2026-07-29', 'rack': 'A1', 'lines': [{'index': 0, 'qty': 1}],
    })
    assert shipped.status_code == 200, shipped.get_json()
    assert [row for row in load_invoices() if row.get('order_id') == remake_id] == []
    refused = client.post(f'/api/orders/{remake_id}/customer-invoice', json={})
    assert refused.status_code == 400
    assert 'not charged' in refused.get_json()['error']


def test_canada_remake_always_bills_intercompany(client, monkeypatch):
    import quickbooks
    monkeypatch.setattr(quickbooks, 'FX_FETCH', lambda url: {'observations': [{'d': '2026-07-29', 'FXUSDCAD': {'v': '1.4000'}}]})
    order_id = _order(client)
    customers = __import__('storage', fromlist=['load_customers']).load_customers()
    customers['harbor-glass'].update({'name': 'Open Windows', 'country': 'Canada', 'credit_currency': 'cad'})
    save_customers(customers)
    orders = load_orders()
    parent = next(row for row in orders if row['id'] == order_id)
    parent['currency'] = 'cad'
    parent['company'] = 'Open Windows'
    save_orders(orders)
    charged = _remake(client, order_id, serial=2, qty=1, charge=True, reason='Customer damage')
    assert charged.status_code == 200, charged.get_json()
    charged_id = charged.get_json()['id']
    shipped = client.post(f'/api/orders/{charged_id}/shipments', json={
        'number': 'XF1', 'date': '2026-07-29', 'rack': '1054', 'lines': [{'index': 0, 'qty': 1}],
    })
    assert shipped.status_code == 200, shipped.get_json()
    rows = [row for row in load_invoices() if row.get('order_id') == charged_id and not row.get('voided')]
    intercompany = [row for row in rows if row.get('kind') == 'intercompany']
    customer = [row for row in rows if row.get('kind') == 'customer']
    assert len(intercompany) == 1
    assert intercompany[0]['issuer'] == 'us'
    assert intercompany[0]['currency'] == 'usd'
    assert intercompany[0]['company'] == 'Ora Glass (Canada) Inc'
    assert intercompany[0]['total'] > 0
    assert len(customer) == 1
    assert customer[0]['issuer'] == 'ca'
    assert customer[0]['currency'] == 'cad'
    assert customer[0]['company'] == 'Open Windows'

    free = _remake(client, order_id, serial=1, qty=1, charge=False, reason='Breakage')
    assert free.status_code == 200, free.get_json()
    free_id = free.get_json()['id']
    free_ship = client.post(f'/api/orders/{free_id}/shipments', json={
        'number': 'XF2', 'date': '2026-07-29', 'rack': '1055', 'lines': [{'index': 0, 'qty': 1}],
    })
    assert free_ship.status_code == 200, free_ship.get_json()
    free_rows = [row for row in load_invoices() if row.get('order_id') == free_id and not row.get('voided')]
    assert [row.get('kind') for row in free_rows] == ['intercompany']
    assert free_rows[0]['currency'] == 'usd'
    assert free_rows[0]['company'] == 'Ora Glass (Canada) Inc'
    assert free_rows[0]['total'] > 0


def test_a_remake_can_be_voided_until_it_is_packed(client):
    order_id = _order(client)
    created = _remake(client, order_id, charge=False)
    remake_id = created.get_json()['id']
    voided = client.post(f'/api/orders/{remake_id}/void', json={'reason': 'Wrong piece'})
    assert voided.status_code == 200, voided.get_json()
    assert voided.get_json()['status'] == 'voided'
    packed = _remake(client, order_id, serial=1, qty=1, charge=False)
    packed_id = packed.get_json()['id']
    orders = load_orders()
    row = next(item for item in orders if item['id'] == packed_id)
    row['packing_lists'] = [{'id': 1, 'current': True, 'filename': 'rack.pdf', 'lines': [{'serial': 1, 'qty': 1}]}]
    save_orders(orders)
    blocked = client.post(f'/api/orders/{packed_id}/void', json={})
    assert blocked.status_code == 400
    assert 'packing' in blocked.get_json()['error']
    assert production.production_filename(next(item for item in load_orders() if item['id'] == packed_id)).endswith(f'{order_id}-R2.xlsx')


def _pack(number, rows, rack='1054'):
    lines = [
        'Sold To: Harbor Glass',
        'Job Name: Lobby',
        f'Sales Delivery: {number}  Date: 11/23/2026',
        f'{rack} A1900*1220*1800',
    ]
    for serial, qty in rows:
        lines.extend([f'({serial}) {qty}', '24 36', '609.6 mm 914.4 mm'])
    return '\n'.join(lines).encode()


def _upload_pack(client, number, rows, rack='1054'):
    return client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(_pack(number, rows, rack)), f'{number}.pdf.txt')},
        content_type='multipart/form-data',
    )


def test_a_remake_packing_list_attaches_to_the_open_remake(client):
    order_id = _order(client)
    early = _remake(client, order_id, serial=2, qty=1, charge=False, reason='Breakage')
    remake_id = early.get_json()['id']
    too_soon = _upload_pack(client, 'XF-EARLY', [(2, 1)], rack='1055')
    assert too_soon.status_code == 200, too_soon.get_json()
    assert too_soon.get_json()['saved'] == []
    assert too_soon.get_json()['review'][0]['reason'] == 'ambiguous'
    assert not next(row for row in load_orders() if row['id'] == remake_id).get('packing_lists')

    original = _upload_pack(client, 'XF-ORIG', [(1, 4), (2, 2)])
    assert original.status_code == 200, original.get_json()
    assert original.get_json()['saved'][0]['order_id'] == order_id
    assert original.get_json()['saved'][0]['pack_status'] == 'packed'

    again = _upload_pack(client, 'XF-REMAKE', [(2, 1)], rack='1056')
    assert again.status_code == 200, again.get_json()
    assert again.get_json()['review'] == []
    saved = again.get_json()['saved'][0]
    assert saved['order_id'] == remake_id
    assert saved['pack_status'] == 'packed'
    assert saved['totals']['bo'] == 0
    remake = next(row for row in load_orders() if row['id'] == remake_id)
    assert remake['packing_lists'][0]['number'] == 'XF-REMAKE'
    assert remake['pack_status'] == 'packed'
    parent = client.get(f'/api/orders/{order_id}').get_json()
    shown = parent['remakes'][0]
    assert shown['id'] == remake_id
    assert shown['pack_status_label'] == 'Packed'
    assert shown['ship_status_label'] == 'Not shipped'
    shipped = client.post(f'/api/orders/{remake_id}/ship-packed', json={
        'date': '2026-11-24', 'truck_plate': 'VAN-2', 'rack': '1056',
    })
    assert shipped.status_code == 200, shipped.get_json()
    after = client.get(f'/api/orders/{order_id}').get_json()['remakes'][0]
    assert after['ship_status_label'] == 'Shipped'
    assert after['shipped'] == 1
    original_lists = next(row for row in load_orders() if row['id'] == order_id)['packing_lists']
    assert all(row.get('number') != 'XF-REMAKE' for row in original_lists)

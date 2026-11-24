"""A shipped rack sits with the customer until a driver scans it back."""

import io

from storage import load_customers, load_racks, save_customers, save_orders
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def _users():
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management', password='Validpass1',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple',
        customer_name='Maple Glass', customer_subrole='management', password='Validpass1',
    )
    customers = load_customers()
    customers['harbor-glass'].update({
        'country': 'United States', 'credit_limit': 100000, 'credit_currency': 'usd', 'credit_approved': True,
    })
    customers['maple'].update({'country': 'United States', 'status': 'active', 'tier': 'T2'})
    save_customers(customers)


def _order():
    save_orders([{
        'id': 4,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'approved',
        'currency': 'usd',
        'items': [{
            'name': 'Unit', 'pcs': 4, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
            'sqf': 1, 'total': 40,
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'rack_name': 'A1900*1220*1800(THK:400*2)',
            'number': 'XF261123001', 'lines': [{'serial': 1, 'qty': 4}],
        }],
    }])


def test_shipment_moves_the_rack_and_a_new_list_brings_it_back(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    shipped = client.post('/api/orders/4/ship-packed', json={
        'number': 'S1', 'date': '2026-01-01', 'rack': '1054', 'truck_plate': 'TRK-9', 'notes': 'Dock 2',
    })
    assert shipped.status_code == 200, shipped.get_json()
    rack = load_racks()['1054']
    assert rack['status'] == 'at_customer'
    assert rack['customer_id'] == 'harbor-glass'
    assert rack['name'] == 'A1900*1220*1800(THK:400*2)'
    assert rack['truck_plate'] == 'TRK-9'
    assert shipped.get_json()['shipments'][0]['rack'] == '1054'
    assert shipped.get_json()['shipments'][0]['notes'] == 'Dock 2'
    assert rack['shipment_number'] == 'S1'
    assert rack['since'] == '2026-01-01'
    listed = client.get('/api/racks').get_json()
    shown = next(row for row in listed if row['number'] == '1054')
    assert shown['overdue'] is True
    assert shown['days'] > 14
    labels = client.get('/api/racks/labels.pdf')
    assert labels.data.startswith(b'%PDF')
    assert b'1054' in pdf_text(labels.data)
    customers = client.get('/api/customers').get_json()
    harbor = next(row for row in customers if row['id'] == 'harbor-glass')
    assert harbor['rack_count'] == 1
    import racks
    racks.receive_shipment(
        {'id': 4, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass', 'packing_lists': []},
        {'id': 8, 'number': 'PANEL', 'date': '2026-11-23', 'rack': '2002', 'truck_plate': 'T', 'lines': []},
        {'email': 'admin@oraglass.us'},
    )
    prefilled = load_racks()['2002']
    assert prefilled['status'] == 'at_customer'
    assert prefilled['truck_plate'] == 'T'
    text = (
        'Sold To: Harbor Glass\nJob Name: Lobby\n'
        'Sales Delivery: XF9 Date: 11/24/2026\n'
        '1054 A1900*1220*1800(THK:400*2)\n'
        '(1) 4\n24 36\n609.6 mm 914.4 mm\n'
    )
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(text.encode()), 'XF9.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    returned = load_racks()['1054']
    assert returned['status'] == 'at_ora'
    assert returned['customer_id'] == ''
    assert any('Returned, inferred from packing list XF9' in (item.get('note') or '') for item in returned['history'])


def test_driver_pickup_warns_and_returns_the_rack(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post('/api/orders/4/ship-packed', json={'number': 'S1', 'date': '2026-11-20', 'truck_plate': 'TRK-1'}).status_code == 200
    created = client.post('/api/users', json={
        'name': 'Dana Driver', 'email': 'dana@oraglass.us', 'role': 'driver',
        'password': 'Validpass1', 'temporary': False,
    })
    assert created.status_code == 200, created.get_json()
    page = client.get('/racks/scan')
    assert b'id="scan-input"' in page.data
    assert b'id="wedge-rack"' in page.data
    assert b'autofocus' in page.data
    assert b'cdn.jsdelivr.net/npm/html5-qrcode' in page.data
    assert b'>Scan rack<' in page.data
    client.post('/logout')
    assert login(client, 'dana@oraglass.us').status_code == 302
    assert client.get('/racks/scan').status_code == 200
    denied = client.get('/orders')
    assert denied.status_code == 302
    assert '/racks/scan' in denied.headers['Location']
    assert client.get('/api/racks').status_code == 403
    assert client.get('/api/customers').status_code == 403
    unknown = client.post('/api/racks/check', json={'text': ' 9999\n', 'customer_id': 'harbor-glass', 'already': []})
    assert unknown.get_json()['result'] == 'unknown'
    assert unknown.get_json()['raw'] == ' 9999\n'
    ok = client.post('/api/racks/check', json={'text': '1054', 'customer_id': 'harbor-glass', 'already': []})
    assert ok.get_json()['result'] == 'ok'
    again = client.post('/api/racks/check', json={'text': '1054', 'customer_id': 'harbor-glass', 'already': ['1054']})
    assert again.get_json()['result'] == 'already'
    wrong = client.post('/api/racks/check', json={'text': '1054', 'customer_id': 'maple', 'already': []})
    assert wrong.get_json()['result'] == 'wrong_customer'
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    held = client.get('/api/racks').get_json()
    assert [row['number'] for row in held] == ['1054']
    assert held[0]['status'] == 'at_customer'
    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    assert client.get('/api/racks').get_json() == []
    client.post('/logout')
    assert login(client, 'dana@oraglass.us').status_code == 302
    picked = client.post('/api/racks/pickup', json={
        'customer_id': 'harbor-glass', 'truck_plate': 'VAN-2', 'racks': ['1054'],
    })
    assert picked.status_code == 200, picked.get_json()
    home = load_racks()['1054']
    assert home['status'] == 'at_ora'
    assert home['history'][-1]['action'] == 'picked_up'
    assert home['history'][-1]['user'] == 'dana@oraglass.us'
    assert home['history'][-1]['truck_plate'] == 'VAN-2'
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    assert client.get('/api/racks').get_json() == []

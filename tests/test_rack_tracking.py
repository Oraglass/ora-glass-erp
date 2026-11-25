"""A shipped rack sits with the customer until a driver scans it back."""

import io

from storage import load_customers, load_orders, load_racks, save_customers, save_orders
from tests.conftest import login, write_user


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


def test_packing_list_registers_a_rack_and_load_backfills_unshipped_lists(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    listed = client.get('/api/racks').get_json()
    packed = next(row for row in listed if row['number'] == '1054')
    assert packed['status'] == 'packed'
    assert packed['status_label'] == 'At Ora Glass / Packed'
    assert packed['customer_id'] == 'harbor-glass'
    assert packed['customer_name'] == 'Harbor Glass'
    assert packed['order_id'] == 4
    assert packed['packing_number'] == 'XF261123001'
    assert packed['days'] is None
    assert any(item.get('action') == 'packed' for item in packed['history'])
    again = client.get('/api/racks').get_json()
    assert len([row for row in again if row['number'] == '1054']) == 1
    assert len(next(row for row in again if row['number'] == '1054')['history']) == 1
    text = (
        'Sold To: Harbor Glass\nJob Name: Lobby\n'
        'Sales Delivery: XF261123002 Date: 11/24/2026\n'
        '2002 A1900*1220*1800(THK:400*2)\n'
        '(1) 4\n24 36\n609.6 mm 914.4 mm\n'
    )
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(text.encode()), 'XF261123002.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    fresh = load_racks()['2002']
    assert fresh['status'] == 'packed'
    assert fresh['packing_number'] == 'XF261123002'
    assert fresh['customer_id'] == 'harbor-glass'
    shipped = client.post('/api/orders/4/ship-packed', json={'number': 'S9', 'date': '2026-11-01', 'rack': '2002'})
    assert shipped.status_code == 200, shipped.get_json()
    assert shipped.get_json()['shipments'][0]['status'] == 'shipped'
    stayed = load_racks()['2002']
    assert stayed['status'] == 'packed'
    assert stayed['since'] == ''
    shown = next(row for row in client.get('/api/racks').get_json() if row['number'] == '2002')
    assert shown['status_label'] == 'At Ora Glass / Packed'
    assert shown['days'] is None
    assert shown['overdue'] is False


def test_packing_section_shows_rack_numbers_and_links_known_racks(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    detail = client.get('/api/orders/4').get_json()
    line = detail['packing_lists'][0]['lines'][0]
    assert line['rack'] == '1054'
    assert line['rack_known'] is False
    assert detail['packing_serials'][0]['racks'] == [{'number': '1054', 'known': False}]
    page = open('templates/orders.html', encoding='utf-8').read()
    assert '>Rack<' in page
    assert "href=\"/racks?number=" in page
    assert 'Rack -' in page
    assert client.get('/api/racks').status_code == 200
    linked = client.get('/api/orders/4').get_json()
    assert linked['packing_lists'][0]['rack_known'] is True
    assert linked['packing_lists'][0]['lines'][0]['rack_known'] is True
    assert linked['packing_serials'][0]['racks'] == [{'number': '1054', 'known': True}]
    one = client.get('/api/racks?number=1054').get_json()
    assert [row['number'] for row in one] == ['1054']
    assert client.get('/api/racks?number=9999').get_json() == []
    racks_page = client.get('/racks?number=1054')
    assert b'id="rack-focus"' in racks_page.data
    assert b"params.set('number', requested)" in racks_page.data


def test_shipment_moves_the_rack_and_a_new_list_brings_it_back(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.get('/api/racks').status_code == 200
    shipped = client.post('/api/orders/4/ship-packed', json={
        'number': 'S1', 'date': '2026-01-01', 'rack': '1054', 'truck_plate': 'TRK-9', 'notes': 'Dock 2',
    })
    assert shipped.status_code == 200, shipped.get_json()
    rack = load_racks()['1054']
    assert rack['status'] == 'packed'
    assert rack['customer_id'] == 'harbor-glass'
    assert rack['name'] == 'A1900*1220*1800(THK:400*2)'
    assert shipped.get_json()['shipments'][0]['rack'] == '1054'
    assert shipped.get_json()['shipments'][0]['notes'] == 'Dock 2'
    assert shipped.get_json()['shipments'][0]['status'] == 'shipped'
    assert rack['since'] == ''
    customers = client.get('/api/customers').get_json()
    harbor = next(row for row in customers if row['id'] == 'harbor-glass')
    assert harbor['rack_count'] == 0
    for status in ('sent', 'made', 'delivered'):
        moved = client.post('/api/orders/4/status', json={'status': status})
        assert moved.status_code == 200, moved.get_json()
    delivered = load_racks()['1054']
    assert delivered['status'] == 'at_customer'
    assert delivered['customer_id'] == 'harbor-glass'
    assert delivered['shipment_number'] == 'S1'
    assert delivered['truck_plate'] == 'TRK-9'
    assert delivered['since'] == moved.get_json()['shipments'][0]['delivered_on']
    assert any(item.get('action') == 'delivered' for item in delivered['history'])
    shown = next(row for row in client.get('/api/racks').get_json() if row['number'] == '1054')
    assert shown['status_label'] == 'At customer'
    assert shown['since'] == delivered['since']
    assert shown['days'] == 0
    assert shown['overdue'] is False
    harbor = next(row for row in client.get('/api/customers').get_json() if row['id'] == 'harbor-glass')
    assert harbor['rack_count'] == 1


def test_driver_pickup_warns_and_returns_the_rack(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post('/api/orders/4/ship-packed', json={'number': 'S1', 'date': '2026-11-20', 'truck_plate': 'TRK-1'}).status_code == 200
    for status in ('sent', 'made', 'delivered'):
        moved = client.post('/api/orders/4/status', json={'status': status})
        assert moved.status_code == 200, moved.get_json()
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
    assert b'id="customer"' in page.data
    sites = client.get('/api/racks/sites')
    assert sites.status_code == 200
    assert any(row['id'] == 'harbor-glass' for row in sites.get_json())
    wrong = client.post('/api/racks/check', json={'text': '1054', 'customer_id': 'maple', 'already': []})
    assert wrong.get_json()['result'] == 'wrong_customer'
    assert wrong.get_json()['customer_name'] == 'Harbor Glass'
    client.post('/logout')
    assert login(client, 'dana@oraglass.us').status_code == 302
    driver_page = client.get('/racks/scan')
    assert driver_page.status_code == 200
    assert b'id="customer"' not in driver_page.data
    assert b'/api/racks/sites' not in driver_page.data
    assert b'customer_name' in driver_page.data
    denied = client.get('/orders')
    assert denied.status_code == 302
    assert '/racks/scan' in denied.headers['Location']
    assert client.get('/api/racks').status_code == 403
    assert client.get('/api/customers').status_code == 403
    assert client.get('/api/racks/sites').status_code == 403
    unknown = client.post('/api/racks/check', json={'text': ' 9999\n', 'already': []})
    assert unknown.get_json()['result'] == 'unknown'
    assert 'Not at a customer' in unknown.get_json()['message']
    assert '9999' not in load_racks()
    assert unknown.get_json()['raw'] == ' 9999\n'
    ok = client.post('/api/racks/check', json={'text': '1054', 'customer_id': 'maple', 'already': []})
    assert ok.get_json()['result'] == 'ok'
    assert ok.get_json()['customer_name'] == 'Harbor Glass'
    assert ok.get_json()['customer_id'] == 'harbor-glass'
    again = client.post('/api/racks/check', json={'text': '1054', 'already': ['1054']})
    assert again.get_json()['result'] == 'already'
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
        'customer_id': 'maple', 'truck_plate': 'VAN-2', 'racks': ['1054'],
    })
    assert picked.status_code == 200, picked.get_json()
    flagged = client.post('/api/racks/check', json={'text': '1054', 'already': []})
    assert flagged.get_json()['result'] == 'not_at_customer'
    assert 'Not at a customer' in flagged.get_json()['message']
    home = load_racks()['1054']
    assert home['status'] == 'returned'
    assert home['history'][-1]['action'] == 'picked_up'
    assert home['history'][-1]['customer_id'] == 'harbor-glass'
    assert home['history'][-1]['customer_name'] == 'Harbor Glass'
    assert 'returned' in (home['history'][-1].get('note') or '').lower() or home['history'][-1]['note'] == 'Picked up and returned.'
    assert home['history'][-1]['user'] == 'dana@oraglass.us'
    assert home['history'][-1]['truck_plate'] == 'VAN-2'
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    assert client.get('/api/racks').get_json() == []


def test_delivered_shipments_backfill_and_only_admin_can_mark_returned(client):
    _users()
    write_user('seller@oraglass.us', 'sales')
    write_user('worker@oraglass.us', 'employee')
    save_orders([{
        'id': 4,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'sent',
        'currency': 'usd',
        'items': [{
            'name': 'Unit', 'pcs': 4, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
            'sqf': 1, 'total': 40,
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
        'packing_lists': [
            {
                'id': 1, 'current': True, 'rack': '1054', 'rack_name': 'A1900',
                'number': 'XF261123001', 'lines': [{'serial': 1, 'qty': 2}],
            },
            {
                'id': 2, 'current': True, 'rack': '1055', 'rack_name': 'A1900',
                'number': 'XF261123002', 'lines': [{'serial': 2, 'qty': 2}],
            },
        ],
        'shipments': [{
            'id': 1, 'number': 'S-OLD', 'date': '2026-11-01', 'status': 'delivered',
            'delivered_on': '2026-11-01', 'rack': '1054', 'truck_plate': 'TRK-9',
            'lines': [{'serial': 1, 'qty': 2, 'index': 0}],
        }, {
            'id': 2, 'number': 'S-NEW', 'date': '2026-11-20', 'status': 'shipped',
            'rack': '1055', 'lines': [{'serial': 2, 'qty': 2, 'index': 1}],
        }],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/racks')
    assert b'Mark returned' in page.data
    assert b'const ADMIN = true' in page.data
    listed = client.get('/api/racks').get_json()
    delivered = next(row for row in listed if row['number'] == '1054')
    waiting = next(row for row in listed if row['number'] == '1055')
    assert delivered['status'] == 'at_customer'
    assert delivered['customer_name'] == 'Harbor Glass'
    assert delivered['since'] == '2026-11-01'
    assert delivered['days'] > 14
    assert delivered['overdue'] is True
    assert any(item.get('action') == 'delivered' for item in delivered['history'])
    assert [item.get('action') for item in delivered['history']] == ['packed', 'delivered']
    assert waiting['status'] == 'packed'
    assert waiting['status_label'] == 'At Ora Glass / Packed'
    assert waiting['days'] is None
    again = client.get('/api/racks').get_json()
    assert len(next(row for row in again if row['number'] == '1054')['history']) == 2
    refused = client.post('/api/racks/1054/return')
    assert refused.status_code == 200, refused.get_json()
    home = load_racks()['1054']
    assert home['status'] == 'returned'
    assert home['customer_id'] == ''
    assert home['history'][-1]['action'] == 'returned'
    assert home['history'][-1]['note'] == 'Marked returned.'
    assert home['history'][-1]['user'] == 'admin@oraglass.us'
    assert home['history'][-1]['at']
    held = client.get('/api/racks?status=at_customer').get_json()
    assert [row['number'] for row in held] == []
    client.post('/logout')
    assert login(client, 'seller@oraglass.us').status_code == 302
    assert b'const ADMIN = false' in client.get('/racks').data
    assert client.post('/api/racks/1055/return').status_code == 403
    client.post('/logout')
    assert login(client, 'worker@oraglass.us').status_code == 302
    denied = client.post('/api/racks/1055/return')
    assert denied.status_code == 403
    assert 'administrator' in (denied.get_json().get('error') or '').lower()
    assert load_racks()['1055']['status'] == 'packed'


def _delivered_harbor():
    save_orders([{
        'id': 4,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'sent',
        'currency': 'usd',
        'items': [{
            'name': 'Unit', 'pcs': 4, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
            'sqf': 1, 'total': 40,
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'rack_name': 'A1900',
            'number': 'XF261123001', 'date': '2026-11-01',
            'uploaded_at': '2026-11-01T08:00:00+00:00',
            'lines': [{'serial': 1, 'qty': 2}],
        }],
        'shipments': [{
            'id': 1, 'number': 'S-OLD', 'date': '2026-11-01', 'status': 'delivered',
            'delivered_on': '2026-11-01', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 2, 'index': 0}],
        }],
    }])


def _upload_list(client, number, rack='1054'):
    text = (
        'Sold To: Harbor Glass\nJob Name: Lobby\n'
        f'Sales Delivery: {number} Date: 11/24/2026\n'
        f'{rack} A1900*1220*1800(THK:400*2)\n'
        '(1) 2\n24 36\n609.6 mm 914.4 mm\n'
    )
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(text.encode()), f'{number}.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    return uploaded


def test_new_packing_list_auto_returns_a_rack_still_at_the_customer(client):
    _users()
    _delivered_harbor()
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.get('/api/racks').status_code == 200
    _upload_list(client, 'XF261123001')
    same = load_racks()['1054']
    assert same['status'] == 'at_customer'
    assert same['since'] == '2026-11-01'
    assert not any('Auto-returned' in (item.get('note') or '') for item in same['history'])
    _upload_list(client, 'XF9')
    reused = load_racks()['1054']
    assert reused['status'] == 'packed'
    assert reused['packing_number'] == 'XF9'
    assert reused['customer_id'] == 'harbor-glass'
    assert reused['customer_name'] == 'Harbor Glass'
    assert reused['order_id'] == 4
    assert reused['since'] == ''
    notes = [item.get('note') for item in reused['history']]
    assert 'Auto-returned (reused on packing list XF9)' in notes
    assert 'Packed on XF9.' in notes
    auto = next(item for item in reused['history'] if item.get('note') == 'Auto-returned (reused on packing list XF9)')
    packed = next(item for item in reused['history'] if item.get('note') == 'Packed on XF9.')
    assert auto['action'] == 'returned'
    assert auto['at']
    assert auto['customer_name'] == 'Harbor Glass'
    assert reused['history'].index(auto) < reused['history'].index(packed)
    shown = next(row for row in client.get('/api/racks').get_json() if row['number'] == '1054')
    assert shown['status_label'] == 'At Ora Glass / Packed'
    assert shown['days'] is None
    assert client.get('/api/racks?status=at_customer').get_json() == []


def test_backfill_replays_reused_racks_in_packing_list_order(client):
    _users()
    glass = {
        'name': 'Unit', 'pcs': 4, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
        'sqf': 1, 'total': 40,
        'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
    }
    save_orders([{
        'id': 9,
        'customer_id': 'maple',
        'company': 'Maple Glass',
        'project': 'Annex',
        'status': 'sent',
        'currency': 'usd',
        'items': [glass],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'rack_name': 'A1900',
            'number': 'XF9', 'date': '2026-11-20',
            'uploaded_at': '2026-11-20T15:00:00+00:00',
            'lines': [{'serial': 1, 'qty': 2}],
        }],
    }, {
        'id': 4,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'sent',
        'currency': 'usd',
        'items': [glass],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'rack_name': 'A1900',
            'number': 'XF261123001', 'date': '2026-11-01',
            'uploaded_at': '2026-11-01T08:00:00+00:00',
            'lines': [{'serial': 1, 'qty': 2}],
        }],
        'shipments': [{
            'id': 1, 'number': 'S-OLD', 'date': '2026-11-02', 'status': 'delivered',
            'delivered_on': '2026-11-02', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 2, 'index': 0}],
        }],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    listed = client.get('/api/racks').get_json()
    rack = next(row for row in listed if row['number'] == '1054')
    assert rack['status'] == 'packed'
    assert rack['customer_id'] == 'maple'
    assert rack['customer_name'] == 'Maple Glass'
    assert rack['order_id'] == 9
    assert rack['packing_number'] == 'XF9'
    assert rack['days'] is None
    assert [item.get('note') for item in rack['history']] == [
        'Packed on XF261123001.',
        'Delivered on S-OLD.',
        'Auto-returned (reused on packing list XF9)',
        'Packed on XF9.',
    ]
    assert all(item.get('at') for item in rack['history'])
    again = client.get('/api/racks').get_json()
    assert [item.get('note') for item in next(row for row in again if row['number'] == '1054')['history']] == [
        item.get('note') for item in rack['history']
    ]


def test_backfill_auto_returns_a_rack_when_a_later_list_shows_up(client):
    _users()
    _delivered_harbor()
    assert login(client, 'admin@oraglass.us').status_code == 302
    first = next(row for row in client.get('/api/racks').get_json() if row['number'] == '1054')
    assert first['status'] == 'at_customer'
    orders = load_orders()
    orders.append({
        'id': 9,
        'customer_id': 'maple',
        'company': 'Maple Glass',
        'project': 'Annex',
        'status': 'sent',
        'currency': 'usd',
        'items': [],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'number': 'XF9',
            'date': '2026-11-20', 'uploaded_at': '2026-11-20T15:00:00+00:00',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    })
    save_orders(orders)
    reused = next(row for row in client.get('/api/racks').get_json() if row['number'] == '1054')
    assert reused['status'] == 'packed'
    assert reused['customer_id'] == 'maple'
    assert reused['order_id'] == 9
    assert reused['packing_number'] == 'XF9'
    assert [item.get('note') for item in reused['history']][-2:] == [
        'Auto-returned (reused on packing list XF9)',
        'Packed on XF9.',
    ]
    assert reused['history'][-2]['at']
    quiet = client.get('/api/racks').get_json()
    assert len(next(row for row in quiet if row['number'] == '1054')['history']) == len(reused['history'])

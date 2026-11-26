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
    assert packed['status_label'] == 'Packed / allocated to customer'
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
    assert shown['status_label'] == 'Packed / allocated to customer'
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
    assert b'autofocus' in page.data
    assert b'id="add-rack"' in page.data
    assert b'id="wedge-rack"' not in page.data
    assert b'Keyboard scanner' not in page.data
    assert b'cdn.jsdelivr.net/npm/html5-qrcode' in page.data
    assert b'>Start camera<' in page.data
    assert b'id="keep-scanning"' in page.data
    assert b"facingMode: 'environment'" in page.data
    assert b'fps: 10' in page.data
    assert b'0.7' in page.data
    assert b'useBarCodeDetectorIfSupported: false' in page.data
    assert b'formatsToSupport' in page.data
    assert b'navigator.vibrate(150)' in page.data
    assert b'unlockBeep' in page.data
    assert b'onCameraCode' in page.data
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
    pickup = next(item for item in home['history'] if item.get('action') == 'picked_up')
    assert pickup['customer_id'] == 'harbor-glass'
    assert pickup['customer_name'] == 'Harbor Glass'
    assert pickup['location_before'] == 'Harbor Glass'
    assert pickup['location_after'] == 'Ora Glass'
    assert 'returned' in (pickup.get('note') or '').lower() or pickup['note'] == 'Picked up and returned.'
    assert pickup['user'] == 'dana@oraglass.us'
    assert pickup['truck_plate'] == 'VAN-2'
    assert home['history'][-1]['action'] == 'scanned'
    assert home['history'].index(pickup) < len(home['history']) - 1
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
    assert waiting['status_label'] == 'Packed / allocated to customer'
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
    assert shown['status_label'] == 'Packed / allocated to customer'
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


def test_a_phone_driver_scan_is_kept_and_the_detail_is_staff_only(client):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from security import hash_password
    from storage import load_users, save_racks, save_users
    _users()
    save_racks({
        '1054': {
            'number': '1054', 'name': 'A1900', 'status': 'at_customer',
            'customer_id': 'harbor-glass', 'customer_name': 'Harbor Glass',
            'since': '2026-11-01', 'packing_number': 'XF1', 'history': [{
                'at': '2026-11-01T18:00:00+00:00', 'action': 'delivered',
                'user': 'admin@oraglass.us', 'user_name': 'Admin', 'user_role': 'admin',
                'note': 'Delivered on S1.', 'customer_id': 'harbor-glass',
                'customer_name': 'Harbor Glass',
            }],
        },
    })
    users = load_users()
    users['3605550199'] = {
        'email': '', 'name': 'Dana Driver', 'role': 'driver', 'phone': '3605550199',
        'password_hash': hash_password('Validpass1'), 'active': True,
        'must_change_password': False,
    }
    save_users(users)
    assert login(client, '3605550199').status_code == 302
    denied = client.get('/racks/1054')
    assert denied.status_code == 302
    assert '/racks/1054' not in denied.headers['Location']
    assert b'id="rack-timeline"' not in denied.data
    scanned = client.post('/api/racks/check', json={'text': '1054', 'scan_method': 'camera', 'already': []})
    assert scanned.get_json()['result'] == 'ok'
    again = client.post('/api/racks/check', json={'text': '9999', 'scan_method': 'typed', 'already': []})
    assert again.get_json()['result'] == 'unknown'
    assert '9999' not in load_racks()
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    hidden = client.get('/racks/1054')
    assert hidden.status_code == 302
    assert b'id="rack-timeline"' not in hidden.data
    assert b'overdue-racks' not in client.get('/dashboard').data
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    stored = load_racks()['1054']
    assert [item.get('note') for item in stored['history']][0] == 'Delivered on S1.'
    scan = stored['history'][-1]
    assert scan['action'] == 'scanned'
    assert scan['scan_method'] == 'camera'
    assert scan['user'] == '3605550199'
    assert scan['user_name'] == 'Dana Driver'
    assert scan['user_role'] == 'driver'
    assert scan['location_before'] == 'Harbor Glass'
    assert scan['location_after'] == 'Harbor Glass'
    assert scan['packing_number'] == 'XF1'
    assert scan['at'].endswith('+00:00')
    page = client.get('/racks/1054')
    assert page.status_code == 200
    assert b'id="rack-timeline"' in page.data
    assert b'Dana Driver' in page.data
    assert b'(Driver)' in page.data
    assert b'Scanned by camera.' in page.data
    assert b'Pacific' in page.data
    assert b'Harbor Glass' in page.data
    listed = client.get('/racks')
    assert b'data-sort="trips"' in listed.data
    assert b'data-sort="avg_days"' in listed.data
    assert b'data-sort="days"' in listed.data
    assert b'badge-overdue' in listed.data
    assert b'Weekly overdue racks' in listed.data
    assert b'It has not run yet' in listed.data
    assert b'Customers are not emailed' in listed.data
    body = next(row for row in client.get('/api/racks?status=overdue').get_json() if row['number'] == '1054')
    assert body['overdue'] is True
    assert body['trips'] >= 1
    assert body['trip_open'] is True
    assert body['days'] > 14
    dash = client.get('/dashboard')
    assert b'id="overdue-racks"' in dash.data
    assert b'1054' in dash.data
    pacific = ZoneInfo('America/Los_Angeles')
    assert racks_mod().run_weekly_overdue_if_due(datetime(2026, 11, 23, 6, 59, tzinfo=pacific)) is None
    record = racks_mod().run_weekly_overdue_if_due(datetime(2026, 11, 23, 7, 0, tzinfo=pacific))
    assert record['week'] == '2026-11-23'
    assert record['emailed'] is False
    assert any(row['number'] == '1054' and row['packing_number'] == 'XF1' for row in record['rows'])
    assert racks_mod().run_weekly_overdue_if_due(datetime(2026, 11, 23, 18, 0, tzinfo=pacific)) is None
    shown = client.get('/racks').data
    assert b'Last run' in shown
    assert b'Pacific' in shown
    nxt = racks_mod().run_weekly_overdue_if_due(datetime(2026, 11, 30, 7, 0, tzinfo=pacific))
    assert nxt['week'] == '2026-11-30'
    assert racks_mod().start_weekly_overdue_report() is None


def racks_mod():
    import racks
    return racks


def test_trip_stats_count_a_return_and_an_open_stay():
    from datetime import date
    import racks
    row = {
        'status': 'at_customer',
        'customer_name': 'Harbor Glass',
        'since': '2026-11-20',
        'history': [
            {'at': '2026-10-01T16:00:00+00:00', 'action': 'delivered', 'customer_name': 'Maple Glass'},
            {'at': '2026-10-11T16:00:00+00:00', 'action': 'returned', 'customer_name': 'Maple Glass'},
            {'at': '2026-11-20T16:00:00+00:00', 'action': 'delivered', 'customer_name': 'Harbor Glass'},
            {'at': '2026-11-21T16:00:00+00:00', 'action': 'scanned', 'scan_method': 'gun',
             'user_name': 'Dana Driver', 'user_role': 'driver', 'user': '3605550199'},
        ],
    }
    stats = racks.lifetime_stats(row, date(2026, 11, 26))
    assert stats['trips'] == 2
    assert stats['trip_open'] is True
    assert stats['total_days'] == 16
    assert stats['avg_days'] == 8
    assert stats['longest_days'] == 10
    assert stats['current_days'] == 6
    assert stats['current_location'] == 'Harbor Glass'
    assert stats['last_scan_by'] == 'Dana Driver'
    assert stats['last_scan_method'] == 'gun'
    assert 'Pacific' in racks.pacific_label('2026-11-26T15:00:00+00:00')


def test_each_company_sets_its_own_rack_alert_and_customer_view(client):
    from datetime import date, timedelta
    from storage import save_racks
    _users()
    write_user('seller@oraglass.us', 'sales')
    today = date.today()
    save_racks({
        '1054': {
            'number': '1054', 'status': 'at_customer', 'customer_id': 'harbor-glass',
            'customer_name': 'Harbor Glass', 'since': (today - timedelta(days=10)).isoformat(),
            'packing_number': 'XF1', 'history': [],
        },
        '2002': {
            'number': '2002', 'status': 'at_customer', 'customer_id': 'maple',
            'customer_name': 'Maple Glass', 'since': (today - timedelta(days=20)).isoformat(),
            'packing_number': 'XF2', 'history': [],
        },
    })
    assert login(client, 'seller@oraglass.us').status_code == 302
    blocked = client.put('/api/customers/harbor-glass', json={'rack_alert_days': 7, 'show_overdue_racks': True})
    assert blocked.status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/customers')
    assert b'Rack alert after (days)' in page.data
    assert b'Show overdue rack alert to customer' in page.data
    rejected = client.put('/api/customers/harbor-glass', json={'rack_alert_days': 0})
    assert rejected.status_code == 400
    assert 'at least 1' in rejected.get_json()['error']
    saved = client.put('/api/customers/harbor-glass', json={'rack_alert_days': 7, 'show_overdue_racks': True})
    assert saved.status_code == 200
    company = next(row for row in client.get('/api/customers').get_json() if row['id'] == 'harbor-glass')
    assert company['rack_alert_days'] == 7
    assert company['show_overdue_racks'] is True
    maple = next(row for row in client.get('/api/customers').get_json() if row['id'] == 'maple')
    assert maple['rack_alert_days'] == 14
    assert maple['show_overdue_racks'] is False
    overdue = {row['number'] for row in client.get('/api/racks?status=overdue').get_json()}
    assert overdue == {'1054', '2002'}
    quiet = client.put('/api/customers/maple', json={'rack_alert_days': 30})
    assert quiet.status_code == 200
    overdue = {row['number'] for row in client.get('/api/racks?status=overdue').get_json()}
    assert overdue == {'1054'}
    report = racks_mod().overdue_rows(load_racks())
    assert {row['number'] for row in report} == {'1054'}
    assert report[0]['packing_number'] == 'XF1'
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    home = client.get('/dashboard')
    assert b'id="customer-overdue-racks"' in home.data
    assert b'1054' in home.data
    assert b'XF1' in home.data
    assert b'2002' not in home.data
    assert b'id="overdue-racks"' not in home.data
    holding = client.get('/racks')
    assert b'id="customer-overdue-racks"' in holding.data
    assert b'1054' in holding.data
    assert b'2002' not in holding.data
    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    maple_home = client.get('/dashboard')
    assert b'id="customer-overdue-racks"' not in maple_home.data
    assert b'1054' not in maple_home.data


def test_packing_import_allocates_and_delivery_starts_the_stay(client):
    from datetime import date, datetime, timezone
    import racks
    _users()
    order = {
        'id': 7,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'racks': ['1054', '1055'], 'rack_name': 'East',
        }],
    }
    racks.sync_order_racks(order, None)
    packed = racks.load_rows()
    assert packed['1054']['status'] == 'packed'
    assert packed['1054']['customer_id'] == 'harbor-glass'
    assert packed['1054']['since'] == ''
    shown = racks.present(packed['1054'], date(2026, 11, 26))
    assert shown['status_label'] == 'Packed / allocated to customer'
    assert shown['overdue'] is False
    assert shown['trips'] == 0
    event = packed['1054']['history'][-1]
    assert event['action'] == 'packed'
    assert event['method'] == 'packing list import'
    assert event['user'] == 'system import'
    assert event['packing_number'] == 'XF1'
    assert event['order_id'] == 7
    assert racks.record_scan('1054', {'email': 'dana@oraglass.us', 'role': 'driver'}, 'camera') is True
    after_scan = racks.load_rows()['1054']
    assert after_scan['status'] == 'packed'
    assert after_scan['since'] == ''
    assert racks.lifetime_stats(after_scan, date(2026, 11, 26))['trips'] == 0
    order['shipments'] = [{
        'id': 1, 'number': 'S1', 'status': 'delivered', 'delivered_on': '2026-11-01',
        'rack': '1054, 1055',
    }]
    when = datetime(2026, 11, 1, 18, 30, tzinfo=timezone.utc)
    racks.deliver_order(order, {'email': 'sam@oraglass.us', 'name': 'Sam', 'role': 'sales'}, when)
    delivered = racks.load_rows()['1054']
    assert delivered['status'] == 'at_customer'
    assert delivered['since'] == '2026-11-01'
    assert delivered['delivered_at'].startswith('2026-11-01T18:30:00')
    history_len = len(delivered['history'])
    racks.deliver_order(order, {'email': 'sam@oraglass.us', 'role': 'sales'}, when)
    assert len(racks.load_rows()['1054']['history']) == history_len
    fresh = {
        'id': 8, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF9', 'rack': '3001', 'racks': ['3001', '3002'],
        }],
    }
    racks.sync_order_racks(fresh, {'email': 'sam@oraglass.us', 'name': 'Sam', 'role': 'sales'})
    racks.sync_order_racks({**fresh, 'packing_lists': [{
        'id': 2, 'current': True, 'number': 'XF9', 'rack': '3002', 'racks': ['3002'],
    }]}, {'email': 'sam@oraglass.us', 'role': 'sales'})
    rows = racks.load_rows()
    assert rows['3001']['status'] == 'at_ora'
    assert rows['3001']['customer_id'] == ''
    assert rows['3001']['history'][-1]['action'] == 'unallocated'
    assert rows['3001']['history'][-1]['method'] == 'packing list import'
    assert rows['3002']['status'] == 'packed'
    assert rows['3002']['customer_id'] == 'harbor-glass'
    assert len([item for item in rows['3002']['history'] if item['action'] == 'packed']) == 1
    racks.release_order_packing(8, {'email': 'sam@oraglass.us', 'role': 'sales'})
    released = racks.load_rows()['3002']
    assert released['status'] == 'at_ora'
    assert released['history'][-1]['action'] == 'unallocated'
    assert racks.lifetime_stats(delivered, date(2026, 11, 11))['longest_days'] == 10


def test_mark_delivered_confirm_matches_the_invoice_rules(client):
    _users()
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/orders/4').get_data(as_text=True)
    assert 'order.delivery_confirm' in page
    plain = client.get('/api/orders/4').get_json()
    assert plain['delivery_creates_invoice'] is True
    assert 'This creates an invoice.' in plain['delivery_confirm']
    assert 'Racks on this order start their customer stay timer now.' in plain['delivery_confirm']
    orders = load_orders()
    orders[0]['shipments'] = [{'id': 1, 'number': 'S1'}]
    save_orders(orders)
    shipped = client.get('/api/orders/4').get_json()
    assert shipped['delivery_creates_invoice'] is False
    assert 'Billing follows the shipments; existing invoice(s) are kept, no new invoice is created.' in shipped['delivery_confirm']
    orders[0]['shipments'] = []
    orders[0]['invoice_id'] = 9
    save_orders(orders)
    kept = client.get('/api/orders/4').get_json()
    assert 'The existing invoice is kept.' in kept['delivery_confirm']
    assert 'This creates an invoice.' not in kept['delivery_confirm']
    orders[0]['invoice_id'] = None
    orders[0]['legacy'] = True
    save_orders(orders)
    legacy = client.get('/api/orders/4').get_json()
    assert 'No invoice is created. Legacy jobs are not invoiced.' in legacy['delivery_confirm']
    orders[0]['legacy'] = False
    orders[0]['remake'] = True
    orders[0]['charge_customer'] = False
    save_orders(orders)
    remake = client.get('/api/orders/4').get_json()
    assert 'No invoice is created. This remake is not charged to the customer.' in remake['delivery_confirm']


def _jpeg(width, height):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', (width, height), (20, 40, 180)).save(buf, 'JPEG', quality=90)
    buf.seek(0)
    return buf


def test_driver_damage_photos_are_kept_and_an_admin_can_clear_them(client):
    from datetime import datetime
    from io import BytesIO
    from zoneinfo import ZoneInfo
    from PIL import Image
    from security import hash_password
    from storage import load_users, save_racks, save_users
    _users()
    save_racks({
        '1054': {
            'number': '1054', 'name': 'A1900', 'status': 'at_customer',
            'customer_id': 'harbor-glass', 'customer_name': 'Harbor Glass',
            'since': '2026-11-01', 'packing_number': 'XF1', 'history': [],
        },
        '2002': {
            'number': '2002', 'name': 'A1900', 'status': 'at_customer',
            'customer_id': 'harbor-glass', 'customer_name': 'Harbor Glass',
            'since': '2026-11-02', 'history': [],
        },
    })
    users = load_users()
    users['3605550199'] = {
        'email': '', 'name': 'Dana Driver', 'role': 'driver', 'phone': '3605550199',
        'password_hash': hash_password('Validpass1'), 'active': True,
        'must_change_password': False,
    }
    save_users(users)
    assert login(client, '3605550199').status_code == 302
    page = client.get('/racks/scan')
    assert page.status_code == 200
    assert b'data-damaged' in page.data
    assert b'Add photos of damage' in page.data
    assert b'accept="image/*"' in page.data
    assert b'capture="environment"' in page.data
    assert b'multiple' in page.data
    assert b'data-remove-photo' in page.data
    assert b'data-note' in page.data
    rejected = client.post('/api/racks/pickup', data={
        'racks': '["1054"]',
        'damaged': '["1054"]',
        'notes': '{}',
        'methods': '{}',
        'photos-1054': (BytesIO(b'not a photo'), 'note.txt'),
    })
    assert rejected.status_code == 400
    assert load_racks()['1054']['status'] == 'at_customer'
    picked = client.post('/api/racks/pickup', data={
        'racks': '["1054"]',
        'damaged': '["1054"]',
        'notes': '{"1054": "Bent corner"}',
        'methods': '{"1054": "camera"}',
        'truck_plate': 'VAN-9',
        'photos-1054': (_jpeg(2400, 900), 'bent.jpg'),
    })
    assert picked.status_code == 200, picked.get_json()
    plain = client.post('/api/racks/pickup', json={
        'racks': ['2002'], 'damaged': ['2002'], 'notes': {}, 'methods': {'2002': 'typed'},
    })
    assert plain.status_code == 200, plain.get_json()
    stored = load_racks()['1054']
    assert stored['status'] == 'damaged'
    assert stored['damaged'] is True
    assert stored['damage_note'] == 'Bent corner'
    event = stored['history'][-1]
    assert event['action'] == 'picked_up'
    assert event['damaged'] is True
    assert event['user'] == '3605550199'
    assert event['user_role'] == 'driver'
    assert event['customer_id'] == 'harbor-glass'
    assert event['customer_name'] == 'Harbor Glass'
    assert 'Bent corner' in event['note']
    photo_id = event['photos'][0]['id']
    import racks as rack_book
    with Image.open(rack_book.damage_photo_path('1054', photo_id)) as image:
        assert image.format == 'JPEG'
        assert image.size == (1600, 600)
    bare = load_racks()['2002']
    assert bare['status'] == 'damaged'
    assert bare['damage_photos'] == []
    assert bare['history'][-1]['note'] == 'Picked up damaged.'
    blocked = client.get(f'/api/racks/1054/photos/{photo_id}')
    assert blocked.status_code != 200
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    refused = client.post('/api/racks/1054/clear-damage')
    assert refused.status_code == 403
    assert load_racks()['1054']['status'] == 'damaged'
    assert b'id="damage-report-alert"' not in client.get('/dashboard').data
    assert b'Racks (' not in client.get('/racks').data
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    photo = client.get(f'/api/racks/1054/photos/{photo_id}')
    assert photo.status_code == 200
    assert photo.mimetype == 'image/jpeg'
    listed = next(row for row in client.get('/api/racks?status=damaged').get_json() if row['number'] == '1054')
    assert listed['status_label'] == 'Damaged'
    assert listed['damaged'] is True
    detail = client.get('/racks/1054')
    assert b'badge-damaged' in detail.data
    assert b'id="damage-gallery"' in detail.data
    assert b'Bent corner' in detail.data
    assert photo_id.encode() in detail.data
    assert b'Clear damaged' in detail.data
    inventory = client.get('/racks')
    assert b'badge-damaged' in inventory.data
    assert b'id="open-damage-reports"' in inventory.data
    assert b'2 open damage reports' in inventory.data
    assert b'Racks (2)' in inventory.data
    assert b'id="damaged-racks"' not in inventory.data
    assert b'Included in the Monday report' not in inventory.data
    reports = client.get('/api/racks/damage-reports').get_json()
    issued = next(row for row in reports if row['rack'] == '1054')
    assert issued['status'] == 'open'
    assert issued['note'] == 'Bent corner'
    assert issued['driver_name'] == 'Dana Driver'
    pacific = ZoneInfo('America/Los_Angeles')
    record = rack_book.run_weekly_overdue_if_due(datetime(2026, 11, 23, 7, 0, tzinfo=pacific))
    assert 'damaged' not in record
    assert all(row['number'] != '1054' for row in record['rows'])
    shown = client.get('/racks').data
    assert b'Weekly overdue racks' in shown
    assert b'Bent corner' not in shown
    cleared = client.post('/api/racks/1054/clear-damage')
    assert cleared.status_code == 200
    after = load_racks()['1054']
    assert after['status'] == 'returned'
    assert after.get('damaged') is False
    assert after['history'][-1]['action'] == 'damage_cleared'
    assert after['history'][-1]['note'] == 'Cleared the damaged flag.'
    assert after['history'][-1]['user'] == 'admin@oraglass.us'
    assert after['history'][-2]['photos'][0]['id'] == photo_id
    quiet = client.get('/racks/1054')
    assert b'id="damage-gallery"' not in quiet.data
    assert b'Cleared the damaged flag.' in quiet.data
    assert photo_id.encode() in quiet.data
    assert client.get(f'/api/racks/1054/photos/{photo_id}').status_code == 200


def test_damage_reports_list_repair_and_charge(client, monkeypatch):
    from security import hash_password
    from storage import load_invoices, load_users, save_customers, save_racks, save_users
    import documents
    import quickbooks
    from tests.test_quote_export import pdf_text
    _users()
    customers = {
        'harbor-glass': {
            'id': 'harbor-glass', 'name': 'Harbor Glass', 'status': 'active', 'tier': 'T2',
            'country': 'United States', 'credit_currency': 'usd', 'credit_approved': True,
            'payment_terms': 'Net 30',
        },
        'maple': {
            'id': 'maple', 'name': 'Maple Glass', 'status': 'active', 'tier': 'T2',
            'country': 'Canada', 'province': 'BC', 'credit_currency': 'cad',
            'payment_terms': 'Net 30',
        },
    }
    save_customers(customers)
    save_racks({
        '1054': {
            'number': '1054', 'name': 'A1900', 'status': 'at_customer',
            'customer_id': 'harbor-glass', 'customer_name': 'Harbor Glass',
            'since': '2026-11-01', 'order_id': 4, 'shipment_id': 9,
            'shipment_number': 'S9', 'packing_number': 'XF1', 'history': [],
        },
        '2002': {
            'number': '2002', 'name': 'A1900', 'status': 'at_customer',
            'customer_id': 'maple', 'customer_name': 'Maple Glass',
            'since': '2026-11-02', 'order_id': 8, 'shipment_number': 'S2', 'history': [],
        },
    })
    users = load_users()
    users['3605550199'] = {
        'email': '', 'name': 'Dana Driver', 'role': 'driver', 'phone': '3605550199',
        'password_hash': hash_password('Validpass1'), 'active': True,
        'must_change_password': False,
    }
    save_users(users)
    write_user('rep@oraglass.us', 'sales')
    assert login(client, '3605550199').status_code == 302
    denied = client.get('/racks/damage')
    assert denied.status_code == 302
    picked = client.post('/api/racks/pickup', json={
        'racks': ['1054', '2002'],
        'damaged': ['1054', '2002'],
        'notes': {'1054': 'Bent corner', '2002': 'Cracked base'},
        'methods': {'1054': 'camera', '2002': 'typed'},
    })
    assert picked.status_code == 200, picked.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    listed = client.get('/api/racks/damage-reports').get_json()
    assert {row['rack'] for row in listed} == {'1054', '2002'}
    harbor = next(row for row in listed if row['rack'] == '1054')
    assert harbor['status'] == 'open'
    assert harbor['status_label'] == 'To be repaired'
    assert harbor['customer_id'] == 'harbor-glass'
    assert harbor['customer_name'] == 'Harbor Glass'
    assert harbor['order_id'] == 4
    assert harbor['shipment_number'] == 'S9'
    assert harbor['note'] == 'Bent corner'
    assert harbor['driver'] == '3605550199'
    assert harbor['driver_name'] == 'Dana Driver'
    assert harbor['event_id']
    assert harbor['photos'] == []
    day = harbor['at'][:10]
    filtered = client.get(f'/api/racks/damage-reports?status=open&customer_id=harbor-glass&rack=1054&from={day}&to={day}').get_json()
    assert [row['rack'] for row in filtered] == ['1054']
    page = client.get(f'/racks/damage?status=open&rack=1054&from={day}&to={day}')
    assert page.status_code == 200
    assert b'id="damage-reports"' in page.data
    assert b'To be repaired' in page.data
    assert b'Bent corner' in page.data
    assert b'Cracked base' not in page.data
    assert b'Mark this report repaired, no cost? This clears the damaged flag.' in page.data
    assert b'Charge the customer and close this report? This creates a repair invoice.' in page.data
    racks_page = client.get('/racks').data
    assert b'2 open damage reports' in racks_page
    assert b'id="open-damage-reports"' in racks_page
    assert b'Racks (2)' in racks_page
    assert b'Included in the Monday report' not in racks_page
    home = client.get('/dashboard').data
    assert b'id="damage-report-alert"' in home
    assert b'2 open damage reports' in home
    assert b'Customers are not emailed' in home
    client.post('/logout')
    assert login(client, 'rep@oraglass.us').status_code == 302
    blocked = client.post(f"/api/racks/damage-reports/{harbor['id']}/charge", json={'amount': 40, 'description': 'Rack repair'})
    assert blocked.status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    repaired = client.post(f"/api/racks/damage-reports/{harbor['id']}/repaired")
    assert repaired.status_code == 200, repaired.get_json()
    assert repaired.get_json()['status_label'] == 'Repaired, no cost'
    rack = load_racks()['1054']
    assert rack['status'] == 'returned'
    assert rack.get('damaged') is False
    assert rack['history'][-1]['action'] == 'damage_repaired'
    assert rack['history'][-1]['note'] == 'Repaired, no cost.'
    assert rack['history'][-1]['user'] == 'admin@oraglass.us'
    assert b'1 open damage report' in client.get('/racks').data
    pushed = []

    def fake_push(invoice):
        pushed.append(invoice)
        return {'Id': 'qbo-1'}

    monkeypatch.setattr(quickbooks, 'push_document', fake_push)
    maple = next(row for row in client.get('/api/racks/damage-reports?rack=2002').get_json())
    charged = client.post(
        f"/api/racks/damage-reports/{maple['id']}/charge",
        json={'amount': 85, 'description': 'Weld the rack base'},
    )
    assert charged.status_code == 200, charged.get_json()
    body = charged.get_json()
    assert body['status'] == 'charged'
    assert body['status_label'] == 'Charge customer'
    invoice = next(row for row in load_invoices() if row.get('id') == body['invoice_id'])
    assert invoice['number'] == body['invoice_number']
    assert invoice['customer_id'] == 'maple'
    assert invoice['issuer'] == 'ca'
    assert invoice['currency'] == 'cad'
    assert invoice['order_id'] == 8
    assert invoice['shipment_number'] == 'S2'
    assert invoice['source'] == 'repair'
    assert invoice['items'][0]['kind'] == 'service'
    assert invoice['items'][0]['item_type'] == 'service'
    assert invoice['items'][0]['description'] == 'Weld the rack base'
    assert invoice['igu_subtotal'] == 0
    labels = [row['label'] for row in documents.invoice_summary(invoice)]
    assert 'Services' in labels
    assert not any('IGU' in label for label in labels)
    assert pushed and pushed[0]['id'] == invoice['id']
    closed = load_racks()['2002']
    assert closed['status'] == 'returned'
    assert closed.get('damaged') is False
    assert closed['history'][-1]['action'] == 'damage_charged'
    assert invoice['number'] in closed['history'][-1]['note']
    assert closed['history'][-1]['customer_name'] == 'Maple Glass'
    pdf = client.get(f"/api/invoices/{invoice['id']}/pdf")
    assert pdf.status_code == 200
    text = pdf_text(pdf.data)
    assert b'Services' in text
    assert b'Weld the rack base' in text
    assert b'IGU' not in text
    monkeypatch.setattr(quickbooks, '_item_id', lambda name: 'svc')
    lines = quickbooks._document_lines({
        'items': invoice['items'], 'total': invoice['total'],
    }, 'NON')
    assert lines[0]['SalesItemLineDetail']['ItemRef']['name'] == 'Services'
    again = client.post(f"/api/racks/damage-reports/{maple['id']}/repaired")
    assert again.status_code == 400

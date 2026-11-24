"""Phone scanning reads the rack number that is already printed in the QR label."""

import racks
from tests.conftest import login, write_user
from storage import save_orders


def _order():
    save_orders([{
        'id': 2,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'PO1',
        'status': 'approved',
        'items': [{
            'name': 'Unit',
            'pcs': 4,
            'floor': 'W01',
            'spec': {'glass1': 'Clear Glass 4mm', 'width': 27, 'height': 73, 'unit': 'in'},
        }],
        'packing_lists': [
            {
                'id': 1, 'current': False, 'rack': '1054', 'number': 'OLD',
                'lines': [{'serial': 1, 'qty': 9}],
            },
            {
                'id': 2, 'current': True, 'rack': '1054', 'number': 'XF261123001', 'date': '2026-11-23',
                'lines': [{'serial': 1, 'qty': 4}],
            },
        ],
    }])


def test_scanned_text_is_the_rack_number():
    assert racks.rack_number_from_scan('  1054\n') == '1054'
    assert racks.rack_number_from_scan('1054') == '1054'
    assert racks.rack_number_from_scan(' https://labels.example/1054 ') == 'https://labels.example/1054'
    found = racks.lookup_rack([{
        'id': 2, 'company': 'HighQ', 'project': 'PO1', 'items': [],
        'packing_lists': [{'current': True, 'rack': '1054', 'number': 'XF', 'lines': []}],
    }], '  1054\n')
    assert found['rack'] == '1054'
    assert found['raw'] == '  1054\n'
    assert found['found'] is True
    missed = racks.lookup_rack([], '  9999 ')
    assert missed['found'] is False
    assert missed['rack'] == '9999'
    assert missed['raw'] == '  9999 '


def test_scan_page_is_for_phones_and_uses_the_label_text(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('driver@oraglass.us', 'shop')
    write_user('ap@highq.example', 'customer', customer_id='highq', customer_name='HighQ Developments Inc')
    _order()
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/racks/scan')
    assert page.status_code == 200
    html = page.data.decode()
    assert 'Scan rack' in html
    assert 'id="manual-rack"' in html
    assert 'id="wedge-rack"' in html
    assert 'getUserMedia' in html
    assert 'viewport' in html
    assert 'pattern' not in html.lower()
    assert html.index('id="scan-rack"') < html.index('id="manual-rack"')
    assert b'href="/racks/scan"' in client.get('/orders').data
    found = client.post('/api/racks/lookup', json={'text': ' 1054\n'})
    assert found.status_code == 200
    body = found.get_json()
    assert body['rack'] == '1054'
    assert body['raw'] == ' 1054\n'
    assert body['found'] is True
    assert body['matches'][0]['number'] == 'XF261123001'
    assert body['matches'][0]['lines'][0]['serial'] == 1
    assert body['matches'][0]['lines'][0]['qty'] == 4
    unknown = client.post('/api/racks/lookup', json={'text': ' 9999 '})
    assert unknown.get_json()['found'] is False
    assert unknown.get_json()['raw'] == ' 9999 '
    client.post('/logout')

    assert login(client, 'driver@oraglass.us').status_code == 302
    assert client.get('/racks/scan').status_code == 200
    client.post('/logout')

    assert login(client, 'ap@highq.example').status_code == 302
    assert client.get('/racks/scan').status_code in (302, 403)
    assert client.post('/api/racks/lookup', json={'text': '1054'}).status_code == 403

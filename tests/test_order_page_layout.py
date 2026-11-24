"""The order page is a detail view, and shipping asks for a date, rack, and truck."""

from storage import load_customers, save_customers, save_orders
from tests.conftest import login, write_user


def _ready(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management', password='Validpass1',
    )
    customers = load_customers()
    customers['harbor-glass'].update({
        'country': 'United States', 'credit_limit': 100000, 'credit_currency': 'usd', 'credit_approved': True,
    })
    save_customers(customers)
    save_orders([{
        'id': 7,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'po': 'PO-7',
        'status': 'approved',
        'currency': 'usd',
        'items': [{
            'name': 'Clear unit', 'pcs': 4, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
            'sqf': 1, 'total': 40,
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
        'packing_lists': [{
            'id': 1, 'current': True, 'rack': '1054', 'number': 'XF1',
            'lines': [{'serial': 1, 'qty': 4}],
        }],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302


def test_order_detail_page_omits_the_list_and_the_old_shipment_form(client):
    _ready(client)
    listing = client.get('/orders')
    assert b'id="order-list"' in listing.data
    assert b'id="packing-upload"' in listing.data
    detail = client.get('/orders/7')
    html = detail.data
    assert b'id="order-list"' not in html
    assert b'id="packing-upload"' not in html
    assert b'Back to orders' in html
    assert b'id="ship-confirm"' in html
    assert b'Shipping date' in html
    assert b'Truck plate' in html
    assert b'Rack number' in html
    assert b'Adjust quantities' in html
    assert b'id="confirm-ship"' in html
    assert b'Match packing list' not in html
    assert b'Rack code or remarks' not in html
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    own = client.get('/orders/7')
    assert own.status_code == 200
    assert b'id="order-list"' not in own.data
    assert b'Back to orders' in own.data


def test_shipping_date_is_required_and_the_panel_fields_are_stored(client):
    _ready(client)
    missing = client.post('/api/orders/7/ship-packed', json={'number': 'S1'})
    assert missing.status_code == 400
    assert 'date' in missing.get_json()['error'].lower()
    blank = client.post('/api/orders/7/ship-packed', json={'date': '11/23/2026'})
    assert blank.status_code == 400
    shipped = client.post('/api/orders/7/ship-packed', json={
        'date': '2026-11-23', 'rack': '1054', 'truck_plate': 'TRK-9', 'notes': 'Dock 2',
    })
    assert shipped.status_code == 200, shipped.get_json()
    body = shipped.get_json()
    shipment = body['shipments'][0]
    assert shipment['date'] == '2026-11-23'
    assert shipment['rack'] == '1054'
    assert shipment['truck_plate'] == 'TRK-9'
    assert shipment['notes'] == 'Dock 2'

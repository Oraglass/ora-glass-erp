"""A packing list stays with the customer named on it, and packed pieces show as ready to ship."""

import io

from openpyxl import load_workbook

import packing
from storage import load_invoices, load_orders, save_orders
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def _spec():
    return {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'}


def _item(serial, pcs):
    return {
        'name': f'Unit {serial}', 'pcs': pcs, 'floor': f'W0{serial}',
        'price_pc': 10, 'price_sqf': 10, 'sqf': 1, 'total': 10 * pcs, 'spec': _spec(),
    }


def _orders():
    save_orders([
        {
            'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc',
            'project': 'Misc 11/15/2026', 'status': 'approved', 'currency': 'cad',
            'items': [_item(1, 2), _item(2, 4), _item(3, 6)],
        },
        {
            'id': 2, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'project': 'AF17.0413.VLD.iKeith Pagnac.HH', 'status': 'approved', 'currency': 'usd',
            'items': [_item(1, 12)],
        },
    ])


def _users(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@highq.example', 'customer', customer_id='highq',
        customer_name='HighQ Developments Inc', customer_subrole='management', password='Validpass1',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['highq']['country'] = 'Canada'
    customers['open-windows'] = {
        'id': 'open-windows', 'name': 'Open Windows Inc', 'country': 'United States',
        'status': 'active', 'tier': 'T2',
    }
    save_customers(customers)


def _upload(client):
    text = (
        'Sold To: HighQ\nJob Name: PO1\n'
        'Sales Delivery: XF261123001  Date: 11/23/2026\n'
        '1054 A1900*1220*1800\n'
        '(1) 4\n24 36\n609.6 mm 914.4 mm\n'
        '(9) 7\n24 36\n609.6 mm 914.4 mm\n'
    )
    return client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(text.encode()), 'XF261123001.pdf.txt')},
        content_type='multipart/form-data',
    )


def test_packed_total_ignores_serials_the_order_does_not_have():
    order = {
        'items': [_item(1, 2)],
        'packing_lists': [{
            'id': 1, 'current': True,
            'lines': [{'serial': 1, 'qty': 4}, {'serial': 9, 'qty': 7}],
        }],
    }
    progress = packing.packing_progress(order)
    assert progress['totals'] == {'ordered': 2, 'packed': 2, 'bo': 0}
    assert progress['serials'][0]['packed'] == 2


def test_assignment_cannot_change_customer_and_can_be_removed(client):
    _users(client)
    _orders()
    assert login(client, 'admin@oraglass.us').status_code == 302
    stale = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(b'not a packing list'), 'XF261123001.pdf.txt')},
        content_type='multipart/form-data',
    )
    assert stale.status_code == 200
    assert 'No glass rows' in stale.get_json()['review'][0]['message']
    uploaded = _upload(client)
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['saved'] == []
    review = body['review'][0]
    assert review['customer_locked'] is True
    assert review['customer_id'] == 'highq'
    assert [row['id'] for row in review['choices']] == [1]
    assert 'Open Windows' not in review['message']
    refused = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 2})
    assert refused.status_code == 400
    assert 'HighQ' in refused.get_json()['error']
    assert 'Open Windows' in refused.get_json()['error']
    assert load_orders()[1].get('packing_lists') in (None, [])
    assigned = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 1})
    assert assigned.status_code == 200, assigned.get_json()
    assert assigned.get_json()['totals']['packed'] == 2
    assert assigned.get_json()['totals']['ordered'] == 12
    assert any(row['serial'] == 9 for row in assigned.get_json()['unmatched'])
    listed = client.get('/api/packing-lists').get_json()
    assert all('No glass rows' not in (row.get('message') or '') for row in listed['inbox'])
    order = client.get('/api/orders/1').get_json()
    assert order['pack_status'] == 'partial'
    assert b'Print Packing List' in client.get('/orders/1').data
    assert b'Print Back Ordered List' in client.get('/orders/1').data
    packing_pdf = client.get('/api/orders/1/packing-lists.pdf')
    assert packing_pdf.status_code == 200 and packing_pdf.data.startswith(b'%PDF')
    packing_text = pdf_text(packing_pdf.data)
    assert b'HighQ Developments Inc' in packing_text
    assert b'Misc 11/15/2026' in packing_text
    assert b'XF261123001' in packing_text
    assert b'Total sqft' in packing_text
    assert b'Total weight' in packing_text
    backorder = client.get('/api/orders/1/backorder.pdf')
    assert backorder.data.startswith(b'%PDF')
    back_text = pdf_text(backorder.data)
    assert b'Back Ordered List' in back_text
    open_rows = client.get('/api/shipments/open').get_json()
    waiting = next(row for row in open_rows if row['id'] == 1)
    assert waiting['ready_to_ship'] == 2
    ready = next(line for line in waiting['lines'] if line['serial'] == 1)
    assert ready['packed'] == 2
    assert ready['shipped'] == 0
    assert ready['ready_to_ship'] == 2
    assert 'in' in ready['size'] and 'mm' in ready['size']
    sheet = load_workbook(io.BytesIO(client.get('/api/shipments/open.xlsx').data)).active
    headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    assert headers[-5:] == ['Packed', 'Shipped', 'Not packed', 'Ready to ship', 'Size']
    export_pdf = client.get('/api/shipments/open.pdf')
    assert export_pdf.data.startswith(b'%PDF')
    assert b'Ready to ship' in pdf_text(export_pdf.data) or b'Packed' in pdf_text(export_pdf.data)
    removed = client.delete('/api/orders/1/packing-lists/1')
    assert removed.status_code == 200, removed.get_json()
    cleared = removed.get_json()
    assert cleared['pack_status'] == ''
    assert cleared['packing_lists'] == []
    assert any(change.get('action') == 'removed' for change in cleared['changelog'][-1]['changes'])
    assert next(row for row in load_orders() if row['id'] == 2).get('pack_status') in (None, '')


def test_removal_is_blocked_after_a_shipment_and_an_unknown_customer_must_be_chosen(client):
    _users(client)
    _orders()
    assert login(client, 'admin@oraglass.us').status_code == 302
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(b'Sales Delivery: XF9  Date: 11/23/2026\n(1) 1\n24 36\n'), 'plain.txt')},
        content_type='multipart/form-data',
    )
    review = uploaded.get_json()['review'][0]
    assert review['customer_locked'] is False
    missing = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 2})
    assert missing.status_code == 400
    assert 'customer' in missing.get_json()['error'].lower()
    wrong = client.post(
        f"/api/packing-inbox/{review['id']}/assign",
        json={'order_id': 2, 'customer_id': 'highq'},
    )
    assert wrong.status_code == 400
    saved = client.post(
        f"/api/packing-inbox/{review['id']}/assign",
        json={'order_id': 1, 'customer_id': 'highq'},
    )
    assert saved.status_code == 200, saved.get_json()
    shipped = client.post('/api/orders/1/ship-packed', json={'number': 'S1', 'date': '2026-11-23'})
    assert shipped.status_code == 200, shipped.get_json()
    blocked = client.delete('/api/orders/1/packing-lists/1')
    assert blocked.status_code == 400
    assert 'shipment' in blocked.get_json()['error'].lower()
    client.post('/logout')
    assert login(client, 'ap@highq.example').status_code == 302
    own = client.get('/api/orders/1/packing-lists.pdf')
    assert own.status_code == 200 and own.data.startswith(b'%PDF')
    assert client.get('/api/orders/2/backorder.pdf').status_code == 404

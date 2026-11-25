"""Intercompany invoices list the racks and weight from the packing lists they bill."""

import io

from storage import load_orders, save_invoices, save_orders
from tests.conftest import login, write_user
from tests.test_packing_pdf_invoice import HIGHQ_LIST
from tests.test_quote_export import pdf_text


def _glass():
    return {
        'name': 'Unit', 'pcs': 6, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
        'sqf': 1, 'total': 60,
        'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
    }


def _order():
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc',
        'status': 'sent',
        'currency': 'cad',
        'items': [_glass()],
        'packing_lists': [
            {
                'id': 1, 'current': True, 'number': 'XF261123001', 'rack': '1054',
                'racks': ['1054'], 'weight': 171, 'weight_unit': 'LBS',
                'lines': [{'serial': 1, 'qty': 4}],
            },
            {
                'id': 2, 'current': True, 'number': 'XF261123002', 'rack': '2002',
                'racks': ['2002', '2003'], 'weight': 90, 'weight_unit': 'LBS',
                'lines': [{'serial': 2, 'qty': 2}],
            },
        ],
    }])


def _invoice(invoice_id, number, items, kind='intercompany'):
    return {
        'id': invoice_id,
        'number': number,
        'order_id': 1,
        'kind': kind,
        'issuer': 'us',
        'company': 'Ora Glass (Canada) Inc' if kind == 'intercompany' else 'HighQ Developments Inc',
        'project': 'Misc',
        'currency': 'usd',
        'total': 100,
        'invoice_date': '2026-11-23',
        'due_date': '2026-12-23',
        'items': items,
    }


def test_two_packing_lists_sum_weight_and_list_each_racks(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _order()
    save_invoices([_invoice(53, 'INV-0053', [
        {'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 60},
        {'name': 'Glass', 'serial': 2, 'pcs': 2, 'total': 40},
    ])])
    body = client.get('/api/invoices/53').get_json()
    packing = body['packing']
    assert packing['rack_count'] == 3
    assert packing['racks'] == ['1054', '2002', '2003']
    assert packing['weight_label'] == '261 LBS'
    assert packing['lists'] == [
        {'id': 1, 'number': 'XF261123001', 'racks': ['1054'], 'weight': 171, 'weight_unit': 'LBS'},
        {'id': 2, 'number': 'XF261123002', 'racks': ['2002', '2003'], 'weight': 90, 'weight_unit': 'LBS'},
    ]
    page = client.get('/invoices/53').data
    assert b'id="invoice-racks"' in page
    assert b'id="invoice-weight"' in page
    pdf = pdf_text(client.get('/api/invoices/53/pdf').data)
    assert b'Racks: 3' in pdf
    assert b'XF261123001: 1054' in pdf
    assert b'171 LBS' in pdf
    assert b'XF261123002: 2002, 2003' in pdf
    assert b'Total weight: 261 LBS' in pdf


def test_a_later_invoice_does_not_repeat_an_earlier_list(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _order()
    save_invoices([
        dict(_invoice(53, 'INV-0053', [{'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 60}]), invoice_date='2026-11-23'),
        dict(_invoice(54, 'INV-0054', [{'name': 'Glass', 'serial': 2, 'pcs': 2, 'total': 40}]), invoice_date='2026-11-24'),
    ])
    first = client.get('/api/invoices/53').get_json()['packing']
    second = client.get('/api/invoices/54').get_json()['packing']
    assert [row['number'] for row in first['lists']] == ['XF261123001']
    assert first['rack_count'] == 1
    assert first['weight_label'] == '171 LBS'
    assert [row['number'] for row in second['lists']] == ['XF261123002']
    assert second['racks'] == ['2002', '2003']
    assert second['weight_label'] == '90 LBS'


def test_customer_invoice_does_not_list_racks(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _order()
    save_invoices([_invoice(60, 'INV-0060', [
        {'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 60},
    ], kind='customer')])
    body = client.get('/api/invoices/60').get_json()
    assert body.get('packing') in (None, {})
    pdf = pdf_text(client.get('/api/invoices/60/pdf').data)
    assert b'Racks:' not in pdf
    assert b'Total weight:' not in pdf


def test_mixed_weight_units_stay_separate(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _order()
    orders = load_orders()
    orders[0]['packing_lists'][1]['weight'] = 40
    orders[0]['packing_lists'][1]['weight_unit'] = 'KG'
    save_orders(orders)
    save_invoices([_invoice(53, 'INV-0053', [
        {'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 60},
        {'name': 'Glass', 'serial': 2, 'pcs': 2, 'total': 40},
    ])])
    packing = client.get('/api/invoices/53').get_json()['packing']
    assert packing['weight_label'] == '171 LBS + 40 KG'


def test_saved_packing_file_supplies_weight_for_an_older_invoice(client, data_dir):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    folder = data_dir / 'packing_files' / '1'
    folder.mkdir(parents=True)
    (folder / 'xf.txt').write_text(HIGHQ_LIST, encoding='utf-8')
    save_orders([{
        'id': 1,
        'company': 'HighQ Developments Inc',
        'project': 'PO1',
        'status': 'sent',
        'items': [_glass()],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF261123001', 'stored': 'xf.txt',
            'lines': [{'serial': 1, 'qty': 4}],
        }],
    }])
    save_invoices([_invoice(53, 'INV-0053', [
        {'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 100},
    ])])
    packing = client.get('/api/invoices/53').get_json()['packing']
    assert packing['rack_count'] == 1
    assert packing['lists'][0]['racks'] == ['1054']
    assert packing['lists'][0]['weight'] == 171
    assert packing['lists'][0]['weight_unit'] == 'LBS'
    assert packing['weight_label'] == '171 LBS'


def test_upload_stores_the_packing_list_weight_and_racks(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management', password='Validpass1',
    )
    save_orders([{
        'id': 4,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'approved',
        'currency': 'usd',
        'items': [_glass()],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    text = (
        'Sold To: Harbor Glass\nJob Name: Lobby\n'
        'Sales Delivery: XF9 Date: 11/24/2026\n'
        'Total Weight : Approx. 48.5 KG\n'
        '1054 A1900*1220*1800(THK:400*2)\n'
        '2002 B1900*1220*1800(THK:400*2)\n'
        '(1) 4\n24 36\n609.6 mm 914.4 mm\n'
    )
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(text.encode()), 'XF9.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    stored = load_orders()[0]['packing_lists'][0]
    assert stored['racks'] == ['1054', '2002']
    assert stored['weight'] == 48.5
    assert stored['weight_unit'] == 'KG'
    rows = [{
        **_invoice(7, 'INV-0007', [{'name': 'Glass', 'serial': 1, 'pcs': 4, 'total': 40}]),
        'order_id': 4,
    }]
    save_invoices(rows)
    packing = client.get('/api/invoices/7').get_json()['packing']
    assert packing['rack_count'] == 2
    assert packing['lists'][0]['racks'] == ['1054', '2002']
    assert packing['weight_label'] == '48.5 KG'

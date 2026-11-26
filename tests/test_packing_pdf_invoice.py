"""The production packing-list layout, partial delivery, and customer invoice status."""

import currency_rules
from storage import load_invoices, load_orders, save_orders
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


HIGHQ_LIST = """
                                                           Sales Delivery: XF261123001         Date:   11/23/2026
                            Packing List                   Customer P.O.#:
                            2312 Leatherwood Pl,                     Production
                           Lynden, WA 98264,USA
                                                           Update By: Production               Net: C.O.D

Sold To: HighQ                                             Job Name: PO1

                          RackDetail               Total   Total Pcs :            11
RackCode                  RackName                  1      Total SQ/FT :          116.52
                                                           Total Weight :         Approx. 171 LBS
      1054       A1900*1220*1800(THK:400*2)
                                                           Remarks:               1054

                                                 Glass Detail
ID QTY WIDTH          HEIGHT           FLOOR #                           ITEM DESCRIPTION
                                                     42 - 4mmSolarban60 VT Tempered +16mmSuperSpacer with
               27   73 15/32                         Argon(Polysulfide)+4mmClear Glass Tempered
(1)    4
             686 mm 1866 mm                          +14mmSuperSpacer with Argon(Polysulfide)+4mmClear Glass
                                                     Tempered
                                                     42 - 4mmSolarban60 VT Tempered +16mmSuperSpacer with
              25 5/8 16 31/32                        Argon(Polysulfide)+4mmClear Glass Tempered
(2)    4
             651 mm 431 mm                           +14mmSuperSpacer with Argon(Polysulfide)+4mmClear Glass
                                                     Tempered
             26 31/32 87 25/32                       24 - 4mmClear Glass Tempered +16mmSuperSpacer with Argon
(6)    3                                             (Polysulfide)+4mmClear Glass Tempered
             685 mm 2230 mm
"""


def _spec(width=24, height=36):
    return {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': width, 'height': height, 'unit': 'in'}


def test_highq_weight_is_read_when_the_amount_is_on_the_next_line():
    import packing
    split = packing.parse_packing_lines([
        'Sales Delivery: XF261123002 Date: 11/24/2026',
        'Total Weight :',
        'Appx. 1200 lb',
        '5016 A1900*1220*1800(THK:400*2)',
        '(1) 2',
        '24 36',
        '609.6 mm 914.4 mm',
    ])
    assert split['number'] == 'XF261123002'
    assert split['weight'] == 1200
    assert split['weight_unit'] == 'LBS'
    assert split['rack'] == '5016'
    both = packing.weights_by_number([
        'Sales Delivery: XF261123001 Date: 11/23/2026',
        'Total Weight : Approx. 171 LBS',
        'Sales Delivery: XF261123002 Date: 11/24/2026',
        'Total Weight :',
        'Appx. 1,200 lb',
    ])
    assert both['XF261123001']['weight'] == 171
    assert both['XF261123002']['weight'] == 1200


def test_weight_on_the_rack_line_is_the_printed_amount(client):
    """A two-column PDF puts 'Appx. 1,200 lb' on the same extracted line as the rack."""
    import io
    from reportlab.pdfgen import canvas
    import packing
    lines = [
        'Sales Delivery: XF261123002 Date: 11/24/2026',
        'Sold To: HighQ',
        'Job Name: PO1',
        'Total Weight :',
        '5016 A1900*1220*1800(THK:400*2)                    Appx. 1,200 lb',
        '(1)    2',
        '24 36',
        '609.6 mm 914.4 mm',
    ]
    parsed = packing.parse_packing_lines(lines)
    assert parsed['number'] == 'XF261123002'
    assert parsed['rack'] == '5016'
    assert parsed['weight'] == 1200
    assert parsed['weight_unit'] == 'LBS'
    assert parsed['weight_missing'] is False
    assert 'Weight not found.' not in parsed['warnings']
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer, pagesize=(792, 612))
    sheet.setFont('Courier', 9)
    sheet.drawString(40, 540, 'Sales Delivery: XF261123002 Date: 11/24/2026')
    sheet.drawString(40, 510, 'Job Name: PO1')
    sheet.drawString(430, 510, 'Total Weight :')
    sheet.drawString(40, 490, '5016 A1900*1220*1800(THK:400*2)')
    sheet.drawString(520, 490, 'Appx. 1200 lb')
    sheet.drawString(40, 450, '(1)    2')
    sheet.drawString(80, 430, '24 36')
    sheet.drawString(80, 410, '609.6 mm 914.4 mm')
    sheet.save()
    from_pdf = packing.parse_packing_pdf(buffer.getvalue())
    assert from_pdf['weight'] == 1200
    assert from_pdf['weight_unit'] == 'LBS'
    assert from_pdf['rack'] == '5016'
    blank = packing.parse_packing_lines([
        'Sales Delivery: XF261123002 Date: 11/24/2026',
        '5016 A1900*1220*1800(THK:400*2)',
        '(1)    2',
        '24 36',
        '609.6 mm 914.4 mm',
    ])
    assert blank['weight'] is None
    assert blank['weight_missing'] is True
    assert 'Weight not found.' in blank['warnings']
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@highq.example', 'customer', customer_id='highq',
        customer_name='HighQ Developments Inc', customer_subrole='management', password='Validpass1',
    )
    from storage import save_orders
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc',
        'project': 'PO1', 'status': 'approved', 'currency': 'cad',
        'items': [{
            'name': 'Unit', 'pcs': 2, 'floor': 'W01', 'price_pc': 10, 'price_sqf': 10,
            'sqf': 1, 'total': 20,
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO('\n'.join(lines).encode()), 'XF261123002.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    stored = next(row for row in __import__('storage').load_orders()[0]['packing_lists'] if row.get('current', True))
    assert stored['number'] == 'XF261123002'
    assert stored['weight'] == 1200
    assert stored['weight_unit'] == 'LBS'
    assert stored['weight_missing'] is False
    assert stored['racks'] == ['5016']


def test_adrian_packing_list_layout_stores_the_printed_weight():
    """pdftotext -layout of XF261123002. The weight is on the totals line, not calculated."""
    from pathlib import Path
    import packing
    text = Path('tests/fixtures/xf261123002_layout.txt').read_text(encoding='utf-8')
    parsed = packing.parse_packing_lines(text.splitlines())
    assert parsed['number'] == 'XF261123002'
    assert parsed['date'] == '2026-11-23'
    assert parsed['rack'] == '5016'
    assert parsed['racks'] == ['5016']
    assert parsed['weight'] == 463
    assert parsed['weight_unit'] == 'LBS'
    assert parsed['total_pcs'] == 3
    assert parsed['total_sqft'] == 75.29
    assert parsed['weight_missing'] is False
    samples = [
        ('Total Weight : Approx. 1,463 LBS', 1463, 'LBS'),
        ('Total Weight : Approx. 463.50 LB', 463.5, 'LBS'),
        ('Total Weight : Approx. 10.5 KG', 10.5, 'KG'),
    ]
    for line, amount, unit in samples:
        row = packing.parse_packing_lines([
            'Sales Delivery: XF9 Date: 11/23/2026',
            line,
            '5016 L3000*920*2200(THK:550)',
            '(1) 1',
            '24 36',
            '609.6 mm 914.4 mm',
        ])
        assert row['weight'] == amount, line
        assert row['weight_unit'] == unit, line


def test_overprinted_and_deduped_total_lines():
    """Bold labels are each glyph four times. dedupe_chars removes the copies."""
    from pathlib import Path
    import packing
    over = Path('tests/fixtures/xf261123002_overprinted.txt').read_text(encoding='utf-8').splitlines()
    deduped = Path('tests/fixtures/xf261123002_deduped.txt').read_text(encoding='utf-8').splitlines()
    assert over[:2] == [
        'RackDetail Total TTTToooottttaaaallll PPPPccccssss :::: 3333',
        'TTTToooottttaaaallll SSSSQQQQ////FFFFTTTT :::: 77775555....22229999',
    ]
    assert deduped == [
        'RackDetail Total Total Pcs : 3',
        'RackCode RackName 1 Total SQ/FT : 75.29',
        'Total Weight : Approx. 463 LBS',
    ]
    parsed = packing.parse_packing_lines(over)
    assert parsed['total_pcs'] == 3
    assert parsed['total_sqft'] == 75.29
    assert parsed['weight'] == 463
    assert parsed['weight_unit'] == 'LBS'
    clean = packing.parse_packing_lines(deduped)
    assert clean['total_pcs'] == 3
    assert clean['total_sqft'] == 75.29
    assert clean['weight'] == 463
    assert clean['weight_unit'] == 'LBS'
    doubled = packing.parse_packing_lines(['TToottaall PPccss :: 33', 'TToottaall SSQQ//FFTT :: 7755..2299'])
    assert doubled['total_pcs'] == 3
    assert doubled['total_sqft'] == 75.29
    assert packing.parse_packing_lines(['Total Pcs : 11', 'Total Weight : Appx. 1200 lb'])['total_pcs'] == 11
    assert packing.parse_packing_lines(['Total Pcs : 11', 'Total Weight : Appx. 1200 lb'])['weight'] == 1200


def test_pdfplumber_text_of_xf261123002_collapses_repeated_glyphs():
    """pdfplumber reads each totals glyph four times on Adrian's packing list."""
    from pathlib import Path
    import packing
    text = Path('tests/fixtures/xf261123002_pdfplumber.txt').read_text(encoding='utf-8')
    assert 'TTTToooottttaaaallll WWWWeeeeiiiigggghhhhtttt' in text
    assert 'PPPPccccssss' in text
    assert '77775555....22229999' in text
    parsed = packing.parse_packing_lines(text.splitlines())
    assert parsed['number'] == 'XF261123002'
    assert parsed['date'] == '2026-11-23'
    assert parsed['rack'] == '5016'
    assert parsed['weight'] == 463
    assert parsed['weight_unit'] == 'LBS'
    assert parsed['total_pcs'] == 3
    assert parsed['total_sqft'] == 75.29
    tight = packing.parse_packing_lines([
        'Sales Delivery: XF261123002 Date: 11/23/2026',
        'TotalPcs: 3',
        'TotalSQ/FT:75.29',
        'TotalWeight：Approx. 463 LBS',
        '5016 L3000*920*2200(THK:550)',
    ])
    assert tight['total_pcs'] == 3
    assert tight['total_sqft'] == 75.29
    assert tight['weight'] == 463


def test_real_xf261123002_pdf_reads_without_pdftotext(monkeypatch):
    import subprocess
    from pathlib import Path
    import packing
    real_run = subprocess.run

    def refuse_pdftotext(*args, **kwargs):
        blob = ' '.join(str(part) for part in args)
        if 'pdftotext' in blob:
            raise AssertionError('pdftotext was called')
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', refuse_pdftotext)
    payload = Path('tests/fixtures/XF261123002.pdf').read_bytes()
    assert packing.pdf_has_text(payload) is True
    lines = '\n'.join(packing.pdf_lines(payload))
    assert 'RackDetail Total Total Pcs : 3' in lines
    assert 'RackCode RackName 1 Total SQ/FT : 75.29' in lines
    assert 'Total Weight : Approx. 463 LBS' in lines
    assert 'TTTToooottttaaaallll' not in lines
    parsed = packing.parse_packing_pdf(payload)
    assert parsed['number'] == 'XF261123002'
    assert parsed['rack'] == '5016'
    assert parsed['weight'] == 463
    assert parsed['weight_unit'] == 'LBS'
    assert parsed['total_pcs'] == 3
    assert parsed['total_sqft'] == 75.29


def test_real_packing_list_reads_parenthesis_serials_and_ignores_the_description():
    import packing
    parsed = packing.parse_packing_lines(HIGHQ_LIST.splitlines())
    assert parsed['number'] == 'XF261123001'
    assert parsed['date'] == '2026-11-23'
    assert parsed['customer'] == 'HighQ'
    assert parsed['job'] == 'PO1'
    assert parsed['project'] == 'PO1'
    assert parsed['rack'] == '1054'
    assert parsed['racks'] == ['1054']
    assert parsed['weight'] == 171
    assert parsed['weight_unit'] == 'LBS'
    assert parsed['rack_name'] == 'A1900*1220*1800(THK:400*2)'
    assert parsed['total_pcs'] == 11
    assert [(row['serial'], row['qty']) for row in parsed['rows']] == [(1, 4), (2, 4), (6, 3)]
    assert parsed['parsed_pcs'] == 11
    assert all(row['description'] == '' for row in parsed['rows'])
    assert parsed['rows'][0]['width_mm'] == 686
    assert parsed['rows'][0]['height_mm'] == 1866


def test_partial_delivery_and_intercompany_do_not_wait_for_the_customer_invoice(client, monkeypatch):
    import io
    import quickbooks
    monkeypatch.setattr(quickbooks, 'FX_FETCH', lambda url: {'observations': [{'d': '2026-11-23', 'FXUSDCAD': {'v': '1.4000'}}]})
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@highq.example', 'customer', customer_id='highq',
        customer_name='HighQ Developments Inc', customer_subrole='management', password='Validpass1',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['highq'].update({'country': 'Canada', 'province': 'BC', 'credit_currency': 'cad'})
    save_customers(customers)
    pieces = [4, 4, 1, 1, 0, 3]
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc 11/15/2026',
        'po': '',
        'status': 'approved',
        'currency': 'cad',
        'extra_charges': [{'label': 'Delivery', 'amount': 50}],
        'items': [
            {
                'name': f'Unit {serial}', 'pcs': pcs, 'floor': f'W0{serial}',
                'price_pc': 10, 'price_sqf': 10, 'sqf': 1, 'total': 10 * pcs,
                'spec': _spec(),
            }
            for serial, pcs in enumerate(pieces, start=1)
        ],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/orders')
    assert b'id="order-invoice"' in page.data
    assert b'Not invoiced to customer' in page.data
    assert b'Invoice the customer for these packed pieces now' in page.data
    assert b'Wait until the order is complete' in page.data
    invoices_page = client.get('/invoices')
    assert b'scrollIntoView' in invoices_page.data
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(HIGHQ_LIST.encode()), 'XF261123001.txt')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    review = uploaded.get_json()['review'][0]
    assert review['reason'] == 'no_match'
    assert review['parsed']['parsed_pcs'] == 11
    assert any(choice['id'] == 1 for choice in review['choices'])
    assigned = client.post(f"/api/packing-inbox/{review['id']}/assign", json={'order_id': 1})
    assert assigned.status_code == 200, assigned.get_json()
    assert assigned.get_json()['pack_status'] == 'partial'
    assert assigned.get_json()['totals'] == {'ordered': 13, 'packed': 11, 'bo': 2}
    waiting = client.get('/api/orders?invoice=not_invoiced').get_json()
    assert [row['id'] for row in waiting] == [1]
    assert waiting[0]['invoice_state'] == 'ready'
    assert waiting[0]['invoice_state_label'] == 'Ready to invoice'
    assert waiting[0]['can_invoice_packed'] is True
    held = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': False})
    assert held.status_code == 200, held.get_json()
    assert held.get_json()['invoice_state'] == 'pending'
    assert held.get_json()['invoice_state_label'] == 'Customer invoice pending'
    assert held.get_json()['can_invoice_packed'] is True
    shipped = client.post('/api/orders/1/ship-packed', json={'number': 'XF261123001', 'date': '2026-11-23', 'rack': '1054'})
    assert shipped.status_code == 200, shipped.get_json()
    body = shipped.get_json()
    assert body['invoice_state'] == 'pending'
    assert body['shipments'][0]['pcs'] == 11
    assert body['shipping']['label'] == 'Partially shipped'
    slip = client.get('/api/orders/1/shipments/1/pdf')
    assert slip.status_code == 200
    assert slip.data.startswith(b'%PDF')
    text = pdf_text(slip.data)
    assert b'Ora Glass Inc' in text
    assert b'GST#' not in text
    assert b'XF261123001' in text
    invoices = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    assert len(invoices) == 1
    assert invoices[0]['kind'] == 'intercompany'
    assert invoices[0].get('shipment_notes') == ''
    assert invoices[0].get('shipment_source') == 'packed'
    assert b'Packed pieces' not in pdf_text(client.get(f"/api/invoices/{invoices[0]['id']}/pdf").data)
    assert invoices[0]['issuer'] == 'us'
    assert invoices[0]['currency'] == 'usd'
    assert invoices[0]['company'] == 'Ora Glass (Canada) Inc'
    assert sum(line.get('pcs') or 0 for line in invoices[0]['items']) == 11
    assert {line.get('serial') for line in invoices[0]['items']} == {1, 2, 6}
    serials = {row['serial']: row for row in body['packing_serials']}
    assert serials[1]['intercompany_invoiced'] == 4
    assert serials[6]['intercompany_invoiced'] == 3
    assert serials[3]['intercompany_invoiced'] == 0
    rest = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(
            b'Customer,Project,PO,Packing List,Date,Rack,Serial Number,Quantity\n'
            b'HighQ Developments Inc,Misc 11/15/2026,,XF261123002,2026-11-24,1055,3,1\n'
            b'HighQ Developments Inc,Misc 11/15/2026,,XF261123002,2026-11-24,1055,4,1\n'
        ), 'rest.csv')},
        content_type='multipart/form-data',
    )
    assert rest.status_code == 200, rest.get_json()
    assert rest.get_json()['saved'][0]['pack_status'] == 'packed'
    ready = client.get('/api/orders/1').get_json()
    assert ready['invoice_state'] == 'ready'
    assert ready['packing_totals']['bo'] == 0
    customer = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': True})
    assert customer.status_code == 200, customer.get_json()
    assert customer.get_json()['invoice_state'] == 'invoiced'
    bills = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    customer_bill = next(row for row in bills if row['kind'] == 'customer')
    assert customer_bill['issuer'] == 'ca'
    assert customer_bill.get('shipment_notes') == ''
    assert customer_bill.get('shipment_source') == 'packed'
    assert customer_bill['currency'] == 'cad'
    assert any(charge.get('label') == 'Delivery' for charge in customer_bill.get('extra_charges') or [])
    assert sum(line.get('pcs') or 0 for line in customer_bill['items'] if isinstance(line.get('pcs'), int)) == 13
    again = client.post('/api/orders/1/ship-packed', json={'number': 'XF261123002', 'date': '2026-11-24'})
    assert again.status_code == 200, again.get_json()
    final = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    intercompany = [row for row in final if row['kind'] == 'intercompany']
    assert len(intercompany) == 2
    assert sorted(sum(line.get('pcs') or 0 for line in row['items']) for row in intercompany) == [2, 11]
    assert len([row for row in final if row['kind'] == 'customer']) == 1
    assert client.get('/api/orders?invoice=not_invoiced').get_json() == []
    stored = next(row for row in load_orders() if row['id'] == 1)
    assert stored['pack_status'] == 'packed'


def _highq_order(pieces):
    from storage import load_customers, save_customers
    write_user('admin@oraglass.us', 'admin')
    customers = load_customers()
    customers['highq'] = dict(customers.get('highq') or {'id': 'highq', 'name': 'HighQ Developments Inc'})
    customers['highq'].update({'country': 'Canada', 'province': 'BC', 'credit_currency': 'cad', 'name': 'HighQ Developments Inc'})
    save_customers(customers)
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc 11/15/2026',
        'po': '',
        'status': 'approved',
        'currency': 'cad',
        'items': [
            {
                'name': f'Unit {serial}', 'pcs': pcs, 'floor': f'W0{serial}',
                'price_pc': 10, 'price_sqf': 10, 'sqf': 1, 'total': 10 * pcs,
                'spec': _spec(),
            }
            for serial, pcs in enumerate(pieces, start=1)
        ],
    }])


def test_a_backorder_does_not_block_invoicing_the_packed_pieces(client, monkeypatch):
    import io
    import quickbooks
    monkeypatch.setattr(quickbooks, 'FX_FETCH', lambda url: {'observations': [{'d': '2026-11-23', 'FXUSDCAD': {'v': '1.4000'}}]})
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    _highq_order([4, 2])
    assert login(client, 'admin@oraglass.us').status_code == 302
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(
            b'Customer,Project,PO,Packing List,Date,Rack,Serial Number,Quantity\n'
            b'HighQ Developments Inc,Misc 11/15/2026,,XF1,2026-11-23,1054,1,4\n'
        ), 'first.csv')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    assert uploaded.get_json()['saved'][0]['pack_status'] == 'partial'
    ready = client.get('/api/orders/1').get_json()
    assert ready['invoice_state'] == 'ready'
    assert ready['can_invoice_packed'] is True
    now = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': True})
    assert now.status_code == 200, now.get_json()
    billed = now.get_json()
    assert billed['invoice_state'] == 'partial'
    assert billed['invoice_state_label'] == 'Partially invoiced'
    assert billed['can_invoice_packed'] is False
    bills = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    customer_bills = [row for row in bills if row['kind'] == 'customer']
    intercompany = [row for row in bills if row['kind'] == 'intercompany']
    assert len(customer_bills) == 1
    assert sum(line.get('pcs') or 0 for line in customer_bills[0]['items']) == 4
    assert {line.get('serial') for line in customer_bills[0]['items']} == {1}
    assert len(intercompany) == 1
    assert sum(line.get('pcs') or 0 for line in intercompany[0]['items']) == 4
    again = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': True})
    assert again.status_code == 200, again.get_json()
    still = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    assert len(still) == 2
    rest = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(
            b'Customer,Project,PO,Packing List,Date,Rack,Serial Number,Quantity\n'
            b'HighQ Developments Inc,Misc 11/15/2026,,XF2,2026-11-24,1055,2,2\n'
        ), 'rest.csv')},
        content_type='multipart/form-data',
    )
    assert rest.status_code == 200, rest.get_json()
    opened = client.get('/api/orders/1').get_json()
    assert opened['invoice_state'] == 'ready'
    assert opened['can_invoice_packed'] is True
    finish = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': True})
    assert finish.status_code == 200, finish.get_json()
    assert finish.get_json()['invoice_state'] == 'invoiced'
    final = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    customer_bills = [row for row in final if row['kind'] == 'customer']
    intercompany = [row for row in final if row['kind'] == 'intercompany']
    assert sorted(sum(line.get('pcs') or 0 for line in row['items']) for row in customer_bills) == [2, 4]
    assert sorted(sum(line.get('pcs') or 0 for line in row['items']) for row in intercompany) == [2, 4]
    assert {line.get('serial') for row in customer_bills for line in row['items']} == {1, 2}


def test_us_packing_list_can_invoice_packed_pieces_without_the_partial_checkbox(client):
    import io
    from storage import load_customers, save_customers
    write_user('admin@oraglass.us', 'admin')
    customers = load_customers()
    customers['harbor-glass'] = dict(customers.get('harbor-glass') or {'id': 'harbor-glass', 'name': 'Harbor Glass'})
    customers['harbor-glass'].update({
        'name': 'Harbor Glass', 'country': 'United States', 'credit_currency': 'usd',
    })
    save_customers(customers)
    save_orders([{
        'id': 1,
        'customer_id': 'harbor-glass',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'status': 'approved',
        'currency': 'usd',
        'partial_invoicing': False,
        'items': [
            {
                'name': 'Unit 1', 'pcs': 4, 'floor': 'W01',
                'price_pc': 10, 'price_sqf': 10, 'sqf': 1, 'total': 40,
                'spec': _spec(),
            },
        ],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    uploaded = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(
            b'Customer,Project,PO,Packing List,Date,Rack,Serial Number,Quantity\n'
            b'Harbor Glass,Lobby,,XF9,2026-11-23,A1,1,2\n'
        ), 'part.csv')},
        content_type='multipart/form-data',
    )
    assert uploaded.status_code == 200, uploaded.get_json()
    ready = client.get('/api/orders/1').get_json()
    assert ready['needs_intercompany'] is False
    assert ready['can_invoice_packed'] is True
    now = client.post('/api/orders/1/customer-invoice', json={'invoice_customer': True, 'create_intercompany': False})
    assert now.status_code == 200, now.get_json()
    assert now.get_json()['invoice_state'] == 'partial'
    bills = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    assert len(bills) == 1
    assert bills[0]['kind'] == 'customer'
    assert bills[0]['issuer'] == 'us'
    assert sum(line.get('pcs') or 0 for line in bills[0]['items']) == 2

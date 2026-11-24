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


def test_real_packing_list_reads_parenthesis_serials_and_ignores_the_description():
    import packing
    parsed = packing.parse_packing_lines(HIGHQ_LIST.splitlines())
    assert parsed['number'] == 'XF261123001'
    assert parsed['date'] == '2026-11-23'
    assert parsed['customer'] == 'HighQ'
    assert parsed['job'] == 'PO1'
    assert parsed['project'] == 'PO1'
    assert parsed['rack'] == '1054'
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
    assert waiting[0]['invoice_state'] == 'not_invoiced'
    assert waiting[0]['invoice_state_label'] == 'Not invoiced'
    blocked = client.post('/api/orders/1/customer-invoice', json={})
    assert blocked.status_code == 400
    shipped = client.post('/api/orders/1/ship-packed', json={'number': 'XF261123001', 'date': '2026-11-23', 'rack': '1054'})
    assert shipped.status_code == 200, shipped.get_json()
    body = shipped.get_json()
    assert body['invoice_state'] == 'not_invoiced'
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
    customer = client.post('/api/orders/1/customer-invoice', json={})
    assert customer.status_code == 200, customer.get_json()
    assert customer.get_json()['invoice_state'] == 'invoiced'
    bills = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    customer_bill = next(row for row in bills if row['kind'] == 'customer')
    assert customer_bill['issuer'] == 'ca'
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

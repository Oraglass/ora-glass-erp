"""Line add-ons follow the pieces onto intercompany and partial customer invoices."""

import currency_rules
from storage import load_invoices, save_invoices
from tests.conftest import login, write_user


def _spec():
    return {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'}


def _glass(serial, total, pcs=1):
    return {
        'name': f'Unit {serial}', 'pcs': pcs, 'floor': f'W0{serial}',
        'price_pc': round(total / pcs, 2), 'price_sqf': 10, 'sqf': 1, 'total': total,
        'spec': _spec(), 'line_id': f'g{serial}', 'irregular_shape': True,
    }


def _surcharge(serial, total):
    return {
        'kind': 'irregular_surcharge',
        'name': f'Irregular shape surcharge – Unit {serial}',
        'pcs': 1, 'price_pc': total, 'total': total,
    }


def _highq(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('seller@oraglass.us', 'sales')
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['highq'] = dict(customers.get('highq') or {'id': 'highq'})
    customers['highq'].update({
        'name': 'HighQ Developments Inc', 'country': 'Canada', 'province': 'BC', 'credit_currency': 'cad',
    })
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302


def test_intercompany_invoice_includes_prorated_shape_surcharges(client):
    import quickbooks
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    _highq(client)
    from storage import save_orders
    items = []
    for serial, total in ((1, 872.68), (2, 872.68), (3, 621.59)):
        items.append(_glass(serial, total))
        items.append(_surcharge(serial, 50))
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc 11/15/2026',
        'status': 'approved',
        'currency': 'cad',
        'items': items,
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': 'R1', 'date': '2026-11-25',
            'lines': [{'serial': 1, 'qty': 1}, {'serial': 2, 'qty': 1}, {'serial': 3, 'qty': 1}],
        }],
    }])
    created = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
    assert created.status_code == 200, created.get_json()
    body = created.get_json()
    assert body['invoice_state'] == 'pending'
    assert body['invoice_state_label'] == 'Customer invoice pending'
    assert body['can_invoice_packed'] is True
    invoice = next(row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided'))
    assert invoice['kind'] == 'intercompany'
    assert invoice['currency'] == 'usd'
    assert invoice['total'] == quickbooks.cad_to_usd(2516.95, 1.4)
    surcharges = [line for line in invoice['items'] if line.get('kind') == 'irregular_surcharge']
    assert len(surcharges) == 3
    glass = [line for line in invoice['items'] if line.get('kind') != 'irregular_surcharge']
    assert sum(line.get('pcs') or 0 for line in glass) == 3
    assert abs(sum(line['total'] for line in surcharges) - 107.14) < 0.02
    assert not any(row.get('kind') == 'customer' for row in load_invoices())
    page = client.get('/orders').data
    assert b'id="list-invoice-now"' in page
    assert b'data-fully-packed' in page
    assert b'list-invoice-default' in page
    customer = client.post('/api/orders/1/customer-invoice', json={
        'create_intercompany': False, 'invoice_customer': True,
    })
    assert customer.status_code == 200, customer.get_json()
    assert customer.get_json()['invoice_state'] == 'invoiced'
    bill = next(row for row in load_invoices() if row.get('kind') == 'customer' and not row.get('voided'))
    assert any(line.get('kind') == 'irregular_surcharge' for line in bill['items'])
    assert abs(sum(line['total'] for line in bill['items'] if line.get('kind') == 'irregular_surcharge') - 150) < 0.02


def test_partial_invoices_prorate_addons_and_unpaid_intercompany_can_be_regenerated(client):
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    _highq(client)
    from storage import save_orders
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc 11/15/2026',
        'status': 'approved',
        'currency': 'cad',
        'items': [_glass(1, 400, pcs=4), _surcharge(1, 200), _glass(2, 100, pcs=2)],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': 'R1', 'date': '2026-11-25',
            'lines': [{'serial': 1, 'qty': 2}],
        }],
    }])
    held = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
    assert held.status_code == 200, held.get_json()
    assert held.get_json()['invoice_state'] == 'pending'
    assert held.get_json()['can_invoice_packed'] is True
    intercompany = next(row for row in load_invoices() if row.get('kind') == 'intercompany')
    addon = next(line for line in intercompany['items'] if line.get('kind') == 'irregular_surcharge')
    assert abs(addon['total'] - round(100 / 1.4, 2)) < 0.02
    assert sum(line.get('pcs') or 0 for line in intercompany['items']) == 2
    short = dict(intercompany)
    short['items'] = [line for line in intercompany['items'] if line.get('kind') != 'irregular_surcharge']
    short['total'] = round(sum(line['total'] for line in short['items']), 2)
    short['balance'] = short['total']
    short['igu_subtotal'] = short['total']
    short['igu_total'] = short['total']
    rows = load_invoices()
    rows[rows.index(intercompany)] = short
    save_invoices(rows)
    denied = client.post('/logout')
    assert denied.status_code == 302
    assert login(client, 'seller@oraglass.us').status_code == 302
    assert client.post(f"/api/invoices/{short['id']}/regenerate", json={}).status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get(f"/invoices/{short['id']}")
    assert b'id="regenerate-doc"' in page.data
    rebuilt = client.post(f"/api/invoices/{short['id']}/regenerate", json={})
    assert rebuilt.status_code == 200, rebuilt.get_json()
    live = [row for row in load_invoices() if row.get('order_id') == 1 and not row.get('voided')]
    voided = [row for row in load_invoices() if row.get('order_id') == 1 and row.get('voided')]
    assert len(voided) == 1
    assert len([row for row in live if row.get('kind') == 'intercompany']) == 1
    fresh = next(row for row in live if row.get('kind') == 'intercompany')
    assert fresh['id'] != short['id']
    assert any(line.get('kind') == 'irregular_surcharge' for line in fresh['items'])
    assert sum(line.get('pcs') or 0 for line in fresh['items']) == 2
    now = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': False, 'invoice_customer': True})
    assert now.status_code == 200, now.get_json()
    customer = next(row for row in load_invoices() if row.get('kind') == 'customer' and not row.get('voided'))
    assert sum(line.get('pcs') or 0 for line in customer['items']) == 2
    assert abs(sum(line['total'] for line in customer['items'] if line.get('kind') == 'irregular_surcharge') - 100) < 0.02
    assert now.get_json()['invoice_state'] == 'partial'
    assert now.get_json()['can_invoice_packed'] is False


def test_repeated_line_totals_are_replaced_and_the_seed_rate_date_is_hidden(client):
    import quickbooks
    from tests.test_quote_export import pdf_text
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    _highq(client)
    from storage import save_orders
    save_orders([{
        'id': 1,
        'customer_id': 'highq',
        'company': 'HighQ Developments Inc',
        'project': 'Misc 11/15/2026',
        'status': 'approved',
        'currency': 'cad',
        'items': [_glass(1, 140, pcs=1), _surcharge(1, 14), _glass(2, 140, pcs=1)],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054', 'date': '2026-11-15',
            'lines': [{'serial': 1, 'qty': 1}, {'serial': 2, 'qty': 1}],
        }],
    }])
    save_invoices([{
        'id': 53,
        'number': 'INV-0053',
        'order_id': 1,
        'kind': 'intercompany',
        'issuer': 'us',
        'currency': 'usd',
        'company': 'Ora Glass (Canada) Inc',
        'fx_rate': 1.4,
        'fx_date': '2000-01-01',
        'cad_total': 280,
        'total': 200,
        'balance': 200,
        'igu_subtotal': 200,
        'igu_total': 200,
        'invoice_date': '2026-11-15',
        'items': [
            {'name': 'Unit 1', 'serial': 1, 'pcs': 1, 'qty': 1, 'unit_price': 200, 'total': 200, 'spec': _spec()},
            {'name': 'Unit 2', 'serial': 2, 'pcs': 1, 'qty': 1, 'unit_price': 200, 'total': 200, 'spec': _spec()},
        ],
    }])
    shown = client.get('/api/invoices/53').get_json()
    assert shown['fx_date'] == '2026-11-15'
    assert shown['total'] == 200
    amounts = [line['total'] for line in shown['items']]
    assert 200 not in amounts
    glass = [line for line in shown['items'] if line.get('kind') != 'irregular_surcharge']
    surcharge = next(line for line in shown['items'] if line.get('kind') == 'irregular_surcharge')
    assert [line.get('serial') for line in glass] == [1, 2]
    assert glass[0]['total'] == quickbooks.cad_to_usd(140, 1.4)
    assert surcharge['total'] == quickbooks.cad_to_usd(14, 1.4)
    assert glass[1]['total'] == quickbooks.cad_to_usd(140, 1.4)
    assert abs(sum(amounts) - quickbooks.cad_to_usd(294, 1.4)) < 0.02
    pdf = pdf_text(client.get('/api/invoices/53/pdf').data)
    assert b'$100.00' in pdf
    assert b'$10.00' in pdf
    changed = client.post('/api/invoices/53/exchange-rate', json={'rate': 2})
    assert changed.status_code == 200, changed.get_json()
    repaired = changed.get_json()['items']
    assert len({line['total'] for line in repaired}) > 1
    assert all(line['total'] != changed.get_json()['total'] for line in repaired)

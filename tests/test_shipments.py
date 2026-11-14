"""Invoices follow shipments, and a quantity is billed only once."""

from storage import load_customers, load_invoices, save_customers
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item


def _order(client, customer_id='harbor-glass', pcs=4, irregular=False):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
        password='Validpass1',
    )
    customers = load_customers()
    customers['harbor-glass'].update({
        'country': 'United States', 'credit_limit': 100000, 'credit_currency': 'usd', 'credit_approved': True,
    })
    save_customers(customers)
    assert login(client, 'sales@harbor.example').status_code == 302
    item = _size_item()
    item['pcs'] = pcs
    item['spec'] = dict(SPEC)
    item['irregular_shape'] = irregular
    if not irregular:
        item['spec'] = dict(item['spec'])
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={'customer_id': customer_id})
    assert created.status_code == 200, created.get_json()
    return created.get_json()['order_id']


def _ship(client, order_id, qty, number, index=0):
    return client.post(f'/api/orders/{order_id}/shipments', json={
        'number': number,
        'date': '2026-07-29',
        'rack': 'A1',
        'lines': [{'index': index, 'qty': qty}],
    })


def test_us_order_invoices_once_when_fully_shipped_and_cannot_overship(client):
    order_id = _order(client, pcs=4)
    first = _ship(client, order_id, 2, 'XF1')
    assert first.status_code == 200, first.get_json()
    body = first.get_json()
    assert body['shipping']['label'] == 'Partially shipped'
    assert body['shipping']['remaining_pcs'] == 2
    assert body['shipments'][0]['number'] == 'XF1'
    assert body['shipments'][0]['invoiced'] is False
    assert load_invoices() == []
    too_many = _ship(client, order_id, 3, 'XF2')
    assert too_many.status_code == 400
    assert 'left to ship' in too_many.get_json()['error']
    second = _ship(client, order_id, 2, 'XF2')
    assert second.status_code == 200, second.get_json()
    done = second.get_json()
    assert done['shipping']['label'] == 'Fully shipped & invoiced'
    invoices = [row for row in load_invoices() if row.get('order_id') == order_id and not row.get('voided')]
    assert len(invoices) == 1
    assert sum(line.get('pcs') or 0 for line in invoices[0]['items'] if line.get('kind') != 'irregular_surcharge') == 4
    open_rows = client.get('/api/shipments/open').get_json()
    assert order_id not in [row['id'] for row in open_rows]
    page = client.get('/shipments')
    assert page.status_code == 200
    assert b'Open shipments' in page.data
    order_page = client.get(f'/orders/{order_id}')
    assert b'Record shipment' in order_page.data
    assert b'Shipments' in order_page.data


def test_partial_us_invoices_follow_each_shipment(client):
    from storage import load_orders, save_orders
    order_id = _order(client, pcs=4)
    orders = load_orders()
    order = next(row for row in orders if row['id'] == order_id)
    order['items'].append({
        'kind': 'irregular_surcharge',
        'name': 'Irregular shape surcharge',
        'pcs': 4,
        'total': 200,
        'unit_price': 50,
        'price_pc': 50,
    })
    save_orders(orders)
    chosen = client.post(f'/api/orders/{order_id}/partial-invoicing', json={'partial_invoicing': True})
    assert chosen.status_code == 200, chosen.get_json()
    assert chosen.get_json()['billing_mode'] == 'us_partial'
    first = _ship(client, order_id, 1, 'XF-A')
    assert first.status_code == 200, first.get_json()
    assert first.get_json()['shipments'][0]['invoice_number']
    second = _ship(client, order_id, 3, 'XF-B')
    assert second.status_code == 200, second.get_json()
    invoices = [row for row in load_invoices() if row.get('order_id') == order_id]
    assert len(invoices) == 2
    assert {row.get('shipment_number') for row in invoices} == {'XF-A', 'XF-B'}
    glass_qty = []
    for invoice in invoices:
        glass_qty.append(sum(line.get('pcs') or 0 for line in invoice['items'] if line.get('kind') != 'irregular_surcharge'))
    assert sorted(glass_qty) == [1, 3]
    surcharge = [line for invoice in invoices for line in invoice['items'] if line.get('kind') == 'irregular_surcharge']
    assert surcharge
    assert abs(sum(line['total'] for line in surcharge) - 200) < 0.02


def test_open_windows_and_canada_invoice_per_shipment(client, monkeypatch):
    import quickbooks
    monkeypatch.setattr(quickbooks, 'FX_FETCH', lambda url: {'observations': [{'d': '2026-07-29', 'FXUSDCAD': {'v': '1.4000'}}]})
    order_id = _order(client, pcs=4)
    customers = load_customers()
    customers['harbor-glass'].update({'id': 'open-windows', 'name': 'Open Windows Inc'})
    # The order points at harbor-glass. Point that record at the Open Windows name.
    customers['harbor-glass']['name'] = 'Open Windows Inc'
    customers['harbor-glass']['id'] = 'harbor-glass'
    save_customers(customers)
    first = _ship(client, order_id, 1, 'XF260729003')
    assert first.status_code == 200, first.get_json()
    second = _ship(client, order_id, 3, 'XF260729004')
    assert second.status_code == 200, second.get_json()
    invoices = [row for row in load_invoices() if row.get('order_id') == order_id]
    assert len(invoices) == 2
    assert all(row['issuer'] == 'us' and row['kind'] == 'customer' for row in invoices)
    assert all(row['company'] == 'Open Windows Inc' for row in invoices)
    assert {row['shipment_number'] for row in invoices} == {'XF260729003', 'XF260729004'}

    customers = load_customers()
    customers['maple'] = {
        'id': 'maple', 'name': 'Maple Glass', 'country': 'Canada', 'province': 'BC',
        'credit_limit': 100000, 'credit_currency': 'cad', 'credit_approved': True, 'tier': 'T2', 'status': 'active',
    }
    save_customers(customers)
    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    item = _size_item()
    item['pcs'] = 2
    item['spec'] = dict(SPEC)
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'cad', 'project': 'Toronto', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={'customer_id': 'maple'})
    assert created.status_code == 200, created.get_json()
    canada_id = created.get_json()['order_id']
    partial = _ship(client, canada_id, 1, 'CA-SHIP-1')
    assert partial.status_code == 200, partial.get_json()
    intercompany = [row for row in load_invoices() if row.get('order_id') == canada_id]
    assert len(intercompany) == 1
    assert intercompany[0]['kind'] == 'intercompany'
    assert intercompany[0]['issuer'] == 'us'
    assert intercompany[0]['shipment_number'] == 'CA-SHIP-1'
    assert partial.get_json()['shipping']['label'] == 'Partially shipped'
    finished = _ship(client, canada_id, 1, 'CA-SHIP-2')
    assert finished.status_code == 200, finished.get_json()
    rows = [row for row in load_invoices() if row.get('order_id') == canada_id]
    assert len([row for row in rows if row['kind'] == 'intercompany']) == 2
    customer_invoices = [row for row in rows if row['kind'] == 'customer' and row['issuer'] == 'ca']
    assert len(customer_invoices) == 1
    assert finished.get_json()['shipping']['label'] == 'Fully shipped & invoiced'

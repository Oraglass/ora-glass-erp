"""Order edits, status, credit, invoices, and payments."""

from datetime import date, timedelta

from app import PRICING
from quoting import reprice_size_line
from storage import load_customers, load_invoices, load_orders, load_payments, load_quotes, load_tiers, load_users, save_customers, save_users
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item
from tests.test_quote_export import pdf_text


def _employee(email, modules, **flags):
    write_user(email, 'employee', password='Validpass1')
    users = load_users()
    users[email]['modules'] = list(modules)
    for key, value in flags.items():
        users[email][key] = value
    save_users(users)


def _customer(email, customer_id, name, tier='T2', subrole='management', limit=0, currency='usd', terms='Net 30', days=None):
    write_user(
        email, 'customer', customer_id=customer_id, customer_name=name,
        customer_tier=tier, customer_subrole=subrole, tier_override='T5',
        allowed_tiers=['T5'], password='Validpass1',
    )
    customers = load_customers()
    customers[customer_id]['credit_limit'] = limit
    customers[customer_id]['credit_currency'] = currency
    customers[customer_id]['payment_terms'] = terms
    if days is not None:
        customers[customer_id]['payment_terms_days'] = days
    save_customers(customers)


def _quote(client, project='Lobby'):
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'usd',
        'project': project,
        'type': 'size',
        'tier': 'T5',
    })
    assert saved.status_code == 200
    return saved.get_json()['id']


def _convert(client, quote_id, customer_id):
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': customer_id})
    assert created.status_code == 200, created.data
    return created.get_json()


def test_admin_edits_lines_discount_and_charges_and_the_log_is_append_only(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('sales@harbor.example', 'harbor-glass', 'Harbor Glass', subrole='sales', limit=100000)
    _customer('boss@harbor.example', 'harbor-glass', 'Harbor Glass', subrole='management', limit=100000)
    _employee('plain@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'])
    _employee('editor@oraglass.us', ['dashboard', 'quotes', 'quote-history', 'orders'], edit_orders=True)

    assert login(client, 'sales@harbor.example').status_code == 302
    quote_id = _quote(client)
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    denied = client.put(f'/api/orders/1', json={'project': 'Nope'})
    assert denied.status_code == 403
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    created = _convert(client, quote_id, 'harbor-glass')
    order_id = created['order_id']
    before = next(row for row in load_quotes() if row['id'] == quote_id)
    priced = reprice_size_line(SPEC, 2, 'T2', 'usd', PRICING, load_tiers())
    original = reprice_size_line(SPEC, 1, 'T2', 'usd', PRICING, load_tiers())
    edited = client.put(f'/api/orders/{order_id}', json={
        'project': 'Renamed lobby',
        'notes': 'Call before delivery',
        'discount': {'type': 'percent', 'value': 10},
        'extra_charges': [{'label': 'Delivery', 'amount': 25}, {'label': 'Crane', 'amount': 10}],
        'items': [{'name': 'Unit - 24 x 36 in', 'pcs': 2, 'spec': SPEC}],
        'changelog': [{'user': 'nobody', 'changes': []}],
    })
    assert edited.status_code == 200, edited.data
    body = edited.get_json()
    subtotal = priced['total']
    discount = round(subtotal * 0.10, 2)
    igu = round(subtotal - discount, 2)
    grand = round(igu + 35, 2)
    assert body['igu_subtotal'] == subtotal
    assert body['discount_amount'] == discount
    assert body['igu_total'] == igu
    assert body['total'] == grand
    assert body['items'][0]['pcs'] == 2
    assert body['items'][0]['total'] == priced['total']
    log = body['changelog']
    assert len(log) == 1
    assert log[0]['user'] == 'admin@oraglass.us'
    assert log[0]['at'].endswith('+00:00')
    fields = {change['field']: change for change in log[0]['changes']}
    assert fields['project']['old'] == 'Lobby'
    assert fields['project']['new'] == 'Renamed lobby'
    assert fields['notes']['new'] == 'Call before delivery'
    assert fields['discount']['new']['type'] == 'percent'
    assert any(change.get('action') == 'added' and change['field'] == 'extra_charge' for change in log[0]['changes'])
    line = next(change for change in log[0]['changes'] if change['field'] == 'line')
    assert line['old']['pcs'] == 1
    assert line['new']['pcs'] == 2
    assert 'nobody' not in str(log)
    quote = next(row for row in load_quotes() if row['id'] == quote_id)
    assert quote['tier'] == before['tier']
    assert quote['items'] == before['items']
    assert quote['order_id'] == order_id

    fixed = client.put(f'/api/orders/{order_id}', json={
        'discount': {'type': 'amount', 'value': 5},
        'extra_charges': [{'label': 'Delivery', 'amount': 25}],
        'items': [{'name': 'Unit - 24 x 36 in', 'pcs': 2, 'spec': SPEC}],
    })
    assert fixed.status_code == 200
    fixed_body = fixed.get_json()
    assert fixed_body['discount_amount'] == 5
    assert fixed_body['igu_total'] == round(subtotal - 5, 2)
    assert fixed_body['total'] == round(subtotal - 5 + 25, 2)
    assert len(fixed_body['changelog']) == 2
    assert client.delete(f'/api/orders/{order_id}/changelog').status_code == 404

    client.post('/logout')
    assert login(client, 'plain@oraglass.us').status_code == 302
    assert client.put(f'/api/orders/{order_id}', json={'notes': 'nope'}).status_code == 403
    page = client.get(f'/orders/{order_id}')
    assert b'const CAN_EDIT = false' in page.data
    client.post('/logout')
    assert login(client, 'editor@oraglass.us').status_code == 302
    assert client.put(f'/api/orders/{order_id}', json={'notes': 'Editor note'}).status_code == 200
    assert original['total'] != priced['total']


def test_status_flow_credit_gate_invoice_and_prepayment(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('boss@harbor.example', 'harbor-glass', 'Harbor Glass', limit=0, terms='Net 45')
    _customer('other@summit.example', 'summit-glass', 'Summit Glass', subrole='management', limit=50000, terms='Due on receipt')
    _employee('status@oraglass.us', ['dashboard', 'orders', 'invoices', 'financial'], update_order_status=False, record_payments=False)
    _employee('clerk@oraglass.us', ['dashboard', 'orders', 'invoices', 'financial', 'quote-history'], update_order_status=True, record_payments=True, edit_orders=True)

    assert login(client, 'boss@harbor.example').status_code == 302
    quote_id = _quote(client, 'Over limit')
    blocked = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert blocked.status_code == 400
    assert b'available credit' in blocked.data
    customers = load_customers()
    customers['harbor-glass']['credit_limit'] = 100000
    save_customers(customers)
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200
    order_id = created.get_json()['order_id']
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'}).status_code == 403
    client.post('/logout')

    assert login(client, 'status@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'}).status_code == 403
    assert client.post('/api/payments', json={
        'customer_id': 'harbor-glass', 'date': '2026-11-11', 'amount': 10,
        'currency': 'usd', 'method': 'cheque',
    }).status_code == 403
    client.post('/logout')

    assert login(client, 'clerk@oraglass.us').status_code == 302
    customers = load_customers()
    customers['harbor-glass']['credit_limit'] = 0
    save_customers(customers)
    refused = client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'})
    assert refused.status_code == 400
    assert b'Shortfall' in refused.data
    order = client.get(f'/api/orders/{order_id}').get_json()
    total = order['total']
    paid = client.post('/api/payments', json={
        'customer_id': 'harbor-glass',
        'date': '2026-11-11',
        'amount': total,
        'currency': 'usd',
        'method': 'e-transfer',
        'reference': 'ET-1',
        'notes': 'Deposit',
    })
    assert paid.status_code == 200
    payment_id = paid.get_json()['id']
    assert paid.get_json()['unallocated'] == total
    too_much = client.post(f'/api/payments/{payment_id}/allocations', json={
        'allocations': [{'target_type': 'order', 'target_id': order_id, 'amount': round(total + 1, 2)}],
    })
    assert too_much.status_code == 400
    half = round(total / 2, 2)
    partial = client.post(f'/api/payments/{payment_id}/allocations', json={
        'allocations': [{'target_type': 'order', 'target_id': order_id, 'amount': half}],
    })
    assert partial.status_code == 200
    assert partial.get_json()['unallocated'] == round(total - half, 2)
    still = client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'})
    assert still.status_code == 400
    rest = client.post(f'/api/payments/{payment_id}/allocations', json={
        'allocations': [{'target_type': 'order', 'target_id': order_id, 'amount': round(total - half, 2)}],
    })
    assert rest.status_code == 200
    assert rest.get_json()['unallocated'] == 0
    sent = client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'})
    assert sent.status_code == 200
    assert sent.get_json()['status'] == 'sent'
    assert any(change['field'] == 'status' and change['new'] == 'Sent to production' for change in sent.get_json()['changelog'][-1]['changes'])
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'delivered'}).status_code == 400
    made = client.post(f'/api/orders/{order_id}/status', json={'status': 'made'})
    assert made.status_code == 200
    delivered = client.post(f'/api/orders/{order_id}/status', json={'status': 'delivered'})
    assert delivered.status_code == 200
    assert delivered.get_json()['status'] == 'delivered'
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'delivered'}).status_code == 400
    invoices = load_invoices()
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice['number'] == 'INV-0001'
    assert invoice['order_id'] == order_id
    assert invoice['quote_id'] == quote_id
    assert (date.fromisoformat(invoice['due_date']) - date.fromisoformat(invoice['invoice_date'])).days == 45
    payments = load_payments()
    targets = [row.get('target_type') for row in payments[0]['allocations']]
    assert targets == ['invoice', 'invoice']
    view = client.get(f'/api/invoices/{invoice["id"]}').get_json()
    assert view['status'] == 'paid'
    assert view['balance'] == 0
    stored_total = invoice['total']
    changed = client.put(f'/api/orders/{order_id}', json={'notes': 'After invoice'})
    assert changed.status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    admin_edit = client.put(f'/api/orders/{order_id}', json={'notes': 'Admin after invoice'})
    assert admin_edit.status_code == 200
    assert any(change['field'] == 'notes' for change in admin_edit.get_json()['changelog'][-1]['changes'])
    assert load_invoices()[0]['total'] == stored_total
    assert load_invoices()[0]['notes'] if False else load_invoices()[0].get('items') == invoice['items']
    page = client.get(f'/orders/{order_id}')
    assert b'const CAN_STATUS = true' in page.data
    assert b'Mark sent to production' in page.data
    assert b'id="coverage-breakdown"' in page.data or b'coverage' in page.data
    users_page = client.get('/users')
    assert b'id="u-edit-orders"' in users_page.data
    assert b'id="u-update-order-status"' in users_page.data
    assert b'id="u-record-payments"' in users_page.data
    invoices_page = client.get('/invoices')
    assert b'id="invoice-list"' in invoices_page.data
    assert b'id="payment-form"' in invoices_page.data
    pdf = client.get(f'/api/invoices/{invoice["id"]}/pdf')
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    text = pdf_text(pdf.data)
    assert b'INV-0001' in text
    assert b'Lynden' in text

    other_quote = None
    client.post('/logout')
    assert login(client, 'other@summit.example').status_code == 302
    assert client.get('/api/invoices').get_json() == []
    assert client.get(f'/api/invoices/{invoice["id"]}').status_code == 404
    assert client.get('/api/payments').get_json() == []
    credit = client.get('/api/credit').get_json()
    assert credit['customer_id'] == 'summit-glass'
    assert client.post('/api/payments', json={'customer_id': 'summit-glass', 'date': '2026-11-11', 'amount': 5, 'currency': 'usd', 'method': 'card'}).status_code == 403


def test_partial_payment_invoice_status_and_void(client):
    write_user('admin@oraglass.us', 'admin')
    _customer('boss@harbor.example', 'harbor-glass', 'Harbor Glass', limit=5000, terms='Custom', days=12)
    _employee('clerk@oraglass.us', ['orders', 'invoices', 'financial', 'quote-history'], update_order_status=True, record_payments=True)
    assert login(client, 'boss@harbor.example').status_code == 302
    quote_id = _quote(client, 'Partial')
    order_id = client.post(f'/api/quotes/{quote_id}/order', json={}).get_json()['order_id']
    client.post('/logout')
    assert login(client, 'clerk@oraglass.us').status_code == 302
    order = client.get(f'/api/orders/{order_id}').get_json()
    total = order['total']
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'sent'}).status_code == 200
    assert client.post(f'/api/orders/{order_id}/status', json={'status': 'made'}).status_code == 200
    delivered = client.post(f'/api/orders/{order_id}/status', json={'status': 'delivered'})
    assert delivered.status_code == 200
    invoice = load_invoices()[0]
    assert (date.fromisoformat(invoice['due_date']) - date.fromisoformat(invoice['invoice_date'])).days == 12
    assert client.get(f'/api/invoices/{invoice["id"]}').get_json()['status'] == 'unpaid'
    payment = client.post('/api/payments', json={
        'customer_id': 'harbor-glass', 'date': '2026-11-11', 'amount': total,
        'currency': 'usd', 'method': 'wire', 'reference': 'W-9',
    }).get_json()
    part = round(total / 2, 2)
    allocated = client.post(f'/api/payments/{payment["id"]}/allocations', json={
        'allocations': [{'target_type': 'invoice', 'target_id': invoice['id'], 'amount': part}],
    })
    assert allocated.status_code == 200
    view = client.get(f'/api/invoices/{invoice["id"]}').get_json()
    assert view['status'] == 'partial'
    assert view['balance'] == round(total - part, 2)
    assert 'allocated' in str(load_payments()[0]['log'])
    voided = client.post(f'/api/payments/{payment["id"]}/void', json={'reason': 'Duplicate'})
    assert voided.status_code == 200
    assert voided.get_json()['voided'] is True
    assert client.post(f'/api/payments/{payment["id"]}/void', json={'reason': 'Again'}).status_code == 400
    assert client.delete(f'/api/payments/{payment["id"]}').status_code == 404
    assert load_payments()[0]['void_reason'] == 'Duplicate'
    assert client.get(f'/api/invoices/{invoice["id"]}').get_json()['status'] == 'unpaid'

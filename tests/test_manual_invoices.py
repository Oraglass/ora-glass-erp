"""Manual invoices and credit notes."""

from datetime import date

from storage import load_customers, load_users, save_customers, save_orders, save_users
from tests.conftest import login, write_user


def _books():
    write_user('admin@oraglass.us', 'admin')
    write_user('buyer@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass', customer_subrole='management')
    write_user('clerk@oraglass.us', 'employee')
    write_user('viewer@oraglass.us', 'employee')
    users = load_users()
    users['buyer@harbor.example']['modules'] = ['dashboard', 'invoices', 'financial', 'orders']
    users['clerk@oraglass.us']['modules'] = ['dashboard', 'invoices', 'financial', 'orders']
    users['clerk@oraglass.us']['issue_invoices'] = True
    users['viewer@oraglass.us']['modules'] = ['dashboard', 'invoices']
    users['viewer@oraglass.us']['issue_invoices'] = False
    save_users(users)
    customers = load_customers()
    customers['harbor']['credit_limit'] = 1000
    customers['harbor']['credit_currency'] = 'usd'
    customers['harbor']['payment_terms'] = 'Net 15'
    customers['other'] = {
        'id': 'other', 'name': 'Other Glass', 'tier': 'T2', 'status': 'active',
        'credit_limit': 0, 'credit_currency': 'usd', 'payment_terms': 'Net 30',
    }
    save_customers(customers)
    save_orders([{
        'id': 7,
        'customer_id': 'harbor',
        'company': 'Harbor Glass',
        'project': 'Lobby',
        'currency': 'usd',
        'status': 'approved',
        'total': 500,
        'items': [],
    }])


def _lines(description='Site measure', qty=2, price=100):
    return [{'description': description, 'qty': qty, 'unit_price': price}]


def test_linked_and_standalone_invoices_and_credit_notes(client):
    _books()
    assert login(client, 'viewer@oraglass.us').status_code == 302
    denied = client.post('/api/invoices', json={
        'doc_type': 'invoice', 'customer_id': 'harbor', 'lines': _lines(),
    })
    assert denied.status_code == 403
    client.post('/logout')

    assert login(client, 'clerk@oraglass.us').status_code == 302
    linked = client.post('/api/invoices', json={
        'doc_type': 'invoice',
        'order_id': 7,
        'lines': _lines('Extra install', 1, 200),
    })
    assert linked.status_code == 200
    body = linked.get_json()
    assert body['number'].startswith('INV-')
    assert body['doc_type'] == 'invoice'
    assert body['customer_id'] == 'harbor'
    assert body['company'] == 'Harbor Glass'
    assert body['order_id'] == 7
    assert body['total'] == 200
    assert body['tax_free'] is True
    assert body['audit'][0]['action'] == 'created'
    invoice_day = date.fromisoformat(body['invoice_date'])
    due = date.fromisoformat(body['due_date'])
    assert (due - invoice_day).days == 15

    standalone = client.post('/api/invoices', json={
        'doc_type': 'invoice',
        'customer_id': 'harbor',
        'due_date': '2026-12-01',
        'lines': _lines('Delivery', 1, 80),
    })
    assert standalone.status_code == 200
    assert standalone.get_json()['number'].startswith('INV-')
    assert standalone.get_json()['number'] != body['number']
    assert standalone.get_json()['due_date'] == '2026-12-01'
    assert standalone.get_json()['order_id'] is None

    note = client.post('/api/invoices', json={
        'doc_type': 'credit_note',
        'order_id': 7,
        'reason': 'Broken lite',
        'lines': _lines('Replacement credit', 1, 50),
    })
    assert note.status_code == 200
    credit = note.get_json()
    assert credit['number'].startswith('CN-')
    assert credit['doc_type'] == 'credit_note'
    assert credit['order_id'] == 7
    assert credit['reason'] == 'Broken lite'
    assert credit['unapplied'] == 50
    assert credit['status'] == 'open'

    loose = client.post('/api/invoices', json={
        'doc_type': 'credit_note',
        'customer_id': 'harbor',
        'reason': 'Goodwill',
        'lines': _lines('Goodwill', 1, 25),
    })
    assert loose.status_code == 200
    assert loose.get_json()['number'].startswith('CN-')
    assert loose.get_json()['order_id'] is None

    credit_view = client.get('/api/credit?customer_id=harbor&currency=usd').get_json()
    # Invoices 280, unapplied credits 75, limit 1000. Available = 1000 - 280 + 75.
    assert credit_view['open_receivables'] == 280
    assert credit_view['unapplied_credits'] == 75
    assert credit_view['available_credit'] == 795
    assert credit_view['balance'] == 205

    applied = client.post(f"/api/invoices/{credit['id']}/apply", json={
        'invoice_id': body['id'], 'amount': 50,
    })
    assert applied.status_code == 200
    assert applied.get_json()['unapplied'] == 0
    assert applied.get_json()['status'] == 'applied'
    invoice = client.get(f"/api/invoices/{body['id']}").get_json()
    assert invoice['balance'] == 150
    after = client.get('/api/credit?customer_id=harbor&currency=usd').get_json()
    assert after['open_receivables'] == 230
    assert after['unapplied_credits'] == 25
    assert after['available_credit'] == 795

    listed = client.get('/api/invoices?type=credit_note').get_json()
    assert {row['number'] for row in listed} == {credit['number'], loose.get_json()['number']}
    assert all(row['type_label'] == 'Credit note' for row in listed)
    order = client.get('/api/orders/7').get_json()
    numbers = {row['number'] for row in order['documents']}
    assert body['number'] in numbers
    assert credit['number'] in numbers

    pdf = client.get(f"/api/invoices/{body['id']}/pdf")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    credit_pdf = client.get(f"/api/invoices/{credit['id']}/pdf")
    assert credit_pdf.status_code == 200
    assert credit_pdf.data.startswith(b'%PDF')
    assert len(credit_pdf.data) > 1000

    voided = client.post(f"/api/invoices/{loose.get_json()['id']}/void", json={'reason': 'Issued in error'})
    assert voided.status_code == 200
    assert voided.get_json()['status'] == 'void'
    assert voided.get_json()['audit'][-1]['action'] == 'voided'
    client.post('/logout')

    assert login(client, 'buyer@harbor.example').status_code == 302
    own = client.get('/api/invoices').get_json()
    assert {row['customer_id'] for row in own} == {'harbor'}
    assert client.post('/api/invoices', json={
        'doc_type': 'invoice', 'customer_id': 'harbor', 'lines': _lines(),
    }).status_code == 403
    page = client.get('/invoices')
    assert b'Create invoice' not in page.data
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    admin_page = client.get('/invoices')
    assert b'Create invoice' in admin_page.data
    assert b'Create credit note' in admin_page.data
    html = admin_page.data.decode()
    assert 'Choose a customer' in html
    assert 'if (filter && filter.value) return filter.value;' in html
    assert html.index('if (filter && filter.value) return filter.value;') < html.index('if (pay && pay.value) return pay.value;')

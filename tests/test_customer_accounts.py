"""A customer can owe each Ora Glass company on its own account."""

from storage import load_customers, load_invoices, save_customers, save_invoices, save_payments
from tests.conftest import login, write_user


def test_open_windows_keeps_the_us_account_and_can_add_canada(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_customers({
        'open-windows': {
            'id': 'open-windows', 'name': 'Open Windows Inc', 'country': 'United States',
            'credit_currency': 'usd', 'credit_limit': 5000, 'tier': 'T2', 'status': 'active',
            'qbo_id': '2',
        },
    })
    loaded = load_customers()['open-windows']
    assert loaded['country'] == 'Canada'
    assert set(loaded['accounts']) == {'us'}
    us = loaded['accounts']['us']
    assert us['currency'] == 'usd'
    assert us['gst'] is False
    assert us['label'] == 'Ora Glass Inc'
    assert loaded['credit_currency'] == 'usd'
    added = client.post('/api/customers/open-windows/accounts', json={'entity': 'ca'})
    assert added.status_code == 200, added.get_json()
    body = added.get_json()
    accounts = {row['entity']: row for row in body['accounts']}
    assert accounts['us']['currency'] == 'usd'
    assert accounts['us']['gst'] is False
    assert accounts['ca']['currency'] == 'cad'
    assert accounts['ca']['gst'] is True
    assert accounts['ca']['qbo_id'] == ''
    again = client.post('/api/customers/open-windows/accounts', json={'entity': 'ca'})
    assert again.status_code == 400


def test_canada_invoice_cannot_post_to_the_us_account(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_customers({
        'open-windows': {
            'id': 'open-windows', 'name': 'Open Windows Inc', 'country': 'Canada',
            'credit_currency': 'usd', 'tier': 'T2', 'status': 'active',
        },
    })
    refused = client.post('/api/invoices', json={
        'customer_id': 'open-windows', 'issuer': 'ca',
        'lines': [{'description': 'Glass', 'qty': 1, 'unit_price': 100}],
    })
    assert refused.status_code == 400
    assert 'Canada' in refused.get_json()['error']
    assert load_invoices() == []
    added = client.post('/api/customers/open-windows/accounts', json={'entity': 'ca'})
    assert added.status_code == 200
    created = client.post('/api/invoices', json={
        'customer_id': 'open-windows', 'issuer': 'ca',
        'lines': [{'description': 'Glass', 'qty': 1, 'unit_price': 100}],
    })
    assert created.status_code == 200, created.get_json()
    invoice = created.get_json()
    assert invoice['issuer'] == 'ca'
    assert invoice['currency'] == 'cad'
    us_payment = client.post('/api/payments', json={
        'customer_id': 'open-windows', 'amount': 10, 'currency': 'usd',
        'method': 'wire', 'date': '2026-11-01', 'invoice_ids': [invoice['id']],
    })
    assert us_payment.status_code == 400
    cad_payment = client.post('/api/payments', json={
        'customer_id': 'open-windows', 'amount': 10, 'currency': 'cad',
        'method': 'wire', 'date': '2026-11-01', 'invoice_ids': [invoice['id']],
    })
    assert cad_payment.status_code == 200, cad_payment.get_json()


def test_balances_split_by_company_and_currency(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_customers({
        'open-windows': {
            'id': 'open-windows', 'name': 'Open Windows Inc', 'country': 'Canada',
            'credit_currency': 'usd', 'credit_limit': 1000, 'tier': 'T2', 'status': 'active',
        },
    })
    client.post('/api/customers/open-windows/accounts', json={'entity': 'ca'})
    save_invoices([
        {
            'id': 1, 'number': 'US-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'open-windows', 'currency': 'usd', 'total': 400, 'voided': False,
            'invoice_date': '2026-11-01', 'items': [],
        },
        {
            'id': 2, 'number': 'CA-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'currency': 'cad', 'total': 80, 'voided': False,
            'invoice_date': '2026-11-02', 'items': [],
        },
    ])
    save_payments([])
    credit = client.get('/api/credit?customer_id=open-windows').get_json()
    books = {row['entity']: row for row in credit['by_account'] if row['entity'] in {'us', 'ca'} and row['currency'] in {'usd', 'cad'}}
    assert books['us']['currency'] == 'usd'
    assert books['us']['open_receivables'] == 400
    assert books['ca']['currency'] == 'cad'
    assert books['ca']['open_receivables'] == 80
    page = client.get('/customers').get_data(as_text=True)
    assert 'Add Ora Glass (Canada) Inc account' in page
    assert 'account-table' in page

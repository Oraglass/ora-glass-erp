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


def test_issued_by_filter_is_in_the_url_and_splits_totals(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('ap@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_customers({
        'open-windows': {
            'id': 'open-windows', 'name': 'Open Windows Inc', 'country': 'Canada',
            'tier': 'T2', 'status': 'active',
        },
        'harbor-glass': {
            'id': 'harbor-glass', 'name': 'Harbor Glass', 'country': 'United States',
            'tier': 'T2', 'status': 'active',
        },
    })
    from storage import load_customers
    customers = load_customers()
    import customer_accounts
    assert customer_accounts.add_account(customers['open-windows'], 'ca') is None
    from storage import save_customers as store_customers
    store_customers(customers)
    save_invoices([
        {
            'id': 1, 'number': 'US-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'open-windows', 'currency': 'usd', 'total': 400, 'balance': 400,
            'voided': False, 'invoice_date': '2026-11-01', 'status': 'unpaid', 'items': [],
        },
        {
            'id': 2, 'number': 'CA-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'currency': 'cad', 'total': 80, 'balance': 80,
            'voided': False, 'invoice_date': '2026-11-02', 'status': 'unpaid', 'items': [],
        },
    ])
    save_payments([
        {
            'id': 1, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'date': '2026-11-03', 'amount': 10, 'currency': 'usd', 'issuer': 'us',
            'method': 'wire', 'voided': False, 'allocations': [],
        },
        {
            'id': 2, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'date': '2026-11-04', 'amount': 5, 'currency': 'cad', 'issuer': 'ca',
            'method': 'wire', 'voided': False, 'allocations': [],
        },
    ])
    page = client.get('/invoices').get_data(as_text=True)
    assert 'id="filter-issuer"' in page
    assert 'All issuers' in page
    assert 'Ora Glass Inc · USD' in page
    assert 'Ora Glass (Canada) Inc · CAD' in page
    assert 'history.replaceState' in page
    assert 'Totals by currency' in page
    choices = client.get('/api/order-customers').get_json()
    by_id = {row['id']: row for row in choices}
    assert 'us' in by_id['open-windows']['accounts']
    assert 'ca' in by_id['open-windows']['accounts']
    assert by_id['harbor-glass']['accounts'] == ['us']
    us_only = client.get('/api/invoices?issuer=us').get_json()
    assert [row['number'] for row in us_only] == ['US-1']
    ca_only = client.get('/api/invoices?issuer=ca&customer_id=open-windows&status=unpaid&type=invoice').get_json()
    assert [row['number'] for row in ca_only] == ['CA-1']
    us_payments = client.get('/api/payments?issuer=us&customer_id=open-windows').get_json()
    assert [row['id'] for row in us_payments] == [1]
    overview = client.get('/api/financial?customer_id=open-windows&issuer=ca').get_json()
    assert [row['number'] for row in overview['invoices']] == ['CA-1']
    assert [row['currency'] for row in overview['payments']] == ['cad']
    assert {row['currency'] for row in overview['credit']['by_account']} == {'cad'}
    pay_page = client.get('/financial/payments').get_data(as_text=True)
    assert 'id="pay-issuer"' in pay_page
    overview_page = client.get('/financial').get_data(as_text=True)
    assert 'id="fin-issuer"' in overview_page

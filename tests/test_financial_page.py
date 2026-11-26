"""The financial page filters its lists by date and leaves the credit cards unchanged."""

from billing import money
from storage import load_customers, save_customers, save_invoices, save_orders, save_payments
from tests.conftest import login, write_user


def test_financial_page_has_the_range_and_the_lists(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/financial').get_data(as_text=True)
    assert 'Summary cards are all-time' in page
    assert 'This month' in page
    assert 'Last 30 days' in page
    assert 'This year' in page
    assert '>All<' in page
    assert 'id="fin-invoices"' in page
    assert 'id="fin-orders"' in page
    assert 'QuickBooks' in page
    assert 'Import open invoices from QuickBooks' in page
    assert 'Each Ora Glass company and currency is shown on its own' in page


def test_date_range_filters_the_lists_and_leaves_the_credit_cards(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('ap@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass')
    save_customers({
        'harbor-glass': {
            'id': 'harbor-glass', 'name': 'Harbor Glass', 'country': 'United States',
            'credit_currency': 'usd', 'credit_limit': 10000, 'credit_approved': True,
            'tier': 'T2', 'status': 'active',
        },
    })
    save_orders([
        {
            'id': 1, 'number': '1', 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'project': 'Lobby', 'status': 'approved', 'currency': 'usd', 'date': '2026-11-02',
            'total': 3000, 'items': [],
        },
        {
            'id': 2, 'number': '2', 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'project': 'Old', 'status': 'approved', 'currency': 'usd', 'date': '2020-01-01',
            'total': 50, 'items': [],
        },
    ])
    save_invoices([
        {
            'id': 10, 'number': 'INV-OLD', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'harbor-glass', 'company': 'Harbor Glass', 'project': 'Tower',
            'currency': 'usd', 'total': 400, 'balance': 400, 'invoice_date': '2026-01-15',
            'due_date': '2026-02-14', 'voided': False, 'items': [{'name': 'Glass', 'pcs': 1, 'total': 400}],
        },
        {
            'id': 11, 'number': 'INV-NOV', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'harbor-glass', 'company': 'Harbor Glass', 'project': 'Annex',
            'currency': 'usd', 'total': 100, 'balance': 100, 'invoice_date': '2026-11-20',
            'due_date': '2026-12-20', 'voided': False, 'qbo_sync': 'synced',
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 100}],
        },
        {
            'id': 12, 'number': 'INV-PART', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'harbor-glass', 'company': 'Harbor Glass', 'project': 'Lobby',
            'currency': 'usd', 'total': 800, 'balance': 800, 'invoice_date': '2026-11-01',
            'due_date': '2026-12-01', 'voided': False, 'order_id': 1,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 800}],
        },
    ])
    save_payments([
        {
            'id': 1, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'date': '2026-11-23', 'amount': 20, 'currency': 'usd', 'method': 'wire',
            'voided': False, 'allocations': [],
        },
        {
            'id': 2, 'customer_id': 'harbor-glass', 'company': 'Harbor Glass',
            'date': '2020-05-01', 'amount': 5, 'currency': 'usd', 'method': 'cheque',
            'voided': False, 'allocations': [],
        },
    ])
    assert login(client, 'admin@oraglass.us').status_code == 302
    credit = client.get('/api/credit?customer_id=harbor-glass').get_json()
    overview = client.get('/api/financial?customer_id=harbor-glass&from=2026-11-01&to=2026-11-30').get_json()
    assert overview['credit']['currency'] == credit['currency']
    assert overview['credit']['open_receivables'] == credit['open_receivables']
    assert overview['credit']['credit_currency'] == 'usd'
    assert overview['credit']['credit_limit'] == 10000
    assert [row['number'] for row in overview['invoices']] == ['INV-NOV', 'INV-PART']
    november = next(row for row in overview['invoices'] if row['number'] == 'INV-NOV')
    assert november['qbo_sync_label'] == 'Synced'
    assert november['due_date'] == '2026-12-20'
    assert november['project'] == 'Annex'
    assert november['balance'] == 100
    assert [row['number'] for row in overview['orders']] == ['1']
    lobby = overview['orders'][0]
    assert lobby['invoiced'] == 800
    assert lobby['not_invoiced'] == money(3000 - 800)
    assert [row['id'] for row in overview['payments']] == [1]
    everything = client.get('/api/financial?customer_id=harbor-glass').get_json()
    assert everything['credit']['open_receivables'] == overview['credit']['open_receivables']
    assert everything['credit']['by_currency'] == overview['credit']['by_currency']
    assert [row['number'] for row in everything['invoices']] == ['INV-NOV', 'INV-PART', 'INV-OLD']


def test_open_windows_cards_keep_the_limit_and_list_every_open_invoice(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@openwindows.example', 'customer', customer_id='open-windows',
        customer_name='Open Windows Inc',
    )
    customers = load_customers()
    customers['open-windows'].update({
        'country': 'United States',
        'credit_currency': 'cad',
        'credit_limit': 10000,
        'credit_approved': True,
    })
    save_customers(customers)
    loaded = load_customers()['open-windows']
    assert loaded['credit_limit'] == 10000
    save_invoices([
        {
            'id': 10, 'number': 'INV-US-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Tower',
            'currency': 'usd', 'total': 400000, 'balance': 400000, 'invoice_date': '2026-01-15',
            'due_date': '2026-02-14', 'voided': False,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 400000}],
        },
        {
            'id': 11, 'number': 'INV-CA-USD', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Annex',
            'currency': 'usd', 'total': 100000, 'balance': 100000, 'invoice_date': '2026-11-20',
            'due_date': '2026-12-20', 'voided': False,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 100000}],
        },
        {
            'id': 12, 'number': 'CA-INV-9', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Small',
            'currency': 'cad', 'total': 2635.02, 'balance': 2635.02, 'invoice_date': '2026-11-01',
            'due_date': '2026-12-01', 'voided': False,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 2635.02}],
        },
    ])
    assert login(client, 'admin@oraglass.us').status_code == 302
    credit = client.get('/api/credit?customer_id=open-windows').get_json()
    books = {row['currency']: row for row in credit['by_currency']}
    assert credit['credit_currency'] == 'cad'
    accounts = {row['entity'] + '-' + row['currency']: row for row in credit['by_account']}
    assert accounts['us-usd']['open_receivables'] == 400000
    assert accounts['ca-usd']['open_receivables'] == 100000
    assert accounts['ca-cad']['balance'] == 2635.02
    assert credit['credit_limit'] == 10000
    assert books['usd']['open_receivables'] == money(400000 + 100000)
    assert books['cad']['open_receivables'] == 2635.02
    overview = client.get('/api/financial?customer_id=open-windows&from=2026-11-01&to=2026-11-30').get_json()
    assert overview['credit']['by_currency'] == credit['by_currency']
    assert overview['credit']['credit_limit'] == 10000
    assert [row['number'] for row in overview['invoices']] == ['INV-CA-USD', 'CA-INV-9']

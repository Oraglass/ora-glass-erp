"""The financial page filters its lists by date and leaves the credit cards unchanged."""

from billing import money
from storage import save_customers, save_invoices, save_orders, save_payments
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
    assert 'by_currency' not in credit
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
    assert [row['number'] for row in everything['invoices']] == ['INV-NOV', 'INV-PART', 'INV-OLD']

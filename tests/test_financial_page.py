"""Financial overview keeps all-time cards and lists one customer's books by currency."""

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


def test_mixed_currencies_stay_separate_and_dates_filter_only_the_lists(client):
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
    save_orders([
        {
            'id': 1, 'number': '1', 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'project': 'Lobby', 'status': 'approved', 'currency': 'cad', 'date': '2026-11-02',
            'total': 3000, 'items': [],
        },
        {
            'id': 2, 'number': '2', 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'project': 'Old', 'status': 'approved', 'currency': 'cad', 'date': '2020-01-01',
            'total': 50, 'items': [],
        },
    ])
    save_invoices([
        {
            'id': 10, 'number': 'INV-US-1', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Tower',
            'currency': 'usd', 'total': 400000, 'balance': 400000, 'invoice_date': '2026-01-15',
            'due_date': '2026-02-14', 'voided': False, 'items': [{'name': 'Glass', 'pcs': 1, 'total': 400000}],
        },
        {
            'id': 11, 'number': 'INV-US-2', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Annex',
            'currency': 'usd', 'total': 100000, 'balance': 100000, 'invoice_date': '2026-11-20',
            'due_date': '2026-12-20', 'voided': False, 'qbo_sync': 'synced',
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 100000}],
        },
        {
            'id': 12, 'number': 'CA-INV-9', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Small',
            'currency': 'cad', 'total': 2635.02, 'balance': 2635.02, 'invoice_date': '2026-11-01',
            'due_date': '2026-12-01', 'voided': False, 'order_id': 1,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 2635.02}],
        },
        {
            'id': 13, 'number': 'INV-IC', 'doc_type': 'invoice', 'kind': 'intercompany', 'issuer': 'us',
            'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'project': 'Lobby',
            'currency': 'usd', 'total': 700, 'balance': 700, 'invoice_date': '2026-11-03',
            'due_date': '2026-12-03', 'voided': False, 'order_id': 1,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 700}],
        },
    ])
    save_payments([
        {
            'id': 1, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'date': '2026-11-23', 'amount': 20, 'currency': 'usd', 'method': 'wire',
            'voided': False, 'allocations': [],
        },
        {
            'id': 2, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
            'date': '2020-05-01', 'amount': 5, 'currency': 'usd', 'method': 'cheque',
            'voided': False, 'allocations': [],
        },
    ])
    assert login(client, 'admin@oraglass.us').status_code == 302
    credit = client.get('/api/credit?customer_id=open-windows').get_json()
    assert credit['credit_currency'] == 'cad'
    assert credit['credit_limit'] == 10000
    books = {row['currency']: row for row in credit['by_currency']}
    assert books['usd']['open_receivables'] == money(400000 + 100000 + 700)
    assert books['cad']['open_receivables'] == 2635.02
    overview = client.get('/api/financial?customer_id=open-windows&from=2026-11-01&to=2026-11-30').get_json()
    assert overview['cards_all_time'] is True
    assert overview['credit']['by_currency'] == credit['by_currency']
    assert [row['number'] for row in overview['invoices']] == ['INV-US-2', 'INV-IC', 'CA-INV-9']
    november = next(row for row in overview['invoices'] if row['number'] == 'INV-US-2')
    assert november['qbo_sync_label'] == 'Synced'
    assert november['due_date'] == '2026-12-20'
    assert [row['number'] for row in overview['orders']] == ['1']
    lobby = overview['orders'][0]
    assert lobby['invoiced'] == 2635.02
    assert lobby['not_invoiced'] == money(3000 - 2635.02)
    assert [row['id'] for row in overview['payments']] == [1]

"""A payment covers ticked invoices in the order they were selected."""

import json
import urllib.parse

import quickbooks
from billing import invoice_balance, money, spread_payment
from quickbooks import US_REALM
from storage import append_invoice, load_customers, load_invoices, load_payments
from tests.conftest import login, write_user
from tests.test_quickbooks import FakeQBO, _configure


FIRST = [
    ('1002', 4000.00),
    ('1003', 4500.00),
    ('1004', 5000.00),
    ('1005', 5100.00),
    ('1006', 5210.07),
    ('1007', 6000.00),
]
SECOND = [
    ('1008', 6699.76),
    ('1009', 8657.64),
    ('1010', 13902.84),
]
FIRST_TOTAL = 29810.07
SECOND_PAYMENT = 22032.27
LEFT_ON_1010 = 7227.97


def test_spread_follows_selection_order_and_stops_at_the_open_balance():
    plan = spread_payment(70, [{'id': 2, 'balance': 50}, {'id': 1, 'balance': 60}])
    assert [(row['id'], row['applied'], row['remaining']) for row in plan['applications']] == [
        (2, 50, 0), (1, 20, 40),
    ]
    assert plan['unallocated'] == 0
    extra = spread_payment(100, [{'id': 1, 'balance': 40}])
    assert extra['applications'][0]['applied'] == 40
    assert extra['unallocated'] == 60


def _open_windows(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@openwindows.example', 'customer', customer_id='open-windows',
        customer_name='Open Windows Inc', customer_subrole='management',
    )
    customers = load_customers()
    customers['open-windows']['country'] = 'United States'
    customers['open-windows']['credit_currency'] = 'cad'
    customers['open-windows']['credit_limit'] = 0
    from storage import save_customers
    save_customers(customers)
    loaded = load_customers()['open-windows']
    assert loaded['credit_currency'] == 'cad'
    assert login(client, 'admin@oraglass.us').status_code == 302
    saved = {}
    for doc, amount in FIRST + SECOND:
        invoice = append_invoice({
            'doc_type': 'invoice',
            'source': 'quickbooks',
            'issuer': 'us',
            'customer_id': 'open-windows',
            'company': 'Open Windows Inc',
            'project': doc,
            'qbo_doc_number': doc,
            'currency': 'usd',
            'total': amount,
            'subtotal': amount,
            'balance': amount,
            'status': 'unpaid',
            'invoice_date': '2026-10-01',
            'due_date': '2026-10-31',
            'voided': False,
            'items': [{'name': 'IGU', 'pcs': 1, 'total': amount}],
        })
        saved[doc] = invoice
        quickbooks.upsert_link('invoice', invoice['id'], doc, '0', 'qbo')
    quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'qbo')
    return saved


def _by_doc(rows):
    return {row.get('qbo_doc_number') or row.get('project'): row for row in rows}


def test_open_windows_usd_invoices_are_the_receivables_even_when_credit_is_cad(client):
    _open_windows(client)
    page = client.get('/invoices')
    assert b'id="open-invoice-list"' in page.data
    assert b'in the order you tick them' in page.data
    credit = client.get('/api/credit?customer_id=open-windows').get_json()
    assert credit['credit_currency'] == 'cad'
    assert credit['credit_limit'] == 0
    assert credit['currency'] == 'usd'
    assert credit['open_receivables'] == money(FIRST_TOTAL + 6699.76 + 8657.64 + 13902.84)
    cad = client.get('/api/credit?customer_id=open-windows&currency=cad').get_json()
    assert cad['open_receivables'] == 0
    listed = client.get('/api/invoices?customer_id=open-windows&currency=usd&issuer=us&balance=open').get_json()
    assert [row['qbo_doc_number'] for row in listed] == [doc for doc, _amount in FIRST + SECOND]
    assert listed[0]['balance'] == 4000
    assert listed[0]['status'] == 'unpaid'


def test_open_windows_payments_follow_tick_order_and_push_one_quickbooks_line_each(client, monkeypatch):
    _configure(monkeypatch)
    invoices = _open_windows(client)
    fake = FakeQBO()
    quickbooks.TRANSPORT = fake
    try:
        start = client.get('/quickbooks/connect')
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
        callback = client.get(f'/quickbooks/callback?code=auth-code&realmId={US_REALM}&state={state}')
        assert callback.status_code == 302
        first_ids = [invoices[doc]['id'] for doc, _amount in FIRST]
        assert money(sum(amount for _doc, amount in FIRST)) == FIRST_TOTAL
        paid = client.post('/api/payments', json={
            'customer_id': 'open-windows',
            'date': '2026-10-29',
            'amount': FIRST_TOTAL,
            'currency': 'usd',
            'method': 'wire',
            'reference': 'Open Windows Oct 29',
            'invoice_ids': first_ids,
        })
        assert paid.status_code == 200, paid.get_json()
        assert paid.get_json()['unallocated'] == 0
        assert [row['amount'] for row in paid.get_json()['allocations']] == [amount for _doc, amount in FIRST]
        open_after = _by_doc(client.get(
            '/api/invoices?customer_id=open-windows&currency=usd&issuer=us&balance=open'
        ).get_json())
        assert list(open_after) == ['1008', '1009', '1010']
        posts = [json.loads(body) for _method, url, body in fake.calls if '/payment' in url and US_REALM in url]
        assert len(posts) == 1
        assert posts[0]['TotalAmt'] == FIRST_TOTAL
        assert posts[0]['CurrencyRef']['value'] == 'USD'
        assert [(line['Amount'], line['LinkedTxn'][0]['TxnId']) for line in posts[0]['Line']] == [
            (amount, doc) for doc, amount in FIRST
        ]
        second_ids = [invoices[doc]['id'] for doc, _amount in SECOND]
        second = client.post('/api/payments', json={
            'customer_id': 'open-windows',
            'date': '2026-10-29',
            'amount': SECOND_PAYMENT,
            'currency': 'usd',
            'method': 'wire',
            'reference': 'Open Windows remainder',
            'invoice_ids': second_ids,
        })
        assert second.status_code == 200, second.get_json()
        applied = [row['amount'] for row in second.get_json()['allocations']]
        assert applied == [6699.76, 8657.64, money(13902.84 - LEFT_ON_1010)]
        assert second.get_json()['unallocated'] == 0
        assert money(sum(applied)) == SECOND_PAYMENT
        remaining = client.get(f"/api/invoices/{invoices['1010']['id']}").get_json()
        assert remaining['status'] == 'partial'
        assert remaining['status_label'] == 'Partially paid'
        assert remaining['balance'] == LEFT_ON_1010
        still_open = client.get(
            '/api/invoices?customer_id=open-windows&currency=usd&issuer=us&balance=open'
        ).get_json()
        assert [row['qbo_doc_number'] for row in still_open] == ['1010']
        posts = [json.loads(body) for _method, url, body in fake.calls if '/payment' in url and US_REALM in url]
        assert len(posts) == 2
        assert [(line['Amount'], line['LinkedTxn'][0]['TxnId']) for line in posts[1]['Line']] == [
            (6699.76, '1008'), (8657.64, '1009'), (money(13902.84 - LEFT_ON_1010), '1010'),
        ]
        stored = load_invoices()
        payments = load_payments()
        for doc, _amount in FIRST + SECOND:
            invoice = next(row for row in stored if row.get('qbo_doc_number') == doc)
            balance = invoice_balance(invoice, payments)
            assert invoice['balance'] == balance
            if doc == '1010':
                assert balance == LEFT_ON_1010
            else:
                assert balance == 0
        credit = client.get('/api/credit?customer_id=open-windows').get_json()
        assert credit['currency'] == 'usd'
        assert credit['open_receivables'] == LEFT_ON_1010
    finally:
        quickbooks.TRANSPORT = None


def test_later_allocation_uses_the_same_tick_order_and_leaves_the_rest_unallocated(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management',
    )
    customers = load_customers()
    customers['harbor-glass']['country'] = 'United States'
    customers['harbor-glass']['credit_currency'] = 'usd'
    from storage import save_customers
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    first = append_invoice({
        'doc_type': 'invoice', 'source': 'manual', 'issuer': 'us', 'customer_id': 'harbor-glass',
        'company': 'Harbor Glass', 'currency': 'usd', 'total': 60, 'balance': 60, 'status': 'unpaid',
        'invoice_date': '2026-10-01', 'voided': False, 'items': [{'name': 'A', 'pcs': 1, 'total': 60}],
    })
    second = append_invoice({
        'doc_type': 'invoice', 'source': 'manual', 'issuer': 'us', 'customer_id': 'harbor-glass',
        'company': 'Harbor Glass', 'currency': 'usd', 'total': 50, 'balance': 50, 'status': 'unpaid',
        'invoice_date': '2026-10-02', 'voided': False, 'items': [{'name': 'B', 'pcs': 1, 'total': 50}],
    })
    recorded = client.post('/api/payments', json={
        'customer_id': 'harbor-glass', 'date': '2026-10-29', 'amount': 80, 'currency': 'usd', 'method': 'cheque',
    })
    assert recorded.status_code == 200, recorded.get_json()
    payment_id = recorded.get_json()['id']
    assert recorded.get_json()['unallocated'] == 80
    allocated = client.post(f'/api/payments/{payment_id}/allocations', json={
        'invoice_ids': [second['id'], first['id']],
    })
    assert allocated.status_code == 200, allocated.get_json()
    assert [(row['target_id'], row['amount']) for row in allocated.get_json()['allocations']] == [
        (second['id'], 50), (first['id'], 30),
    ]
    assert allocated.get_json()['unallocated'] == 0
    assert client.get(f"/api/invoices/{second['id']}").get_json()['status'] == 'paid'
    partial = client.get(f"/api/invoices/{first['id']}").get_json()
    assert partial['status'] == 'partial'
    assert partial['balance'] == 30
    open_rows = client.get('/api/invoices?customer_id=harbor-glass&currency=usd&balance=open').get_json()
    assert [row['id'] for row in open_rows] == [first['id']]
    leftover = client.post('/api/payments', json={
        'customer_id': 'harbor-glass', 'date': '2026-10-30', 'amount': 100, 'currency': 'usd',
        'method': 'cheque', 'invoice_ids': [first['id']],
    })
    assert leftover.status_code == 200, leftover.get_json()
    assert leftover.get_json()['allocations'][0]['amount'] == 30
    assert leftover.get_json()['unallocated'] == 70
    assert client.get(f"/api/invoices/{first['id']}").get_json()['balance'] == 0
    customers = load_customers()
    customers['harbor-glass']['credit_currency'] = 'cad'
    customers['harbor-glass']['country'] = 'Canada'
    save_customers(customers)
    summary = client.get('/api/credit?customer_id=harbor-glass').get_json()
    assert summary['credit_currency'] == 'cad'
    assert summary['currency'] == 'usd'
    assert summary['open_receivables'] == 0
    assert summary['unallocated_payments'] == 70

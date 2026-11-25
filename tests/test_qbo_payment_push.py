"""Open Windows payments are posted to the US QuickBooks company without a second copy."""

import json
import urllib.parse

import quickbooks
from quickbooks import US_REALM
from storage import load_payments, save_invoices, save_payments
from tests.conftest import login, write_user
from tests.test_quickbooks import FakeQBO, _configure


def _connect(client, fake):
    quickbooks.TRANSPORT = fake
    start = client.get('/quickbooks/connect?connection=us')
    state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
    callback = client.get(f'/quickbooks/callback?code=auth-code&realmId={US_REALM}&state={state}')
    assert callback.status_code == 302, callback.headers.get('Location')


def _books():
    write_user(
        'ap@open.example', 'customer', customer_id='open-windows',
        customer_name='Open Windows Inc',
    )
    save_invoices([{
        'id': 20, 'number': '1020', 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
        'issuer': 'us', 'currency': 'usd', 'qbo_id': '1020', 'total': 80000, 'balance': 80000,
        'invoice_date': '2026-11-01', 'items': [{'name': 'Glass', 'pcs': 1, 'total': 80000}],
    }])


def _payment(payment_id, amount, paid_on, allocated=None):
    return {
        'id': payment_id,
        'customer_id': 'open-windows',
        'company': 'Open Windows Inc',
        'date': paid_on,
        'amount': amount,
        'currency': 'usd',
        'method': 'wire',
        'voided': False,
        'allocations': allocated or [],
    }


def _payment_posts(fake):
    posts = []
    for call in fake.calls:
        if call[0] != 'POST' or '/payment' not in call[1] or 'operation=void' in call[1]:
            continue
        posts.append((call, json.loads(call[2])))
    return posts


def test_unconnected_payment_keeps_the_reason_and_retry_is_admin_only(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('seller@oraglass.us', 'sales')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_payments([_payment(1, 20987.25, '2026-11-23', [
        {'target_type': 'invoice', 'target_id': 20, 'amount': 20987.25},
    ])])
    waiting = client.post('/api/payments/1/quickbooks')
    assert waiting.status_code == 200, waiting.get_json()
    body = waiting.get_json()
    assert body['qbo_sync'] == 'not_connected'
    assert body['qbo_sync_label'] == 'Not connected'
    assert US_REALM in body['qbo_error']
    page = client.get('/invoices').get_data(as_text=True)
    assert 'Send unsynced payments to QuickBooks' in page
    assert 'data-send-qbo' in page
    client.post('/logout')
    assert login(client, 'seller@oraglass.us').status_code == 302
    assert client.post('/api/payments/1/quickbooks').status_code == 403
    assert client.post('/api/payments/quickbooks').status_code == 403


def test_payment_posts_to_the_us_company_and_links_part_of_invoice_1020(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_payments([
        _payment(1, 20987.25, '2026-11-23', [
            {'target_type': 'invoice', 'target_id': 20, 'amount': 20987.25},
        ]),
        _payment(2, 53039.99, '2026-11-25', [
            {'target_type': 'invoice', 'target_id': 20, 'amount': 40000},
        ]),
    ])
    fake = FakeQBO()
    try:
        _connect(client, fake)
        with quickbooks.connection_scope('us'):
            quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'erp', realm_id=US_REALM)
        sent = client.post('/api/payments/1/quickbooks')
        assert sent.status_code == 200, sent.get_json()
        body = sent.get_json()
        assert body['qbo_sync'] == 'synced'
        assert body['qbo_id'] == '82'
        creates = [item for item in _payment_posts(fake) if 'Id' not in item[1]]
        assert len(creates) == 1
        assert US_REALM in creates[0][0][1]
        payload = creates[0][1]
        assert payload['CustomerRef']['value'] == '2'
        assert payload['TotalAmt'] == 20987.25
        assert payload['TxnDate'] == '2026-11-23'
        assert payload['CurrencyRef']['value'] == 'USD'
        assert payload['Line'][0]['Amount'] == 20987.25
        assert payload['Line'][0]['LinkedTxn'][0] == {'TxnId': '1020', 'TxnType': 'Invoice'}
        again = client.post('/api/payments/1/quickbooks')
        assert again.get_json()['qbo_sync'] == 'synced'
        assert len([item for item in _payment_posts(fake) if 'Id' not in item[1]]) == 1
        bulk = client.post('/api/payments/quickbooks')
        assert bulk.status_code == 200, bulk.get_json()
        assert bulk.get_json()['count'] == 1
        second = next(row for row in load_payments() if row['id'] == 2)
        assert second['qbo_sync'] == 'synced'
        assert second['qbo_id'] == '82'
        partial = [item for item in _payment_posts(fake) if item[1].get('TxnDate') == '2026-11-25']
        assert len(partial) == 1
        assert partial[0][1]['TotalAmt'] == 53039.99
        assert partial[0][1]['Line'][0]['Amount'] == 40000
        assert partial[0][1]['Line'][0]['LinkedTxn'][0]['TxnId'] == '1020'
        stored = next(row for row in load_payments() if row['id'] == 1)
        assert stored['qbo_realm_id'] == US_REALM
    finally:
        quickbooks.TRANSPORT = None


def test_an_existing_quickbooks_payment_is_reused(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_payments([_payment(1, 20987.25, '2026-11-23', [
        {'target_type': 'invoice', 'target_id': 20, 'amount': 1000},
    ])])
    fake = FakeQBO()
    fake.payments = [
        {
            'Id': '11', 'SyncToken': '1', 'CustomerRef': {'value': '2'},
            'TxnDate': '2026-11-23', 'TotalAmt': 10,
        },
        {
            'Id': '900', 'SyncToken': '4', 'CustomerRef': {'value': '2'},
            'TxnDate': '2026-11-23', 'TotalAmt': 20987.25,
        },
    ]
    try:
        _connect(client, fake)
        with quickbooks.connection_scope('us'):
            quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'erp', realm_id=US_REALM)
        sent = client.post('/api/payments/1/quickbooks')
        assert sent.status_code == 200, sent.get_json()
        assert sent.get_json()['qbo_id'] == '900'
        assert sent.get_json()['qbo_sync'] == 'synced'
        creates = [item for item in _payment_posts(fake) if 'Id' not in item[1]]
        assert creates == []
        updates = [item for item in _payment_posts(fake) if item[1].get('Id') == '900']
        assert len(updates) == 1
        assert updates[0][1]['Line'][0]['Amount'] == 1000
        assert updates[0][1]['Line'][0]['LinkedTxn'][0]['TxnId'] == '1020'
        assert updates[0][1]['sparse'] is True
    finally:
        quickbooks.TRANSPORT = None

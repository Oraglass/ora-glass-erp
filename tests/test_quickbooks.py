import base64
import hashlib
import hmac
import json
import urllib.parse

import quickbooks
from billing import invoice_balance
from storage import load_customers, load_invoices, load_payments
from tests.conftest import login, write_user


class FakeQBO:
    def __init__(self):
        self.calls = []
        self.conflict = False
        self.customers = [{
            'Id': '2',
            'SyncToken': '0',
            'DisplayName': 'Open Windows Inc',
            'PrimaryEmailAddr': {'Address': 'support@open-windows.ca'},
            'CurrencyRef': {'value': 'USD'},
            'Balance': 546000,
            'SalesTermRef': {'name': 'Net 30'},
        }]
        self.invoices = [{
            'Id': '90',
            'SyncToken': '0',
            'DocNumber': 'QBO-100',
            'CustomerRef': {'value': '2'},
            'TotalAmt': 546000,
            'Balance': 546000,
            'CurrencyRef': {'value': 'USD'},
            'TxnDate': '2026-01-15',
            'DueDate': '2026-02-14',
            'Line': [{'Amount': 546000, 'Description': 'IGU order', 'DetailType': 'SalesItemLineDetail'}],
        }]
        self.payments = []
        self.cdc = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path.endswith('/tokens/bearer') or 'oauth' in path:
            return 200, {}, json.dumps({
                'access_token': 'access-1',
                'refresh_token': 'refresh-1',
                'expires_in': 3600,
                'token_type': 'bearer',
            }).encode()
        if 'companyinfo' in path:
            return 200, {}, json.dumps({'CompanyInfo': {'CompanyName': 'Open Windows Sandbox'}}).encode()
        if path.endswith('/query'):
            statement = query.get('query', [''])[0]
            if 'from Customer' in statement:
                payload = {'Customer': self.customers}
            elif 'from Invoice' in statement:
                payload = {'Invoice': self.invoices}
            elif 'from CreditMemo' in statement:
                payload = {'CreditMemo': []}
            elif 'from Payment' in statement:
                payload = {'Payment': self.payments}
            elif 'from Item' in statement:
                name = 'Delivery' if 'Delivery' in statement else 'Insulated Glass Units'
                payload = {'Item': [{'Id': '10' if name.startswith('Insulated') else '11', 'Name': name}]}
            elif 'from Term' in statement:
                payload = {'Term': [{'Id': '3', 'Name': 'Net 30'}]}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        if path.endswith('/cdc'):
            return 200, {}, json.dumps({'CDCResponse': [{'QueryResponse': self.cdc}]}).encode()
        data = json.loads(body.decode() if isinstance(body, bytes) else body or '{}')
        if self.conflict and data.get('Id'):
            return 400, {}, json.dumps({'Fault': {'Error': [{'code': '5010', 'Message': 'Stale Object Error'}]}}).encode()
        if '/customer/' in path and method == 'GET':
            return 200, {}, json.dumps({'Customer': {**self.customers[0], 'SyncToken': '4'}}).encode()
        if path.rstrip('/').endswith('/customer'):
            return 200, {}, json.dumps({'Customer': {'Id': data.get('Id') or '55', 'SyncToken': '1', 'DisplayName': data.get('DisplayName')}}).encode()
        if path.rstrip('/').endswith('/invoice') or 'operation=void' in url and '/invoice' in path:
            return 200, {}, json.dumps({'Invoice': {'Id': data.get('Id') or '80', 'SyncToken': '0'}}).encode()
        if 'creditmemo' in path:
            return 200, {}, json.dumps({'CreditMemo': {'Id': data.get('Id') or '81', 'SyncToken': '0'}}).encode()
        if '/payment' in path:
            return 200, {}, json.dumps({'Payment': {'Id': data.get('Id') or '82', 'SyncToken': '0', 'TotalAmt': data.get('TotalAmt')}}).encode()
        if '/invoice/' in path and method == 'GET':
            return 200, {}, json.dumps({'Invoice': self.invoices[0]}).encode()
        return 200, {}, b'{}'

    def posts(self, suffix):
        return [call for call in self.calls if call[0] == 'POST' and suffix in urllib.parse.urlparse(call[1]).path]


def _configure(monkeypatch):
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://ora-glass-erp.onrender.com/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')


def _connect(client, fake):
    quickbooks.TRANSPORT = fake
    start = client.get('/quickbooks/connect')
    state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
    callback = client.get(f'/quickbooks/callback?code=auth-code&realmId=123456&state={state}')
    assert callback.status_code == 302
    assert callback.headers['Location'].endswith('/settings/quickbooks')


def test_settings_are_admin_only_and_oauth_stores_the_company(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    assert login(client, 'lee@harbor.example').status_code == 302
    assert client.get('/settings/quickbooks').status_code in (302, 403)
    assert b'QuickBooks' not in client.get('/dashboard').data
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert b'QuickBooks' in client.get('/dashboard').data
    page = client.get('/settings/quickbooks')
    assert page.status_code == 200
    assert b'Connect to QuickBooks' in page.data
    fake = FakeQBO()
    try:
        _connect(client, fake)
        connected = client.get('/settings/quickbooks')
        assert b'Open Windows Sandbox' in connected.data
        assert b'123456' in connected.data
        assert b'Disconnect' in connected.data
        assert b'Sync now' in connected.data
    finally:
        quickbooks.TRANSPORT = None


def test_import_links_open_windows_and_matches_the_open_balance(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('support@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        preview = client.post('/api/quickbooks/import/preview')
        assert preview.status_code == 200, preview.get_json()
        row = preview.get_json()['customers'][0]
        assert row['qbo_id'] == '2'
        assert row['open_balance'] == 546000
        assert row['currency'] == 'usd'
        assert row['suggested_customer_id'] == 'open-windows'
        assert row['action'] == 'link'
        applied = client.post('/api/quickbooks/import/apply', json={
            'decisions': {'2': {'action': 'link', 'customer_id': 'open-windows'}},
        })
        assert applied.status_code == 200, applied.get_json()
        assert applied.get_json()['invoices'] == 1
        invoice = next(item for item in load_invoices() if item.get('customer_id') == 'open-windows')
        assert invoice['qbo_origin'] is True
        assert invoice_balance(invoice, load_payments()) == 546000
        assert load_customers()['open-windows']['qbo_id'] == '2'
        again = client.post('/api/quickbooks/import/apply', json={
            'decisions': {'2': {'action': 'link', 'customer_id': 'open-windows'}},
        })
        assert again.status_code == 200
        assert len([item for item in load_invoices() if item.get('customer_id') == 'open-windows']) == 1
    finally:
        quickbooks.TRANSPORT = None


def test_erp_invoice_payment_and_credit_note_are_pushed_once(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        created = client.post('/api/invoices', json={
            'doc_type': 'invoice',
            'customer_id': 'harbor',
            'lines': [{'description': 'IGU', 'qty': 2, 'unit_price': 100}],
        })
        assert created.status_code == 200, created.get_json()
        invoice_id = created.get_json()['id']
        assert len(fake.posts('/invoice')) == 1
        body = json.loads(fake.posts('/invoice')[0][2])
        assert body['Line'][0]['SalesItemLineDetail']['ItemRef']['name'] == 'Insulated Glass Units'
        assert body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
        assert body['CurrencyRef']['value'] == 'USD'
        quickbooks.push_document_id(invoice_id)
        assert len(fake.posts('/invoice')) == 1
        note = client.post('/api/invoices', json={
            'doc_type': 'credit_note',
            'customer_id': 'harbor',
            'reason': 'Broken lite',
            'lines': [{'description': 'Credit', 'qty': 1, 'unit_price': 40}],
        })
        assert note.status_code == 200, note.get_json()
        assert len(fake.posts('/creditmemo')) == 1
        payment = client.post('/api/payments', json={
            'customer_id': 'harbor',
            'amount': 50,
            'currency': 'usd',
            'method': 'wire',
            'date': '2026-11-12',
        })
        assert payment.status_code == 200, payment.get_json()
        unapplied = json.loads(fake.posts('/payment')[-1][2])
        assert 'Line' not in unapplied
        allocated = client.post(f"/api/payments/{payment.get_json()['id']}/allocations", json={
            'allocations': [{'target_type': 'invoice', 'target_id': invoice_id, 'amount': 50}],
        })
        assert allocated.status_code == 200, allocated.get_json()
        linked = json.loads(fake.posts('/payment')[-1][2])
        assert linked['Line'][0]['LinkedTxn'][0]['TxnType'] == 'Invoice'
        voided = client.post(f'/api/invoices/{invoice_id}/void', json={'reason': 'Wrong company'})
        assert voided.status_code == 200, voided.get_json()
        assert any('operation=void' in call[1] for call in fake.calls)
    finally:
        quickbooks.TRANSPORT = None


def test_webhook_signature_cdc_and_no_loop(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        customers = load_customers()
        customers['open-windows'] = {
            'id': 'open-windows', 'name': 'Open Windows', 'tier': 'T2', 'credit_currency': 'usd',
        }
        from storage import save_customers
        save_customers(customers)
        quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'qbo')
        before = len(fake.posts('/invoice'))
        payload = json.dumps({'eventNotifications': [{
            'realmId': '123456',
            'dataChangeEvent': {'entities': [{'name': 'Invoice', 'id': '90', 'operation': 'Create'}]},
        }]}).encode()
        bad = client.post('/quickbooks/webhook', data=payload, headers={'intuit-signature': 'nope', 'Content-Type': 'application/json'})
        assert bad.status_code == 401
        signature = base64.b64encode(hmac.new(b'verifier-token', payload, hashlib.sha256).digest()).decode()
        accepted = client.post('/quickbooks/webhook', data=payload, headers={'intuit-signature': signature, 'Content-Type': 'application/json'})
        assert accepted.status_code == 200, accepted.get_json()
        assert len([row for row in load_invoices() if row.get('qbo_origin')]) == 1
        client.post('/quickbooks/webhook', data=payload, headers={'intuit-signature': signature, 'Content-Type': 'application/json'})
        assert len([row for row in load_invoices() if row.get('qbo_origin')]) == 1
        quickbooks.push_document(next(row for row in load_invoices() if row.get('qbo_origin')))
        assert len(fake.posts('/invoice')) == before
        fake.cdc = [{'Payment': [{
            'Id': '70',
            'SyncToken': '0',
            'CustomerRef': {'value': '2'},
            'TotalAmt': 100,
            'TxnDate': '2026-11-01',
            'CurrencyRef': {'value': 'USD'},
            'Line': [],
        }]}]
        first = quickbooks.poll_cdc()
        assert first['count'] == 1
        assert len(load_payments()) == 1
        second = quickbooks.poll_cdc()
        assert len(load_payments()) == 1
        assert second['count'] == 1
    finally:
        quickbooks.TRANSPORT = None


def test_sync_token_conflict_is_logged_and_not_pushed_again(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        created = client.post('/api/customers', json={'name': 'Harbor Glass', 'tier': 'T2', 'credit_currency': 'usd'})
        assert created.status_code == 200, created.get_json()
        customer_id = created.get_json()['id']
        fake.conflict = True
        updated = client.put(f'/api/customers/{customer_id}', json={'name': 'Harbor Glass Ltd', 'tier': 'T2'})
        assert updated.status_code == 200, updated.get_json()
        assert load_customers()[customer_id]['name'] == 'Harbor Glass Ltd'
        assert any('SyncToken conflict' in row['message'] for row in quickbooks.load_log())
    finally:
        quickbooks.TRANSPORT = None

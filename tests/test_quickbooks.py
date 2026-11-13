import base64
import hashlib
import hmac
import json
import urllib.parse

import quickbooks
from quickbooks import CANADA_REALM, US_REALM
from billing import invoice_balance
from storage import load_customers, load_invoices, load_payments, save_customers
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
        assert b'Import existing invoices & payments' in page.data
        assert b'Link customer' in page.data
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


def test_sync_does_not_import_history_until_the_backfill(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('support@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    write_user('ap@enerstar.example', 'customer', customer_id='enerstar', customer_name='Enerstar Windows')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    fake.invoices = [
        {**fake.invoices[0], 'Id': str(1000 + offset), 'DocNumber': str(1000 + offset), 'TotalAmt': 100, 'Balance': 100}
        for offset in range(2, 5)
    ]
    try:
        _connect(client, fake)
        synced = client.post('/api/quickbooks/sync', json={'connection': 'us'})
        assert synced.status_code == 200, synced.get_json()
        assert load_invoices() == []
        assert any('read 0 records' in row.get('message', '') for row in synced.get_json()['connections'][0]['log'])
        empty = client.post('/api/quickbooks/import/history', json={'connection': 'us'})
        assert empty.status_code == 200, empty.get_json()
        assert empty.get_json()['result']['customers'] == 0
        assert load_invoices() == []
        linked = client.post('/api/quickbooks/link-customer', json={
            'connection': 'us', 'qbo_id': '2', 'customer_id': 'open-windows',
        })
        assert linked.status_code == 200, linked.get_json()
        assert linked.get_json()['result']['customer_id'] == 'open-windows'
        assert load_customers()['open-windows']['qbo_id'] == '2'
        still = client.post('/api/quickbooks/sync', json={'connection': 'us'})
        assert still.status_code == 200
        assert load_invoices() == []
        imported = client.post('/api/quickbooks/import/history', json={'connection': 'us'})
        assert imported.status_code == 200, imported.get_json()
        result = imported.get_json()['result']
        assert result['customers'] == 1
        assert result['invoices'] == 3
        assert result['payments'] == 0
        assert any(result['message'] in row.get('message', '') for row in imported.get_json()['connections'][0]['log'])
        numbers = sorted(row['project'] for row in load_invoices())
        assert numbers == ['1002', '1003', '1004']
        again = client.post('/api/quickbooks/import/history', json={'connection': 'us'})
        assert again.get_json()['result']['invoices'] == 0
        assert len(load_invoices()) == 3
    finally:
        quickbooks.TRANSPORT = None


class TwoRealmQBO:
    """Customer id 2 is a different person in each QuickBooks company."""

    def __init__(self):
        self.calls = []
        self.customers = {
            US_REALM: {
                '2': {
                    'Id': '2', 'SyncToken': '1',
                    'DisplayName': 'Open Windows Inc',
                    'CompanyName': 'Open Windows Inc',
                    'GivenName': 'Calvin',
                    'FamilyName': 'Chong',
                },
            },
            CANADA_REALM: {
                '2': {
                    'Id': '2', 'SyncToken': '4',
                    'DisplayName': 'Calvin Chong',
                    'CompanyName': '',
                    'GivenName': 'Calvin',
                    'FamilyName': 'Chong',
                },
            },
        }
        self.invoices = {
            US_REALM: [{
                'Id': '1002', 'SyncToken': '0', 'DocNumber': '1002',
                'CustomerRef': {'value': '2'}, 'TotalAmt': 10, 'Balance': 10,
                'CurrencyRef': {'value': 'USD'}, 'TxnDate': '2026-01-02', 'DueDate': '2026-02-01',
                'Line': [{'Description': 'IGU', 'Amount': 10}],
            }],
            CANADA_REALM: [{
                'Id': '9', 'SyncToken': '0', 'DocNumber': 'CA-9',
                'CustomerRef': {'value': '2'}, 'TotalAmt': 5, 'Balance': 5,
                'CurrencyRef': {'value': 'CAD'}, 'TxnDate': '2026-01-02',
                'Line': [{'Description': 'Other', 'Amount': 5}],
            }],
        }

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split('/') if part]
        realm = parts[parts.index('company') + 1] if 'company' in parts else ''
        if parsed.path.rstrip('/').endswith('/query'):
            statement = urllib.parse.parse_qs(parsed.query).get('query', [''])[0]
            if 'from Customer' in statement:
                row = self.customers.get(realm, {}).get('2') if "Id = '2'" in statement else None
                payload = {'Customer': [row] if row else []}
            elif 'from Invoice' in statement:
                payload = {'Invoice': self.invoices.get(realm, [])}
            elif 'from CreditMemo' in statement:
                payload = {'CreditMemo': []}
            elif 'from Payment' in statement:
                payload = {'Payment': []}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        if '/customer/' in parsed.path and method == 'GET':
            row = self.customers.get(realm, {}).get(parts[-1])
            if not row:
                return 404, {}, b'{}'
            return 200, {}, json.dumps({'Customer': row}).encode()
        return 200, {}, b'{}'


def _save_connection(key, realm, company):
    with quickbooks.connection_scope(key):
        quickbooks.save_tokens({
            'access_token': key, 'refresh_token': key, 'realm_id': realm,
            'expires_at': 9_999_999_999, 'company_name': company,
        })


def test_link_reads_the_card_realm_and_uses_display_name(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('support@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    write_user('calvin@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TwoRealmQBO()
    quickbooks.TRANSPORT = fake
    try:
        _save_connection('us', US_REALM, 'Ora Glass Inc')
        _save_connection('ca', CANADA_REALM, 'Ora Glass (Canada) Inc')
        linked = client.post('/api/quickbooks/link-customer', json={
            'connection': 'us', 'qbo_id': '2', 'customer_id': 'open-windows',
        })
        assert linked.status_code == 200, linked.get_json()
        assert linked.get_json()['result']['name'] == 'Open Windows Inc'
        assert linked.get_json()['result']['realm_id'] == US_REALM
        us_log = ' '.join(row.get('message', '') for row in linked.get_json()['connections'][0]['log'])
        assert 'Open Windows Inc' in us_log and US_REALM in us_log
        assert 'Calvin Chong' not in us_log

        def customer_reads(realm):
            return [
                url for _method, url, _body in fake.calls
                if f'/company/{realm}/' in url and ('Customer+where+Id' in url or f'/customer/' in url)
            ]

        assert customer_reads(US_REALM)
        assert not customer_reads(CANADA_REALM)
        canada = client.post('/api/quickbooks/link-customer', json={
            'connection': 'ca', 'qbo_id': '2', 'customer_id': 'maple',
        })
        assert canada.status_code == 200, canada.get_json()
        assert canada.get_json()['result']['name'] == 'Calvin Chong'
        assert canada.get_json()['result']['realm_id'] == CANADA_REALM
        assert any(f'/v3/company/{CANADA_REALM}/query' in url for _method, url, _body in fake.calls)

        links = quickbooks.load_links()
        for row in links:
            if row.get('connection') == 'us' and row.get('kind') == 'customer':
                row.pop('realm_id', None)
        quickbooks.save_links(links)
        blocked = client.post('/api/quickbooks/import/history', json={'connection': 'us'})
        assert blocked.status_code == 200, blocked.get_json()
        assert blocked.get_json()['result']['invoices'] == 0
        assert 'Link' in blocked.get_json()['message']
        assert load_invoices() == []

        again = client.post('/api/quickbooks/link-customer', json={
            'connection': 'us', 'qbo_id': '2', 'customer_id': 'open-windows',
        })
        assert again.status_code == 200
        imported = client.post('/api/quickbooks/import/history', json={'connection': 'us'})
        assert imported.status_code == 200, imported.get_json()
        assert imported.get_json()['result']['invoices'] == 1
        assert [row['project'] for row in load_invoices()] == ['1002']
        invoice_urls = [url for _method, url, _body in fake.calls if 'Invoice' in url]
        assert invoice_urls
        assert all(US_REALM in url for url in invoice_urls)
    finally:
        quickbooks.TRANSPORT = None


def test_canada_customer_two_is_not_linked_to_open_windows(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('support@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TwoRealmQBO()
    quickbooks.TRANSPORT = fake
    try:
        _save_connection('us', US_REALM, 'Ora Glass Inc')
        _save_connection('ca', CANADA_REALM, 'Ora Glass (Canada) Inc')
        quickbooks.save_links([
            {
                'kind': 'customer', 'connection': 'ca', 'erp_id': 'open-windows',
                'qbo_id': '2', 'sync_token': '0', 'origin': 'qbo', 'realm_id': CANADA_REALM,
            },
            {
                'kind': 'customer', 'connection': 'us', 'erp_id': 'open-windows',
                'qbo_id': '2', 'sync_token': '1', 'origin': 'qbo', 'realm_id': US_REALM,
            },
        ])
        page = client.get('/settings/quickbooks')
        assert page.status_code == 200
        assert b'Unlink customer' in page.data
        kept = [row for row in quickbooks.load_links() if row.get('kind') == 'customer']
        assert [row.get('connection') for row in kept] == ['us']
        with quickbooks.connection_scope('ca'):
            messages = [row.get('message', '') for row in quickbooks.load_settings().get('log') or []]
        assert any('Removed QuickBooks customer 2 from Open Windows' in message for message in messages)
        refused = client.post('/api/quickbooks/link-customer', json={
            'connection': 'ca', 'qbo_id': '2', 'customer_id': 'open-windows',
        })
        assert refused.status_code == 400
        assert 'not Open Windows Inc' in refused.get_json()['error']
        unlinked = client.post('/api/quickbooks/unlink-customer', json={
            'connection': 'us', 'customer_id': 'open-windows',
        })
        assert unlinked.status_code == 200, unlinked.get_json()
        assert [row for row in quickbooks.load_links() if row.get('kind') == 'customer'] == []
        script = __import__('subprocess').run(
            ['node', 'tests/check_qbo_link_buttons.cjs'],
            cwd='/workspace',
            capture_output=True,
            text=True,
        )
        assert script.returncode == 0, script.stdout + script.stderr
    finally:
        quickbooks.TRANSPORT = None


def test_erp_invoice_payment_and_credit_note_are_pushed_once(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    customers = load_customers()
    customers['harbor']['country'] = 'United States'
    customers['harbor']['qbo_tax_code'] = 'NON'
    save_customers(customers)
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

"""Two QuickBooks companies and Canadian intercompany invoices. The API is mocked."""

import json
import urllib.parse
from datetime import date

import quickbooks
from storage import load_customers, load_invoices, save_customers
from tests.conftest import login, write_user
from tests.test_production_orders import _size_item


def _fx(url):
    return {'observations': [
        {'d': '2026-11-10', 'FXUSDCAD': {'v': '1.2500'}},
        {'d': '2026-11-12', 'FXUSDCAD': {'v': '1.4000'}},
    ]}


def _customer(customer_id, name, country, currency='cad'):
    write_user(
        f'sales@{customer_id}.example', 'customer', customer_id=customer_id, customer_name=name,
        customer_tier='T2', customer_subrole='management', tier_override='T5',
        allowed_tiers=['T5'], password='Validpass1',
    )
    customers = load_customers()
    customers[customer_id]['country'] = country
    customers[customer_id]['credit_currency'] = currency
    customers[customer_id]['credit_limit'] = 100000
    customers[customer_id]['credit_approved'] = True
    save_customers(customers)


def _deliver(client, customer_id, currency):
    assert login(client, f'sales@{customer_id}.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': currency,
        'project': 'Lobby',
        'type': 'size',
        'tier': 'T5',
    })
    assert saved.status_code == 200, saved.get_json()
    quote_id = saved.get_json()['id']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': customer_id})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    for status in ('sent', 'made', 'delivered'):
        moved = client.post(f'/api/orders/{order_id}/status', json={'status': status})
        assert moved.status_code == 200, moved.get_json()
    return client.get(f'/api/orders/{order_id}').get_json()


def test_legacy_us_tokens_and_country_default(data_dir):
    (data_dir / 'qbo_tokens.json').write_text(json.dumps({
        'refresh_token': 'keep-me',
        'realm_id': '9341456057091137',
        'company_name': 'Ora Glass Inc',
    }))
    assert quickbooks.load_tokens()['company_name'] == 'Ora Glass Inc'
    assert quickbooks.connection_for_realm('9341456057091137') == 'us'
    with quickbooks.connection_scope('ca'):
        assert quickbooks.load_tokens() == {}
    save_customers({'maple': {'id': 'maple', 'name': 'Maple Glass', 'state': 'BC', 'address': '1 Main'}})
    assert load_customers()['maple']['country'] == 'Canada'
    save_customers({'harbor': {'id': 'harbor', 'name': 'Harbor', 'address': '1 Dock', 'city': 'Seattle'}})
    assert load_customers()['harbor']['country'] == 'United States'


def test_canadian_order_issues_both_invoices_and_us_orders_stay_single(client, monkeypatch):
    monkeypatch.setattr(quickbooks, 'FX_FETCH', _fx)
    write_user('admin@oraglass.us', 'admin')
    _customer('harbor-glass', 'Harbor Glass', 'United States', currency='usd')
    _customer('maple-glass', 'Maple Glass', 'Canada', currency='cad')

    us_order = _deliver(client, 'harbor-glass', 'usd')
    assert len(us_order['documents']) == 1
    assert us_order['documents'][0]['number'] == 'INV-0001'
    assert us_order['documents'][0]['issuer'] == 'us'
    assert us_order['documents'][0]['currency'] == 'usd'

    order = _deliver(client, 'maple-glass', 'cad')
    documents = order['documents']
    assert len(documents) == 2
    customer_invoice = next(row for row in documents if row['issuer'] == 'ca')
    intercompany = next(row for row in documents if row['kind'] == 'intercompany')
    assert customer_invoice['number'].startswith('CA-INV-')
    assert customer_invoice['currency'] == 'cad'
    assert intercompany['issuer'] == 'us'
    assert intercompany['number'].startswith('INV-')
    assert intercompany['currency'] == 'usd'
    assert intercompany['fx_rate'] == 1.4
    assert intercompany['fx_date'] == '2026-11-12'
    stored = {row['id']: row for row in load_invoices()}
    cad_total = stored[customer_invoice['id']]['total']
    usd_total = stored[intercompany['id']]['total']
    assert cad_total == order['total']
    assert usd_total == round(cad_total / 1.4, 2)
    assert stored[intercompany['id']]['company'] == 'Ora Glass (Canada) Inc'
    assert stored[intercompany['id']]['fx_pair'] == 'FXUSDCAD'
    assert order['invoice_id'] == customer_invoice['id']
    names = [row['name'] for row in client.get('/api/customers').get_json()]
    assert 'Ora Glass (Canada) Inc' not in names
    assert 'Maple Glass' in names

    changed = client.post(
        f"/api/invoices/{intercompany['id']}/exchange-rate",
        json={'rate': 2},
    )
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()['total'] == round(cad_total / 2, 2)
    assert changed.get_json()['fx_rate'] == 2
    assert changed.get_json()['issuer_label'] == 'Ora Glass Inc'
    assert changed.get_json()['type_label'] == 'Intercompany invoice'

    manual = client.post('/api/invoices', json={
        'customer_id': 'maple-glass',
        'lines': [{'description': 'Site visit', 'qty': 1, 'unit_price': 50}],
    })
    assert manual.status_code == 200, manual.get_json()
    assert manual.get_json()['issuer'] == 'ca'
    assert manual.get_json()['number'].startswith('CA-INV-')
    credit = client.post('/api/invoices', json={
        'doc_type': 'credit_note',
        'invoice_id': manual.get_json()['id'],
        'reason': 'Site visit waived',
        'lines': [{'description': 'Site visit', 'qty': 1, 'unit_price': 50}],
    })
    assert credit.status_code == 200, credit.get_json()
    assert credit.get_json()['issuer'] == 'ca'
    assert credit.get_json()['number'].startswith('CA-CN-')

    page = client.get('/orders').data
    assert b'Ora Glass (Canada) Inc' in page
    assert b'Ora Glass Inc' in page
    invoices = client.get('/invoices').data
    assert b'Issued by' in invoices
    listed = client.get('/api/invoices').get_json()
    assert any(row['type_label'] == 'Intercompany invoice' for row in listed)


class RecordingQBO:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path.endswith('/tokens/bearer'):
            return 200, {}, json.dumps({
                'access_token': 'access-1', 'refresh_token': 'refresh-1', 'expires_in': 3600,
            }).encode()
        if 'companyinfo' in path:
            name = 'Ora Glass (Canada) Inc' if '/999/' in path else 'Ora Glass Inc'
            return 200, {}, json.dumps({'CompanyInfo': {'CompanyName': name}}).encode()
        if path.endswith('/query'):
            statement = query.get('query', [''])[0]
            if 'from Customer' in statement and 'DisplayName' in statement:
                payload = {'Customer': [{'Id': '77', 'DisplayName': 'Ora Glass (Canada) Inc'}]}
            elif 'from Customer' in statement:
                payload = {'Customer': [{
                    'Id': '8', 'SyncToken': '0', 'DisplayName': 'North Glass',
                    'CurrencyRef': {'value': 'CAD'}, 'Balance': 10,
                    'BillAddr': {'City': 'Toronto', 'CountrySubDivisionCode': 'ON'},
                }]}
            elif 'from Item' in statement:
                payload = {'Item': [{'Id': '10', 'Name': 'Insulated Glass Units'}]}
            elif 'from Term' in statement:
                payload = {'Term': [{'Id': '3', 'Name': 'Net 30'}]}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        data = json.loads(body.decode() if isinstance(body, bytes) else (body or b'{}'))
        if path.rstrip('/').endswith('/customer'):
            return 200, {}, json.dumps({'Customer': {'Id': '77', 'SyncToken': '0', 'DisplayName': data.get('DisplayName')}}).encode()
        if path.rstrip('/').endswith('/invoice'):
            return 200, {}, json.dumps({'Invoice': {'Id': '80', 'SyncToken': '0'}}).encode()
        if '/payment' in path:
            return 200, {}, json.dumps({'Payment': {'Id': '82', 'SyncToken': '0'}}).encode()
        if '/customer/' in path and method == 'GET':
            return 200, {}, json.dumps({'Customer': {
                'Id': '8', 'SyncToken': '1', 'DisplayName': 'North Glass', 'CurrencyRef': {'value': 'CAD'},
            }}).encode()
        return 200, {}, b'{}'


def _realm(url):
    parts = urllib.parse.urlparse(url).path.split('/')
    if 'company' in parts:
        return parts[parts.index('company') + 1]
    return ''


def test_each_company_has_its_own_connection_import_and_push(client, monkeypatch):
    monkeypatch.setattr(quickbooks, 'FX_FETCH', _fx)
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://ora-glass-erp.onrender.com/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = RecordingQBO()
    quickbooks.TRANSPORT = fake
    try:
        page = client.get('/settings/quickbooks')
        assert b'Ora Glass Inc' in page.data
        assert b'Ora Glass (Canada) Inc' in page.data
        assert b'Connect to QuickBooks' in page.data
        start = client.get('/quickbooks/connect?connection=ca')
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
        callback = client.get(f'/quickbooks/callback?code=auth-code&realmId=999&state={state}')
        assert callback.status_code == 302
        with quickbooks.connection_scope('ca'):
            assert quickbooks.load_tokens()['realm_id'] == '999'
            assert quickbooks.load_tokens()['company_name'] == 'Ora Glass (Canada) Inc'
        assert quickbooks.load_tokens() == {}

        preview = client.post('/api/quickbooks/import/preview', json={'connection': 'ca'})
        assert preview.status_code == 200, preview.get_json()
        assert preview.get_json()['customers'][0]['currency'] == 'cad'
        applied = client.post('/api/quickbooks/import/apply', json={
            'connection': 'ca',
            'decisions': {'8': {'action': 'create', 'customer_id': ''}},
        })
        assert applied.status_code == 200, applied.get_json()
        imported = next(row for row in load_customers().values() if row.get('name') == 'North Glass')
        assert imported['country'] == 'Canada'
        assert imported['credit_currency'] == 'cad'

        quickbooks.save_tokens({
            'access_token': 'us-access', 'refresh_token': 'us-refresh',
            'realm_id': '111', 'expires_at': 9_999_999_999, 'company_name': 'Ora Glass Inc',
        })
        _customer('maple-glass', 'Maple Glass', 'Canada', currency='cad')
        customers = load_customers()
        customers['maple-glass']['qbo_tax_code'] = '5'
        save_customers(customers)
        fake.calls.clear()
        order = _deliver(client, 'maple-glass', 'cad')
        realms = [_realm(url) for method, url, _body in fake.calls if method == 'POST' and '/invoice' in url]
        assert '999' in realms
        assert '111' in realms
        created = [json.loads(body) for method, url, body in fake.calls if method == 'POST' and url.rstrip('/').endswith('/customer')]
        assert any(row.get('DisplayName') == 'Ora Glass (Canada) Inc' for row in created) is False
        matched = client.post('/api/quickbooks/intercompany', json={'action': 'match'})
        assert matched.status_code == 200
        assert matched.get_json()['intercompany_qbo_id'] == '77'
        paid = client.post('/api/payments', json={
            'customer_id': 'maple-glass',
            'amount': 10,
            'currency': 'cad',
            'method': 'wire',
            'date': date.today().isoformat(),
        })
        assert paid.status_code == 200, paid.get_json()
        payment_realms = [_realm(url) for method, url, _body in fake.calls if method == 'POST' and '/payment' in url]
        assert payment_realms == ['999']
        assert order['documents']
    finally:
        quickbooks.TRANSPORT = None

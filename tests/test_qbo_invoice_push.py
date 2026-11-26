"""ERP invoices are posted to the QuickBooks company that should hold them."""

import json
import urllib.parse

import quickbooks
from quickbooks import CANADA_REALM, US_REALM
from storage import load_invoices, save_customers, save_invoices, save_orders
from tests.conftest import login, write_user
from tests.test_quickbooks import FakeQBO, _configure


def _spec():
    return {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'}


def _connect(client, fake, key, realm):
    quickbooks.TRANSPORT = fake
    start = client.get(f'/quickbooks/connect?connection={key}')
    state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
    callback = client.get(f'/quickbooks/callback?code=auth-code&realmId={realm}&state={state}')
    assert callback.status_code == 302, callback.headers.get('Location')


def _books():
    from storage import load_customers
    customers = load_customers()
    customers['ora-glass-canada'] = {
        'id': 'ora-glass-canada', 'name': 'Ora Glass (Canada) Inc', 'books_entity': 'ca',
        'country': 'Canada', 'status': 'active',
    }
    customers['highq'] = {
        'id': 'highq', 'name': 'HighQ Developments Inc', 'country': 'Canada', 'province': 'BC',
        'qbo_tax_code': '5', 'status': 'active',
    }
    save_customers(customers)


def test_unconnected_invoice_keeps_the_reason_and_retry_is_admin_only(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('seller@oraglass.us', 'sales')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_invoices([{
        'id': 54, 'number': 'INV-0054', 'kind': 'intercompany', 'issuer': 'us', 'currency': 'usd',
        'customer_id': 'ora-glass-canada', 'company': 'Ora Glass (Canada) Inc',
        'total': 10, 'balance': 10, 'invoice_date': '2026-11-20',
        'items': [{'name': 'Glass', 'pcs': 1, 'qty': 1, 'unit_price': 10, 'total': 10}],
    }])
    waiting = client.post('/api/invoices/54/quickbooks')
    assert waiting.status_code == 200, waiting.get_json()
    body = waiting.get_json()
    assert body['qbo_sync'] == 'not_connected'
    assert body['qbo_sync_label'] == 'Not connected'
    assert US_REALM in body['qbo_error']
    assert 'Send to QuickBooks' in client.get('/invoices/54').get_data(as_text=True)
    client.post('/logout')
    assert login(client, 'seller@oraglass.us').status_code == 302
    assert client.post('/api/invoices/54/quickbooks').status_code == 403


def test_intercompany_posts_to_the_us_company_and_retry_does_not_duplicate(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'project': 'Misc',
        'status': 'approved', 'currency': 'cad',
        'items': [{'name': 'Unit', 'pcs': 1, 'total': 140, 'price_pc': 140, 'spec': _spec()}],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    }])
    save_invoices([{
        'id': 54, 'number': 'INV-0054', 'order_id': 1, 'kind': 'intercompany', 'issuer': 'us',
        'currency': 'usd', 'customer_id': 'ora-glass-canada', 'company': 'Ora Glass (Canada) Inc',
        'total': 100, 'balance': 100, 'invoice_date': '2026-11-20',
        'items': [{'name': 'Unit', 'serial': 1, 'pcs': 1, 'qty': 1, 'unit_price': 100, 'total': 100, 'spec': _spec()}],
    }])
    fake = FakeQBO()
    fake.customers = [{
        'Id': '100000001', 'SyncToken': '0', 'DisplayName': 'Ora Glass (Canada) Inc',
        'CurrencyRef': {'value': 'USD'},
    }]
    try:
        _connect(client, fake, 'us', US_REALM)
        sent = client.post('/api/invoices/54/quickbooks')
        assert sent.status_code == 200, sent.get_json()
        body = sent.get_json()
        assert body['qbo_sync'] == 'synced'
        assert body['qbo_id'] == '80'
        assert body['qbo_doc_number'] == 'INV-0054'
        posts = [call for call in fake.calls if call[0] == 'POST' and '/invoice' in call[1] and 'operation=void' not in call[1]]
        assert len(posts) == 1
        assert US_REALM in posts[0][1]
        payload = json.loads(posts[0][2])
        assert payload['CustomerRef']['value'] == '100000001'
        assert payload['DocNumber'] == 'INV-0054'
        assert payload['CurrencyRef']['value'] == 'USD'
        again = client.post('/api/invoices/54/quickbooks')
        assert again.get_json()['qbo_sync'] == 'synced'
        posts = [call for call in fake.calls if call[0] == 'POST' and '/invoice' in call[1] and 'operation=void' not in call[1]]
        assert len(posts) == 1
        stored = next(row for row in load_invoices() if row['id'] == 54)
        assert stored['qbo_id'] == '80'
        assert stored['qbo_realm_id'] == US_REALM
    finally:
        quickbooks.TRANSPORT = None


def test_canada_customer_invoice_posts_to_the_canada_company(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    save_invoices([{
        'id': 1, 'number': 'CA-INV-0001', 'kind': 'customer', 'issuer': 'ca', 'currency': 'cad',
        'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'qbo_tax_code': '5',
        'ship_province': 'BC', 'total': 80, 'balance': 80, 'invoice_date': '2026-11-20',
        'items': [{'name': 'Glass', 'pcs': 1, 'qty': 1, 'unit_price': 80, 'total': 80}],
    }])
    fake = FakeQBO()
    try:
        _connect(client, fake, 'ca', CANADA_REALM)
        sent = client.post('/api/invoices/1/quickbooks')
        assert sent.status_code == 200, sent.get_json()
        assert sent.get_json()['qbo_sync'] == 'synced', sent.get_json()
        posts = [
            call for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
        ]
        assert len(posts) == 1
        assert CANADA_REALM in posts[0][1]
        payload = json.loads(posts[0][2])
        assert payload['CurrencyRef']['value'] == 'CAD'
        assert payload['DocNumber'] == 'CA-INV-0001'
        assert sent.get_json()['qbo_realm_id'] == CANADA_REALM or load_invoices()[0]['qbo_realm_id'] == CANADA_REALM
    finally:
        quickbooks.TRANSPORT = None


def test_regenerate_updates_the_quickbooks_invoice_in_place(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    from storage import load_customers
    customers = load_customers()
    customers['highq'].update({'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'cad'})
    save_customers(customers)
    import currency_rules
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'project': 'Misc',
        'status': 'approved', 'currency': 'cad',
        'items': [{'name': 'Unit', 'pcs': 1, 'total': 140, 'price_pc': 140, 'spec': _spec()}],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    }])
    fake = FakeQBO()
    fake.customers = [{
        'Id': '100000001', 'SyncToken': '1', 'DisplayName': 'Ora Glass (Canada) Inc',
        'CurrencyRef': {'value': 'USD'},
    }]
    try:
        _connect(client, fake, 'us', US_REALM)
        created = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
        assert created.status_code == 200, created.get_json()
        original = next(row for row in load_invoices() if row.get('kind') == 'intercompany' and not row.get('voided'))
        assert original['qbo_sync'] == 'synced'
        assert original['qbo_id'] == '80'
        first_posts = [
            call for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
            and 'operation=void' not in call[1]
        ]
        assert len(first_posts) == 1
        replaced = client.post(f"/api/invoices/{original['id']}/regenerate")
        assert replaced.status_code == 200, replaced.get_json()
        voids = [call for call in fake.calls if call[0] == 'POST' and 'operation=void' in call[1] and '/invoice' in call[1]]
        assert voids == []
        writes = [
            call for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
            and 'operation=void' not in call[1]
        ]
        assert len(writes) == 2
        updated = json.loads(writes[1][2])
        assert updated['Id'] == '80'
        assert updated.get('sparse') is True
        assert updated.get('SyncToken')
        assert updated.get('DocNumber') == json.loads(writes[0][2]).get('DocNumber')
        fresh = next(row for row in load_invoices() if row.get('kind') == 'intercompany' and not row.get('voided'))
        assert fresh['id'] != original['id']
        assert fresh['qbo_sync'] == 'synced'
        assert fresh['qbo_id'] == '80'
        voided = next(row for row in load_invoices() if row['id'] == original['id'])
        assert voided['voided'] is True
        assert voided.get('qbo_id') in (None, '')
        assert voided['qbo_sync'] == 'moved'
    finally:
        quickbooks.TRANSPORT = None


def test_regenerate_refuses_when_the_old_quickbooks_invoice_cannot_be_voided(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    from storage import load_customers
    customers = load_customers()
    customers['highq'].update({'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'cad'})
    save_customers(customers)
    import currency_rules
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'project': 'Misc',
        'status': 'approved', 'currency': 'cad',
        'items': [{'name': 'Unit', 'pcs': 1, 'total': 140, 'price_pc': 140, 'spec': _spec()}],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    }])
    fake = FakeQBO()
    fake.customers = [{
        'Id': '100000001', 'SyncToken': '1', 'DisplayName': 'Ora Glass (Canada) Inc',
        'CurrencyRef': {'value': 'USD'},
    }]
    try:
        _connect(client, fake, 'us', US_REALM)
        created = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
        assert created.status_code == 200, created.get_json()
        original = next(row for row in load_invoices() if row.get('kind') == 'intercompany')
        with quickbooks.connection_scope('us'):
            quickbooks.disconnect()
        refused = client.post(f"/api/invoices/{original['id']}/regenerate")
        assert refused.status_code == 400, refused.get_json()
        assert US_REALM in (refused.get_json().get('error') or '')
        kept = next(row for row in load_invoices() if row['id'] == original['id'])
        assert not kept.get('voided')
        assert [row for row in load_invoices() if row.get('kind') == 'intercompany'] == [kept]
        voids = [call for call in fake.calls if 'operation=void' in call[1]]
        assert voids == []
    finally:
        quickbooks.TRANSPORT = None


def test_regenerate_updates_in_place_when_quickbooks_shows_a_payment(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    from storage import load_customers
    customers = load_customers()
    customers['highq'].update({'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'cad'})
    save_customers(customers)
    import currency_rules
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'project': 'Misc',
        'status': 'approved', 'currency': 'cad',
        'items': [{'name': 'Unit', 'pcs': 1, 'total': 140, 'price_pc': 140, 'spec': _spec()}],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    }])
    fake = FakeQBO()
    fake.customers = [{
        'Id': '100000001', 'SyncToken': '1', 'DisplayName': 'Ora Glass (Canada) Inc',
        'CurrencyRef': {'value': 'USD'},
    }]
    try:
        _connect(client, fake, 'us', US_REALM)
        created = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
        assert created.status_code == 200, created.get_json()
        original = next(row for row in load_invoices() if row.get('kind') == 'intercompany')
        remote = next(row for row in fake.invoices if str(row.get('Id')) == str(original['qbo_id']))
        remote['TotalAmt'] = 100
        remote['Balance'] = 40
        remote['DocNumber'] = original.get('qbo_doc_number') or original.get('number')
        replaced = client.post(f"/api/invoices/{original['id']}/regenerate")
        assert replaced.status_code == 200, replaced.get_json()
        voids = [call for call in fake.calls if 'operation=void' in call[1]]
        assert voids == []
        writes = [
            call for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
            and 'operation=void' not in call[1]
        ]
        updated = json.loads(writes[-1][2])
        assert updated['Id'] == original['qbo_id']
        assert updated.get('sparse') is True
        assert updated.get('DocNumber') == remote['DocNumber']
        fresh = next(row for row in load_invoices() if row.get('kind') == 'intercompany' and not row.get('voided'))
        assert fresh['qbo_id'] == original['qbo_id']
    finally:
        quickbooks.TRANSPORT = None


def test_regenerate_moves_the_payment_onto_the_replacement(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _books()
    from storage import load_customers, load_payments, save_payments
    customers = load_customers()
    customers['highq'].update({'credit_approved': True, 'credit_limit': 100000, 'credit_currency': 'cad'})
    save_customers(customers)
    import currency_rules
    currency_rules.record_rate(1.4, '2026-11-01', 'admin@oraglass.us')
    save_orders([{
        'id': 1, 'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'project': 'Misc',
        'status': 'approved', 'currency': 'cad',
        'items': [{'name': 'Unit', 'pcs': 1, 'total': 140, 'price_pc': 140, 'spec': _spec()}],
        'packing_lists': [{
            'id': 1, 'current': True, 'number': 'XF1', 'rack': '1054',
            'lines': [{'serial': 1, 'qty': 1}],
        }],
    }])
    fake = FakeQBO()
    fake.customers = [{
        'Id': '100000001', 'SyncToken': '1', 'DisplayName': 'Ora Glass (Canada) Inc',
        'CurrencyRef': {'value': 'USD'},
    }]
    try:
        _connect(client, fake, 'us', US_REALM)
        created = client.post('/api/orders/1/customer-invoice', json={'create_intercompany': True})
        assert created.status_code == 200, created.get_json()
        original = next(row for row in load_invoices() if row.get('kind') == 'intercompany' and not row.get('voided'))
        save_payments([{
            'id': 7, 'customer_id': original['customer_id'], 'currency': 'usd', 'amount': 25,
            'voided': False, 'date': '2026-11-23',
            'allocations': [{'target_type': 'invoice', 'target_id': original['id'], 'amount': 25}],
        }])
        replaced = client.post(f"/api/invoices/{original['id']}/regenerate")
        assert replaced.status_code == 200, replaced.get_json()
        fresh = next(row for row in load_invoices() if row.get('kind') == 'intercompany' and not row.get('voided'))
        assert fresh['id'] != original['id']
        payment = load_payments()[0]
        assert payment['allocations'] == [
            {'target_type': 'invoice', 'target_id': fresh['id'], 'amount': 25},
        ]
        assert not any(
            row.get('target_id') == original['id'] for row in payment['allocations']
        )
        voids = [call for call in fake.calls if 'operation=void' in call[1]]
        assert voids == []
    finally:
        quickbooks.TRANSPORT = None


def test_excess_allocation_is_left_as_unallocated_credit():
    from app import _move_invoice_allocations
    from storage import load_payments, save_invoices, save_payments
    old = {'id': 54, 'number': 'INV-0054', 'total': 100}
    new = {'id': 55, 'number': 'INV-0055', 'total': 40}
    save_invoices([old, new])
    save_payments([{
        'id': 7, 'amount': 80, 'voided': False,
        'allocations': [{'target_type': 'invoice', 'target_id': 54, 'amount': 80}],
    }])
    warning = _move_invoice_allocations(old, new)
    assert '40.00' in warning
    assert 'unallocated credit' in warning
    payment = load_payments()[0]
    assert payment['allocations'] == [
        {'target_type': 'invoice', 'target_id': 55, 'amount': 40},
    ]


def test_repointing_a_quickbooks_payment_happens_before_the_void(monkeypatch):
    _configure(monkeypatch)
    fake = FakeQBO()
    fake.invoices.append({
        'Id': '568', 'SyncToken': '2', 'DocNumber': 'INV-0054',
        'TotalAmt': 100, 'Balance': 40,
    })
    fake.invoices.append({
        'Id': '569', 'SyncToken': '0', 'DocNumber': 'INV-0055',
        'TotalAmt': 100, 'Balance': 100,
    })
    fake.payments.append({
        'Id': '82', 'SyncToken': '1', 'TotalAmt': 60,
        'Line': [{'Amount': 60, 'LinkedTxn': [{'TxnId': '568', 'TxnType': 'Invoice'}]}],
    })
    quickbooks.TRANSPORT = fake
    with quickbooks.connection_scope('us'):
        quickbooks.save_tokens({
            'access_token': 'access-1', 'refresh_token': 'refresh-1',
            'realm_id': quickbooks.US_REALM, 'expires_at': 9999999999,
        })
        ok, error = quickbooks.repoint_payments_then_void('568', '569', ['82'])
    assert ok, error
    assert error == ''
    kinds = []
    for method, url, body in fake.calls:
        if method != 'POST':
            continue
        path = urllib.parse.urlparse(url).path
        if 'operation=void' in url:
            kinds.append('void')
        elif path.rstrip('/').endswith('/payment'):
            kinds.append('payment')
    assert kinds == ['payment', 'void']
    moved = next(row for row in fake.payments if row['Id'] == '82')
    assert moved['Line'][0]['LinkedTxn'][0]['TxnId'] == '569'
    quickbooks.TRANSPORT = None


def test_a_failed_payment_relink_does_not_void_the_old_invoice(monkeypatch):
    _configure(monkeypatch)
    fake = FakeQBO()
    fake.invoices.append({
        'Id': '568', 'SyncToken': '2', 'DocNumber': 'INV-0054',
        'TotalAmt': 100, 'Balance': 40,
    })
    quickbooks.TRANSPORT = fake

    def fail_get(path):
        if '/payment/' in path:
            raise quickbooks.QuickBooksError('Payment could not be read.')
        return quickbooks.api_get.__wrapped__(path) if hasattr(quickbooks.api_get, '__wrapped__') else None

    with quickbooks.connection_scope('us'):
        quickbooks.save_tokens({
            'access_token': 'access-1', 'refresh_token': 'refresh-1',
            'realm_id': quickbooks.US_REALM, 'expires_at': 9999999999,
        })
        original_fetch = quickbooks._fetch_remote_payment
        quickbooks._fetch_remote_payment = lambda qbo_id: (_ for _ in ()).throw(
            quickbooks.QuickBooksError('Payment could not be read.')
        )
        try:
            ok, error = quickbooks.repoint_payments_then_void('568', '569', ['82'])
        finally:
            quickbooks._fetch_remote_payment = original_fetch
    assert ok is False
    assert 'was not moved' in error
    assert [call for call in fake.calls if 'operation=void' in call[1]] == []
    quickbooks.TRANSPORT = None

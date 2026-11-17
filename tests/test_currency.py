"""Canadian companies are CAD. The intercompany bill uses the ERP exchange rate."""

import io
import json

from openpyxl import load_workbook

import currency_rules
import quickbooks
from storage import load_customers, load_invoices, load_orders, load_quotes, save_customers
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item


def test_canadian_credit_currency_is_forced_and_migrated(client, caplog):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/customers', json={
        'name': 'Maple Glass', 'tier': 'T2', 'country': 'Canada', 'province': 'ON',
        'credit_currency': 'usd', 'credit_limit': 10,
    })
    assert created.status_code == 200, created.get_json()
    customer_id = created.get_json()['id']
    assert load_customers()[customer_id]['credit_currency'] == 'cad'
    page = client.get('/customers')
    assert b'c-credit-currency' in page.data
    save_customers({
        'north': {'id': 'north', 'name': 'North Glass', 'country': 'Canada', 'credit_currency': 'usd', 'tier': 'T2', 'status': 'active'},
        'open-windows': {'id': 'open-windows', 'name': 'Open Windows', 'country': 'United States', 'credit_currency': 'usd', 'tier': 'T2', 'status': 'active'},
    })
    with caplog.at_level('INFO', logger='ora_glass.currency'):
        loaded = load_customers()
    assert loaded['north']['credit_currency'] == 'cad'
    assert loaded['open-windows']['country'] == 'Canada'
    assert loaded['open-windows']['credit_currency'] == 'cad'
    assert 'North Glass' in caplog.text


def test_customer_currency_is_locked_and_staff_must_convert(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'kiu@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management',
    )
    customers = load_customers()
    customers['maple']['country'] = 'Canada'
    customers['maple']['credit_limit'] = 100000
    customers['maple']['credit_approved'] = True
    save_customers(customers)
    assert login(client, 'kiu@maple.example').status_code == 302
    page = client.get('/quotes')
    assert b'Currency is CAD for this company.' in page.data
    assert b'aria-disabled="true"' in page.data
    item = _size_item()
    item['spec'] = dict(SPEC)
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    stored = next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])
    assert stored['currency'] == 'cad'
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    staff = client.post('/api/quotes', json={
        'items': [item], 'currency': 'usd', 'project': 'Staff', 'type': 'size', 'tier': 'T2',
    })
    quote_id = staff.get_json()['id']
    blocked = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'maple'})
    assert blocked.status_code == 409
    assert blocked.get_json()['currency_mismatch'] is True
    assert blocked.get_json()['customer_currency'] == 'cad'
    changed = client.post(f'/api/quotes/{quote_id}/currency', json={'currency': 'cad'})
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()['currency'] == 'cad'
    from app import PRICING
    from quoting import reprice_size_line
    from storage import load_tiers
    expected = reprice_size_line(dict(SPEC), item.get('pcs') or 1, 'T2', 'cad', PRICING, load_tiers())
    glass = next(row for row in changed.get_json()['items'] if row.get('spec'))
    assert glass['price_sqf'] == expected['price_sqf']
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'maple'})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    assert order['currency'] == 'cad'


def test_intercompany_uses_the_erp_rate_in_effect_on_the_invoice_date(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    currency_rules.record_rate(1.25, '2020-01-01', 'admin@oraglass.us')
    currency_rules.record_rate(1.5, '2099-01-01', 'admin@oraglass.us')
    rate, effective = currency_rules.rate_on('2026-11-15')
    assert rate == 1.25
    assert effective == '2020-01-01'
    assert currency_rules.rate_on('2099-06-01')[0] == 1.5
    page = client.get('/currency-settings')
    assert page.status_code == 200
    assert b'ERP exchange rate' in page.data
    assert b'admin@oraglass.us' in page.data
    denied = client.post('/logout')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor', customer_tier='T2')
    assert login(client, 'lee@harbor.example').status_code == 302
    assert client.get('/currency-settings').status_code == 302


def test_production_excel_unit_price_is_usd(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'kiu@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management',
    )
    customers = load_customers()
    customers['maple'].update({'country': 'Canada', 'credit_limit': 100000, 'credit_approved': True})
    save_customers(customers)
    currency_rules.record_rate(1.25, '2000-01-01', 'admin@oraglass.us')
    assert login(client, 'kiu@maple.example').status_code == 302
    item = _size_item()
    item['spec'] = dict(SPEC)
    item['price_sqf'] = 12.5
    saved = client.post('/api/quotes', json={
        'items': [item], 'currency': 'cad', 'project': 'Excel', 'type': 'size', 'tier': 'T2',
    })
    quote_id = saved.get_json()['id']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'maple'})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    download = client.get(f'/api/orders/{order_id}/production.xlsx')
    sheet = load_workbook(io.BytesIO(download.data)).active
    values = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))]
    order = next(row for row in load_orders() if row['id'] == order_id)
    cad_price = order['items'][0]['price_sqf']
    assert values[4] == round(cad_price / 1.25, 4)


def test_canada_push_is_cad_and_an_existing_usd_link_is_not_modified(client, monkeypatch):
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://example.test/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')
    posted = []

    def transport(method, url, headers, body):
        posted.append((method, url, body))
        if 'tokens/bearer' in url or url.endswith('/tokens/bearer'):
            return 200, {}, json.dumps({'access_token': 'a', 'refresh_token': 'r', 'expires_in': 3600}).encode()
        if 'companyinfo' in url:
            return 200, {}, json.dumps({'CompanyInfo': {'CompanyName': 'Ora Glass (Canada) Inc'}}).encode()
        path = url.split('?', 1)[0].rstrip('/')
        if path.endswith('/customer') and method == 'POST':
            data = json.loads(body)
            return 200, {}, json.dumps({'Customer': {'Id': '55', 'SyncToken': '0', 'DisplayName': data.get('DisplayName'), 'CurrencyRef': data.get('CurrencyRef')}}).encode()
        if '/query' in url:
            return 200, {}, json.dumps({'QueryResponse': {'Item': [{'Id': '10', 'Name': 'Insulated Glass Units'}], 'Term': [{'Id': '3', 'Name': 'Net 30'}]}}).encode()
        return 200, {}, b'{}'

    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    quickbooks.TRANSPORT = transport
    try:
        with quickbooks.connection_scope('ca'):
            quickbooks.save_tokens({
                'access_token': 'ca', 'refresh_token': 'ca-r', 'realm_id': quickbooks.CANADA_REALM,
                'expires_at': 9_999_999_999,
            })
        customers = load_customers()
        customers['maple'] = {
            'id': 'maple', 'name': 'Maple Glass', 'country': 'Canada', 'credit_currency': 'usd',
            'tier': 'T2', 'status': 'active',
        }
        save_customers(customers)
        posted.clear()
        remote = quickbooks.push_customer('maple')
        assert remote['CurrencyRef']['value'] == 'CAD'
        body = json.loads(posted[-1][2])
        assert body['CurrencyRef']['value'] == 'CAD'
        with quickbooks.connection_scope('ca'):
            quickbooks.upsert_link('customer', 'maple', '55', '0', 'erp', qbo_currency='usd')
        posted.clear()
        assert quickbooks.push_customer('maple') is None
        assert not any(url.split('?', 1)[0].rstrip('/').endswith('/customer') and method == 'POST' for method, url, _body in posted)
        flags = []
        with quickbooks.connection_scope('ca'):
            flags = quickbooks.load_settings().get('usd_customer_flags') or []
        assert 'maple' in flags
    finally:
        quickbooks.TRANSPORT = None

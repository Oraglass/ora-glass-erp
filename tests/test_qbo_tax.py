"""Province suggestions and the customer's own QuickBooks tax code. The API is mocked."""

import json
import urllib.parse

import quickbooks
from storage import inferred_province, load_customers, load_invoices, save_customers
from tests.conftest import login, write_user
from tests.test_production_orders import _size_item

SUPPLY_ITEM = 'Insulated Glass Units (supply only)'
TAX_CODES = [
    {'Id': '5', 'Name': 'GST', 'Active': True},
    {'Id': '6', 'Name': 'HST ON', 'Active': True},
    {'Id': '7', 'Name': 'GST/PST BC', 'Active': True},
    {'Id': '8', 'Name': 'GST/QST QC', 'Active': True},
    {'Id': '9', 'Name': 'Exempt', 'Active': True},
    {'Id': '10', 'Name': 'Old inactive', 'Active': False},
]


def _configure(monkeypatch):
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://ora-glass-erp.onrender.com/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')
    monkeypatch.setattr(quickbooks, 'FX_FETCH', lambda url: {
        'observations': [{'d': '2026-11-12', 'FXUSDCAD': {'v': '1.4000'}}],
    })


class TaxQBO:
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
            name = 'Ora Glass (Canada) Inc' if '/123146408064299/' in path else 'Ora Glass Inc'
            return 200, {}, json.dumps({'CompanyInfo': {'CompanyName': name}}).encode()
        if path.endswith('/query'):
            statement = query.get('query', [''])[0]
            if 'from TaxCode' in statement:
                payload = {'TaxCode': TAX_CODES}
            elif 'from Item' in statement:
                payload = {'Item': [{'Id': '42', 'Name': SUPPLY_ITEM}]}
            elif 'from Term' in statement:
                payload = {'Term': [{'Id': '3', 'Name': 'Net 30'}]}
            elif 'from Customer' in statement and 'DisplayName' in statement:
                payload = {'Customer': [{'Id': '77', 'DisplayName': 'Ora Glass (Canada) Inc'}]}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        data = json.loads(body.decode() if isinstance(body, bytes) else (body or b'{}'))
        if path.rstrip('/').endswith('/customer'):
            return 200, {}, json.dumps({'Customer': {'Id': '55', 'SyncToken': '0', 'DisplayName': data.get('DisplayName')}}).encode()
        if path.rstrip('/').endswith('/invoice'):
            return 200, {}, json.dumps({'Invoice': {'Id': '80', 'SyncToken': '0'}}).encode()
        if 'taxcode' in path.lower() and method != 'GET':
            raise AssertionError('tax codes are read-only')
        return 200, {}, b'{}'


def _posts(fake, suffix):
    return [call for call in fake.calls if call[0] == 'POST' and suffix in urllib.parse.urlparse(call[1]).path]


def _queries(fake):
    found = []
    for method, url, _body in fake.calls:
        if method == 'GET' and url.endswith('/query') or (method == 'GET' and '/query?' in url):
            found.append(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('query', [''])[0])
    return found


def test_province_is_derived_from_the_address():
    assert inferred_province({'state': 'Ontario'}) == 'ON'
    assert inferred_province({'state': 'BC'}) == 'BC'
    assert inferred_province({'address': '100 King Street', 'city': 'Toronto', 'zip': 'M5V 2T6'}) == 'ON'
    assert inferred_province({'address': '1 Main', 'city': 'Vancouver', 'state': 'British Columbia'}) == 'BC'
    assert inferred_province({'address': 'on the waterfront', 'city': 'Toronto'}) == ''
    save_customers({'maple': {
        'id': 'maple', 'name': 'Maple', 'country': 'Canada', 'state': 'QC', 'address': '1 Rue',
    }})
    assert load_customers()['maple']['province'] == 'QC'


def test_customer_tax_code_is_suggested_then_used_on_the_canada_invoice(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@maple.example', 'customer', customer_id='maple-glass', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management', tier_override='T5',
        allowed_tiers=['T5'], password='Validpass1',
    )
    customers = load_customers()
    customers['maple-glass'].update({
        'country': 'Canada', 'state': 'ON', 'city': 'Toronto', 'zip': 'M5V 2T6',
        'credit_currency': 'cad', 'credit_limit': 100000, 'credit_approved': True,
    })
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TaxQBO()
    quickbooks.TRANSPORT = fake
    try:
        start = client.get('/quickbooks/connect?connection=ca')
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
        callback = client.get(
            f'/quickbooks/callback?code=auth-code&realmId=123146408064299&state={state}'
        )
        assert callback.status_code == 302
        quickbooks.save_tokens({
            'access_token': 'us-access', 'refresh_token': 'us-refresh',
            'realm_id': '9341456057091137', 'expires_at': 9_999_999_999, 'company_name': 'Ora Glass Inc',
        })
        saved = client.put('/api/quickbooks/settings', json={
            'connection': 'ca',
            'default_item': SUPPLY_ITEM,
            'province_tax': {'ON': '6', 'BC': '7', 'QC': '8', 'AB': '5'},
            'tax_exempt_code': '9',
        })
        assert saved.status_code == 200, saved.get_json()
        canada = next(row for row in saved.get_json()['connections'] if row['key'] == 'ca')
        assert canada['default_item'] == SUPPLY_ITEM
        assert canada['province_tax']['ON'] == '6'
        names = {row['id']: row['name'] for row in canada['tax_codes']}
        assert names['6'] == 'HST ON'
        assert '10' not in names
        page = client.get('/settings/quickbooks').data
        assert b'Sales tax by ship-to province' in page
        assert b'HST ON' in page
        assert b'GST/PST BC' in page

        codes = client.get('/api/quickbooks/tax-codes')
        assert codes.status_code == 200
        assert codes.get_json()['province_tax']['BC'] == '7'

        suggested = client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'ON', 'qbo_tax_code': '',
        })
        assert suggested.status_code == 200
        listed = client.get('/api/customers').get_json()
        maple = next(row for row in listed if row['id'] == 'maple-glass')
        assert maple['province'] == 'ON'
        assert maple['qbo_tax_code'] == ''
        assert maple['qbo_tax_code_label'] == ''

        kept = client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'BC', 'qbo_tax_code': '6',
        })
        assert kept.status_code == 200
        assert load_customers()['maple-glass']['qbo_tax_code'] == '6'

        invoice = client.post('/api/invoices', json={
            'customer_id': 'maple-glass',
            'lines': [{'description': 'Supply only', 'qty': 1, 'unit_price': 80}],
        })
        assert invoice.status_code == 200, invoice.get_json()
        assert invoice.get_json()['issuer'] == 'ca'
        assert invoice.get_json()['tax_label'] == 'GST + PST, shipped to British Columbia'
        bodies = [json.loads(call[2]) for call in _posts(fake, '/invoice')]
        assert len(bodies) == 1
        detail = bodies[0]['Line'][0]['SalesItemLineDetail']
        assert detail['TaxCodeRef']['value'] == '7'
        assert detail['ItemRef']['name'] == SUPPLY_ITEM
        assert detail['ItemRef']['value'] == '42'
        assert any(SUPPLY_ITEM in query for query in _queries(fake))
        assert not any(call[0] != 'GET' and 'taxcode' in call[1].lower() for call in fake.calls)

        cleared = client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': '', 'state': '', 'zip': '', 'qbo_tax_code': '', 'tax_exempt': False,
        })
        assert cleared.status_code == 200
        assert load_customers()['maple-glass']['qbo_tax_code'] == ''
        missing = client.post('/api/invoices', json={
            'customer_id': 'maple-glass',
            'lines': [{'description': 'No code', 'qty': 1, 'unit_price': 25}],
        })
        assert missing.status_code == 200, missing.get_json()
        assert 'no QuickBooks tax code' in missing.get_json()['qbo_warning']
        assert len(_posts(fake, '/invoice')) == 1
        stored = next(row for row in load_invoices() if row['id'] == missing.get_json()['id'])
        assert stored['qbo_warning']
        assert any('no QuickBooks tax code' in row['message'] for row in quickbooks.load_log())

        exempt = client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'ON', 'tax_exempt': True, 'qbo_tax_code': '',
        })
        assert exempt.status_code == 200
        assert load_customers()['maple-glass']['qbo_tax_code'] == '9'
        exempt_invoice = client.post('/api/invoices', json={
            'customer_id': 'maple-glass',
            'lines': [{'description': 'Exempt supply', 'qty': 1, 'unit_price': 10}],
        })
        assert exempt_invoice.status_code == 200, exempt_invoice.get_json()
        exempt_body = json.loads(_posts(fake, '/invoice')[-1][2])
        assert exempt_body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == '9'

        write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
        customers = load_customers()
        customers['harbor']['country'] = 'United States'
        customers['harbor']['qbo_tax_code'] = 'NON'
        save_customers(customers)
        us_invoice = client.post('/api/invoices', json={
            'customer_id': 'harbor',
            'lines': [{'description': 'US glass', 'qty': 1, 'unit_price': 40}],
        })
        assert us_invoice.status_code == 200, us_invoice.get_json()
        assert us_invoice.get_json()['issuer'] == 'us'
        us_body = json.loads(_posts(fake, '/invoice')[-1][2])
        assert us_body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
        assert '/9341456057091137/' in _posts(fake, '/invoice')[-1][1]

        assert login(client, 'sales@maple.example').status_code == 302
        quote = client.post('/api/quotes', json={
            'items': [_size_item()], 'currency': 'cad', 'project': 'Lobby', 'type': 'size', 'tier': 'T5',
        })
        assert quote.status_code == 200, quote.get_json()
        client.post('/logout')
        assert login(client, 'admin@oraglass.us').status_code == 302
        customers = load_customers()
        customers['maple-glass']['tax_exempt'] = False
        customers['maple-glass']['qbo_tax_code'] = '6'
        customers['maple-glass']['province'] = 'ON'
        save_customers(customers)
        created = client.post(f"/api/quotes/{quote.get_json()['id']}/order", json={'customer_id': 'maple-glass'})
        assert created.status_code == 200, created.get_json()
        order_id = created.get_json()['order_id']
        for status in ('sent', 'made', 'delivered'):
            moved = client.post(f'/api/orders/{order_id}/status', json={'status': status})
            assert moved.status_code == 200, moved.get_json()
        order = client.get(f'/api/orders/{order_id}').get_json()
        intercompany = next(row for row in order['documents'] if row['kind'] == 'intercompany')
        customer_invoice = next(row for row in order['documents'] if row['issuer'] == 'ca')
        posted = {json.loads(call[2])['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value']: call[1] for call in _posts(fake, '/invoice')}
        assert customer_invoice['number'].startswith('CA-INV-')
        intercompany_calls = [call for call in _posts(fake, '/invoice') if '/9341456057091137/' in call[1]]
        canada_calls = [call for call in _posts(fake, '/invoice') if '/123146408064299/' in call[1]]
        assert any(json.loads(call[2])['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON' for call in intercompany_calls)
        assert any(json.loads(call[2])['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == '6' for call in canada_calls)
        assert intercompany['currency'] == 'usd'
        assert 'NON' in posted
    finally:
        quickbooks.TRANSPORT = None


def _pdf_text(data):
    import base64
    import re
    import zlib
    chunks = []
    for match in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.S):
        raw = match.group(1).rstrip(b'\r\n')
        try:
            raw = base64.a85decode(raw, adobe=True)
        except (ValueError, base64.binascii.Error):
            pass
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        chunks.append(raw)
    return b'\n'.join(chunks)


def _last_canada_tax(fake):
    calls = [call for call in _posts(fake, '/invoice') if '/123146408064299/' in call[1]]
    body = json.loads(calls[-1][2])
    return body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value']


def test_ship_to_province_and_registration_numbers_choose_canada_tax(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@maple.example', 'customer', customer_id='maple-glass', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management', tier_override='T5',
        allowed_tiers=['T5'], password='Validpass1',
    )
    customers = load_customers()
    customers['maple-glass'].update({
        'country': 'Canada', 'province': 'BC', 'state': 'BC', 'city': 'Vancouver', 'zip': 'V6B 1A1',
        'credit_currency': 'cad', 'credit_limit': 100000, 'credit_approved': True,
        'pst_number': '', 'qst_number': '', 'tax_code_override': False, 'qbo_tax_code': '',
    })
    customers['harbor'] = {
        'id': 'harbor', 'name': 'Harbor Glass', 'country': 'United States', 'us_state': 'WA',
        'qbo_tax_code': 'NON', 'credit_currency': 'usd',
    }
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TaxQBO()
    quickbooks.TRANSPORT = fake
    try:
        start = client.get('/quickbooks/connect?connection=ca')
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers['Location']).query)['state'][0]
        assert client.get(
            f'/quickbooks/callback?code=auth-code&realmId=123146408064299&state={state}'
        ).status_code == 302
        quickbooks.save_tokens({
            'access_token': 'us-access', 'refresh_token': 'us-refresh',
            'realm_id': '9341456057091137', 'expires_at': 9_999_999_999, 'company_name': 'Ora Glass Inc',
        })
        mapping = {
            'ON': {'hst': '6'},
            'BC': {'with_pst': '7', 'gst_only': '5'},
            'QC': {'with_qst': '8', 'gst_only': '5'},
            'AB': {'gst': '5'},
        }
        saved = client.put('/api/quickbooks/settings', json={
            'connection': 'ca', 'default_item': SUPPLY_ITEM, 'province_tax': mapping, 'tax_exempt_code': '9',
        })
        assert saved.status_code == 200, saved.get_json()
        canada = next(row for row in saved.get_json()['connections'] if row['key'] == 'ca')
        assert canada['province_tax']['BC']['with_pst'] == '7'
        assert canada['province_tax']['BC']['gst_only'] == '5'
        page = client.get('/settings/quickbooks?company=ca').data
        assert b'Sales tax by ship-to province' in page
        assert b'GST only, PST exempt' in page
        assert b'With QST' in page
        customers_page = client.get('/customers').data
        assert b'PST number' in customers_page
        assert b'QST number' in customers_page
        assert b'Override ship-to tax' in customers_page

        def invoice(ship, description):
            posted = client.post('/api/invoices', json={
                'customer_id': 'maple-glass',
                'ship_province': ship,
                'lines': [{'description': description, 'qty': 1, 'unit_price': 40}],
            })
            assert posted.status_code == 200, posted.get_json()
            return posted.get_json()

        ontario = invoice('ON', 'Delivered to Ontario')
        assert ontario['tax_label'] == 'HST, shipped to Ontario'
        assert ontario['qbo_tax_code'] == '6'
        assert _last_canada_tax(fake) == '6'

        british_columbia = invoice('BC', 'Delivered to BC')
        assert british_columbia['tax_label'] == 'GST + PST, shipped to British Columbia'
        assert _last_canada_tax(fake) == '7'

        numbered = client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'BC', 'pst_number': 'PST-123456', 'qbo_tax_code': '',
        })
        assert numbered.status_code == 200
        assert load_customers()['maple-glass']['pst_number'] == 'PST-123456'
        exempt_pst = invoice('BC', 'BC with PST number')
        assert exempt_pst['tax_label'] == 'GST only, shipped to British Columbia (PST number on file)'
        assert _last_canada_tax(fake) == '5'

        client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'QC', 'pst_number': '', 'qst_number': '', 'qbo_tax_code': '',
            'tax_code_override': False,
        })
        quebec = invoice('QC', 'Delivered to Quebec')
        assert quebec['tax_label'] == 'GST + QST, shipped to Quebec'
        assert _last_canada_tax(fake) == '8'
        client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'QC', 'qst_number': 'QST-999', 'qbo_tax_code': '',
        })
        quebec_registered = invoice('QC', 'Quebec with QST number')
        assert quebec_registered['tax_label'] == 'GST only, shipped to Quebec (QST number on file)'
        assert _last_canada_tax(fake) == '5'

        client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'AB', 'qst_number': '', 'pst_number': '', 'qbo_tax_code': '',
        })
        alberta = invoice('AB', 'Delivered to Alberta')
        assert alberta['tax_label'] == 'GST, shipped to Alberta'
        assert _last_canada_tax(fake) == '5'

        client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'AB', 'qbo_tax_code': '6', 'tax_code_override': True,
        })
        overridden = invoice('BC', 'Override ignores ship-to')
        assert overridden['qbo_tax_code'] == '6'
        assert _last_canada_tax(fake) == '6'

        us_invoice = client.post('/api/invoices', json={
            'customer_id': 'harbor',
            'ship_province': 'ON',
            'lines': [{'description': 'US glass', 'qty': 1, 'unit_price': 40}],
        })
        assert us_invoice.status_code == 200, us_invoice.get_json()
        assert us_invoice.get_json()['issuer'] == 'us'
        us_body = json.loads([call for call in _posts(fake, '/invoice') if '/9341456057091137/' in call[1]][-1][2])
        assert us_body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'

        client.put('/api/customers/maple-glass', json={
            'country': 'Canada', 'province': 'BC', 'pst_number': '', 'qst_number': '',
            'qbo_tax_code': '', 'tax_code_override': False,
        })
        client.post('/logout')
        assert login(client, 'sales@maple.example').status_code == 302
        quote = client.post('/api/quotes', json={
            'items': [_size_item()], 'currency': 'cad', 'project': 'Lobby', 'type': 'size', 'tier': 'T5',
        })
        assert quote.status_code == 200, quote.get_json()
        listed = client.get('/api/quotes').get_json()
        saved_quote = next(row for row in listed if row['id'] == quote.get_json()['id'])
        assert saved_quote['tax_label'] == 'GST + PST, shipped to British Columbia'
        pdf = client.post('/api/export-quote', json={
            'items': [_size_item()],
            'currency': 'cad',
            'project': 'Lobby',
            'tax_label': saved_quote['tax_label'],
        })
        assert pdf.status_code == 200
        assert pdf.mimetype == 'application/pdf'
        assert b'GST + PST, shipped to British Columbia' in _pdf_text(pdf.data)
        client.post('/logout')
        assert login(client, 'admin@oraglass.us').status_code == 302
        created = client.post(
            f"/api/quotes/{quote.get_json()['id']}/order",
            json={'customer_id': 'maple-glass', 'ship_province': 'ON'},
        )
        assert created.status_code == 200, created.get_json()
        order_id = created.get_json()['order_id']
        for status in ('sent', 'made', 'delivered'):
            moved = client.post(f'/api/orders/{order_id}/status', json={'status': status})
            assert moved.status_code == 200, moved.get_json()
        order = client.get(f'/api/orders/{order_id}').get_json()
        assert order['ship_province'] == 'ON'
        assert order['tax_label'] == 'HST, shipped to Ontario'
        canada_invoice = next(row for row in order['documents'] if row['issuer'] == 'ca')
        stored = next(row for row in load_invoices() if row['id'] == canada_invoice['id'])
        assert stored['qbo_tax_code'] == '6'
        assert stored['tax_label'] == 'HST, shipped to Ontario'
        assert _last_canada_tax(fake) == '6'
        invoice_pdf = client.get(f"/api/invoices/{stored['id']}/pdf")
        assert invoice_pdf.status_code == 200
        assert b'HST, shipped to Ontario' in _pdf_text(invoice_pdf.data)
    finally:
        quickbooks.TRANSPORT = None

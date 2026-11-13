"""US export, resale certificates, and sales tax. QuickBooks is mocked and nothing is written there."""

import io
import json
import urllib.parse
from datetime import timedelta

import quickbooks
import resale
from storage import data_dir, load_customers, save_customers
from tests.conftest import login, write_user

PDF = b'%PDF-1.4\n%'


def _configure(monkeypatch):
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://ora-glass-erp.onrender.com/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')


class TaxTransport:
    def __init__(self, automated=False):
        self.automated = automated
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        if parsed.path.endswith('/query'):
            statement = urllib.parse.parse_qs(parsed.query).get('query', [''])[0]
            if 'from TaxCode' in statement:
                payload = {'TaxCode': [
                    {'Id': 'NON', 'Name': 'NON', 'Active': True},
                    {'Id': '12', 'Name': 'California', 'Active': True},
                ]}
            elif 'from Preferences' in statement:
                payload = {'Preferences': [{'TaxPrefs': {'PartnerTaxEnabled': self.automated}}]}
            elif 'from Item' in statement:
                payload = {'Item': [{'Id': '10', 'Name': 'Insulated Glass Units'}]}
            elif 'from Term' in statement:
                payload = {'Term': [{'Id': '3', 'Name': 'Net 30'}]}
            elif 'DisplayName' in statement:
                payload = {'Customer': [{'Id': '77', 'DisplayName': 'Ora Glass (Canada) Inc'}]}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        if parsed.path.rstrip('/').endswith('/invoice'):
            return 200, {}, json.dumps({'Invoice': {'Id': '80', 'SyncToken': '0'}}).encode()
        if parsed.path.rstrip('/').endswith('/customer'):
            return 200, {}, json.dumps({'Customer': {'Id': '55', 'SyncToken': '0'}}).encode()
        if 'companyinfo' in parsed.path:
            return 200, {}, json.dumps({'CompanyInfo': {'CompanyName': 'Ora Glass Inc'}}).encode()
        if method != 'GET' and 'taxcode' in parsed.path.lower():
            raise AssertionError('tax codes are read-only')
        return 200, {}, b'{}'


def _connect_us(client, fake):
    quickbooks.TRANSPORT = fake
    quickbooks.save_tokens({
        'access_token': 'us-access', 'refresh_token': 'us-refresh',
        'realm_id': '9341456057091137', 'expires_at': 9_999_999_999, 'company_name': 'Ora Glass Inc',
    })


def _invoice(client, customer_id):
    return client.post('/api/invoices', json={
        'customer_id': customer_id,
        'lines': [{'description': 'Glass', 'qty': 1, 'unit_price': 40}],
    })


def _posted(fake):
    rows = []
    for method, url, body in fake.calls:
        path = urllib.parse.urlparse(url).path.rstrip('/')
        if method == 'POST' and path.endswith('/invoice'):
            rows.append(json.loads(body))
    return rows


def test_open_windows_is_an_export_and_intercompany_is_untaxed(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('support@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    assert load_customers()['open-windows']['export_no_tax'] is True
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TaxTransport()
    try:
        _connect_us(client, fake)
        exported = _invoice(client, 'open-windows')
        assert exported.status_code == 200, exported.get_json()
        assert _posted(fake)[-1]['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
        customers = load_customers()
        customers['ora-glass-canada'] = {
            'id': 'ora-glass-canada', 'name': 'Ora Glass (Canada) Inc', 'books_entity': 'ca',
            'country': 'Canada', 'status': 'active', 'tier': 'T1',
        }
        save_customers(customers)
        intercompany = client.post('/api/invoices', json={
            'customer_id': 'ora-glass-canada',
            'lines': [{'description': 'Intercompany', 'qty': 1, 'unit_price': 10}],
        })
        assert intercompany.status_code == 200, intercompany.get_json()
        assert intercompany.get_json()['issuer'] == 'us'
        assert _posted(fake)[-1]['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
    finally:
        quickbooks.TRANSPORT = None


def test_resale_certificate_upload_review_and_tax(client, monkeypatch, data_dir):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    write_user('sam@other.example', 'customer', customer_id='other', customer_name='Other Glass')
    customers = load_customers()
    customers['harbor'].update({'country': 'United States', 'state': 'CA', 'city': 'Sacramento', 'address': '1 Capitol', 'zip': '95814'})
    save_customers(customers)
    assert load_customers()['harbor']['us_state'] == 'CA'
    assert login(client, 'lee@harbor.example').status_code == 302
    page = client.get('/company')
    assert page.status_code == 200
    assert b'Upload a certificate' in page.data
    denied = client.post('/api/customers/other/resale', data={
        'number': 'NO', 'state': 'CA', 'file': (io.BytesIO(PDF), 'cert.pdf'),
    }, content_type='multipart/form-data')
    assert denied.status_code == 403
    uploaded = client.post('/api/customers/harbor/resale', data={
        'number': 'RC-100', 'state': 'CA', 'expires': (resale.tax_today() + timedelta(days=10)).isoformat(),
        'file': (io.BytesIO(PDF), 'harbor-cert.pdf'),
    }, content_type='multipart/form-data')
    assert uploaded.status_code == 200, uploaded.get_json()
    assert uploaded.get_json()['status'] == 'pending'
    certificate_id = uploaded.get_json()['id']
    stored = load_customers()['harbor']['resale_certificates']
    assert len(stored) == 1
    path = resale.certificate_path('harbor', stored[0]['stored'])
    assert path.startswith(str(data_dir))
    assert 'resale_certificates' in path
    own = client.get(f'/api/customers/harbor/resale/{certificate_id}/file')
    assert own.status_code == 200
    assert own.data.startswith(b'%PDF')
    client.post('/logout')
    assert login(client, 'sam@other.example').status_code == 302
    assert client.get(f'/api/customers/harbor/resale/{certificate_id}/file').status_code == 403
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    review_page = client.get('/customers/harbor/resale')
    assert review_page.status_code == 200
    assert b'Verify' in review_page.data
    assert b'RC-100' in review_page.data
    verified = client.post(f'/api/customers/harbor/resale/{certificate_id}', json={'status': 'verified'})
    assert verified.status_code == 200
    assert verified.get_json()['status'] == 'verified'
    again = client.post('/api/customers/harbor/resale', data={
        'number': 'RC-OLD', 'state': 'CA', 'file': (io.BytesIO(PDF), 'older.pdf'),
    }, content_type='multipart/form-data')
    assert again.status_code == 200
    assert len(load_customers()['harbor']['resale_certificates']) == 2
    fake = TaxTransport()
    try:
        _connect_us(client, fake)
        covered = _invoice(client, 'harbor')
        assert covered.status_code == 200, covered.get_json()
        assert _posted(fake)[-1]['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
        customers = load_customers()
        customers['harbor']['resale_certificates'][0]['expires'] = (resale.tax_today() - timedelta(days=1)).isoformat()
        save_customers(customers)
        dashboard = client.get('/dashboard')
        assert b'resale certificate expired' in dashboard.data
        assert b'now taxable' in dashboard.data
        missing = _invoice(client, 'harbor')
        assert missing.status_code == 200, missing.get_json()
        assert 'taxable and has no QuickBooks tax code' in missing.get_json()['qbo_warning']
        assert len(_posted(fake)) == 1
        customers = load_customers()
        customers['harbor']['qbo_tax_code'] = '12'
        customers['harbor']['resale_certificates'][0]['expires'] = (resale.tax_today() + timedelta(days=20)).isoformat()
        save_customers(customers)
        soon = client.get('/dashboard').data
        assert b'expires in' in soon
        still_untaxed = _invoice(client, 'harbor')
        assert still_untaxed.get_json().get('qbo_warning', '') == ''
        assert _posted(fake)[-1]['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'NON'
    finally:
        quickbooks.TRANSPORT = None


def test_state_suggestion_and_automated_sales_tax(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    customers = load_customers()
    customers['harbor'].update({'country': 'United States', 'us_state': 'CA', 'address': '1 Capitol', 'city': 'Sacramento', 'zip': '95814'})
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = TaxTransport(automated=True)
    try:
        _connect_us(client, fake)
        saved = client.put('/api/quickbooks/settings', json={
            'connection': 'us',
            'tax_code': 'NON',
            'state_tax': {'CA': '12', 'NY': 'NON'},
        })
        assert saved.status_code == 200, saved.get_json()
        us = next(row for row in saved.get_json()['connections'] if row['key'] == 'us')
        assert us['automated_sales_tax'] is True
        assert us['state_tax']['CA'] == '12'
        suggested = client.put('/api/customers/harbor', json={
            'country': 'United States', 'us_state': 'CA', 'qbo_tax_code': '',
        })
        assert suggested.status_code == 200
        assert load_customers()['harbor']['qbo_tax_code'] == '12'
        invoice = _invoice(client, 'harbor')
        assert invoice.status_code == 200, invoice.get_json()
        body = _posted(fake)[-1]
        assert body['Line'][0]['SalesItemLineDetail']['TaxCodeRef']['value'] == 'TAX'
        assert body['ShipAddr']['City'] == 'Sacramento'
        assert body['ShipAddr']['CountrySubDivisionCode'] == 'CA'
        kept = client.put('/api/customers/harbor', json={
            'country': 'United States', 'us_state': 'NY', 'qbo_tax_code': '12',
        })
        assert kept.status_code == 200
        assert load_customers()['harbor']['qbo_tax_code'] == '12'
    finally:
        quickbooks.TRANSPORT = None

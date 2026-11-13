"""Irregular shape is a flat per-piece line, stored with the quote, and sent to QuickBooks."""

import json
import urllib.parse

import quickbooks
from quoting import expand_irregular_lines, reprice_size_line
from storage import load_orders, load_pricing_settings, load_quotes, load_tiers, save_pricing_settings
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text

SPEC = {
    'glass1': 'Clear Glass 4mm',
    'temper1': 'n/a',
    'spacer1': 'n/a',
    'sealant1': 'n/a',
    'glass2': 'n/a',
    'temper2': 'n/a',
    'spacer2': 'n/a',
    'sealant2': 'n/a',
    'glass3': 'n/a',
    'temper3': 'n/a',
    'width': 24,
    'height': 36,
    'unit': 'in',
}


def _line(pcs=2, irregular=True, rate=None, currency='usd'):
    tiers = load_tiers()
    priced = reprice_size_line(SPEC, pcs, 'T2', currency, __import__('app').PRICING, tiers)
    row = {
        'name': 'Clear Glass 4mm - 24 x 36 in',
        'pcs': pcs,
        'spec': SPEC,
        'sqf': priced['sqf'],
        'price_sqf': priced['price_sqf'],
        'price_pc': priced['price_pc'],
        'total': priced['total'],
        'irregular_shape': irregular,
    }
    if rate is not None:
        row['surcharge_rate'] = rate
    return row


def test_surcharge_is_flat_and_saved_quotes_keep_their_rate():
    plain = expand_irregular_lines([_line(pcs=3, irregular=False)], 50)
    flagged = expand_irregular_lines([_line(pcs=3)], 50)
    assert len(plain) == 1
    assert len(flagged) == 2
    surcharge = flagged[1]
    assert surcharge['kind'] == 'irregular_surcharge'
    assert surcharge['name'].startswith('Irregular shape surcharge – ')
    assert surcharge['pcs'] == 3
    assert surcharge['unit_price'] == 50
    assert surcharge['total'] == 150
    assert flagged[0]['total'] == plain[0]['total']
    kept = expand_irregular_lines(flagged, 80)
    assert kept[1]['unit_price'] == 50
    assert kept[1]['total'] == 150
    fresh = expand_irregular_lines([_line(pcs=1)], 80)
    assert fresh[1]['unit_price'] == 80
    cad = expand_irregular_lines([_line(pcs=2, currency='cad')], 50)
    assert cad[1]['total'] == 100
    tiers = load_tiers()
    from app import PRICING
    t2 = reprice_size_line(SPEC, 2, 'T2', 'usd', PRICING, tiers)['total']
    t5 = reprice_size_line(SPEC, 2, 'T5', 'usd', PRICING, tiers)['total']
    assert t5 != t2
    assert expand_irregular_lines([_line()], 50)[1]['total'] == 100


def test_quote_order_pdf_and_import_carry_the_surcharge(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management', password='Validpass1',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['harbor-glass']['credit_limit'] = 100000
    customers['harbor-glass']['credit_currency'] = 'usd'
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert load_pricing_settings()['irregular_shape_surcharge'] == 50
    saved = client.post('/api/quotes', json={
        'items': [_line(pcs=2)],
        'currency': 'usd',
        'project': 'Lobby',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    quote = next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])
    assert quote['items'][1]['total'] == 100
    assert quote['items'][0]['surcharge_rate'] == 50
    pdf = client.post('/api/export-quote', json={'items': [_line(pcs=2)], 'currency': 'usd', 'project': 'Lobby'})
    assert pdf.status_code == 200
    text = pdf_text(pdf.data)
    assert b'Irregular shape surcharge' in text
    save_pricing_settings({'irregular_shape_surcharge': 80})
    assert load_quotes()[0]['items'][1]['unit_price'] == 50
    newer = client.post('/api/quotes', json={
        'items': [_line(pcs=1, irregular=True)],
        'currency': 'cad',
        'project': 'Later',
        'type': 'size',
        'tier': 'T2',
    })
    assert newer.status_code == 200
    later = next(row for row in load_quotes() if row['id'] == newer.get_json()['id'])
    assert later['items'][1]['unit_price'] == 80
    assert later['currency'] == 'cad'
    created = client.post(f"/api/quotes/{quote['id']}/order", json={'customer_id': 'harbor-glass'})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    surcharge = next(row for row in order['items'] if row.get('kind') == 'irregular_surcharge')
    assert surcharge['unit_price'] == 50
    assert surcharge['total'] == 100
    assert order['total'] == round(sum(row['total'] for row in order['items']), 2)


class ItemQBO:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        if parsed.path.endswith('/query'):
            statement = urllib.parse.parse_qs(parsed.query).get('query', [''])[0]
            if 'from Item' in statement:
                name = 'Irregular shape' if 'Irregular' in statement else 'Insulated Glass Units'
                payload = {'Item': [{'Id': '21' if 'Irregular' in statement else '10', 'Name': name}]}
            elif 'from Term' in statement:
                payload = {'Term': [{'Id': '3', 'Name': 'Net 30'}]}
            elif 'from TaxCode' in statement or 'from Preferences' in statement:
                payload = {}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        if parsed.path.rstrip('/').endswith('/invoice'):
            return 200, {}, json.dumps({'Invoice': {'Id': '80', 'SyncToken': '0'}}).encode()
        if parsed.path.rstrip('/').endswith('/customer'):
            return 200, {}, json.dumps({'Customer': {'Id': '55', 'SyncToken': '0'}}).encode()
        return 200, {}, b'{}'


def test_quickbooks_uses_the_configured_surcharge_item(client, monkeypatch):
    monkeypatch.setenv('QBO_CLIENT_ID', 'client-id')
    monkeypatch.setenv('QBO_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('QBO_REDIRECT_URI', 'https://ora-glass-erp.onrender.com/quickbooks/callback')
    monkeypatch.setenv('QBO_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('QBO_WEBHOOK_VERIFIER', 'verifier-token')
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['harbor']['country'] = 'United States'
    customers['harbor']['qbo_tax_code'] = 'NON'
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = ItemQBO()
    quickbooks.TRANSPORT = fake
    try:
        quickbooks.save_tokens({
            'access_token': 'us', 'refresh_token': 'us', 'realm_id': '9341456057091137',
            'expires_at': 9_999_999_999, 'company_name': 'Ora Glass Inc',
        })
        with quickbooks.connection_scope('ca'):
            quickbooks.save_tokens({
                'access_token': 'ca', 'refresh_token': 'ca', 'realm_id': '123146408064299',
                'expires_at': 9_999_999_999, 'company_name': 'Ora Glass (Canada) Inc',
            })
            settings = quickbooks.load_settings()
            settings['irregular_item'] = 'Irregular shape CA'
            quickbooks.save_settings(settings)
        settings = quickbooks.load_settings()
        settings['irregular_item'] = 'Irregular shape US'
        quickbooks.save_settings(settings)
        from storage import append_invoice
        stored = append_invoice({
            'customer_id': 'harbor',
            'issuer': 'us',
            'currency': 'usd',
            'doc_type': 'invoice',
            'items': expand_irregular_lines([_line(pcs=2)], 50),
            'total': 100,
        })
        quickbooks.push_document(stored)
        body = json.loads(next(
            call[2] for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
            and '/9341456057091137/' in call[1]
        ))
        names = [line['SalesItemLineDetail']['ItemRef']['name'] for line in body['Line']]
        assert 'Irregular shape US' in names
        assert 'Insulated Glass Units' in names
        customers['maple'] = {
            'id': 'maple', 'name': 'Maple Glass', 'country': 'Canada', 'province': 'ON',
            'qbo_tax_code': '6', 'tier': 'T2', 'status': 'active',
        }
        save_customers(customers)
        canada = append_invoice({
            'customer_id': 'maple',
            'issuer': 'ca',
            'currency': 'cad',
            'doc_type': 'invoice',
            'items': expand_irregular_lines([_line(pcs=1, currency='cad')], 50),
            'total': 50,
        })
        quickbooks.push_document(canada)
        canada_body = json.loads(next(
            call[2] for call in fake.calls
            if call[0] == 'POST' and urllib.parse.urlparse(call[1]).path.rstrip('/').endswith('/invoice')
            and '/123146408064299/' in call[1]
        ))
        canada_names = [line['SalesItemLineDetail']['ItemRef']['name'] for line in canada_body['Line']]
        assert 'Irregular shape CA' in canada_names
        assert any('Irregular shape US' in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('query', [''])[0]
                   for _method, url, _body in fake.calls if 'query' in url)
    finally:
        quickbooks.TRANSPORT = None

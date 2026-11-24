"""Irregular shape is a flat per-piece line, stored with the quote, and sent to QuickBooks."""

import io
import json
import os
import urllib.parse

import quickbooks
from quoting import DRAWING_NOTE, expand_irregular_lines, reprice_size_line
from storage import (
    data_dir, load_invoices, load_orders, load_pricing_settings, load_quotes, load_tiers, save_pricing_settings,
)
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
    assert b'A CAD drawing \\(DWG\\) will be required for irregular shapes when the order is placed.' in text
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
    line_id = quote['items'][0]['line_id']
    created = client.post(
        f"/api/quotes/{quote['id']}/order",
        data={'customer_id': 'harbor-glass', f'drawing_{line_id}': (io.BytesIO(b'AC1015'), 'lobby.dwg')},
        content_type='multipart/form-data',
    )
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    surcharge = next(row for row in order['items'] if row.get('kind') == 'irregular_surcharge')
    assert surcharge['unit_price'] == 50
    assert surcharge['total'] == 100
    assert not any(row.get('kind') == 'dwg_creation' for row in order['items'])
    assert order['drawings'][0]['is_dwg'] is True
    assert order['total'] == round(sum(row['total'] for row in order['items']), 2)


def _order_with_drawing(client, filename, payload, pcs=2, currency='usd'):
    saved = client.post('/api/quotes', json={
        'items': [_line(pcs=pcs, currency=currency)],
        'currency': currency,
        'project': 'Lobby',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    quote = next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])
    line_id = quote['items'][0]['line_id']
    created = client.post(
        f"/api/quotes/{quote['id']}/order",
        data={'customer_id': 'harbor-glass', f'drawing_{line_id}': (io.BytesIO(payload), filename)},
        content_type='multipart/form-data',
    )
    return quote, created


def test_drawings_are_required_and_non_dwg_files_add_a_flat_charge(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management', password='Validpass1',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management', password='Validpass1',
    )
    from storage import load_customers, save_customers
    customers = load_customers()
    customers['harbor-glass']['credit_limit'] = 100000
    customers['harbor-glass']['credit_currency'] = 'usd'
    customers['harbor-glass']['country'] = 'United States'
    customers['harbor-glass']['qbo_tax_code'] = 'NON'
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert DRAWING_NOTE.encode() in page.data
    history = client.get('/quote-history')
    assert DRAWING_NOTE.encode() in history.data
    plain = client.post('/api/export-quote', json={'items': [_line(irregular=False)], 'currency': 'usd', 'project': 'Plain'})
    assert b'will be required for irregular shapes' not in pdf_text(plain.data)

    blocked_quote, blocked = _order_with_drawing(client, 'missing.pdf', b'')
    refused = client.post(f"/api/quotes/{blocked_quote['id']}/order", json={'customer_id': 'harbor-glass'})
    assert refused.status_code == 400
    assert refused.get_json()['needs_drawings'] is True
    assert refused.get_json()['dwg_creation_fee'] == 50
    assert load_orders() == []

    empty = client.post(
        f"/api/quotes/{blocked_quote['id']}/order",
        data={'customer_id': 'harbor-glass', f"drawing_{blocked_quote['items'][0]['line_id']}": (io.BytesIO(b''), 'empty.pdf')},
        content_type='multipart/form-data',
    )
    assert empty.status_code == 400
    assert load_orders() == []

    quote, created = _order_with_drawing(client, 'sketch.pdf', b'%PDF-1.4 sketch')
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    charge = next(row for row in order['items'] if row.get('kind') == 'dwg_creation')
    assert charge['name'] == 'DWG drawing creation'
    assert charge['unit_price'] == 50
    assert charge['total'] == 50
    assert charge['drawing_for'] == quote['items'][0]['line_id']
    assert order['dwg_rate'] == 50
    assert order['total'] == round(sum(row['total'] for row in order['items']), 2)
    stored = next(row for row in order['drawings'] if row['line_id'] == quote['items'][0]['line_id'])
    folder = os.path.join(data_dir(), 'order_drawings', str(order['id']), str(stored['line_id']))
    assert os.path.isfile(os.path.join(folder, stored['stored']))
    downloaded = client.get(f"/api/orders/{order['id']}/drawings/{stored['line_id']}")
    assert downloaded.status_code == 200
    assert downloaded.data == b'%PDF-1.4 sketch'

    save_pricing_settings({'dwg_creation_fee': 90})
    edited = client.put(f"/api/orders/{order['id']}", json={
        'items': [{
            'name': order['items'][0]['name'],
            'pcs': order['items'][0]['pcs'],
            'spec': order['items'][0]['spec'],
            'irregular_shape': True,
            'surcharge_rate': 50,
        }],
        'price_choice': 'technical',
        'customer_note': 'The drawing charge stays at the rate from when the order was placed.',
    })
    assert edited.status_code == 200, edited.get_json()
    kept = [row for row in load_orders() if row['id'] == order['id']][0]
    assert next(row['total'] for row in kept['items'] if row.get('kind') == 'dwg_creation') == 50

    replaced = client.post(
        f"/api/orders/{order['id']}/drawings/{stored['line_id']}",
        data={'drawing': (io.BytesIO(b'AC1015'), 'final.dwg')},
        content_type='multipart/form-data',
    )
    assert replaced.status_code == 200, replaced.get_json()
    cleared = next(row for row in load_orders() if row['id'] == order['id'])
    assert cleared['drawings'][0]['is_dwg'] is True
    assert not any(row.get('kind') == 'dwg_creation' for row in cleared['items'])
    brought_back = client.post(
        f"/api/orders/{order['id']}/drawings/{stored['line_id']}",
        data={'drawing': (io.BytesIO(b'not a dwg'), 'again.pdf')},
        content_type='multipart/form-data',
    )
    assert brought_back.status_code == 200
    restored = next(row for row in load_orders() if row['id'] == order['id'])
    assert next(row['unit_price'] for row in restored['items'] if row.get('kind') == 'dwg_creation') == 50

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    decided = client.post(f"/api/orders/{order['id']}/customer-decision", json={'action': 'approve'})
    assert decided.status_code == 200, decided.get_json()
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    released = client.post(f"/api/orders/{order['id']}/approve-production", json={})
    assert released.status_code == 200, released.get_json()

    for status in ('sent', 'made', 'delivered'):
        moved = client.post(f"/api/orders/{order['id']}/status", json={'status': status})
        assert moved.status_code == 200, moved.get_json()
    invoice = load_invoices()[0]
    assert any(row.get('name') == 'DWG drawing creation' and row.get('total') == 50 for row in invoice['items'])
    pdf = client.get(f"/api/invoices/{invoice['id']}/pdf")
    assert pdf.status_code == 200
    assert b'DWG drawing creation' in pdf_text(pdf.data)

    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    hidden = client.get(f"/api/orders/{order['id']}/drawings/{stored['line_id']}")
    assert hidden.status_code in {403, 404}


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
            settings['dwg_item'] = 'DWG service CA'
            quickbooks.save_settings(settings)
        settings = quickbooks.load_settings()
        settings['irregular_item'] = 'Irregular shape US'
        settings['dwg_item'] = 'DWG service US'
        quickbooks.save_settings(settings)
        from storage import append_invoice
        stored = append_invoice({
            'customer_id': 'harbor',
            'issuer': 'us',
            'currency': 'usd',
            'doc_type': 'invoice',
            'items': expand_irregular_lines([_line(pcs=2)], 50) + [{
                'kind': 'dwg_creation', 'name': 'DWG drawing creation', 'description': 'DWG drawing creation',
                'pcs': 1, 'qty': 1, 'unit_price': 50, 'price_pc': 50, 'total': 50,
            }],
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
        assert 'DWG service US' in names
        assert 'Insulated Glass Units' in names
        customers['maple'] = {
            'id': 'maple', 'name': 'Maple Glass', 'country': 'Canada', 'province': 'ON',
            'qbo_tax_code': '6', 'tax_code_override': True, 'tier': 'T2', 'status': 'active',
        }
        save_customers(customers)
        canada = append_invoice({
            'customer_id': 'maple',
            'issuer': 'ca',
            'currency': 'cad',
            'doc_type': 'invoice',
            'items': expand_irregular_lines([_line(pcs=1, currency='cad')], 50) + [{
                'kind': 'dwg_creation', 'name': 'DWG drawing creation', 'description': 'DWG drawing creation',
                'pcs': 1, 'qty': 1, 'unit_price': 50, 'price_pc': 50, 'total': 50,
            }],
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
        assert 'DWG service CA' in canada_names
        assert any('Irregular shape US' in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('query', [''])[0]
                   for _method, url, _body in fake.calls if 'query' in url)
    finally:
        quickbooks.TRANSPORT = None


def test_drawing_uploads_name_the_quote_line_they_belong_to(client):
    """Same IGU, different size: each drawing is labeled and stored on that line."""
    from documents import build_label
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    wide = dict(SPEC)
    wide['width'] = 35
    wide['height'] = 48
    other = dict(SPEC)
    other['width'] = 40
    other['height'] = 60

    def item(spec, pcs, floor, irregular):
        priced = reprice_size_line(spec, pcs, 'T2', 'usd', __import__('app').PRICING, load_tiers())
        return {
            'name': 'Clear Glass 4mm', 'pcs': pcs, 'spec': spec, 'floor': floor,
            'irregular_shape': irregular, 'sqf': priced['sqf'], 'price_sqf': priced['price_sqf'],
            'price_pc': priced['price_pc'], 'total': priced['total'],
        }

    saved = client.post('/api/quotes', json={
        'items': [
            item(SPEC, 1, 'W0', False),
            item(wide, 2, '1', True),
            item(other, 1, '2', True),
        ],
        'currency': 'usd', 'project': 'Drawings', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    quote_id = saved.get_json()['id']
    viewed = client.get(f'/api/quotes/{quote_id}')
    assert viewed.status_code == 200, viewed.get_json()
    glass = [row for row in viewed.get_json()['items'] if row.get('kind') not in {'irregular_surcharge', 'dwg_creation'}]
    assert [row['line_number'] for row in glass] == [1, 2, 3]
    label = build_label(wide)
    thickness, makeup = label.split(' - ', 1)
    assert glass[0].get('drawing_title') in (None, '')
    assert glass[1]['drawing_title'] == f'Line 2 · {thickness} · 35 x 48 · Qty 2 · Floor 1 · Irregular'
    assert glass[1]['drawing_spec'] == makeup
    assert glass[2]['drawing_title'] == f'Line 3 · {thickness} · 40 x 60 · Qty 1 · Floor 2 · Irregular'
    assert glass[2]['drawing_spec'] == makeup
    history = client.get('/quote-history')
    assert b'line.drawing_title' in history.data
    assert b'line.drawing_spec' in history.data
    assert b"Drawing for ' +" not in history.data
    second, third = glass[1]['line_id'], glass[2]['line_id']
    created = client.post(
        f'/api/quotes/{quote_id}/order',
        data={
            'customer_id': 'harbor-glass',
            f'drawing_{second}': (io.BytesIO(b'AC1015 wide'), 'wide.dwg'),
            f'drawing_{third}': (io.BytesIO(b'AC1015 other'), 'other.dwg'),
        },
        content_type='multipart/form-data',
    )
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    stored = {row['line_id']: row for row in order['drawings']}
    assert stored[second]['filename'] == 'wide.dwg'
    assert stored[second]['line_number'] == 2
    assert stored[third]['filename'] == 'other.dwg'
    assert stored[third]['line_number'] == 3
    assert os.path.isfile(os.path.join(data_dir(), 'order_drawings', str(order['id']), str(second), stored[second]['stored']))
    assert os.path.isfile(os.path.join(data_dir(), 'order_drawings', str(order['id']), str(third), stored[third]['stored']))
    shown = client.get(f"/api/orders/{order['id']}")
    assert shown.status_code == 200, shown.get_json()
    public = {row['line_id']: row for row in shown.get_json()['drawings']}
    assert public[second]['line_number'] == 2
    assert public[third]['line_number'] == 3
    assert b'drawing.line_number' in client.get('/orders').data

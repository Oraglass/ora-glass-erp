import base64
import re
import zlib

from tests.conftest import login, write_user


def pdf_text(data):
    chunks = []
    for match in re.finditer(rb'stream\r?\n(.*?)endstream', data, re.S):
        body = match.group(1).strip()
        if body.endswith(b'~>'):
            body = body[:-2]
        try:
            decoded = base64.a85decode(body, adobe=False)
        except Exception:
            continue
        try:
            decoded = zlib.decompress(decoded)
        except zlib.error:
            pass
        chunks.append(decoded)
    return b'\n'.join(chunks)


def export(client, items, currency, project='Job'):
    response = client.post('/api/export-quote', json={
        'items': items,
        'currency': currency,
        'project': project,
    })
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF')
    return pdf_text(response.data)


def test_size_pdf_total_matches_line_totals_in_both_currencies(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    lines = [
        {'name': 'A', 'sqf': 77.86, 'price_sqf': 27.27, 'price_pc': 2123.39, 'total': 2123.39, 'pcs': 1},
        {'name': 'B', 'sqf': 10, 'price_sqf': 5, 'price_pc': 25, 'total': 50, 'pcs': 2},
    ]
    # 2123.39 + 50 = 2173.39. The old CAD total multiplied that sum by 1.40.
    wrong = '3042.75'
    for currency in ('cad', 'usd'):
        text = export(client, lines, currency)
        assert b'($2123.39)' in text
        assert b'($50.00)' in text
        assert b'($2173.39)' in text
        assert wrong.encode() not in text
        assert b'TOTAL \\(3 pcs\\)' in text


def test_sqf_pdf_total_matches_line_prices_in_both_currencies(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    lines = [
        {'name': 'Clear', 'price': '27.27'},
        {'name': 'Bronze', 'price': '10.00'},
    ]
    for currency in ('cad', 'usd'):
        text = export(client, lines, currency)
        assert b'($27.27)' in text
        assert b'($10.00)' in text
        assert b'($37.27)' in text
        assert b'52.18' not in text


def test_quote_history_reexport_uses_stored_line_totals(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    items = [{
        'name': 'Unit - 2692 x 2687 mm',
        'sqf': 77.86,
        'price_sqf': 27.27,
        'price_pc': 2123.39,
        'total': 2123.39,
        'pcs': 1,
    }]
    saved = client.post('/api/quotes', json={
        'items': items,
        'currency': 'cad',
        'project': 'Nistook',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200
    stored = next(quote for quote in client.get('/api/quotes').get_json() if quote['project'] == 'Nistook')
    text = export(client, stored['items'], stored['currency'], stored['project'])
    assert b'($2123.39)' in text
    assert b'2972.75' not in text
    assert b'2692 x 2687 mm' in text


def test_quote_item_tables_keep_size_and_price_on_one_line(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    page = client.get('/quotes').data
    assert page.count(b'>Qty</th>') == 2
    assert page.count(b'class="col-size"') >= 2
    assert b'white-space:nowrap' in page
    assert b"const size=w+' x '+h+' '+unit;" in page
    assert b'quote-remove' in page

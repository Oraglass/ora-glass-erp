"""Floor is the line's install location, reused everywhere a mark used to be."""

import io

from openpyxl import load_workbook
import pdfplumber

import packing
import production
from order_import import parse_table_rows
from app import _render_invoice_pdf
from storage import load_orders, load_quotes
from tests.conftest import login, write_user


def _pdf_text(payload):
    with pdfplumber.open(io.BytesIO(payload)) as document:
        return '\n'.join(page.extract_text() or '' for page in document.pages)


def test_floor_column_is_read_from_floor_mark_or_location_headers():
    parsed = parse_table_rows(
        [
            ['Location', 'Width', 'Height', 'Qty', 'Glass'],
            ['D19', '24', '36', '1', 'Clear'],
        ],
        {
            'header_row': 1,
            'unit': 'in',
            'columns': {'width': 'Width', 'height': 'Height', 'qty': 'Qty', 'glass': 'Glass'},
        },
    )
    assert parsed['lines'][0]['mark'] == 'D19'


def test_floor_is_saved_copied_shown_and_exported(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'sales@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'>Floor<' in page.data
    assert b'>Floor<' in client.get('/orders').data
    assert b'Floor column' in page.data
    quote = client.post('/api/quotes', json={
        'items': [{
            'name': 'Clear lite',
            'sqf': 5,
            'price_sqf': 10,
            'price_pc': 50,
            'total': 100,
            'pcs': 2,
            'floor': 'W12',
            'spec': {
                'glass1': 'Clear Glass 6mm', 'temper1': 'n/a',
                'width': 24, 'height': 30, 'unit': 'in',
            },
        }],
        'currency': 'usd', 'project': 'Floor', 'type': 'size', 'tier': 'T5',
    })
    assert quote.status_code == 200, quote.get_json()
    quote_id = quote.get_json()['id']
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    assert stored['items'][0]['floor'] == 'W12'
    copied = client.post(f'/api/quotes/{quote_id}/copy', json={})
    fresh = next(row for row in load_quotes() if row['id'] == copied.get_json()['id'])
    assert fresh['items'][0]['floor'] == 'W12'
    pdf = client.post('/api/export-quote', json={
        'items': stored['items'], 'currency': 'usd', 'project': 'Floor', 'type': 'size',
    })
    assert pdf.status_code == 200
    assert 'W12' in _pdf_text(pdf.data)
    assert 'FLOOR' in _pdf_text(pdf.data)
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass'})
    order_id = created.get_json()['order_id']
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['items'][0]['floor'] == 'W12'
    edited = client.put(f'/api/orders/{order_id}', json={
        'items': [{
            'name': order['items'][0]['name'],
            'pcs': 2,
            'floor': 'W52 / 2',
            'spec': order['items'][0]['spec'],
        }],
        'price_choice': 'apply',
        'customer_note': 'Floor is now W52 / 2.',
    })
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()['status'] == 'customer_review'
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 409
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['items'][0]['floor'] == 'W52 / 2'
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
    )
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.post(f'/api/orders/{order_id}/customer-decision', json={'action': 'approve'}).status_code == 200
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post(f'/api/orders/{order_id}/approve-production', json={}).status_code == 200
    sheet = load_workbook(io.BytesIO(client.get(f'/api/orders/{order_id}/production.xlsx').data)).active
    values = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))]
    assert values[1] == 'W52 / 2'
    invoice = _render_invoice_pdf({
        'number': 'I-1', 'currency': 'usd', 'company': 'Harbor Glass',
        'items': [{'name': 'Clear lite', 'pcs': 2, 'total': 100, 'floor': 'W52 / 2'}],
    })
    invoice.direct_passthrough = False
    assert 'Floor: W52 / 2' in _pdf_text(invoice.get_data())
    exported = production.line_export(order['items'][0])
    review = packing.match_packing(order, {'rows': [{
        'qty': 1, 'floor': 'W52 / 2', 'description': exported['Product Name'],
        'width_mm': exported['Width(mm)'], 'height_mm': exported['Height(mm)'],
    }], 'warnings': []})
    assert review['lines'] == [{'index': 0, 'qty': 1}]
    assert production.floor_code({'location': 'D19'}) == 'D19'

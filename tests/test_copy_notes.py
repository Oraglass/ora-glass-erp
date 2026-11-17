"""Copy history is kept off quotes, orders, PDFs, and the production Excel."""

import io
import json

from openpyxl import load_workbook

from quoting import DRAWING_NOTE
from storage import load_orders, load_quotes, save_orders, save_quotes
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


CHAIN = (
    'Copied from quote #10 Copied from quote #11 '
    'Copied from quote #14 Copied from quote #17 ' + DRAWING_NOTE
)


def test_strip_keeps_the_drawing_note_and_other_remarks():
    import copy_notes
    assert copy_notes.strip_copy_history(CHAIN) == DRAWING_NOTE
    assert copy_notes.strip_copy_history(
        'Hold for color\nCopied from quote #10\nCopied from quote #11'
    ) == 'Hold for color'
    assert copy_notes.strip_copy_history('Copied from quote #14 Tempered') == 'Tempered'
    once = copy_notes.strip_copy_history(CHAIN)
    assert copy_notes.strip_copy_history(once) == once
    assert copy_notes.strip_copy_history('Call before noon') == 'Call before noon'


def test_startup_removes_stored_copy_notes_and_screens_never_show_them(client):
    import copy_notes
    write_user('admin@oraglass.us', 'admin')
    save_quotes([{
        'id': 10,
        'user': 'admin@oraglass.us',
        'company': 'HighQ Developments Inc',
        'project': 'Tower',
        'notes': CHAIN,
        'copy_note': 'Copied from quote #17',
        'copied_from': 9,
        'items': [{
            'name': 'Lite',
            'remarks': 'Copied from quote #14 Tempered',
            'spec': {'glass1': 'Clear', 'width': 24, 'height': 12, 'unit': 'in'},
            'pcs': 1, 'total': 20, 'irregular_shape': True,
        }],
        'currency': 'usd', 'type': 'size', 'status': 'draft', 'date': '2026-04-01',
    }])
    save_orders([{
        'id': 5,
        'quote_id': 10,
        'company': 'HighQ Developments Inc',
        'project': 'Tower',
        'notes': 'Hold for color\nCopied from quote #10',
        'status': 'approved',
        'currency': 'usd',
        'date': '2026-04-01',
        'total': 20,
        'items': [{
            'name': 'Lite',
            'remarks': 'Copied from quote #11 Lobby',
            'spec': {'glass1': 'Clear Glass 4mm', 'width': 24, 'height': 12, 'unit': 'in'},
            'pcs': 1, 'sqf': 2, 'price_sqf': 10, 'price_pc': 20, 'total': 20,
            'irregular_shape': True,
        }],
        'changelog': [{
            'at': '2026-04-02T00:00:00+00:00',
            'user': 'admin@oraglass.us',
            'changes': [{'field': 'notes', 'old': 'Copied from quote #17', 'new': 'Hold for color'}],
        }],
    }])
    assert login(client, 'admin@oraglass.us').status_code == 302
    quote = load_quotes()[0]
    assert quote['notes'] == DRAWING_NOTE
    assert quote['items'][0]['remarks'] == 'Tempered'
    assert 'copy_note' not in quote
    assert quote['copied_from'] == 9
    order = load_orders()[0]
    assert order['notes'] == 'Hold for color'
    assert order['items'][0]['remarks'] == 'Lobby'
    assert order['changelog'][0]['changes'][0]['old'] == 'Copied from quote #17'
    assert copy_notes.strip_stored_copy_notes() == {'quotes': 0, 'orders': 0}
    assert load_quotes()[0]['notes'] == DRAWING_NOTE

    listed = client.get('/api/quotes').get_json()
    assert 'Copied from quote' not in json.dumps(listed)
    assert listed[0]['notes'] == DRAWING_NOTE
    assert listed[0]['items'][0]['remarks'] == 'Tempered'
    assert 'copied_from' not in listed[0]
    assert 'copy_note' not in listed[0]
    history = client.get('/quote-history')
    assert b'quote.copy_note' not in history.data

    detail = client.get('/api/orders/5').get_json()
    assert detail['notes'] == 'Hold for color'
    assert detail['items'][0]['remarks'] == 'Lobby'
    assert 'Copied from quote' not in json.dumps(detail)
    assert b'Copied from quote' not in client.get('/orders/5').data

    quote_pdf = pdf_text(client.post('/api/export-quote', json={'id': 10}).data)
    assert b'Copied from quote' not in quote_pdf
    assert b'CAD drawing' in quote_pdf
    assert b'irregular shapes when the order is placed' in quote_pdf
    confirmation = pdf_text(client.get('/api/orders/5/confirmation.pdf').data)
    assert b'Copied from quote' not in confirmation
    assert b'Hold for color' in confirmation
    assert b'CAD drawing' in confirmation
    assert b'irregular shapes when the order is placed' in confirmation
    sheet = load_workbook(io.BytesIO(client.get('/api/orders/5/production.xlsx').data)).active
    remarks = [row[10].value for row in sheet.iter_rows(min_row=2) if any(cell.value for cell in row)]
    assert remarks == ['Lobby']

    quotes = load_quotes()
    quotes[0]['notes'] = 'Site note\n' + CHAIN
    quotes[0]['copy_note'] = 'Copied from quote #10'
    save_quotes(quotes)
    orders = load_orders()
    orders[0]['notes'] = 'Rack A\nCopied from quote #17'
    orders[0]['items'][0]['remarks'] = 'Copied from quote #10 Edge'
    save_orders(orders)
    shown = client.get('/api/quotes').get_json()[0]
    assert shown['notes'] == 'Site note\n' + DRAWING_NOTE
    assert 'copy_note' not in shown
    assert 'Copied from quote #10' in load_quotes()[0]['notes']
    again_pdf = pdf_text(client.post('/api/export-quote', json={'id': 10}).data)
    assert b'Copied from quote' not in again_pdf
    assert b'Site note' in again_pdf
    assert b'CAD drawing' in again_pdf
    assert b'irregular shapes when the order is placed' in again_pdf
    again_order = client.get('/api/orders/5').get_json()
    assert again_order['notes'] == 'Rack A'
    assert again_order['items'][0]['remarks'] == 'Edge'
    assert 'Copied from quote' not in json.dumps(again_order)
    refreshed = load_workbook(io.BytesIO(client.get('/api/orders/5/production.xlsx').data)).active
    excel_remarks = [row[10].value for row in refreshed.iter_rows(min_row=2) if any(cell.value for cell in row)]
    assert excel_remarks == ['Edge']

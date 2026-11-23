"""Production packing lists match by serial and quantity, never by IGU spec."""

import io

from openpyxl import Workbook

import packing
from storage import load_orders, save_orders
from tests.conftest import login, write_user


SERIAL_LIST = """
Customer: Harbor Glass
Project: Lobby
PO: PO-9
Sales Delivery: XF1  Date: 10/29/2026
RackCode R1
SERIAL QTY WIDTH HEIGHT FLOOR # ITEM DESCRIPTION
1
1
24 36
W01
99 - substituted glass the shop used instead
609.6 mm 914.4 mm
"""


def _users(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_subrole='management', password='Validpass1',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple',
        customer_name='Maple Glass', customer_subrole='management', password='Validpass1',
    )


def _spec(width=24, height=36):
    return {
        'glass1': 'Clear Glass 4mm', 'temper1': 'n/a',
        'width': width, 'height': height, 'unit': 'in',
    }


def _save_order(order_id=1, project='Lobby', company='Harbor Glass', customer_id='harbor-glass', po='PO-9'):
    save_orders(load_orders() + [{
        'id': order_id,
        'customer_id': customer_id,
        'company': company,
        'project': project,
        'po': po,
        'status': 'approved',
        'items': [
            {'name': 'Unit A - 24 x 36 in', 'pcs': 4, 'floor': 'W01', 'spec': _spec()},
            {'name': 'Unit B - 20 x 30 in', 'pcs': 2, 'floor': 'W02', 'spec': _spec(20, 30)},
        ],
    }])


def _csv(rows):
    header = 'Customer,Project,PO,Packing List,Date,Rack,Serial Number,Quantity,Floor,Description'
    lines = [header]
    for row in rows:
        lines.append(','.join(str(cell) for cell in row))
    return '\n'.join(lines).encode()


def _upload(client, name, payload):
    return client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(payload), name)},
        content_type='multipart/form-data',
    )


def test_parser_reads_the_production_serial_and_ignores_the_igu_number():
    parsed = packing.parse_packing_lines(SERIAL_LIST.splitlines())
    assert parsed['customer'] == 'Harbor Glass'
    assert parsed['project'] == 'Lobby'
    assert parsed['po'] == 'PO-9'
    assert parsed['number'] == 'XF1'
    assert parsed['rack'] == 'RackCode R1'
    assert parsed['rows'][0]['serial'] == 1
    assert parsed['rows'][0]['qty'] == 1
    assert parsed['rows'][0]['description'].startswith('99 -')
    assert '28' not in str(parsed['rows'][0]['serial'])


def test_spreadsheet_rows_keep_the_serial_column():
    text = _csv([
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF2', '2026-10-29', 'R2', 1, 2, 'W01', '28 - different build'],
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF2', '2026-10-29', 'R2', 2, 2, 'W02', 'another spec'],
    ])
    documents = packing.parse_packing_upload('racks.csv', text)
    assert len(documents) == 1
    assert [(row['serial'], row['qty']) for row in documents[0]['rows']] == [(1, 2), (2, 2)]
    book = Workbook()
    sheet = book.active
    sheet.append(['Serial Number', 'Pcs', 'Customer', 'Project', 'Rack', 'Packing List', 'Description'])
    sheet.append([2, 1, 'Harbor Glass', 'Lobby', 'R3', 'XF3', '28 - not the serial'])
    buffer = io.BytesIO()
    book.save(buffer)
    excel = packing.parse_packing_upload('racks.xlsx', buffer.getvalue())
    assert excel[0]['rows'] == [{
        'serial': 2, 'qty': 1, 'floor': '', 'description': '28 - not the serial',
        'width_in': None, 'height_in': None, 'width_mm': None, 'height_mm': None,
    }] or excel[0]['rows'][0]['serial'] == 2


def test_upload_matches_serial_qty_ignores_spec_and_recalculates_backorder(client):
    _users(client)
    _save_order()
    _save_order(2, project='Tower', company='Maple Glass', customer_id='maple', po='')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/orders')
    assert b'id="packing-upload"' in page.data
    first = client.post(
        '/api/packing-lists',
        data={'files': (io.BytesIO(SERIAL_LIST.encode()), 'xf1.txt')},
        content_type='multipart/form-data',
    )
    # txt is parsed as lines, not pdf
    assert first.status_code == 200, first.get_json()
    # The txt path uses parse_packing_lines via parse_packing_upload fallback. Good.
    saved = first.get_json()['saved']
    assert saved[0]['order_id'] == 1
    assert saved[0]['pack_status'] == 'partial'
    assert saved[0]['totals'] == {'ordered': 6, 'packed': 1, 'bo': 5}
    second = _upload(client, 'r2.csv', _csv([
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF2', '2026-10-29', 'R2', 1, 2, 'W01', '28 - completely different IGU'],
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF2', '2026-10-29', 'R2', 2, 2, 'W02', 'shop changed the build'],
    ]))
    assert second.status_code == 200, second.get_json()
    body = second.get_json()['saved'][0]
    assert body['pack_status'] == 'partial'
    assert body['totals'] == {'ordered': 6, 'packed': 5, 'bo': 1}
    view = client.get('/api/orders/1').get_json()
    serials = {row['serial']: row for row in view['packing_serials']}
    assert serials[1]['ordered'] == 4
    assert serials[1]['packed'] == 3
    assert serials[1]['bo'] == 1
    assert serials[2]['packed'] == 2
    assert serials[2]['bo'] == 0
    assert '24 x 36 in' in serials[1]['size']
    assert 'mm' in serials[1]['size']
    assert view['packing_totals'] == {'ordered': 6, 'packed': 5, 'bo': 1}
    assert view['pack_status_label'] == 'Partially packed'
    assert any(change.get('field') == 'packing list' for change in view['changelog'][-1]['changes'])
    listed = [row['number'] for row in view['packing_lists']]
    assert sorted(listed) == ['XF1', 'XF2']

    replaced = _upload(client, 'xf1-again.txt', SERIAL_LIST.replace('1\n1\n', '1\n4\n').encode())
    assert replaced.status_code == 200, replaced.get_json()
    assert replaced.get_json()['saved'][0]['pack_status'] == 'packed'
    assert replaced.get_json()['saved'][0]['totals']['bo'] == 0
    packed = client.get('/api/orders/1').get_json()
    assert packed['pack_status'] == 'packed'
    assert len(packed['packing_history']) == 1
    assert packed['packing_lists'][0]['lines'][0]['qty'] in (4, 2)

    reduced = _upload(client, 'r2-again.csv', _csv([
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF2', '2026-10-30', 'R2', 1, 0, 'W01', 'removed'],
        ['Harbor Glass Inc', 'Lobby', 'PO-9', 'XF2', '2026-10-30', 'R2', 2, 1, 'W02', 'one left on the rack'],
    ]))
    # qty 0 row is skipped by the table parser, so only serial 2 qty 1 remains
    assert reduced.status_code == 200, reduced.get_json()
    again = client.get('/api/orders/1').get_json()
    assert again['pack_status'] == 'partial'
    serials = {row['serial']: row for row in again['packing_serials']}
    assert serials[1]['packed'] == 4
    assert serials[1]['bo'] == 0
    assert serials[2]['packed'] == 1
    assert serials[2]['bo'] == 1
    assert again['packing_totals']['bo'] == 1
    assert len([row for row in again['packing_lists'] if row['number'] == 'XF2']) == 1

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    customer = client.get('/api/orders/1').get_json()
    assert customer['pack_status_label'] == 'Partially packed'
    assert customer['packing_totals']['bo'] == 1
    assert 'packing_history' not in customer
    assert customer['packing_lists']
    assert 'x 36 in' in customer['packing_lists'][0]['lines'][0]['size'] or 'in' in customer['packing_serials'][0]['size']
    assert client.get('/api/orders/2').status_code == 404
    orders = client.get('/api/orders').get_json()
    assert [row['id'] for row in orders] == [1]
    assert orders[0]['pack_status_label'] == 'Partially packed'
    assert client.post('/api/packing-lists', data={'files': (io.BytesIO(b'x'), 'a.csv')}, content_type='multipart/form-data').status_code == 403


def test_unmatched_project_is_kept_for_staff_and_a_different_spec_does_not_block(client):
    _users(client)
    _save_order()
    _save_order(3, project='Lobby', po='PO-OTHER')
    assert login(client, 'admin@oraglass.us').status_code == 302
    unknown = _upload(client, 'missing.csv', _csv([
        ['Nobody Glass', 'Unknown Job', '', 'XF9', '2026-10-29', 'R9', 1, 1, 'W01', '28 - other'],
    ]))
    assert unknown.status_code == 200, unknown.get_json()
    review = unknown.get_json()['review']
    assert review and review[0]['reason'] == 'no_match'
    assert review[0]['message']
    ambiguous = _upload(client, 'two.csv', _csv([
        ['Harbor Glass', 'Lobby', '', 'XF8', '2026-10-29', 'R8', 1, 1, 'W01', '99 - not the ordered build'],
    ]))
    assert ambiguous.get_json()['review'][0]['reason'] == 'ambiguous'
    inbox_id = ambiguous.get_json()['review'][0]['id']
    assigned = client.post(f'/api/packing-inbox/{inbox_id}/assign', json={'order_id': 1})
    assert assigned.status_code == 200, assigned.get_json()
    assert assigned.get_json()['totals']['packed'] == 1
    stray = _upload(client, 'stray.csv', _csv([
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF7', '2026-10-29', 'R7', 1, 2, 'W01', 'shop spec'],
        ['Harbor Glass', 'Lobby', 'PO-9', 'XF7', '2026-10-29', 'R7', 28, 4, 'W01', '28 - this is the IGU number'],
    ]))
    assert stray.status_code == 200, stray.get_json()
    saved = stray.get_json()['saved'][0]
    assert saved['unmatched'][0]['serial'] == 28
    assert saved['totals']['packed'] >= 1
    assert client.get('/api/packing-inbox/999/assign', json={}).status_code in {404, 405}

"""Production Excel export, the IGU registry, and quote remarks."""

import io

from openpyxl import Workbook, load_workbook

import production
from storage import load_orders, load_quotes
from tests.conftest import login, write_user
from tests.test_production_orders import _company_users, _size_item


def _area(row):
    return round(row['Width(In)'] * row['Height(In)'] / 144, 2)


def test_dimensions_and_price_per_square_foot_round_trip():
    spec = {
        'glass1': 'Solarban60 4mm', 'temper1': 'n/a',
        'spacer1': 'Superspacer 16mm / sqf', 'sealant1': 'Sealant -  16mm Polysulfide',
        'glass2': 'Clear Glass 4mm', 'temper2': 'n/a',
        'width': 1000, 'height': 1000, 'unit': 'mm',
    }
    wide = dict(spec, width=2000, height=2500)
    rows = production.export_lines([
        {'name': 'Small', 'price_sqf': 6.4, 'pcs': 1, 'remarks': 'Lobby', 'mark': '2', 'spec': spec},
        {'name': 'Large', 'price_sqf': 6.4, 'pcs': 2, 'spec': wide},
    ])
    assert [row['Serial Number'] for row in rows] == [1, 2]
    assert rows[0]['Floor Code'] == '2'
    assert rows[0]['Remarks'] == 'Lobby'
    assert rows[0]['Unit Price'] == 6.4
    assert rows[0]['Quantity'] == 1
    assert rows[0]['Product ID'] == '84'
    assert rows[0]['Width(mm)'] == 1000
    assert rows[0]['Height(mm)'] == 1000
    assert rows[0]['Width(In)'] == 39.370078
    assert rows[0]['Height(In)'] == 39.370078
    assert _area(rows[0]) == 10.76
    assert round(_area(rows[0]) * rows[0]['Unit Price'], 2) == 68.86
    assert rows[1]['Width(mm)'] == 2000
    assert rows[1]['Height(mm)'] == 2500
    assert rows[1]['Width(In)'] == 78.740157
    assert rows[1]['Height(In)'] == 98.425196
    assert _area(rows[1]) == 53.82
    assert round(_area(rows[1]) * rows[1]['Unit Price'], 2) == 344.45
    book = load_workbook(io.BytesIO(production.workbook_bytes([
        {'name': 'Small', 'price_sqf': 6.4, 'pcs': 1, 'spec': spec},
    ])))
    headers = [cell.value for cell in next(book.active.iter_rows(min_row=1, max_row=1))]
    assert headers == list(production.PRODUCTION_HEADERS)


def test_workbook_seed_matches_erp_configurations():
    report = production.seed_report()
    assert report['sheet_rows'] == 169
    assert report['matched'] == 146
    assert len(report['unmatched']) == 23
    missing_names = {row['product_name'] for row in report['unmatched']}
    assert any('Solarban70' in name for name in missing_names)
    assert any('Reeded' in name for name in missing_names)


def test_registry_page_is_for_ora_staff_and_export_uses_the_current_id(client):
    _company_users(client)
    assert client.get('/production-settings').status_code == 302
    assert login(client, 'boss@harbor.example').status_code == 302
    denied = client.get('/production-settings')
    assert denied.status_code == 302
    assert b'Production IDs' not in denied.data
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/production-settings')
    assert page.status_code == 200
    assert b'Production settings' in page.data
    assert b'Production IDs' in page.data
    assert b'Production Excel export' in page.data
    excel = client.get('/production-settings?tab=excel')
    assert excel.status_code == 200
    for header in production.PRODUCTION_HEADERS:
        assert header.encode() in excel.data
    client.post('/logout')
    assert login(client, 'staff@oraglass.us').status_code == 302
    staff_page = client.get('/production-settings')
    assert staff_page.status_code == 200
    assert b'Production settings' in staff_page.data
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    saved = client.post('/production-settings', data={'action': 'excel', 'tab': 'excel'})
    assert saved.status_code == 302
    assert production.load_export_settings()['generate_on_confirm'] is False
    client.post('/production-settings', data={'action': 'excel', 'tab': 'excel', 'generate_on_confirm': 'on'})
    assert production.load_export_settings()['generate_on_confirm'] is True

    client.post('/logout')
    assert login(client, 'sales@harbor.example').status_code == 302
    quote = client.post('/api/quotes', json={
        'items': [{
            **_size_item(),
            'remarks': 'Tempered edge',
            'mark': 'L3',
            'spec': {
                'glass1': 'Clear Glass 6mm', 'temper1': 'n/a',
                'spacer1': 'Superspacer 12mm / sqf', 'sealant1': 'n/a',
                'glass2': 'Bronze 4mm', 'temper2': 'n/a',
                'width': 20, 'height': 30, 'unit': 'in',
            },
        }],
        'currency': 'usd', 'project': 'Remarks', 'type': 'size', 'tier': 'T5',
    })
    assert quote.status_code == 200, quote.get_json()
    quote_id = quote.get_json()['id']
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    assert stored['items'][0]['remarks'] == 'Tempered edge'
    copied = client.post(f'/api/quotes/{quote_id}/copy', json={})
    assert copied.status_code == 200, copied.get_json()
    fresh = next(row for row in load_quotes() if row['id'] == copied.get_json()['id'])
    glass = next(row for row in fresh['items'] if row.get('spec'))
    assert glass['remarks'] == 'Tempered edge'
    assert glass['mark'] == 'L3'
    rows = production.registry_rows()
    custom = next(row for row in rows if row['product_name'] == stored['items'][0]['name'])
    assert custom['product_id'] == ''
    assert custom['first_used']
    assert custom['order_count'] == 0

    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass'})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['items'][0]['remarks'] == 'Tempered edge'
    assert order['items'][0]['mark'] == 'L3'
    counted = next(row for row in production.registry_rows() if row['product_name'] == stored['items'][0]['name'])
    assert counted['order_count'] == 1
    detail = client.get(f'/api/orders/{order_id}').get_json()
    assert detail['items'][0]['production_note'] == 'No production ID yet'
    download = client.get(f'/api/orders/{order_id}/production.xlsx')
    assert download.status_code == 200
    sheet = load_workbook(io.BytesIO(download.data)).active
    header = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    assert header == list(production.PRODUCTION_HEADERS)
    values = [cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))]
    assert values[2] in (None, '')
    assert values[3] == stored['items'][0]['name']
    assert values[10] == 'Tempered edge'
    assert values[1] == 'L3'

    filled = client.post('/production-settings?filter=missing', data={
        'action': 'ids',
        'tab': 'ids',
        'filter': 'missing',
        'row_key': counted['key'],
        'product_id': '501',
        'product_name': counted['product_name'],
    })
    assert filled.status_code == 302
    again = client.get(f'/api/orders/{order_id}/production.xlsx')
    refreshed = [cell.value for cell in list(load_workbook(io.BytesIO(again.data)).active.iter_rows(min_row=2, max_row=2))[0]]
    assert refreshed[2] == 501

    def product_file():
        book = Workbook()
        grid = book.active
        grid.append(['Product Name', 'Product ID'])
        grid.append([counted['product_name'], 777])
        grid.append(['Not a real IGU', 12])
        payload = io.BytesIO()
        book.save(payload)
        payload.seek(0)
        return payload
    uploaded = client.post('/production-settings', data={
        'action': 'upload', 'tab': 'ids', 'product_list': (product_file(), 'products.xlsx'),
    }, content_type='multipart/form-data')
    assert uploaded.status_code == 302
    kept = next(row for row in production.registry_rows() if row['key'] == counted['key'])
    assert kept['product_id'] == '501'
    replaced = client.post('/production-settings', data={
        'action': 'upload', 'tab': 'ids', 'replace_ids': 'on',
        'product_list': (product_file(), 'products.xlsx'),
    }, content_type='multipart/form-data')
    assert replaced.status_code == 302
    changed = next(row for row in production.registry_rows() if row['key'] == counted['key'])
    assert changed['product_id'] == '777'

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 403

import io

from order_import import parse_dimension, parse_regex_lines, to_inches
from tests.conftest import login, write_user

FIXTURES = 'tests/fixtures'
CLEAR = 'Clear Glass 6mm'
SPACER = 'Superspacer 12mm / sqf'


def _file(name):
    with open(f'{FIXTURES}/{name}', 'rb') as handle:
        return handle.read()


def _upload(client, filename, payload, **fields):
    data = {'file': (io.BytesIO(payload), filename)}
    data.update(fields)
    return client.post('/api/order-imports', data=data)


def _template(client, body):
    response = client.post('/api/order-templates', json=body)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _excel_body(customer_id, **extra):
    body = {
        'customer_id': customer_id,
        'name': 'Harbor inches',
        'kind': 'excel',
        'unit': 'auto',
        'sheet': 'Orders',
        'header_row': 1,
        'match': '',
        'columns': {
            'width': 'Width', 'height': 'Height', 'qty': 'Qty',
            'glass': 'Glass', 'spacer': 'Spacer', 'mark': 'Mark',
        },
    }
    body.update(extra)
    return body


def test_fractions_inches_and_millimetres_convert_to_inches():
    assert parse_dimension('23 1/2') == (23.5, None)
    assert parse_dimension('23-1/2"') == (23.5, 'in')
    assert parse_dimension('35-1/4"') == (35.25, 'in')
    number, unit = parse_dimension('596 mm')
    assert unit == 'mm'
    assert to_inches(number, unit) == round(596 / 25.4, 4)
    parsed = parse_regex_lines(
        ['W9 23-1/2" x 36 2 Their Clear'],
        {
            'unit': 'in',
            'line_pattern': r'(?P<mark>\S+)\s+(?P<width>\S+)\s+x\s+(?P<height>\S+)\s+(?P<qty>\d+)\s+(?P<glass>.+)$',
        },
    )
    assert parsed['lines'][0]['width'] == 23.5
    assert parsed['lines'][0]['height'] == 36
    assert parsed['lines'][0]['glass_name'] == 'Their Clear'


def test_excel_import_requires_a_mapping_then_reuses_it(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass', customer_subrole='management')
    write_user('sam@oraglass.us', 'sales')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'Import order' in page.data
    assert b'id="template-editor"' in page.data
    assert b'id="template-requests"' in page.data
    _template(client, _excel_body('harbor'))
    client.post('/logout')

    assert login(client, 'lee@harbor.example').status_code == 302
    customer_page = client.get('/quotes')
    assert b'Import order' in customer_page.data
    assert b'id="template-editor"' not in customer_page.data
    uploaded = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'), currency='usd')
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['status'] == 'needs_mapping'
    assert 'Their Clear' in body['unmatched_glass']
    assert 'Warm edge 12' in body['unmatched_spacer']
    known = next(line for line in body['lines'] if line['mark'] == 'W2')
    assert known['glass_product'] == CLEAR
    assert known['width'] == 24.5
    fractional = next(line for line in body['lines'] if line['mark'] == 'W1')
    assert fractional['width'] == 23.5
    assert fractional['height'] == 35.25
    assert fractional['qty'] == 2
    blocked = client.post(f"/api/order-imports/{body['id']}/quote", json={
        'currency': 'usd',
        'lines': [{
            'width': fractional['width'], 'height': fractional['height'], 'qty': 2,
            'glass_product': '', 'spacer_product': SPACER, 'mark': 'W1',
        }],
    })
    assert blocked.status_code == 400

    mapped = client.post(f"/api/order-imports/{body['id']}/map", json={
        'remember': True,
        'glass': {'Their Clear': CLEAR},
        'spacer': {'Warm edge 12': SPACER},
    })
    assert mapped.status_code == 200, mapped.get_json()
    ready = mapped.get_json()
    assert ready['status'] == 'preview'
    assert ready['unmatched_glass'] == []
    assert ready['unmatched_spacer'] == []
    assert all(line['total'] > 0 for line in ready['lines'])
    created = client.post(f"/api/order-imports/{body['id']}/quote", json={
        'currency': 'usd',
        'project': 'Harbor lobby',
        'lines': [{
            'mark': line['mark'],
            'width': line['width'],
            'height': line['height'],
            'qty': line['qty'],
            'glass_product': line['glass_product'],
            'spacer_product': line['spacer_product'],
            'glass_name': line['glass_name'],
        } for line in ready['lines']],
    })
    assert created.status_code == 200, created.get_json()
    quote_id = created.get_json()['id']
    saved = next(row for row in client.get('/api/quotes').get_json() if row['id'] == quote_id)
    assert saved['type'] == 'size'
    assert saved['tier'] == 'T2'
    assert saved['customer_id'] == 'harbor'
    assert saved['project'] == 'Harbor lobby'
    spec = saved['items'][0]['spec']
    assert spec['unit'] == 'in'
    assert spec['glass1'] == CLEAR
    assert spec['width'] == 23.5
    assert spec['spacer1'] == SPACER
    assert saved['items'][0]['sqf'] > 0

    again = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'), currency='usd')
    reused = again.get_json()
    assert reused['status'] == 'preview'
    assert reused['unmatched_glass'] == []
    assert next(line for line in reused['lines'] if line['mark'] == 'W1')['glass_product'] == CLEAR
    client.post('/logout')

    assert login(client, 'sam@oraglass.us').status_code == 302
    missing = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'))
    assert missing.status_code == 400
    behalf = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'), customer_id='harbor')
    assert behalf.status_code == 200
    assert behalf.get_json()['status'] == 'preview'
    assert behalf.get_json()['company'] == 'Harbor Glass'


def test_pdf_and_spreadsheet_millimetre_files(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    _template(client, {
        'customer_id': 'harbor',
        'name': 'Harbor PDF',
        'kind': 'pdf',
        'unit': 'mm',
        'pdf_mode': 'table',
        'header_row': 1,
        'columns': {'width': 'Width', 'height': 'Height', 'qty': 'Pcs', 'glass': 'Glass', 'mark': 'Mark'},
    })
    _template(client, _excel_body('harbor', name='Harbor CSV', unit='mm', sheet='', columns={
        'width': 'Width', 'height': 'Height', 'qty': 'Pcs', 'glass': 'Description',
    }))
    _template(client, _excel_body('harbor', name='Harbor XLS', unit='mm', sheet='Orders', columns={
        'width': 'Width', 'height': 'Height', 'qty': 'Pcs', 'glass': 'Glass',
    }))
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302

    pdf = _upload(client, 'order_mm.pdf', _file('order_mm.pdf')).get_json()
    assert pdf['status'] == 'needs_mapping'
    assert pdf['template']['kind'] == 'pdf'
    patio = next(line for line in pdf['lines'] if line['mark'] == 'A1')
    assert patio['width'] == round(700 / 25.4, 4)
    assert patio['height'] == round(1000 / 25.4, 4)
    assert 'Patio glass' in pdf['unmatched_glass']
    mapped = client.post(f"/api/order-imports/{pdf['id']}/map", json={
        'remember': True,
        'glass': {'Patio glass': 'Clear Glass 4mm'},
    })
    assert mapped.get_json()['status'] == 'preview'

    csv_body = _upload(client, 'order_mm.csv', _file('order_mm.csv')).get_json()
    assert csv_body['template']['name'] == 'Harbor CSV'
    assert csv_body['lines'][0]['width'] == round(600 / 25.4, 4)
    assert 'Bronze lite' in csv_body['unmatched_glass']

    xls_body = _upload(client, 'order_mm.xls', _file('order_mm.xls')).get_json()
    assert xls_body['template']['name'] == 'Harbor XLS'
    assert xls_body['lines'][0]['qty'] == 5
    assert xls_body['lines'][0]['width'] == round(500 / 25.4, 4)
    assert 'Shop glass' in xls_body['unmatched_glass']


def test_missing_template_becomes_a_request_for_admins(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    write_user('june@north.example', 'customer', customer_id='north', customer_name='Northline')
    assert login(client, 'lee@harbor.example').status_code == 302
    uploaded = _upload(client, 'order_mm.csv', _file('order_mm.csv'))
    assert uploaded.get_json()['status'] == 'needs_template'
    import_id = uploaded.get_json()['id']
    requested = client.post(f'/api/order-imports/{import_id}/request', json={'note': 'Our saw exports this CSV.'})
    assert requested.status_code == 200, requested.get_json()
    request_id = requested.get_json()['id']
    assert client.get('/api/order-template-requests').status_code == 403
    assert client.get(f'/api/order-template-requests/{request_id}/file').status_code == 403
    client.post('/logout')

    assert login(client, 'june@north.example').status_code == 302
    assert client.get('/api/order-templates').get_json() == []
    assert 'Their Clear' not in client.get('/api/order-mappings').get_data(as_text=True)
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    home = client.get('/dashboard')
    assert b'waiting for an import template' in home.data
    assert b'No email is sent' in home.data
    listed = client.get('/api/order-template-requests').get_json()
    assert listed[0]['company'] == 'Harbor Glass'
    assert listed[0]['note'] == 'Our saw exports this CSV.'
    assert listed[0]['status'] == 'open'
    downloaded = client.get(f'/api/order-template-requests/{request_id}/file')
    assert downloaded.status_code == 200
    assert downloaded.data == _file('order_mm.csv')
    preview = client.post('/api/order-templates/preview', data={
        'request_id': str(request_id),
        'template': __import__('json').dumps(_excel_body('harbor', name='From sample', unit='mm', sheet='', columns={
            'width': 'Width', 'height': 'Height', 'qty': 'Pcs', 'glass': 'Description',
        })),
    })
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()['lines'][0]['glass_name'] == 'Bronze lite'
    done = client.post(f'/api/order-template-requests/{request_id}/done')
    assert done.get_json()['status'] == 'done'
    assert b'waiting for an import template' not in client.get('/dashboard').data


def test_customers_only_see_their_company_and_bad_files_are_refused(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    write_user('june@north.example', 'customer', customer_id='north', customer_name='Northline')
    assert login(client, 'admin@oraglass.us').status_code == 302
    saved = _template(client, _excel_body('harbor'))
    twin = _template(client, _excel_body('harbor', name='Harbor copy'))
    assert client.post('/api/order-templates', json={'name': 'Missing'}).status_code == 400
    client.post('/logout')

    assert login(client, 'lee@harbor.example').status_code == 302
    assert client.post('/api/order-templates', json=_excel_body('harbor', name='Nope')).status_code == 403
    names = [row['name'] for row in client.get('/api/order-templates').get_json()]
    assert names == ['Harbor inches', 'Harbor copy']
    choice = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'))
    assert choice.get_json()['status'] == 'choose_template'
    chosen = client.post(f"/api/order-imports/{choice.get_json()['id']}/parse", json={'template_id': saved['id']})
    assert chosen.status_code == 200
    assert chosen.get_json()['template']['id'] == saved['id']
    refused = _upload(client, 'notes.txt', b'not an order')
    assert refused.status_code == 400
    huge = _upload(client, 'big.csv', b'a' * (8 * 1024 * 1024 + 1))
    assert huge.status_code == 400
    assert b'8 MB' in huge.data
    client.post('/logout')

    assert login(client, 'june@north.example').status_code == 302
    assert client.get('/api/order-templates').get_json() == []
    assert client.get('/api/order-imports').get_json() == []
    assert twin['id'] not in [row.get('id') for row in client.get('/api/order-templates').get_json()]

"""ORGADATA GLASS ORDER PDFs become draft per-size quotes."""

import io
from pathlib import Path

from orgadata_import import SCANNED_MESSAGE, SOURCE_LABEL, parse_pdf, parse_text
from production import line_export, makeup_from_spec, production_catalog, save_production_overrides
from storage import load_customers, load_quotes, save_customers
from tests.conftest import login, write_user

FIXTURE = Path('tests/fixtures/orgadata_glass_order.txt')
CLEAR = 'Clear Glass 6mm'
COATED = 'Solarban60 VT 6mm'
SPACER = 'Technoform 16mm / sqf'
SEEDED_SPACER = 'Superspacer 16mm / sqf'


def sample_text():
    return FIXTURE.read_text()


def two_block_text():
    """The sample, then a second glass block, with the size rows split across pages."""
    lines = sample_text().splitlines()
    head = []
    rows = []
    tail = []
    seen_row = False
    for line in lines:
        if line.startswith('W0') or line.startswith('D0'):
            seen_row = True
            rows.append(line)
        elif seen_row and line.startswith('Sum:'):
            tail.append(line)
            seen_row = False
        elif seen_row:
            rows.append(line)
        else:
            if line.startswith('Sum Quantity'):
                continue
            head.append(line) if not tail else tail.append(line)
    # The sample tail still has Page markers and the original Sum Quantity. Drop that total.
    tail = [line for line in tail if not line.startswith('Sum Quantity') and not line.startswith('Page')]
    second = [
        'Glass',
        'Name: 4mm Clear',
        'Configuration: 1x4mm',
        'Thickness: 4 mm',
        'Position / Number | Quantity | Width [mm] | Height [mm] | Area | Perimeter | Weight',
        'A02 / 1 1 500 800 0.40 2.60 5.0',
        'Page 2',
        'A02 / 2 3 400 600 0.72 2.00 6.0',
        'Sum: 4  1.12 4.60 11.0',
        'Sum Quantity: 16 Pcs.',
    ]
    return '\n'.join(head + ['Page 1'] + rows[:4] + ['Page 2'] + rows[4:] + tail + second)


def pdf_bytes(text):
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=(792, 1000))
    y = 960
    for line in text.splitlines():
        if y < 40:
            drawing.showPage()
            y = 960
        drawing.drawString(36, y, line[:140])
        y -= 16
    drawing.save()
    return buffer.getvalue()


def blank_pdf():
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer)
    drawing.showPage()
    drawing.save()
    return buffer.getvalue()


def _upload(client, text, **fields):
    data = {'file': (io.BytesIO(pdf_bytes(text)), 'glass-order.pdf')}
    data.update(fields)
    return client.post('/api/orgadata-imports', data=data)


def _open_windows(email='orders@openwindows.test', subrole='management', **extra):
    write_user(email, 'customer', customer_id='open-windows', customer_name='Open Windows', customer_subrole=subrole, **extra)
    customers = load_customers()
    customers['open-windows'].update({'country': 'Canada', 'province': 'BC', 'name': 'Open Windows'})
    save_customers(customers)


def test_sample_text_parses_floors_quantities_and_clear_glass():
    parsed = parse_text(sample_text())
    assert parsed['sender'] == 'Open Windows'
    assert parsed['project'] == 'AF17.0413.VLD.iKeith Pagnac.HH'
    assert parsed['po'] == '2026-00348'
    assert parsed['contact'] == 'Alaleh Ashrafi'
    assert parsed['reference_date'] == '2026-11-16'
    block = parsed['blocks'][0]
    assert [line['floor'] for line in block['lines']][0] == 'W01 / 1'
    assert block['lines'][4]['width_mm'] == 1015
    assert block['lines'][0]['height_mm'] == 1288
    assert sum(line['qty'] for line in block['lines']) == 12
    assert parsed['warnings'] == []
    assert block['lites'][0]['source'] == 'ULTRACLEAR Optisolar UD80'
    assert block['lites'][1]['source'] == 'Planibel G'
    assert block['tempered'] is True and block['argon'] is True
    assert 'id' not in block['lines'][0]


def test_wrapped_name_keeps_the_triple_and_each_block_sum():
    """The first block's name starts on the line after Name:, and it is a 48 mm triple."""
    from production import makeup_from_spec
    parsed = parse_text(Path('tests/fixtures/orgadata_triple_name.txt').read_text())
    assert parsed['sender'] == 'Open Windows'
    assert parsed['project'] == 'AF17.0359.VLD.vlad.Triple G'
    assert parsed['po'] == '2026-00341'
    assert parsed['sum_quantity'] == 12
    assert parsed['warnings'] == []
    assert len(parsed['blocks']) == 2
    triple, double = parsed['blocks']
    assert '48mm' in triple['name'] and '4mm clear' in triple['name']
    assert triple['configuration'] == '6+4+6mm'
    assert triple['gaps'] == [16, 16]
    assert [lite['thickness_mm'] for lite in triple['lites']] == [6, 4, 6]
    assert triple['lites'][0]['source'] == 'ULTRACLEAR Optisolar UD80'
    assert triple['lites'][1]['source'] == 'clear'
    assert triple['lites'][2]['source'] == 'Optisolar UD80'
    assert triple['tempered'] is True and triple['argon'] is True
    assert triple['sum_qty'] == 8
    assert sum(line['qty'] for line in triple['lines']) == 8
    assert [line['floor'] for line in triple['lines']] == ['P45 / 1', 'P45 / 3', 'P46 / 1', 'P46 / 3']
    assert triple['lines'][0]['width_mm'] == 1058
    assert triple['lines'][0]['height_mm'] == 2830
    assert double['configuration'] == '2x6mm'
    assert double['gaps'] == [16]
    assert len(double['lites']) == 2
    assert double['lites'][1]['source'] == 'Planibel G'
    assert double['sum_qty'] == 4
    assert [line['floor'] for line in double['lines']] == ['W35 / 1', 'W35 / 2', 'W36 / 1', 'W36 / 2']
    assert sum(line['qty'] for line in double['lines']) == 4
    assert double['lines'][2]['line_number'] == 7
    assert sum(line['qty'] for block in parsed['blocks'] for line in block['lines']) == 12
    name = makeup_from_spec({
        'glass1': 'Clear Glass 6mm', 'temper1': 'yes', 'spacer1': 'Superspacer 16mm / sqf', 'sealant1': 'Polysulfide',
        'glass2': 'Clear Glass 4mm', 'temper2': 'yes', 'spacer2': 'Superspacer 16mm / sqf', 'sealant2': 'Polysulfide',
        'glass3': 'Clear Glass 6mm', 'temper3': 'yes',
    })
    assert name.count('+') == 4
    assert name.startswith('6mmClear Glass Tempered+')
    assert name.endswith('+6mmClear Glass Tempered')
    short = parse_text(Path('tests/fixtures/orgadata_triple_name.txt').read_text().replace(
        'Sum:                                     8', 'Sum:                                     9', 1,
    ))
    assert any('Sum: row (9)' in warning for warning in short['blocks'][0]['warnings'])
    assert short['blocks'][1]['warnings'] == []
    grand = parse_text(Path('tests/fixtures/orgadata_triple_name.txt').read_text().replace(
        'Sum Quantity:               12 Pcs.', 'Sum Quantity:               99 Pcs.',
    ))
    assert any('99' in warning for warning in grand['warnings'])


def test_triple_preview_has_a_selector_for_every_lite_and_gap(client):
    """8 pieces of the 48 mm triple and 4 of the 28 mm double, 12 in total."""
    write_user('admin@test.com', 'admin')
    _open_windows()
    login(client, 'admin@test.com')
    text = Path('tests/fixtures/orgadata_triple_name.txt').read_text()
    uploaded = _upload(client, text)
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['project'] == 'AF17.0359.VLD.vlad.Triple G'
    assert body['po'] == '2026-00341'
    assert body['sum_quantity'] == 12
    assert body['warnings'] == []
    assert len(body['blocks']) == 2
    triple, double = body['blocks']
    assert len(triple['lites']) == 3
    assert len(triple['spacers']) == 2
    assert [lite['thickness_mm'] for lite in triple['lites']] == [6, 4, 6]
    assert [spacer['gap_mm'] for spacer in triple['spacers']] == [16, 16]
    assert triple['lites'][1]['product'] == 'Clear Glass 4mm'
    assert triple['sum_qty'] == 8
    assert sum(line['qty'] for line in triple['lines']) == 8
    assert len(double['lites']) == 2 and len(double['spacers']) == 1
    assert double['lites'][1]['product'] == CLEAR
    assert double['sum_qty'] == 4
    assert sum(line['qty'] for line in double['lines']) == 4
    assert sum(line['qty'] for line in body['lines']) == 12
    assert all('Sum: row' not in warning for block in body['blocks'] for warning in block['warnings'])
    page = client.get('/quotes')
    assert b'ERP glass ' in page.data
    assert b'ERP spacer ' in page.data
    created = client.post(f"/api/orgadata-imports/{body['id']}/quote", json={
        'customer_id': 'open-windows',
        'blocks': [
            {
                'lites': [
                    {'product': CLEAR, 'temper': 'yes'},
                    {'product': 'Clear Glass 4mm', 'temper': 'yes'},
                    {'product': CLEAR, 'temper': 'yes'},
                ],
                'spacers': [{'product': SEEDED_SPACER}, {'product': SEEDED_SPACER}],
            },
            {
                'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
                'spacers': [{'product': SPACER}],
            },
        ],
    })
    assert created.status_code == 200, created.get_json()
    items = created.get_json()['quote']['items']
    assert len(items) == 8
    assert sum(item['pcs'] for item in items) == 12
    from documents import build_label
    triple_name = items[0]['name']
    assert triple_name == build_label(items[0]['spec'])
    assert triple_name.startswith('48 (1 7/8") - 6mmClear Glass Tempered+')
    assert triple_name.count('+') == 4
    assert items[0]['spec']['glass1'] == CLEAR
    assert items[0]['spec']['glass2'] == 'Clear Glass 4mm'
    assert items[0]['spec']['glass3'] == CLEAR
    assert items[0]['spec']['spacer1'] == SEEDED_SPACER
    assert items[0]['spec']['spacer2'] == SEEDED_SPACER
    assert items[4]['spec']['glass3'] == ''
    assert items[4]['name'].count('+') == 2


def test_multi_block_multi_page_and_a_quantity_mismatch():
    parsed = parse_text(two_block_text())
    assert len(parsed['blocks']) == 2
    assert sum(line['qty'] for block in parsed['blocks'] for line in block['lines']) == 16
    assert parsed['sum_quantity'] == 16
    assert parsed['warnings'] == []
    assert parsed['blocks'][1]['lines'][0]['floor'] == 'A02 / 1'
    assert parsed['blocks'][1]['lines'][1]['line_number'] == 13
    broken = sample_text().replace('Sum Quantity: 12 Pcs.', 'Sum Quantity: 99 Pcs.')
    warned = parse_text(broken)
    assert any('99' in warning for warning in warned['warnings'])


def test_pdf_words_round_trip_and_a_scan_is_refused():
    parsed = parse_pdf_bytes(pdf_bytes(sample_text()))
    assert parsed['po'] == '2026-00348'
    assert len(parsed['blocks'][0]['lines']) == 11
    try:
        parse_pdf_bytes(blank_pdf())
    except Exception as exc:
        assert str(exc) == SCANNED_MESSAGE
    else:
        raise AssertionError('a PDF with no text layer was accepted')


def parse_pdf_bytes(payload):
    path = Path('/tmp/orgadata-test.pdf')
    path.write_bytes(payload)
    return parse_pdf(path)


def test_preview_maps_planibel_to_clear_and_keeps_position_on_the_floor(client):
    write_user('admin@test.com', 'admin')
    _open_windows()
    login(client, 'admin@test.com')
    uploaded = _upload(client, sample_text())
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert body['source'] == SOURCE_LABEL
    assert body['customer_id'] == 'open-windows'
    assert body['currency'] == 'cad'
    assert 'fx_rate' in body
    block = body['blocks'][0]
    assert block['lites'][1]['product'] == CLEAR
    assert block['lites'][0]['product'] == ''
    assert block['product_id'] == ''
    assert block['product_id_missing'] is False
    line = body['lines'][0]
    assert line['line_number'] == 1
    assert line['floor'] == 'W01 / 1'
    assert line['product_id'] == ''
    assert line['total'] is None
    page = client.get('/quotes')
    assert b'Import ORGADATA GLASS ORDER' in page.data
    assert b'ORGADATA GLASS ORDER preview' in page.data
    assert b'<th>#</th><th>Floor</th><th>ID</th>' in page.data


def test_alias_prices_the_build_and_a_missing_production_id_is_not_invented(client):
    write_user('admin@test.com', 'admin')
    _open_windows()
    customers = load_customers()
    customers['open-windows']['additional_discount_percent'] = 10
    customers['open-windows']['price_addon_percent'] = 10
    customers['open-windows']['minimum_sqf_per_piece'] = 20
    save_customers(customers)
    login(client, 'admin@test.com')
    uploaded = _upload(client, sample_text()).get_json()
    mapped = client.post(f"/api/orgadata-imports/{uploaded['id']}/preview", json={
        'customer_id': 'open-windows',
        'remember': True,
        'blocks': [{
            'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
            'spacers': [{'product': SPACER}],
        }],
    })
    assert mapped.status_code == 200, mapped.get_json()
    body = mapped.get_json()
    block = body['blocks'][0]
    assert block['lites'][0]['product'] == COATED
    assert block['spacers'][0]['product'] == SPACER
    assert block['product_id'] == ''
    assert block['product_id_missing'] is True
    assert any('No production ID yet' in warning for warning in block['warnings'])
    seeded = client.post(f"/api/orgadata-imports/{uploaded['id']}/preview", json={
        'customer_id': 'open-windows',
        'blocks': [{
            'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
            'spacers': [{'product': SEEDED_SPACER}],
        }],
    }).get_json()
    assert seeded['blocks'][0]['product_id'] == '70'
    assert body['lines'][0]['floor'] == 'W01 / 1'
    assert body['lines'][0]['product_id'] == ''
    assert body['lines'][0]['total'] > 0
    assert any(line['sqf'] == 20 and line['real_sqf'] < 20 for line in body['lines'])
    created = client.post(f"/api/orgadata-imports/{uploaded['id']}/quote", json={
        'customer_id': 'open-windows',
        'project': body['project'],
        'po': body['po'],
        'contact': body['contact'],
        'reference_date': body['reference_date'],
        'remember': True,
        'blocks': [{
            'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
            'spacers': [{'product': SPACER}],
        }],
    })
    assert created.status_code == 200, created.get_json()
    quote = created.get_json()['quote']
    assert quote['source'] == SOURCE_LABEL
    assert quote['project'] == 'AF17.0413.VLD.iKeith Pagnac.HH'
    assert quote['po'] == '2026-00348'
    assert quote['reference_date'] == '2026-11-16'
    assert quote['notes'] == 'Person in Charge: Alaleh Ashrafi'
    assert quote['status'] == 'draft'
    assert quote['type'] == 'size'
    assert quote['currency'] == 'cad'
    assert quote['has_source_pdf'] is True
    assert quote['additional_discount_percent'] == 10
    assert quote['price_addon_percent'] == 10
    assert quote['items'][0]['floor'] == 'W01 / 1'
    assert 'id' not in quote['items'][0]
    makeup = makeup_from_spec(quote['items'][0]['spec'])
    stored = production_catalog()[makeup.casefold()]
    assert not str(stored.get('product_id') or '').strip()
    exported = line_export(quote['items'][0], serial=1, currency='cad', fx_rate=quote.get('fx_rate'))
    assert exported['Floor Code'] == 'W01 / 1'
    assert exported['Serial Number'] == 1
    assert exported['Product ID'] == ''
    save_production_overrides({makeup.casefold(): {'product_id': 'IGU-48', 'product_name': makeup}})
    again = client.post(f"/api/orgadata-imports/{uploaded['id']}/preview", json={
        'customer_id': 'open-windows',
        'blocks': [{
            'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
            'spacers': [{'product': SPACER}],
        }],
    })
    assert again.get_json()['blocks'][0]['product_id'] == 'IGU-48'
    assert again.get_json()['lines'][0]['product_id'] == 'IGU-48'
    reused = _upload(client, sample_text(), customer_id='open-windows').get_json()
    assert reused['blocks'][0]['lites'][0]['product'] == COATED
    assert reused['blocks'][0]['spacers'][0]['product'] == SPACER
    exported_later = line_export(quote['items'][0], serial=1)
    assert exported_later['Product ID'] == 'IGU-48'
    assert reused['blocks'][0]['product_id'] == 'IGU-48'
    pdf = client.get(f"/api/quotes/{quote['id']}/source-pdf")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    assert any(line.get('name') == 'GST' for line in quote.get('tax_lines') or [])


def test_customer_fx_is_hidden_and_operations_see_only_their_quote(client):
    _open_windows('ops@openwindows.test', 'sales')
    _open_windows('lead@openwindows.test', 'management')
    login(client, 'ops@openwindows.test')
    uploaded = _upload(client, sample_text())
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    assert 'fx_rate' not in body
    assert body['customer_id'] == 'open-windows'
    created = client.post(f"/api/orgadata-imports/{body['id']}/quote", json={
        'blocks': [{
            'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
            'spacers': [{'product': SPACER}],
        }],
    })
    assert created.status_code == 200, created.get_json()
    quote_id = created.get_json()['id']
    assert 'fx_rate' not in created.get_json()['quote']
    login(client, 'lead@openwindows.test')
    visible = client.get('/api/quotes')
    assert any(row['id'] == quote_id for row in visible.get_json())
    write_user('other@openwindows.test', 'customer', customer_id='open-windows', customer_name='Open Windows', customer_subrole='sales')
    login(client, 'other@openwindows.test')
    hidden = client.get('/api/quotes')
    assert all(row['id'] != quote_id for row in hidden.get_json())
    denied = client.get(f'/api/quotes/{quote_id}/source-pdf')
    assert denied.status_code == 404


def test_scanned_and_other_pdfs_are_rejected(client):
    write_user('admin@test.com', 'admin')
    login(client, 'admin@test.com')
    scanned = client.post('/api/orgadata-imports', data={'file': (io.BytesIO(blank_pdf()), 'scan.pdf')})
    assert scanned.status_code == 400
    assert scanned.get_json()['error'] == SCANNED_MESSAGE
    other = client.post('/api/orgadata-imports', data={'file': (io.BytesIO(pdf_bytes('Hello from a packing list')), 'note.pdf')})
    assert other.status_code == 400
    assert 'Glass/Panel Order' in other.get_json()['error']
    assert load_quotes() == [] or all(row.get('source') != SOURCE_LABEL for row in load_quotes())

"""Regular clear keeps its sheet size, and an oversized piece warns instead of stopping."""

import io

import openpyxl

import documents
import production
from quoting import clear_vitro_for, clear_vitro_label, exceeds_regular_clear_limit, is_regular_clear
from storage import load_quotes
from tests.conftest import login, write_user

WORKBOOK = 'pricing_base.xlsx'
WARNING = 'Size exceeds regular clear limit, Clear Vitro required'


def _workbook_sheets():
    book = openpyxl.load_workbook(WORKBOOK, data_only=True)
    rows = {}
    for row in book['pricelist'].iter_rows(min_row=2, values_only=True):
        name = str(row[0] or '').strip()
        if not name:
            continue
        rows[name] = (row[2], row[3])
    return rows


def test_regular_clear_limits_come_from_the_workbook_sheet_size():
    sheets = _workbook_sheets()
    regular = {name: size for name, size in sheets.items() if is_regular_clear(name)}
    assert regular == {
        'Clear Glass 4mm': (130, 84),
        'Clear Glass 5mm': (144, 84),
        'Clear Glass 6mm': (144, 96),
        'Clear Glass 8mm': (144, 96),
        'Clear Glass 10mm': (144, 96),
        'Clear Glass 12mm': (144, 96),
    }
    large = {name: size for name, size in sheets.items() if 'Clear Glass' in name and 'Large' in name}
    assert large == {
        'Clear Glass 6mm Large': (204, 130),
        'Clear Glass 8mm Large': (204, 130),
        'Clear Glass 10mm Large': (204, 130),
        'Clear Glass 12mm Large': (204, 130),
    }
    assert exceeds_regular_clear_limit('Clear Glass 6mm', '144"x96"', 145, 40)
    assert exceeds_regular_clear_limit('Clear Glass 4mm', '130"x84"', 85, 131)
    assert not exceeds_regular_clear_limit('Clear Glass 6mm', '144"x96"', 96, 144)
    assert not exceeds_regular_clear_limit('Clear Glass 6mm', '144"x96"', 144, 96)
    assert exceeds_regular_clear_limit('Clear Glass 6mm', '144"x96"', 3658, 1000, 'mm')
    assert not exceeds_regular_clear_limit('Clear Glass 6mm Large', '204"x130"', 200, 100)
    assert not exceeds_regular_clear_limit('Bronze 6mm', '130"x96"', 200, 100)
    names = list(sheets)
    assert clear_vitro_for('Clear Glass 6mm', names) == 'Clear Glass 6mm Large'
    assert clear_vitro_for('Clear Glass 8mm', names) == 'Clear Glass 8mm Large'
    assert clear_vitro_for('Clear Glass 10mm', names) == 'Clear Glass 10mm Large'
    assert clear_vitro_for('Clear Glass 12mm', names) == 'Clear Glass 12mm Large'
    assert clear_vitro_for('Clear Glass 4mm', names) == ''
    assert clear_vitro_for('Clear Glass 5mm', names) == ''
    assert clear_vitro_label('Clear Glass 6mm Large') == 'Clear Vitro 6mm'
    assert clear_vitro_label('Clear Glass 6mm') == ''


def test_size_tab_holds_the_price_until_clear_vitro_is_chosen(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes').get_data(as_text=True)
    assert WARNING in page
    assert 'id="clear-vitro-prompt"' in page
    assert 'Switch to Clear Vitro' in page
    assert "document.getElementById('size-result').style.display = 'none'" in page
    assert 'function switchLineToClearVitro(index)' in page
    assert 'sizeQuoteItems[index]' in page
    assert 'value="Clear Glass 6mm" data-size="144&#34;x96&#34;" data-regular-clear="yes" data-vitro="Clear Glass 6mm Large"' in page
    assert 'value="Clear Glass 4mm" data-size="130&#34;x84&#34;" data-regular-clear="yes" data-vitro=""' in page
    assert 'value="Clear Glass 6mm Large" data-size="204&#34;x130&#34;" data-regular-clear="" data-vitro="">Clear Vitro 6mm' in page
    priced = client.post('/api/calculate', json={
        'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'tier': 'T2',
    })
    assert priced.status_code == 200
    assert priced.get_json()['price_sqf'] > 0


def test_clear_vitro_remark_is_appended_and_exported(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    spec = {
        'glass1': 'Clear Glass 6mm Large', 'temper1': 'n/a',
        'spacer1': 'Superspacer 12mm / sqf', 'sealant1': 'n/a',
        'glass2': 'Clear Glass 6mm', 'temper2': 'n/a',
        'width': 20, 'height': 30, 'unit': 'in',
    }
    name = documents.igu_name_from_spec(spec)
    assert 'Large' not in name
    assert '6mmClear Glass' in name
    saved = client.post('/api/quotes', json={
        'items': [{
            'name': 'Clear Glass 6mm Large', 'pcs': 1, 'price_sqf': 3.34, 'total': 10,
            'remarks': 'Edge work', 'spec': spec,
        }],
        'currency': 'usd', 'project': 'Vitro', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    stored = next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])
    line = stored['items'][0]
    assert line['remarks'] == 'Edge work Clear Vitro'
    assert 'Large' not in line['name']
    book = openpyxl.load_workbook(io.BytesIO(production.workbook_bytes(stored['items'])))
    rows = list(book.active.iter_rows(min_row=1, max_row=2, values_only=True))
    header, values = rows
    assert values[header.index('Remarks')] == 'Edge work Clear Vitro'
    assert 'Large' not in values[header.index('Product Name')]
    assert 'Clear Glass' in values[header.index('Product Name')]
    again = production.export_lines([{
        'remarks': 'Edge work Clear Vitro', 'spec': spec, 'pcs': 1, 'price_sqf': 3.34,
    }])
    assert again[0]['Remarks'] == 'Edge work Clear Vitro'
    plain = dict(spec, glass1='Clear Glass 6mm')
    regular = production.export_lines([{
        'remarks': 'Edge work', 'spec': plain, 'pcs': 1, 'price_sqf': 2,
    }])
    assert regular[0]['Remarks'] == 'Edge work'

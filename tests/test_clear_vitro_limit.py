"""Regular clear keeps its sheet size, and an oversized piece warns instead of stopping."""

import openpyxl

from quoting import exceeds_regular_clear_limit, is_regular_clear
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


def test_size_tab_shows_the_clear_vitro_warning_without_blocking(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes').get_data(as_text=True)
    assert WARNING in page
    assert 'id="clear-vitro-note"' in page
    assert "sel.dataset.regularClear === 'yes'" in page
    assert 'value="Clear Glass 6mm" data-size="144&#34;x96&#34;" data-regular-clear="yes"' in page
    assert 'value="Clear Glass 4mm" data-size="130&#34;x84&#34;" data-regular-clear="yes"' in page
    assert 'value="Clear Glass 6mm Large" data-size="204&#34;x130&#34;" data-regular-clear=""' in page
    priced = client.post('/api/calculate', json={
        'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'tier': 'T2',
    })
    assert priced.status_code == 200
    assert priced.get_json()['price_sqf'] > 0

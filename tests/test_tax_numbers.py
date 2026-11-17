"""PST and QST changes are confirmed, logged, and frozen on old documents."""

import quickbooks
from quickbooks import GST_PROVINCES, HST_PROVINCES, PST_PROVINCES, resolve_canada_tax
from storage import load_customers, load_invoices, load_orders, load_tax_number_history, save_customers
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


PROVINCE_TAX = {
    'ON': {'hst': '6'}, 'NB': {'hst': '6'}, 'NL': {'hst': '6'}, 'NS': {'hst': '6'}, 'PE': {'hst': '6'},
    'BC': {'with_pst': '7', 'gst_only': '5'},
    'MB': {'with_pst': '7', 'gst_only': '5'},
    'SK': {'with_pst': '7', 'gst_only': '5'},
    'QC': {'with_qst': '8', 'gst_only': '5'},
    'AB': {'gst': '5'}, 'NT': {'gst': '5'}, 'NU': {'gst': '5'}, 'YT': {'gst': '5'},
}


def _codes():
    with quickbooks.connection_scope('ca'):
        quickbooks.save_settings({'province_tax': PROVINCE_TAX, 'tax_exempt_code': '9'})


def _change(client, customer_id, field, number='', reason='Registration updated', remove=False):
    return client.post(f'/api/customers/{customer_id}/tax-number', json={
        'field': field, 'number': number, 'number_confirm': number,
        'reason': reason, 'remove': remove,
    })


def test_ship_to_tax_uses_only_a_saved_number():
    _codes()
    company = {'name': 'Harbor', 'country': 'Canada', 'province': 'BC'}
    for province in sorted(PST_PROVINCES):
        label = resolve_canada_tax(dict(company, pst_number=''), province)[2]
        assert label == f'GST + PST, shipped to {quickbooks.PROVINCE_NAMES[province]}'
        spaced = resolve_canada_tax(dict(company, pst_number='   '), province)[2]
        assert 'GST + PST' in spaced
        invalid = resolve_canada_tax(dict(company, pst_number='abc'), province)[2]
        assert 'GST + PST' in invalid
        registered = resolve_canada_tax(dict(company, pst_number=' PST-100 '), province)[2]
        assert registered.startswith('GST only')
        assert 'PST number on file' in registered
    qst_missing = resolve_canada_tax(dict(company, province='QC', qst_number=''), 'QC')[2]
    assert qst_missing == 'GST + QST, shipped to Quebec'
    qst_saved = resolve_canada_tax(dict(company, province='QC', qst_number='QST-999'), 'QC')[2]
    assert qst_saved.startswith('GST only')
    for province in sorted(HST_PROVINCES):
        label = resolve_canada_tax(dict(company, pst_number='PST-100', qst_number='QST-999'), province)[2]
        assert label.startswith('HST, shipped to ')
    for province in sorted(GST_PROVINCES):
        label = resolve_canada_tax(dict(company, pst_number='PST-100'), province)[2]
        assert label.startswith('GST, shipped to ')
        assert 'PST' not in label


def test_change_requires_a_match_and_a_reason_and_is_append_only(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'lee@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_subrole='management',
    )
    customers = load_customers()
    customers['maple'].update({'country': 'Canada', 'province': 'BC', 'pst_number': 'PST-100'})
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    seeded = load_tax_number_history()
    assert any(row['customer_id'] == 'maple' and row['new_value'] == 'PST-100' and row['reason'] == 'Initial number on file' for row in seeded)
    page = client.get('/customers')
    assert b'Change PST number' in page.data
    assert b'Change QST number' in page.data
    staff = client.get('/customers/maple/resale')
    assert b'Change PST number' in staff.data
    assert b'Download PDF' in staff.data
    assert b'Tax number history' in staff.data
    assert b'not asked for a US resale certificate' not in staff.data

    mismatch = client.post('/api/customers/maple/tax-number', json={
        'field': 'pst_number', 'number': 'PST-200', 'number_confirm': 'PST-201', 'reason': 'Typo',
    })
    assert mismatch.status_code == 400
    blank = client.post('/api/customers/maple/tax-number', json={
        'field': 'pst_number', 'number': '   ', 'number_confirm': '   ', 'reason': 'Clear it',
    })
    assert blank.status_code == 400
    same = _change(client, 'maple', 'pst_number', 'PST-100', reason='Again')
    assert same.status_code == 400
    no_reason = client.post('/api/customers/maple/tax-number', json={
        'field': 'pst_number', 'number': 'PST-200', 'number_confirm': 'PST-200', 'reason': '   ',
    })
    assert no_reason.status_code == 400
    assert load_customers()['maple']['pst_number'] == 'PST-100'

    changed = _change(client, 'maple', 'pst_number', 'PST-200', reason='New registration')
    assert changed.status_code == 200, changed.get_json()
    assert load_customers()['maple']['pst_number'] == 'PST-200'
    history = load_tax_number_history()
    latest = [row for row in history if row['customer_id'] == 'maple'][-1]
    assert latest['field'] == 'pst_number'
    assert latest['old_value'] == 'PST-100'
    assert latest['new_value'] == 'PST-200'
    assert latest['changed_by'] == 'admin@oraglass.us'
    assert latest['reason'] == 'New registration'
    assert latest['ip']
    assert latest['at']
    log = load_customers()['maple']['changelog'][-1]
    assert log['user'] == 'admin@oraglass.us'
    assert log['changes'][0]['old'] == 'PST-100'
    assert log['changes'][0]['new'] == 'PST-200'
    assert client.put('/api/customers/maple', json={'pst_number': 'NOPE', 'changelog': []}).status_code == 200
    assert load_customers()['maple']['pst_number'] == 'PST-200'
    assert load_customers()['maple']['changelog'][-1]['changes'][0]['new'] == 'PST-200'
    assert client.delete('/api/customers/maple/tax-number').status_code == 405
    pdf = client.get('/customers/maple/tax-numbers.pdf')
    assert pdf.status_code == 200
    assert pdf.mimetype == 'application/pdf'
    assert pdf.data.startswith(b'%PDF')
    client.post('/logout')
    assert login(client, 'lee@maple.example').status_code == 302
    own = client.get('/company')
    assert b'Change PST number' in own.data
    assert b'not asked for a US resale certificate' not in own.data
    assert b'instead of a US resale certificate' not in client.get('/dashboard').data
    assert client.get('/customers/maple/tax-numbers.pdf').status_code in {302, 403}


def test_removing_a_number_changes_new_documents_only(client):
    _codes()
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-tax', customer_name='Harbor Tax',
        customer_tier='T2', customer_subrole='management',
    )
    customers = load_customers()
    customers['harbor-tax'].update({
        'country': 'Canada', 'province': 'BC', 'pst_number': '', 'qst_number': '',
        'credit_limit': 100000, 'credit_approved': True, 'credit_currency': 'cad',
    })
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302

    def invoice(ship, description):
        posted = client.post('/api/invoices', json={
            'customer_id': 'harbor-tax', 'ship_province': ship,
            'lines': [{'description': description, 'qty': 1, 'unit_price': 40}],
        })
        assert posted.status_code == 200, posted.get_json()
        return posted.get_json()

    first = invoice('BC', 'Before a number')
    assert first['tax_label'] == 'GST + PST, shipped to British Columbia'
    assert first['qbo_tax_code'] == '7'
    assert first['pst_number'] == ''
    saskatchewan = invoice('SK', 'Saskatchewan')
    assert saskatchewan['tax_label'] == 'GST + PST, shipped to Saskatchewan'
    manitoba = invoice('MB', 'Manitoba')
    assert manitoba['tax_label'] == 'GST + PST, shipped to Manitoba'
    assert invoice('ON', 'Ontario')['tax_label'] == 'HST, shipped to Ontario'
    assert invoice('AB', 'Alberta')['tax_label'] == 'GST, shipped to Alberta'
    assert invoice('QC', 'Quebec')['tax_label'] == 'GST + QST, shipped to Quebec'

    added = _change(client, 'harbor-tax', 'pst_number', 'PST-55555', reason='Now registered')
    assert added.status_code == 200, added.get_json()
    registered = invoice('BC', 'With PST')
    assert registered['tax_label'] == 'GST only, shipped to British Columbia (PST number on file)'
    assert registered['pst_number'] == 'PST-55555'
    assert registered['qbo_tax_code'] == '5'
    kept = next(row for row in load_invoices() if row['id'] == first['id'])
    assert kept['tax_label'] == 'GST + PST, shipped to British Columbia'
    assert kept['pst_number'] == ''
    assert kept['qbo_tax_code'] == '7'

    quote = client.post('/api/quotes', json={
        'customer_id': 'harbor-tax',
        'items': [{
            'name': 'Lite',
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
            'sqf': 6, 'price_sqf': 10, 'price_pc': 60, 'pcs': 1, 'total': 60,
        }],
        'currency': 'cad', 'project': 'Tax quote', 'type': 'size', 'tier': 'T2', 'ship_province': 'BC',
    })
    assert quote.status_code == 200, quote.get_json()
    quote_id = quote.get_json()['id']
    ordered = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-tax', 'ship_province': 'BC'})
    assert ordered.status_code == 200, ordered.get_json()
    order = next(row for row in load_orders() if row['id'] == ordered.get_json()['order_id'])
    assert order['pst_number'] == 'PST-55555'
    assert 'GST only' in order['tax_label']
    removed = _change(client, 'harbor-tax', 'pst_number', reason='Registration cancelled', remove=True)
    assert removed.status_code == 200, removed.get_json()
    assert load_customers()['harbor-tax']['pst_number'] == ''
    after = invoice('BC', 'After removal')
    assert after['tax_label'] == 'GST + PST, shipped to British Columbia'
    assert after['pst_number'] == ''
    frozen_order = next(row for row in load_orders() if row['id'] == order['id'])
    assert frozen_order['pst_number'] == 'PST-55555'
    assert frozen_order['tax_label'] == order['tax_label']
    shown = client.get(f"/api/orders/{order['id']}").get_json()
    assert shown['tax_label'] == order['tax_label']
    assert shown['pst_number'] == 'PST-55555'
    confirmation = pdf_text(client.get(f"/api/orders/{order['id']}/confirmation.pdf").data)
    assert b'GST only' in confirmation
    assert b'GST + PST' not in confirmation
    quote_pdf = pdf_text(client.post('/api/export-quote', json={'id': quote_id}).data)
    assert b'GST + PST' in quote_pdf

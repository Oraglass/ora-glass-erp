"""A second imported file appends lines onto the same quote and its order."""

import io

from storage import append_invoice, load_orders
from tests.conftest import login, write_user
from tests.test_order_import import (
    CLEAR,
    SPACER,
    _file,
    _template,
    _excel_body,
    _upload,
)
from tests.test_orgadata_import import (
    COATED,
    SPACER as ORGADATA_SPACER,
    _open_windows,
    _upload as upload_orgadata,
    pdf_bytes,
    sample_text,
)

BLOCKS = [{
    'lites': [{'product': COATED, 'temper': 'yes'}, {'product': CLEAR, 'temper': 'yes'}],
    'spacers': [{'product': ORGADATA_SPACER}],
}]


def glass_lines(items):
    return [
        item for item in items or []
        if isinstance(item, dict) and item.get('kind') not in {'irregular_surcharge', 'dwg_creation'}
    ]


def _orgadata_quote(client, remember=True):
    uploaded = upload_orgadata(client, sample_text())
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    created = client.post(f"/api/orgadata-imports/{body['id']}/quote", json={
        'customer_id': 'open-windows',
        'remember': remember,
        'blocks': BLOCKS,
    })
    assert created.status_code == 200, created.get_json()
    return created.get_json()['quote']


def _post_file(client, quote_id, filename, payload):
    return client.post(
        f'/api/quotes/{quote_id}/import-file',
        data={'file': (io.BytesIO(payload), filename)},
        content_type='multipart/form-data',
    )


def test_pages_offer_import_another_file(client):
    write_user('admin@test.com', 'admin')
    login(client, 'admin@test.com')
    assert b'Import another file' in client.get('/quote-history').data
    assert b'Import another file' in client.get('/orders').data


def test_orgadata_appends_a_second_file_then_blocks_after_invoice(client):
    write_user('admin@test.com', 'admin')
    _open_windows()
    login(client, 'admin@test.com')
    quote = _orgadata_quote(client)
    assert quote['import_kind'] == 'orgadata'
    assert quote['can_append_file'] is True
    assert quote['tier']
    first = glass_lines(quote['items'])
    assert first[0]['line_number'] == 1
    assert first[-1]['line_number'] == len(first)
    assert first[0]['source_filename'] == 'glass-order.pdf'
    assert first[0]['spec']['unit'] == 'mm'
    assert first[0]['spec']['width'] > 100
    assert ' in' in first[0]['size_label'] and ' mm' in first[0]['size_label']
    kept_total = first[0]['total']
    kept_tier = quote['tier']
    discount = quote['additional_discount_amount']

    added = _post_file(client, quote['id'], 'second-order.pdf', pdf_bytes(sample_text()))
    assert added.status_code == 200, added.get_json()
    body = added.get_json()
    assert body['added'] == len(first)
    merged = body['quote']
    lines = glass_lines(merged['items'])
    assert len(lines) == len(first) * 2
    assert lines[0]['total'] == kept_total
    assert lines[0]['line_number'] == 1
    assert lines[len(first)]['line_number'] == len(first) + 1
    assert lines[len(first)]['source_filename'] == 'second-order.pdf'
    assert lines[len(first)]['spec']['unit'] == 'mm'
    assert ' in' in lines[len(first)]['size_label'] and ' mm' in lines[len(first)]['size_label']
    assert merged['tier'] == kept_tier
    assert merged['currency'] == quote['currency']
    assert merged['additional_discount_amount'] >= discount
    assert merged['source_filenames'] == ['glass-order.pdf', 'second-order.pdf']

    converted = client.post(f"/api/quotes/{quote['id']}/order", json={'customer_id': 'open-windows'})
    assert converted.status_code == 200, converted.get_json()
    order_id = converted.get_json()['order_id']
    before = client.get(f'/api/orders/{order_id}').get_json()
    before_lines = glass_lines(before['items'])
    assert before['can_append_file'] is True
    assert before_lines[0]['source_filename'] == 'glass-order.pdf'
    assert before_lines[-1]['line_number'] == len(before_lines)
    before_total = before_lines[0]['total']
    before_order_total = before['total']

    third = _post_file(client, quote['id'], 'third-order.pdf', pdf_bytes(sample_text()))
    assert third.status_code == 200, third.get_json()
    order = third.get_json()['order']
    order_lines = glass_lines(order['items'])
    assert len(order_lines) == len(before_lines) + len(first)
    assert order_lines[0]['total'] == before_total
    assert order_lines[len(before_lines)]['line_number'] == len(before_lines) + 1
    assert order_lines[len(before_lines)]['source_filename'] == 'third-order.pdf'
    assert order_lines[len(before_lines)]['spec']['unit'] == 'mm'
    assert order['tier'] == before['tier']
    assert order['total'] != before_order_total
    assert order['packing_serials'][-1]['serial'] == len(order_lines)
    quote_after = client.get(f"/api/quotes/{quote['id']}").get_json()
    assert len(glass_lines(quote_after['items'])) == len(order_lines)

    append_invoice({
        'order_id': order_id,
        'kind': 'customer',
        'doc_type': 'invoice',
        'issuer': 'ca',
        'voided': False,
        'total': 1,
    })
    blocked = _post_file(client, quote['id'], 'fourth-order.pdf', pdf_bytes(sample_text()))
    assert blocked.status_code == 400
    assert blocked.get_json()['error'] == 'This order has been invoiced.'
    stored = next(row for row in load_orders() if row['id'] == order_id)
    assert len(glass_lines(stored['items'])) == len(order_lines)
    invoiced = client.get(f'/api/orders/{order_id}').get_json()
    assert invoiced['append_blocked'] == 'This order has been invoiced.'
    assert invoiced['can_append_file'] is False


def test_orgadata_review_then_confirm_appends(client):
    write_user('admin@test.com', 'admin')
    _open_windows()
    login(client, 'admin@test.com')
    quote = _orgadata_quote(client, remember=False)
    count = len(glass_lines(quote['items']))
    review = _post_file(client, quote['id'], 'extra.pdf', pdf_bytes(sample_text()))
    assert review.status_code == 200, review.get_json()
    body = review.get_json()
    assert body['needs_review'] is True
    assert body['import_kind'] == 'orgadata'
    confirmed = client.post(f"/api/quotes/{quote['id']}/import-file", json={
        'import_id': body['import_id'],
        'remember': True,
        'blocks': BLOCKS,
    })
    assert confirmed.status_code == 200, confirmed.get_json()
    lines = glass_lines(confirmed.get_json()['quote']['items'])
    assert len(lines) == count * 2
    assert lines[count]['line_number'] == count + 1
    assert lines[count]['source_filename'] == 'extra.pdf'


def test_order_file_appends_with_the_same_template_and_tier(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass', customer_subrole='management')
    login(client, 'admin@oraglass.us')
    _template(client, _excel_body('harbor'))
    client.post('/logout')
    login(client, 'lee@harbor.example')
    uploaded = _upload(client, 'harbor-inches.xlsx', _file('order_inches.xlsx'), currency='usd')
    assert uploaded.status_code == 200, uploaded.get_json()
    body = uploaded.get_json()
    mapped = client.post(f"/api/order-imports/{body['id']}/map", json={
        'remember': True,
        'glass': {'Their Clear': CLEAR},
        'spacer': {'Warm edge 12': SPACER},
    })
    assert mapped.status_code == 200, mapped.get_json()
    ready = mapped.get_json()
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
            'irregular_shape': line['mark'] == 'W1',
        } for line in ready['lines']],
    })
    assert created.status_code == 200, created.get_json()
    quote_id = created.get_json()['id']
    quote = next(row for row in client.get('/api/quotes').get_json() if row['id'] == quote_id)
    assert quote['import_kind'] == 'order'
    assert quote['can_append_file'] is True
    first = glass_lines(quote['items'])
    kept = first[0]['total']
    added = _post_file(client, quote_id, 'second-harbor.xlsx', _file('order_inches.xlsx'))
    assert added.status_code == 200, added.get_json()
    merged = added.get_json()['quote']
    lines = glass_lines(merged['items'])
    assert len(lines) == len(first) * 2
    assert lines[0]['total'] == kept
    assert lines[0]['line_number'] == 1
    assert lines[len(first)]['line_number'] == len(first) + 1
    assert lines[0]['source_filename'] == 'harbor-inches.xlsx'
    assert lines[len(first)]['source_filename'] == 'second-harbor.xlsx'
    assert lines[len(first)]['spec']['unit'] == 'in'
    assert ' in' in lines[len(first)]['size_label'] and ' mm' in lines[len(first)]['size_label']
    assert merged['tier'] == quote['tier'] == 'T2'

    plain = client.post('/api/quotes', json={
        'items': [{
            'name': 'Clear',
            'pcs': 1,
            'spec': {'glass1': CLEAR, 'temper1': 'n/a', 'spacer1': '', 'sealant1': '', 'width': 24, 'height': 36, 'unit': 'in'},
        }],
        'currency': 'usd',
        'type': 'size',
        'tier': 'T2',
        'project': 'Typed',
    })
    assert plain.status_code == 200, plain.get_json()
    refused = _post_file(client, plain.get_json()['id'], 'second-harbor.xlsx', _file('order_inches.xlsx'))
    assert refused.status_code == 400
    assert 'imported file' in refused.get_json()['error']

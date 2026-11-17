"""A job address is recorded on the quote, copied to the order, and does not set tax."""

from storage import load_customers, load_orders, load_quotes, save_customers
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text

ADDRESS = '10 Job Road\nToronto, ON M5V 1A1\nCanada'


def _company(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-job',
        customer_name='Harbor Job', customer_tier='T2', customer_subrole='management',
    )
    write_user(
        'lee@harbor.example', 'customer', customer_id='harbor-job',
        customer_name='Harbor Job', customer_tier='T2', customer_subrole='sales',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple-job',
        customer_name='Maple Job', customer_tier='T2', customer_subrole='management',
    )
    write_user('pat@oraglass.us', 'sales')
    customers = load_customers()
    customers['harbor-job'].update({
        'country': 'Canada', 'province': 'BC', 'credit_limit': 100000,
        'credit_approved': True, 'credit_currency': 'cad', 'address': '1 Billing St',
        'city': 'Vancouver',
    })
    save_customers(customers)


def _quote(client):
    saved = client.post('/api/quotes', json={
        'items': [{
            'name': 'Lite',
            'spec': {
                'glass1': 'Clear Glass 4mm', 'temper1': 'n/a',
                'width': 24, 'height': 36, 'unit': 'in',
            },
            'sqf': 6, 'price_sqf': 10, 'price_pc': 60, 'pcs': 1, 'total': 60,
        }],
        'currency': 'cad', 'project': 'Cane', 'type': 'size', 'tier': 'T2',
        'job_address': ADDRESS, 'ship_province': 'ON',
    })
    assert saved.status_code == 200, saved.get_json()
    return next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])


def test_job_address_follows_the_quote_and_not_the_tax_province(client):
    _company(client)
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'Job address' in page.data
    assert b'id="job-address"' in page.data
    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    billing = client.post('/api/quotes', json={
        'items': [{
            'name': 'Lite',
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 36, 'unit': 'in'},
            'sqf': 6, 'price_sqf': 10, 'price_pc': 60, 'pcs': 1, 'total': 60,
        }],
        'currency': 'cad', 'project': 'Billing', 'type': 'size', 'tier': 'T2',
        'job_address': ADDRESS,
    })
    assert billing.status_code == 200, billing.get_json()
    billed = next(row for row in load_quotes() if row['id'] == billing.get_json()['id'])
    assert billed['job_address'] == ADDRESS
    assert billed['ship_province'] == 'BC'
    quote = _quote(client)
    assert quote['job_address'] == ADDRESS
    assert quote['ship_province'] == 'ON'
    tax = quote.get('tax_label') or ''
    assert 'Toronto' not in (quote.get('ship_province') or '')
    copied = client.post(f"/api/quotes/{quote['id']}/copy", json={})
    assert copied.status_code == 200, copied.get_json()
    fresh = next(row for row in load_quotes() if row['id'] == copied.get_json()['id'])
    assert fresh['job_address'] == ADDRESS
    assert fresh['ship_province'] == 'ON'
    assert next(row for row in load_quotes() if row['id'] == quote['id'])['job_address'] == ADDRESS
    pdf = pdf_text(client.post('/api/export-quote', json={'id': quote['id']}).data)
    assert b'10 Job Road' in pdf
    assert b'Toronto, ON M5V 1A1' in pdf
    assert b'Cane' in pdf
    created = client.post(f"/api/quotes/{quote['id']}/order", json={'ship_province': 'ON'})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['job_address'] == ADDRESS
    assert order['ship_province'] == 'ON'
    assert (order.get('tax_label') or '') == tax
    confirmation = pdf_text(client.get(f'/api/orders/{order_id}/confirmation.pdf').data)
    assert b'10 Job Road' in confirmation
    changed = client.put(f'/api/orders/{order_id}', json={'job_address': '99 New Site\nOttawa, ON'})
    assert changed.status_code == 200, changed.get_json()
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['job_address'] == '99 New Site\nOttawa, ON'
    assert order['ship_province'] == 'ON'
    assert (order.get('tax_label') or '') == tax
    assert next(row for row in load_quotes() if row['id'] == quote['id'])['job_address'] == ADDRESS
    log = order['changelog'][-1]
    assert log['user'] == 'boss@harbor.example'
    assert log['changes'][0]['field'] == 'job_address'
    assert '10 Job Road' in log['changes'][0]['old']
    assert log['changes'][0]['new'] == '99 New Site\nOttawa, ON'
    screen = client.get(f'/orders/{order_id}')
    assert b'job-address-edit' in screen.data
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    assert client.put(f'/api/orders/{order_id}', json={'job_address': 'Nope'}).status_code in {302, 403}
    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    assert client.put(f'/api/orders/{order_id}', json={'job_address': 'Nope'}).status_code in {302, 403, 404}
    client.post('/logout')
    assert login(client, 'pat@oraglass.us').status_code == 302
    refused = client.put(f'/api/orders/{order_id}', json={'project': 'Renamed', 'job_address': 'Staff site'})
    assert refused.status_code == 403
    allowed = client.put(f'/api/orders/{order_id}', json={'job_address': 'Staff site\nCalgary'})
    assert allowed.status_code == 200, allowed.get_json()
    order = next(row for row in load_orders() if row['id'] == order_id)
    assert order['job_address'] == 'Staff site\nCalgary'
    assert order['project'] == 'Cane'
    assert order['ship_province'] == 'ON'
    assert next(row for row in load_quotes() if row['id'] == quote['id'])['project'] == 'Cane'

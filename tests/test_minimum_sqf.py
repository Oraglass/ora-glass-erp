"""A company minimum charges a small piece as that many square feet."""

from openpyxl import load_workbook
import io

from production import export_lines
from shipments import shipment_lines
from storage import load_customers, load_orders, load_quotes, save_customers
from tests.conftest import login, write_user


def _customer(client, minimum):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/customers', json={
        'name': 'Harbor Glass', 'tier': 'T2', 'minimum_sqf_per_piece': minimum,
        'country': 'United States', 'credit_limit': 100000, 'credit_approved': True,
    })
    assert created.status_code == 200, created.get_json()
    return created.get_json()['id']


def _line(pcs=3):
    return {
        'name': 'Small lite',
        'spec': {
            'glass1': 'Clear Glass 4mm', 'temper1': 'n/a',
            'width': 24, 'height': 12, 'unit': 'in',
        },
        'sqf': 2, 'price_sqf': 10, 'price_pc': 20, 'pcs': pcs, 'total': 20 * pcs,
    }


def test_three_small_pieces_are_charged_as_eighteen_square_feet(client):
    from tests.test_quote_export import pdf_text
    customer_id = _customer(client, 6)
    saved = client.post('/api/quotes', json={
        'items': [_line()], 'currency': 'usd', 'project': 'Minimum', 'type': 'size',
        'tier': 'T2', 'customer_id': customer_id,
    })
    assert saved.status_code == 200, saved.get_json()
    quote = next(row for row in load_quotes() if row['project'] == 'Minimum')
    line = quote['items'][0]
    assert line['real_sqf'] == 2
    assert line['minimum_sqf'] == 6
    assert line['sqf'] == 6
    assert line['price_sqf'] == 10
    assert line['price_pc'] == 60
    assert line['total'] == 180
    pdf = client.post('/api/export-quote', json={
        'items': quote['items'], 'currency': 'usd', 'project': 'Minimum',
    })
    text = pdf_text(pdf.data)
    assert b'Total SQ/FT' in text
    assert b'18.00' in text
    assert b'QTY' in text

    customers = load_customers()
    customers[customer_id]['minimum_sqf_per_piece'] = 9
    save_customers(customers)
    again = next(row for row in load_quotes() if row['id'] == quote['id'])
    assert again['items'][0]['sqf'] == 6
    assert again['items'][0]['total'] == 180

    created = client.post(f"/api/quotes/{quote['id']}/order", json={'customer_id': customer_id})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    ordered = order['items'][0]
    assert ordered['minimum_sqf'] == 9
    assert ordered['sqf'] == 9
    assert ordered['real_sqf'] == 2
    assert ordered['pcs'] == 3
    built, pieces, area = shipment_lines(order, {0: 3})
    assert pieces == 3
    assert area == 27
    assert built[0]['total'] == ordered['total']
    view = client.get('/api/orders').get_json()
    listed = next(row for row in view if row['id'] == order['id'])
    assert listed['items'][0]['sqf'] == 9
    exported = export_lines(order['items'], 'usd', 1)
    assert abs(exported[0]['Width(In)'] - 24) < 0.001
    assert abs(exported[0]['Height(In)'] - 12) < 0.001
    assert exported[0]['Quantity'] == 3
    assert exported[0]['Unit Price'] == ordered['price_sqf']
    book = load_workbook(io.BytesIO(
        __import__('production').workbook_bytes(order['items'], 'usd', 1)
    )).active
    headers = [cell.value for cell in next(book.iter_rows(min_row=1, max_row=1))]
    values = [cell.value for cell in next(book.iter_rows(min_row=2, max_row=2))]
    row = dict(zip(headers, values))
    assert abs(row['Width(In)'] - 24) < 0.001
    assert abs(row['Height(In)'] - 12) < 0.001
    assert row['Quantity'] == 3
    assert row['Unit Price'] == ordered['price_sqf']


def test_company_minimum_overrides_the_global_one_and_blank_falls_back(client):
    import pricing_catalog
    customer_id = _customer(client, 0)
    pricing_catalog.update_formula({'minimum_sqf': 4}, 'admin@oraglass.us')
    saved = client.post('/api/quotes', json={
        'items': [_line(pcs=1)], 'currency': 'usd', 'project': 'Global', 'type': 'size',
        'tier': 'T2', 'customer_id': customer_id,
    })
    assert saved.status_code == 200, saved.get_json()
    line = next(row for row in load_quotes() if row['project'] == 'Global')['items'][0]
    assert line['real_sqf'] == 2
    assert line['sqf'] == 4
    assert line['minimum_sqf'] == 4
    customers = load_customers()
    customers[customer_id]['minimum_sqf_per_piece'] = 6
    save_customers(customers)
    override = client.post('/api/quotes', json={
        'items': [_line(pcs=1)], 'currency': 'usd', 'project': 'Company', 'type': 'size',
        'tier': 'T2', 'customer_id': customer_id,
    })
    assert override.status_code == 200, override.get_json()
    company_line = next(row for row in load_quotes() if row['project'] == 'Company')['items'][0]
    assert company_line['sqf'] == 6
    assert company_line['minimum_sqf'] == 6
    plain = client.post('/api/quotes', json={
        'items': [{'name': 'Clear', 'price': 4, 'total': 4}],
        'currency': 'usd', 'project': 'Per square foot', 'type': 'sqf', 'tier': 'T2',
        'customer_id': customer_id,
    })
    assert plain.status_code == 200, plain.get_json()
    per_sqf = next(row for row in load_quotes() if row['project'] == 'Per square foot')['items'][0]
    assert per_sqf['total'] == 4
    assert 'minimum_sqf' not in per_sqf


def test_customer_sees_the_minimum_and_cannot_edit_company_settings(client):
    customer_id = _customer(client, 6)
    write_user(
        'lee@harbor.example', 'customer', customer_id=customer_id,
        customer_name='Harbor Glass', customer_tier='T2',
    )
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    page = client.get('/quotes')
    assert b'Minimum sq ft per piece' not in page.data
    assert b'charged as 6' in page.data
    assert b'Total sqf' in page.data
    assert client.put(f'/api/customers/{customer_id}', json={'minimum_sqf_per_piece': 1}).status_code in {302, 403}
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    settings = client.get('/customers')
    assert b'Minimum sq ft per piece' in settings.data
    assert b'c-minimum-sqf' in settings.data
    staff_quote = client.get('/quotes')
    assert b'data-minimum="6' in staff_quote.data

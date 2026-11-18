"""Production orders can be printed, and staff can download the production Excel."""

from storage import load_customers, load_orders, save_customers, save_orders
from tests.conftest import login, write_user
from tests.test_production_orders import SPEC, _size_item
from tests.test_quote_export import pdf_text


def test_pacific_approval_time():
    from app import format_pacific
    assert format_pacific('2026-11-18T15:37:02+00:00') == '2026-11-18 7:37 AM PT'
    assert format_pacific('') == ''


def _people():
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@highq.example', 'customer', customer_id='highq',
        customer_name='HighQ Developments Inc', customer_tier='T2', customer_subrole='management',
    )
    write_user(
        'ops@highq.example', 'customer', customer_id='highq',
        customer_name='HighQ Developments Inc', customer_tier='T2', customer_subrole='sales',
    )
    write_user(
        'other@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
    )
    write_user('staff@oraglass.us', 'employee')
    from storage import load_users, save_users
    users = load_users()
    users['staff@oraglass.us']['modules'] = ['quotes', 'quote-history', 'orders']
    save_users(users)
    customers = load_customers()
    customers['highq'].update({
        'country': 'Canada', 'province': 'BC', 'status': 'active',
        'credit_limit': 100000, 'credit_currency': 'cad', 'credit_approved': True,
    })
    customers['harbor-glass'].update({
        'country': 'United States', 'status': 'active',
        'credit_limit': 100000, 'credit_currency': 'usd', 'credit_approved': True,
    })
    save_customers(customers)


def test_customers_print_their_confirmation_and_staff_download_excel(client):
    _people()
    assert login(client, 'boss@highq.example').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [_size_item()],
        'currency': 'cad',
        'project': 'Misc',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    created = client.post(f"/api/quotes/{saved.get_json()['id']}/order", json={})
    assert created.status_code == 200, created.get_json()
    order_id = created.get_json()['order_id']
    orders = load_orders()
    for order in orders:
        if order['id'] == order_id:
            order['approved_at'] = '2026-11-18T15:37:02+00:00'
    save_orders(orders)

    listed = client.get('/api/orders').get_json()
    assert listed[0]['approved_at_display'] == '2026-11-18 7:37 AM PT'
    page = client.get('/orders')
    assert b'Print / Download PDF' in page.data
    assert b'const STAFF = false' in page.data
    assert b'confirmation.pdf' in page.data
    pdf = client.get(f'/api/orders/{order_id}/confirmation.pdf')
    assert pdf.status_code == 200
    assert pdf.mimetype == 'application/pdf'
    text = pdf_text(pdf.data)
    assert b'Order Confirmation' in text or b'Confirmation' in text
    assert b'GST#' in text
    assert b'729631119 RT0001' in text
    assert b'Canada' in text
    assert b'USD/CAD' not in text
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 403
    client.post('/logout')

    assert login(client, 'ops@highq.example').status_code == 302
    ops_page = client.get('/orders')
    assert ops_page.status_code == 200
    assert b'Print / Download PDF' in ops_page.data
    assert b'const STAFF = false' in ops_page.data
    ops_pdf = client.get(f'/api/orders/{order_id}/confirmation.pdf')
    assert ops_pdf.status_code == 200
    assert ops_pdf.data.startswith(b'%PDF')
    assert client.get(f'/api/orders/{order_id}/production.xlsx').status_code == 403
    client.post('/logout')

    assert login(client, 'other@harbor.example').status_code == 302
    assert client.get(f'/api/orders/{order_id}/confirmation.pdf').status_code == 404
    client.post('/logout')

    assert login(client, 'staff@oraglass.us').status_code == 302
    staff_page = client.get('/orders')
    assert b'Print / Download PDF' in staff_page.data
    assert b'Download production Excel' in staff_page.data
    assert b'const STAFF = true' in staff_page.data
    detail = client.get(f'/orders/{order_id}')
    assert b'id="order-confirmation"' in detail.data or b'Print / Download PDF' in detail.data
    assert b'id="production-excel"' in detail.data or b'Download production Excel' in detail.data
    assert client.post(f'/api/orders/{order_id}/approve-production', json={}).status_code == 200
    workbook = client.get(f'/api/orders/{order_id}/production.xlsx')
    assert workbook.status_code == 200
    assert workbook.data.startswith(b'PK')
    assert SPEC['glass1'] == 'Clear Glass 4mm'

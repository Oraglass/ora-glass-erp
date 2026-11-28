"""An admin can open a customer portal without taking over that user's account."""

import io

from impersonation import load_audit
from storage import load_customers, load_orders, load_quotes, load_users, save_customers, save_users
from tests.conftest import login, write_user
from tests.test_production_orders import _size_item

LABEL = 'Created by Ada Admin (admin) on behalf of Casey Sales'
ORDER_LABEL = 'Created by Ada Admin (admin) on behalf of Blair Manager'


def _names():
    users = load_users()
    users['admin@oraglass.us']['name'] = 'Ada Admin'
    users['casey@harbor.example']['name'] = 'Casey Sales'
    users['boss@harbor.example']['name'] = 'Blair Manager'
    save_users(users)


def _company():
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'casey@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='sales',
    )
    write_user(
        'other@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='sales',
    )
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', customer_subrole='management',
    )
    customers = load_customers()
    customers['harbor-glass'].update({'credit_limit': 100000, 'credit_approved': True, 'credit_currency': 'usd'})
    customers['empty-glass'] = {
        'id': 'empty-glass', 'name': 'Empty Glass', 'status': 'active', 'tier': 'T2',
        'country': 'United States',
    }
    save_customers(customers)
    _names()
    return load_users()['casey@harbor.example']['password_hash']


def test_admin_login_offers_a_choice_and_can_continue(client):
    _company()
    signed = login(client, 'admin@oraglass.us')
    assert signed.status_code == 302
    assert signed.headers['Location'].endswith('/enter')
    page = client.get('/enter')
    assert b'Continue to ERP as admin' in page.data
    assert b'id="continue-admin" autofocus' in page.data
    assert b'This customer has no portal users.' in page.data
    assert b'Viewing as' not in page.data
    continued = client.post('/enter/continue')
    assert continued.status_code == 302
    assert continued.headers['Location'].endswith('/dashboard')
    home = client.get('/dashboard')
    assert b'id="impersonation-banner"' not in home.data
    assert b'Users' in home.data


def test_impersonation_keeps_records_on_the_customer_and_labels_the_admin(client):
    password_hash = _company()
    assert login(client, 'admin@oraglass.us').status_code == 302
    denied_target = client.post('/impersonate', json={'account': 'admin@oraglass.us'})
    assert denied_target.status_code == 400
    started = client.post('/impersonate', data={'account': 'casey@harbor.example'})
    assert started.status_code == 302
    portal = client.get('/dashboard')
    assert b'Viewing as Casey Sales (Harbor Glass) - Return to admin' in portal.data
    assert b'QuickBooks' not in portal.data
    assert b'Users' not in portal.data
    customers = client.get('/customers')
    assert customers.status_code == 302

    saved = client.post('/api/quotes', json={
        'items': [_size_item()], 'currency': 'usd', 'project': 'Lobby', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    quote_id = saved.get_json()['id']
    quote = next(row for row in load_quotes() if row['id'] == quote_id)
    assert quote['user'] == 'casey@harbor.example'
    assert quote['customer_id'] == 'harbor-glass'
    assert quote['by_admin'] is True
    assert quote['acting_label'] == LABEL
    assert quote['created_by_admin'] == 'admin@oraglass.us'

    uploaded = client.post('/api/order-imports', data={
        'file': (io.BytesIO(b'Width,Height\n24,36\n'), 'form.csv'),
        'project': 'Lobby',
    })
    assert uploaded.status_code == 200, uploaded.get_json()
    assert uploaded.get_json()['acting_label'] == LABEL
    assert uploaded.get_json()['by_admin'] is True

    copied = client.post(f'/api/quotes/{quote_id}/copy', json={})
    assert copied.status_code == 200, copied.get_json()
    copy = next(row for row in load_quotes() if row['id'] == copied.get_json()['id'])
    assert copy['user'] == 'casey@harbor.example'
    assert copy['customer_id'] == 'harbor-glass'
    assert copy['acting_label'] == LABEL

    client.post('/impersonate/stop')
    assert client.post('/impersonate', data={'account': 'boss@harbor.example'}).status_code == 302
    created = client.post(f'/api/quotes/{quote_id}/order', json={})
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    assert order['customer_id'] == 'harbor-glass'
    assert order['approved_by'] == 'boss@harbor.example'
    assert order['converted_by'] == 'boss@harbor.example'
    assert order['acting_label'] == ORDER_LABEL
    assert order['changelog'][0]['changes'][0]['new'] == ORDER_LABEL

    casey = load_users()['casey@harbor.example']
    assert casey['password_hash'] == password_hash
    assert casey.get('must_change_password') in (False, None)
    assert casey.get('invite_token_hash') in (None, '')

    actions = [row['action'] for row in load_audit()]
    assert actions[0] == 'start'
    assert 'quote' in actions
    assert 'import' in actions
    assert 'order' in actions
    assert 'stop' in actions
    assert all(row['admin'] == 'admin@oraglass.us' for row in load_audit())
    by_user = {row['action']: row['user'] for row in load_audit()}
    assert by_user['quote'] == 'casey@harbor.example'
    assert by_user['import'] == 'casey@harbor.example'
    assert by_user['order'] == 'boss@harbor.example'

    client.post('/logout')
    assert login(client, 'casey@harbor.example').status_code == 302
    seen = client.get(f'/api/quotes/{quote_id}')
    assert seen.status_code == 200
    assert seen.get_json()['acting_label'] == LABEL
    assert seen.get_json()['by_admin'] is True
    history = client.get('/quote-history')
    assert b'by admin' in history.data
    assert b'acting-label' in history.data
    listed = client.get('/api/quotes').get_json()
    assert any(row['id'] == quote_id and row['by_admin'] and row['acting_label'] == LABEL for row in listed)
    order_view = client.get(f"/api/orders/{order['id']}")
    assert order_view.status_code == 200
    assert order_view.get_json()['acting_label'] == ORDER_LABEL
    assert order_view.get_json()['by_admin'] is True
    assert order_view.get_json()['changelog'][0]['changes'][0]['new'] == ORDER_LABEL
    orders_page = client.get('/orders')
    assert b'by admin' in orders_page.data
    assert b'acting-label' in orders_page.data

    client.post('/logout')
    assert login(client, 'other@harbor.example').status_code == 302
    hidden = client.get(f'/api/quotes/{quote_id}')
    assert hidden.status_code == 404
    shared_order = client.get(f"/api/orders/{order['id']}")
    assert shared_order.get_json()['acting_label'] == ORDER_LABEL

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    management = client.get(f'/api/quotes/{quote_id}')
    assert management.get_json()['acting_label'] == LABEL


def test_return_restores_admin_without_a_password_and_blocks_everyone_else(client):
    _company()
    users = load_users()
    users['casey@harbor.example']['must_change_password'] = True
    save_users(users)
    before = load_users()['casey@harbor.example']['password_hash']
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post('/impersonate', data={'account': 'casey@harbor.example'}).status_code == 302
    quotes = client.get('/quotes')
    assert quotes.status_code == 200
    assert b'account/password' not in quotes.data
    assert load_users()['casey@harbor.example']['password_hash'] == before
    assert load_users()['casey@harbor.example']['must_change_password'] is True
    stopped = client.post('/impersonate/stop')
    assert stopped.status_code == 302
    assert stopped.headers['Location'].endswith('/dashboard')
    home = client.get('/dashboard')
    assert b'id="impersonation-banner"' not in home.data
    assert b'Users' in home.data
    assert load_audit()[-1]['action'] == 'stop'
    assert load_users()['casey@harbor.example']['password_hash'] == before

    client.post('/logout')
    assert login(client, 'casey@harbor.example').headers['Location'].endswith('/account/password')
    blocked = client.post('/impersonate', json={'account': 'other@harbor.example'})
    assert blocked.status_code == 403
    choices = client.get('/api/impersonation/customers')
    assert choices.status_code == 403

    client.post('/logout')
    write_user('pat@oraglass.us', 'sales')
    assert login(client, 'pat@oraglass.us').status_code == 302
    assert client.post('/impersonate', json={'account': 'casey@harbor.example'}).status_code == 403
    assert b'Choose how to continue' not in client.get('/enter', follow_redirects=True).data

    client.post('/logout')
    write_user('shop@oraglass.us', 'shop')
    shop = login(client, 'shop@oraglass.us')
    assert shop.headers['Location'].endswith('/punch')


def test_customer_page_lists_portal_users_for_login_as(client):
    _company()
    assert login(client, 'admin@oraglass.us').status_code == 302
    client.post('/enter/continue')
    page = client.get('/customers')
    assert b'Log in as' in page.data
    assert b'This customer has no portal users.' in page.data
    assert b'data-login-as' in page.data
    rows = client.get('/api/impersonation/customers').get_json()
    harbor = next(row for row in rows if row['id'] == 'harbor-glass')
    assert {user['email'] for user in harbor['users']} == {
        'casey@harbor.example', 'other@harbor.example', 'boss@harbor.example',
    }
    empty = next(row for row in rows if row['id'] == 'empty-glass')
    assert empty['users'] == []

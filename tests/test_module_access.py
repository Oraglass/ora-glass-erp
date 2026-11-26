from app import (
    CUSTOMER_CEILING,
    LEGACY_ROLE_MODULES,
    MODULE_IDS,
    effective_module_ids,
)
from tests.conftest import login, write_user


def test_migration_preserves_existing_role_access():
    sales = {'role': 'sales', 'email': 's@oraglass.us'}
    production = {'role': 'production', 'email': 'p@oraglass.us'}
    viewer = {'role': 'viewer', 'email': 'v@oraglass.us'}
    customer = {'role': 'customer', 'email': 'c@example.com'}
    assert effective_module_ids(sales) == list(LEGACY_ROLE_MODULES['sales'])
    assert effective_module_ids(production) == list(LEGACY_ROLE_MODULES['production'])
    assert 'quotes' not in effective_module_ids(production)
    assert effective_module_ids(viewer) == list(LEGACY_ROLE_MODULES['viewer'])
    assert 'pricing' not in effective_module_ids(viewer)
    customer_modules = effective_module_ids(customer)
    assert customer_modules[0] == 'dashboard'
    # Both customer roles can open Production orders to print a confirmation.
    assert set(CUSTOMER_CEILING) <= set(customer_modules)
    assert 'orders' in customer_modules
    assert 'users' not in customer_modules
    assert 'pricing' not in customer_modules
    management = dict(customer, customer_subrole='management')
    assert 'orders' in effective_module_ids(management)
    assert effective_module_ids({'role': 'admin'}) == list(MODULE_IDS)


def test_employee_sees_only_ticked_modules(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    created = client.post('/api/users', json={
        'name': 'Riley',
        'email': 'riley@oraglass.us',
        'role': 'employee',
        'password': 'Validpass1',
        'temporary': False,
        'tier_override': 'T2',
        'allowed_tiers': ['T2'],
        'modules': ['quotes', 'orders'],
    })
    assert created.status_code == 200
    from storage import load_users
    stored = load_users()['riley@oraglass.us']
    assert stored['role'] == 'employee'
    assert stored['modules'] == ['quotes', 'orders']
    client.post('/logout')
    assert login(client, 'riley@oraglass.us').status_code == 302

    dashboard = client.get('/dashboard')
    assert dashboard.status_code in (302, 403)
    quotes = client.get('/quotes')
    assert quotes.status_code == 200
    assert b'href="/quotes"' in quotes.data
    assert b'href="/orders"' in quotes.data
    assert b'href="/pricing"' not in quotes.data
    assert b'href="/users"' not in quotes.data
    assert b'href="/inventory"' not in quotes.data

    pricing = client.get('/pricing')
    assert pricing.status_code == 302
    assert '/pricing' not in pricing.headers['Location']
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'}).status_code == 200
    assert client.get('/api/quotes').status_code == 403
    orders = client.get('/orders')
    assert orders.status_code == 200


def test_customer_ceiling_cannot_be_exceeded(client):
    from storage import load_customers, load_users, save_customers
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    customers = load_customers()
    customers['north-glass'] = {
        'id': 'north-glass', 'name': 'North Glass', 'tier': 'T2', 'status': 'active',
        'domain': '', 'address': '', 'city': '', 'state': '', 'zip': '', 'phone': '', 'email': '',
        'credit_approved': False, 'payment_terms': 'Net 30',
    }
    save_customers(customers)
    blocked = client.post('/api/users', json={
        'name': 'Pat',
        'email': 'pat@north.example',
        'role': 'customer',
        'customer_id': 'north-glass',
        'modules': ['quotes', 'users', 'pricing'],
    })
    assert blocked.status_code == 400
    assert b'not available to customer users' in blocked.data
    assert 'pat@north.example' not in load_users()
    created = client.post('/api/users', json={
        'name': 'Pat',
        'email': 'pat@north.example',
        'role': 'customer',
        'customer_id': 'north-glass',
        'password': 'Validpass1',
        'temporary': False,
        'modules': ['quotes', 'invoices'],
    })
    assert created.status_code == 200
    tampered = client.put('/api/users/pat@north.example', json={'modules': ['quotes', 'financial', 'inventory']})
    assert tampered.status_code == 400
    stored = load_users()['pat@north.example']
    assert stored['modules'] == ['quotes', 'invoices']
    client.post('/logout')
    assert login(client, 'pat@north.example').status_code == 302
    assert client.get('/quotes').status_code == 200
    assert client.get('/invoices').status_code == 200
    assert client.get('/financial').status_code == 302
    assert client.get('/inventory').status_code == 302
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'tier': 'T2'}).status_code == 200
    home = client.get('/dashboard')
    assert home.status_code == 200
    assert b'href="/quotes"' in home.data
    assert b'href="/financial"' not in home.data
    assert b'href="/users"' not in home.data


def test_users_list_shows_module_summary_and_only_admin_edits_access(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('sales@oraglass.us', 'sales')
    login(client, 'admin@oraglass.us')
    rows = client.get('/api/users').get_json()
    sales = next(row for row in rows if row['email'] == 'sales@oraglass.us')
    assert sales['role'] == 'employee'
    assert 'Quotes' in sales['modules_label']
    assert 'Pricing' not in sales['modules_label']
    admin = next(row for row in rows if row['email'] == 'admin@oraglass.us')
    assert admin['modules_label'] == 'All modules'
    page = client.get('/users')
    assert b'id="u-modules"' in page.data
    assert b'id="filter-company"' in page.data
    assert b'Ora Glass (internal staff/admins)' in page.data
    assert b'Shop workers' in page.data
    assert b'Ora Glass employee' in page.data
    client.post('/logout')
    login(client, 'sales@oraglass.us')
    denied = client.put('/api/users/sales@oraglass.us', json={'modules': ['quotes', 'pricing']})
    assert denied.status_code == 403


def test_new_module_key_is_not_granted_to_existing_users():
    """The version 1 grant is once. A module added after that stays off until it is ticked."""
    from app import MODULE_IDS, MODULES_VERSION, migrate_user_modules
    staff = {'role': 'employee', 'modules': ['quotes', 'orders']}
    assert migrate_user_modules(staff) is True
    assert staff['modules_version'] == MODULES_VERSION
    assert 'production-settings' in staff['modules']
    assert 'shipments' in staff['modules']
    assert 'racks' in staff['modules']
    assert 'quickbooks' not in staff['modules']
    staff['modules'] = [item for item in staff['modules'] if item != 'shipments']
    assert migrate_user_modules(staff, module_ids=list(MODULE_IDS) + ['future-widget']) is True
    assert 'shipments' not in staff['modules']
    assert 'future-widget' not in staff['modules']
    assert 'future-widget' in staff['known_modules']
    assert migrate_user_modules(staff, module_ids=list(MODULE_IDS) + ['future-widget']) is False

    clerk = {'role': 'employee', 'modules': ['quotes']}
    migrate_user_modules(clerk)
    assert 'production-settings' in clerk['modules']
    assert 'shipments' not in clerk['modules']
    assert 'quickbooks' not in clerk['modules']

    customer = {'role': 'customer', 'modules': ['quotes', 'invoices']}
    migrate_user_modules(customer)
    assert 'racks' in customer['modules']
    assert 'users' not in customer['modules']
    customer['modules'] = ['quotes', 'invoices']
    migrate_user_modules(customer, module_ids=list(MODULE_IDS) + ['future-widget'])
    assert customer['modules'] == ['quotes', 'invoices']
    assert 'racks' not in customer['modules']
    assert 'future-widget' not in customer['modules']

    driver = {'role': 'driver', 'modules': ['orders', 'racks']}
    migrate_user_modules(driver)
    assert driver['modules'] == []
    migrate_user_modules(driver, module_ids=list(MODULE_IDS) + ['future-widget'])
    assert driver['modules'] == []
    assert 'future-widget' not in driver['modules']


def test_checklist_matches_the_menu_and_new_modules_gate_their_pages(client):
    from storage import load_customers, save_customers
    write_user('admin@oraglass.us', 'admin')
    write_user('sales@oraglass.us', 'sales')
    login(client, 'admin@oraglass.us')
    page = client.get('/users')
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for label in ('Production settings', 'Shipments', 'Racks', 'QuickBooks', 'Invoices', 'Financials'):
        assert label in body
    assert 'Payroll and time are not in this list' in body
    assert 'Manage payroll' in body
    assert '"id": "payroll"' not in body
    created = client.post('/api/users', json={
        'name': 'Riley',
        'email': 'riley@oraglass.us',
        'role': 'employee',
        'password': 'Validpass1',
        'temporary': False,
        'tier_override': 'T2',
        'allowed_tiers': ['T2'],
        'modules': ['quotes'],
    })
    assert created.status_code == 200, created.get_json()
    client.post('/logout')
    assert login(client, 'riley@oraglass.us').status_code == 302
    assert client.get('/production-settings').status_code == 302
    assert client.get('/shipments').status_code == 302
    assert client.get('/racks').status_code == 302
    assert client.get('/settings/quickbooks').status_code == 302
    home = client.get('/quotes')
    assert b'href="/production-settings"' not in home.data
    assert b'href="/shipments"' not in home.data
    assert b'href="/racks"' not in home.data
    assert b'href="/settings/quickbooks"' not in home.data
    client.post('/logout')

    assert login(client, 'sales@oraglass.us').status_code == 302
    assert client.get('/production-settings').status_code == 200
    assert client.get('/shipments').status_code == 200
    assert client.get('/racks').status_code == 200
    assert client.get('/settings/quickbooks').status_code == 302
    sales_nav = client.get('/orders').get_data(as_text=True)
    assert 'href="/production-settings"' in sales_nav
    assert 'href="/shipments"' in sales_nav
    assert 'href="/racks"' in sales_nav
    assert 'href="/settings/quickbooks"' not in sales_nav
    client.post('/logout')

    write_user('pay@oraglass.us', 'employee')
    from storage import load_users, save_users
    users = load_users()
    users['pay@oraglass.us']['manage_payroll'] = True
    users['pay@oraglass.us']['modules'] = ['dashboard']
    users['pay@oraglass.us']['modules_version'] = 1
    users['pay@oraglass.us']['known_modules'] = ['dashboard']
    save_users(users)
    assert login(client, 'pay@oraglass.us').status_code == 302
    payroll_nav = client.get('/dashboard').get_data(as_text=True)
    assert 'href="/payroll"' in payroll_nav
    assert '>Payroll<' in payroll_nav
    client.post('/logout')

    customers = load_customers()
    customers['north-glass'] = {
        'id': 'north-glass', 'name': 'North Glass', 'tier': 'T2', 'status': 'active',
        'domain': '', 'address': '', 'city': '', 'state': '', 'zip': '', 'phone': '', 'email': '',
    }
    save_customers(customers)
    assert login(client, 'admin@oraglass.us').status_code == 302
    made = client.post('/api/users', json={
        'name': 'Pat',
        'email': 'pat@north.example',
        'role': 'customer',
        'customer_id': 'north-glass',
        'password': 'Validpass1',
        'temporary': False,
        'modules': ['quotes', 'racks'],
    })
    assert made.status_code == 200, made.get_json()
    blocked = client.post('/api/users', json={
        'name': 'Sam',
        'email': 'sam@north.example',
        'role': 'customer',
        'customer_id': 'north-glass',
        'password': 'Validpass1',
        'temporary': False,
        'modules': ['quotes'],
    })
    assert blocked.status_code == 200, blocked.get_json()
    client.post('/logout')
    assert login(client, 'pat@north.example').status_code == 302
    assert client.get('/racks').status_code == 200
    client.post('/logout')
    assert login(client, 'sam@north.example').status_code == 302
    assert client.get('/racks').status_code == 302

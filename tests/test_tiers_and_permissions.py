import json

from quoting import allowed_tier_ids, calculate_price_sqf, resolve_effective_tier, tier_for_request
from seed_data import SEED_USERS, apply_seed
from security import hash_invite_token, verify_password
from storage import load_customers, load_quotes, load_tiers, load_users, save_users
from tests.conftest import login, write_user


def test_allowed_tiers_include_company_and_default_and_block_the_rest():
    customers = {'c': {'tier': 'T2'}}
    valid = {'T1': {}, 'T2': {}, 'T3': {}, 'T4': {}, 'T5': {}}
    legacy = {'role': 'customer', 'customer_id': 'c', 'tier_override': 'T5'}
    assert allowed_tier_ids(legacy, customers, valid) == ['T2', 'T5']
    assert tier_for_request(legacy, 'T5', customers, valid) == 'T5'
    assert tier_for_request(legacy, 'T2', customers, valid) == 'T2'
    assert tier_for_request(legacy, 'T1', customers, valid) is None
    explicit = dict(legacy, allowed_tiers=['T4', 'T5'])
    assert allowed_tier_ids(explicit, customers, valid) == ['T4', 'T5']
    assert tier_for_request(explicit, 'T4', customers, valid) == 'T4'
    assert tier_for_request(explicit, 'T2', customers, valid) is None
    assert tier_for_request(explicit, 'T3', customers, valid) is None
    assert tier_for_request(explicit, '', customers, valid) == 'T5'
    staff = {'role': 'sales', 'tier_override': 'T1', 'allowed_tiers': ['T1']}
    assert tier_for_request(staff, 'T5', {}, valid) == 'T5'
    assert tier_for_request(staff, 'NOPE', {}, valid) == 'T1'


def test_resolve_effective_tier_override_then_customer_then_default():
    customers = {'c': {'tier': 'T5'}}
    valid = {'T1': {}, 'T2': {}, 'T5': {}}
    assert resolve_effective_tier({'tier_override': 'T2', 'customer_id': 'c'}, customers, valid) == 'T2'
    assert resolve_effective_tier({'tier_override': '', 'customer_id': 'c'}, customers, valid) == 'T5'
    assert resolve_effective_tier({'tier_override': None, 'customer_id': 'c'}, customers, valid) == 'T5'
    assert resolve_effective_tier({'tier_override': 'NOPE', 'customer_id': 'c'}, customers, valid) == 'T5'
    assert resolve_effective_tier({'role': 'admin'}, {}, valid) == 'T2'


def test_calculate_uses_glass_and_spacer_markups_separately():
    pricing = [
        {'name': 'Clear Glass 4mm', 'base_price': 1.0},
        {'name': 'Superspacer 10mm / sqf', 'base_price': 1.0},
    ]
    tiers = {'T2': {'glass_markup': 0.40, 'spacer_markup': 0.20}}
    combined = calculate_price_sqf(
        {'glass1': 'Clear Glass 4mm', 'spacer1': 'Superspacer 10mm / sqf', 'temper1': 'n/a'},
        'T2', pricing, tiers,
    )
    tempered = calculate_price_sqf(
        {'glass1': 'Clear Glass 4mm', 'temper1': 'yes'},
        'T2', pricing, tiers,
    )
    assert combined == 2.60
    assert tempered == 1.90


def test_end_user_is_existing_t5_markup(data_dir):
    from storage import save_tiers
    save_tiers({
        'T1': {'label': 'Tier 1', 'glass_markup': 0.30, 'spacer_markup': 0.20},
        'T5': {'label': 'End User', 'glass_markup': 4.25, 'spacer_markup': 4.25},
        'EU': {'label': 'End User', 'glass_markup': 1.0, 'spacer_markup': 1.0, 'placeholder': True},
    })
    tiers = load_tiers()
    assert 'EU' not in tiers
    assert set(tiers) == {'T1', 'T2', 'T3', 'T4', 'T5'}
    assert tiers['T5']['label'] == 'Tier 5 (End User)'
    assert tiers['T5']['glass_markup'] == 4.25
    assert tiers['T5']['spacer_markup'] == 4.25
    assert tiers['T1']['glass_markup'] == 0.30
    assert tiers['T1']['spacer_markup'] == 0.20


def test_seed_has_no_passwords_and_assigns_tiers(data_dir):
    apply_seed()
    users = load_users()
    customers = load_customers()
    tiers = load_tiers()
    assert customers['open-windows']['name'] == 'Open Windows'
    assert customers['open-windows']['domain'] == 'open-windows.ca'
    assert customers['open-windows']['tier'] == 'T2'
    assert 'alex@oraglass.us' not in users
    assert users['alex@open-windows.ca']['tier_override'] == 'T5'
    assert users['kiu@open-windows.ca']['tier_override'] == 'T5'
    assert users['vlad@open-windows.ca']['tier_override'] == 'T5'
    assert users['support@open-windows.ca']['tier_override'] == 'T5'
    assert resolve_effective_tier(users['alex@open-windows.ca'], customers, tiers) == 'T5'
    assert resolve_effective_tier(users['kiu@open-windows.ca'], customers, tiers) == 'T5'
    assert resolve_effective_tier(users['vlad@open-windows.ca'], customers, tiers) == 'T5'
    assert resolve_effective_tier(users['support@open-windows.ca'], customers, tiers) == 'T5'
    for seeded in SEED_USERS:
        assert allowed_tier_ids(users[seeded['email']], customers, tiers) == ['T2', 'T5']
    assert tiers['T5']['label'] == 'Tier 5 (End User)'
    for spec in SEED_USERS:
        user = users[spec['email']]
        assert user['password_hash'] is None
        assert user['customer_id'] == 'open-windows'
    apply_seed()
    assert load_users()['kiu@open-windows.ca']['password_hash'] is None


def test_seed_renames_old_alex_address_and_folds_duplicate_tier(data_dir):
    from seed_data import OPEN_WINDOWS_ID, new_user
    from storage import save_customers, save_users
    save_customers({
        OPEN_WINDOWS_ID: {
            'id': OPEN_WINDOWS_ID,
            'name': 'Open Windows',
            'domain': 'open-windows.ca',
            'tier': 'EU',
            'status': 'active',
        }
    })
    save_users({
        'alex@oraglass.us': new_user(
            email='alex@oraglass.us',
            name='Alex',
            role='customer',
            customer_id=OPEN_WINDOWS_ID,
            tier_override='T2',
        )
    })
    apply_seed()
    users = load_users()
    assert 'alex@oraglass.us' not in users
    assert users['alex@open-windows.ca']['tier_override'] == 'T5'
    assert users['alex@open-windows.ca']['password_hash'] is None
    assert users['kiu@open-windows.ca']['tier_override'] == 'T5'
    assert users['vlad@open-windows.ca']['tier_override'] == 'T5'
    assert users['support@open-windows.ca']['tier_override'] == 'T5'
    assert load_customers()[OPEN_WINDOWS_ID]['tier'] == 'T2'
    assert resolve_effective_tier(users['kiu@open-windows.ca'], load_customers(), load_tiers()) == 'T5'
    assert resolve_effective_tier(users['vlad@open-windows.ca'], load_customers(), load_tiers()) == 'T5'
    customers = load_customers()
    customers[OPEN_WINDOWS_ID]['tier'] = 'T3'
    save_customers(customers)
    users['alex@open-windows.ca']['tier_override'] = 'T1'
    save_users(users)
    apply_seed()
    assert load_customers()[OPEN_WINDOWS_ID]['tier'] == 'T3'
    assert load_users()['alex@open-windows.ca']['tier_override'] == 'T1'


def test_seed_revision_sets_t5_override_for_vlad_and_support(data_dir):
    from seed_data import OPEN_WINDOWS_ID, new_user
    from storage import save_customers, save_users
    save_customers({
        OPEN_WINDOWS_ID: {
            'id': OPEN_WINDOWS_ID,
            'name': 'Open Windows',
            'domain': 'open-windows.ca',
            'tier': 'T2',
            'status': 'active',
            'seed_revision': 2,
        }
    })
    save_users({
        'vlad@open-windows.ca': new_user(
            email='vlad@open-windows.ca', name='Vlad', role='customer',
            customer_id=OPEN_WINDOWS_ID, tier_override=None,
        ),
        'support@open-windows.ca': new_user(
            email='support@open-windows.ca', name='Open Windows Support', role='customer',
            customer_id=OPEN_WINDOWS_ID, tier_override=None,
        ),
    })
    apply_seed()
    users = load_users()
    assert users['vlad@open-windows.ca']['tier_override'] == 'T5'
    assert users['support@open-windows.ca']['tier_override'] == 'T5'
    assert load_customers()[OPEN_WINDOWS_ID]['tier'] == 'T2'
    users['support@open-windows.ca']['tier_override'] = 'T4'
    save_users(users)
    apply_seed()
    assert load_users()['support@open-windows.ca']['tier_override'] == 'T4'


def test_customer_cannot_choose_a_different_tier(client):
    write_user(
        'kiu@open-windows.ca', 'customer', customer_id='open-windows',
        customer_name='Open Windows', customer_tier='T5', tier_override='T2',
    )
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    page = client.get('/quotes')
    assert page.data.count(b'<select id="tier">') == 1
    assert page.data.count(b'<select id="s-tier">') == 1
    assert page.data.count(b'value="T2" selected') == 2
    assert page.data.count(b'value="T5"') == 2
    assert b'value="T1"' not in page.data
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'})
    assert blocked.status_code == 400
    assert b'price_sqf' not in blocked.data
    priced = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'})
    body = priced.get_json()
    assert priced.status_code == 200
    assert body['tier'] == 'T2'
    from app import PRICING
    expected = calculate_price_sqf({'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'}, 'T2', PRICING, load_tiers())
    other = calculate_price_sqf({'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'}, 'T1', PRICING, load_tiers())
    assert body['price_sqf'] == expected
    assert expected != other
    company_tier = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T5'})
    assert company_tier.get_json()['tier'] == 'T5'


def test_staff_can_choose_tier_including_end_user(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'<select id="tier">' in page.data
    assert b'Tier 5 (End User)' in page.data
    assert b'value="T5"' in page.data
    payload = {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T5'}
    response = client.post('/api/calculate', json=payload)
    body = response.get_json()
    assert body['tier'] == 'T5'
    from app import PRICING
    assert body['price_sqf'] == calculate_price_sqf(payload, 'T5', PRICING, load_tiers())


def test_customer_sees_only_own_company_quotes_and_storage_path(client, data_dir):
    write_user('admin@oraglass.us', 'admin')
    write_user('kiu@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows', customer_tier='T5')
    write_user('other@example.com', 'customer', customer_id='other-co', customer_name='Other Co', customer_tier='T1')
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '12.50'}],
        'currency': 'usd',
        'project': 'Open Job',
        'type': 'sqf',
        'tier': 'T5',
    })
    assert saved.status_code == 200
    on_disk = json.loads((data_dir / 'quotes.json').read_text())
    assert on_disk[0]['project'] == 'Open Job'
    assert on_disk[0]['customer_id'] == 'open-windows'
    assert on_disk[0]['company'] == 'Open Windows'
    assert on_disk[0]['user'] == 'kiu@open-windows.ca'
    assert on_disk[0]['tier'] == 'T5'
    client.post('/logout')
    assert login(client, 'other@example.com').status_code == 302
    client.post('/api/quotes', json={
        'items': [{'name': 'Other', 'price': '3.00'}],
        'currency': 'usd',
        'project': 'Secret Job',
        'type': 'sqf',
    })
    visible = client.get('/api/quotes').get_json()
    assert [quote['project'] for quote in visible] == ['Secret Job']
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    everything = client.get('/api/quotes').get_json()
    assert {quote['project'] for quote in everything} == {'Open Job', 'Secret Job'}
    assert len(load_quotes()) == 2


def test_permissions_and_anonymous_access(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('sales@oraglass.us', 'sales')
    write_user('kiu@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows', customer_tier='T5')

    anonymous = client.get('/api/quotes')
    assert anonymous.status_code == 401
    assert client.get('/quote-history').status_code == 302
    assert client.get('/invoices').status_code == 302
    assert client.get('/financial').status_code == 302
    assert '/login' in client.get('/quote-history').headers['Location']

    assert login(client, 'sales@oraglass.us').status_code == 302
    assert client.get('/api/customers').status_code == 200
    assert client.post('/api/customers', json={'name': 'Nope'}).status_code == 403
    assert client.post('/api/users', json={'name': 'Nope', 'email': 'x@y.z', 'role': 'viewer'}).status_code == 403
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/quotes/clear').status_code == 403
    pricing_denied = client.post('/pricing', data={'label_T2': 'Tier 2', 'glass_T2': '40', 'spacer_T2': '40'})
    assert pricing_denied.status_code == 302
    assert '/dashboard' in pricing_denied.headers['Location']
    client.post('/logout')

    assert login(client, 'kiu@open-windows.ca').status_code == 302
    assert client.get('/api/users').status_code == 403
    assert client.get('/customers').status_code == 302
    assert client.get('/api/customers').status_code == 403
    assert client.post('/api/users/admin@oraglass.us/invite', json={}).status_code == 403
    history = client.get('/quote-history')
    assert history.status_code == 200
    assert b'Open Windows' in history.data
    financial = client.get('/financial')
    assert financial.status_code == 200
    assert b'Open Windows' in financial.data
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/customers', json={'name': 'North Glass', 'tier': 'T3', 'domain': 'north.example'})
    assert created.status_code == 200
    assert created.get_json()['id'] == 'north-glass'
    created_user = client.post('/api/users', json={
        'name': 'Pat North',
        'email': 'pat@north.example',
        'role': 'customer',
        'customer_id': 'north-glass',
        'tier_override': 'T5',
    })
    body = created_user.get_json()
    assert created_user.status_code == 200
    assert body['setup_url'].startswith('http')
    assert 'password' not in body
    stored = load_users()['pat@north.example']
    assert stored['password_hash'] is None
    assert stored['tier_override'] == 'T5'
    assert stored['invite_token_hash']
    assert verify_password(stored['password_hash'], 'anything') is False
    removed = client.delete('/api/users/' + 'admin@oraglass.us')
    assert removed.status_code == 400


def _admin(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302


def _company(client, name, tier):
    created = client.post('/api/customers', json={'name': name, 'tier': tier})
    assert created.status_code == 200
    return created.get_json()['id']


def test_create_customer_user_with_tier_override(client):
    _admin(client)
    customer_id = _company(client, 'North Glass', 'T3')
    created = client.post('/api/users', json={
        'name': 'Pat North',
        'email': 'pat@north.example',
        'role': 'customer',
        'customer_id': customer_id,
        'password': 'Validpass1',
        'temporary': False,
        'tier_override': 'T5',
    })
    assert created.status_code == 200
    stored = load_users()['pat@north.example']
    assert stored['tier_override'] == 'T5'
    assert stored['customer_id'] == customer_id
    assert stored['role'] == 'customer'
    row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'pat@north.example')
    assert row['company_tier'] == 'T3'
    assert row['company_tier_label'] == 'Tier 3'
    assert row['tier_override'] == 'T5'
    assert row['effective_tier'] == 'T5'
    assert row['effective_tier_label'] == 'Tier 5 (End User)'
    assert 'allowed_tiers' not in stored
    assert row['allowed_tiers'] == ['T3', 'T5']
    assert row['allowed_tier_labels'] == ['Tier 3', 'Tier 5 (End User)']
    client.post('/logout')
    assert login(client, 'pat@north.example').status_code == 302
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'})
    assert blocked.status_code == 400
    priced = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T5'})
    assert priced.status_code == 200
    assert priced.get_json()['tier'] == 'T5'


def test_create_customer_user_without_tier_override(client):
    _admin(client)
    customer_id = _company(client, 'South Glass', 'T3')
    omitted = client.post('/api/users', json={
        'name': 'Sam South',
        'email': 'sam@south.example',
        'role': 'customer',
        'customer_id': customer_id,
        'password': 'Validpass1',
        'temporary': False,
    })
    blank = client.post('/api/users', json={
        'name': 'Jamie South',
        'email': 'jamie@south.example',
        'role': 'customer',
        'customer_id': customer_id,
        'tier_override': '   ',
    })
    assert omitted.status_code == 200
    assert blank.status_code == 200
    assert load_users()['sam@south.example']['tier_override'] is None
    assert load_users()['jamie@south.example']['tier_override'] is None
    row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'sam@south.example')
    assert row['company_tier'] == 'T3'
    assert row['company_tier_label'] == 'Tier 3'
    assert row['tier_override'] is None
    assert row['effective_tier'] == 'T3'
    assert row['effective_tier_label'] == 'Tier 3'
    assert row['allowed_tiers'] == ['T3']
    assert row['allowed_tier_labels'] == ['Tier 3']
    client.post('/logout')
    assert login(client, 'sam@south.example').status_code == 302
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'})
    assert blocked.status_code == 400
    assert b'price_sqf' not in blocked.data
    priced = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'})
    assert priced.get_json()['tier'] == 'T3'
    client.post('/logout')
    _admin(client)
    edited = client.put('/api/users/sam@south.example', json={'tier_override': 'T4'})
    assert edited.status_code == 200
    assert load_users()['sam@south.example']['tier_override'] == 'T4'
    assert 'allowed_tiers' not in load_users()['sam@south.example']
    edited_row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'sam@south.example')
    assert edited_row['allowed_tiers'] == ['T3', 'T4']
    cleared = client.put('/api/users/sam@south.example', json={'tier_override': ''})
    assert cleared.status_code == 200
    assert load_users()['sam@south.example']['tier_override'] is None
    cleared_row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'sam@south.example')
    assert cleared_row['allowed_tiers'] == ['T3']


def test_create_user_rejects_unknown_tier(client):
    _admin(client)
    customer_id = _company(client, 'East Glass', 'T2')
    rejected = client.post('/api/users', json={
        'name': 'Pat East',
        'email': 'pat@east.example',
        'role': 'customer',
        'customer_id': customer_id,
        'tier_override': 'EU',
    })
    assert rejected.status_code == 400
    assert b'Unknown pricing tier' in rejected.data
    assert 'pat@east.example' not in load_users()
    created = client.post('/api/users', json={
        'name': 'Pat East',
        'email': 'pat@east.example',
        'role': 'customer',
        'customer_id': customer_id,
    })
    assert created.status_code == 200
    rejected_edit = client.put('/api/users/pat@east.example', json={'tier_override': 'NOPE'})
    assert rejected_edit.status_code == 400
    assert load_users()['pat@east.example']['tier_override'] is None


def test_non_admin_cannot_assign_tier_override(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('sales@oraglass.us', 'sales')
    assert login(client, 'sales@oraglass.us').status_code == 302
    denied = client.post('/api/users', json={
        'name': 'Pat',
        'email': 'pat@example.com',
        'role': 'sales',
        'tier_override': 'T5',
    })
    assert denied.status_code == 403
    assert 'pat@example.com' not in load_users()
    denied_edit = client.put('/api/users/admin@oraglass.us', json={'tier_override': 'T1', 'allowed_tiers': ['T5']})
    assert denied_edit.status_code == 403
    assert load_users()['admin@oraglass.us'].get('tier_override') is None
    assert 'allowed_tiers' not in load_users()['admin@oraglass.us']


def test_staff_without_company_uses_tier_as_quoting_default(client):
    _admin(client)
    created = client.post('/api/users', json={
        'name': 'Riley Sales',
        'email': 'riley@oraglass.us',
        'role': 'sales',
        'password': 'Validpass1',
        'temporary': False,
        'tier_override': 'T1',
        'allowed_tiers': ['T1'],
    })
    assert created.status_code == 200
    stored = load_users()['riley@oraglass.us']
    assert stored['customer_id'] is None
    assert stored['tier_override'] == 'T1'
    assert stored['allowed_tiers'] == ['T1']
    row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'riley@oraglass.us')
    assert row['company'] is None
    assert row['company_tier_label'] is None
    assert row['tier_override'] == 'T1'
    assert row['effective_tier_label'] == 'Tier 1'
    client.post('/logout')
    assert login(client, 'riley@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert page.status_code == 200
    assert page.data.count(b'value="T1" selected') == 2
    assert b'value="T5"' in page.data
    assert b'Tier 5 (End User)' in page.data
    default_price = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'})
    assert default_price.get_json()['tier'] == 'T1'
    chosen = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T5'})
    assert chosen.get_json()['tier'] == 'T5'


def test_users_form_includes_company_default_and_user_tier(client):
    _admin(client)
    page = client.get('/users')
    assert page.status_code == 200
    body = page.data
    assert b'Company tier' in body
    assert b"This user's tier" in body
    assert b'id="company-tier-group"' in body
    assert b'id="company-tier-label"' in body
    assert b'aria-readonly="true"' in body
    assert b'id="u-tier"' in body
    assert b'<option value="">Use company default</option>' in body
    assert b'<option value="T5">Tier 5 (End User)</option>' in body
    assert b'<option value="T1">Tier 1</option>' in body
    assert body.count(b"tier_override: document.getElementById('u-tier').value") == 2
    assert body.count(b'allowed_tiers: collectAllowed()') == 2
    assert b'<th>Allowed tiers</th>' in body
    assert b'id="allowed-tiers"' in body
    assert b'<label><input type="checkbox" value="T5"> Tier 5 (End User)</label>' in body
    assert b"label.textContent = company.tier_label || company.tier || ''" in body
    assert b'Choose at least one allowed tier.' in body
    assert b'The company tier is not allowed. Choose one of the allowed tiers for this user.' in body
    assert b'class="row-actions"' in body
    assert b'act act-primary' in body
    assert b'act act-secondary' in body
    assert b'act act-danger' in body
    assert b'white-space:nowrap' in body
    customers = client.get('/customers')
    assert customers.status_code == 200
    assert b'act act-primary' in customers.data
    assert b'act act-danger' in customers.data
    assert b'white-space:nowrap' in customers.data


def test_customer_can_choose_an_allowed_tier_and_not_a_blocked_one(client):
    write_user(
        'kiu@open-windows.ca', 'customer', customer_id='open-windows',
        customer_name='Open Windows', customer_tier='T2', tier_override='T5',
        allowed_tiers=['T4', 'T5'],
    )
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    page = client.get('/quotes')
    assert page.data.count(b'<select id="tier">') == 1
    assert page.data.count(b'<select id="s-tier">') == 1
    assert page.data.count(b'value="T5" selected') == 2
    assert page.data.count(b'value="T4"') == 2
    assert b'value="T2"' not in page.data
    assert b'value="T1"' not in page.data
    assert b'value="T3"' not in page.data
    assert b'Tier 2' not in page.data
    allowed = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T4'})
    assert allowed.status_code == 200
    assert allowed.get_json()['tier'] == 'T4'
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'})
    assert blocked.status_code == 400
    assert b'price_sqf' not in blocked.data
    assert b'Tier 2' not in blocked.data
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '1'}],
        'currency': 'usd',
        'project': 'Allowed',
        'type': 'sqf',
        'tier': 'T4',
    })
    assert saved.status_code == 200
    blocked_quote = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '1'}],
        'currency': 'usd',
        'project': 'Blocked',
        'type': 'size',
        'tier': 'T2',
    })
    assert blocked_quote.status_code == 400
    by_project = {quote['project']: quote['tier'] for quote in load_quotes()}
    assert by_project == {'Allowed': 'T4'}


def test_customer_with_only_t5_never_sees_the_company_tier(client):
    import re
    write_user(
        'sam@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass', customer_tier='T2', tier_override='T5',
        allowed_tiers=['T5'],
    )
    from storage import append_quote
    append_quote({
        'date': '2026-01-01',
        'time': '09:00',
        'user': 'sam@harbor.example',
        'company': 'Harbor Glass',
        'customer_id': 'harbor-glass',
        'project': 'Old company price',
        'items': [{'name': 'Clear Glass', 'price': '10.00'}],
        'currency': 'usd',
        'type': 'sqf',
        'tier': 'T2',
    })
    assert login(client, 'sam@harbor.example').status_code == 302
    forbidden = (b'Tier 2', b'value="T2"', b'"T2"')
    pages = ('/quotes', '/quote-history', '/orders', '/invoices', '/financial', '/dashboard')
    for path in pages:
        page = client.get(path)
        assert page.status_code == 200, path
        for needle in forbidden:
            assert needle not in page.data, (path, needle)
    quotes = client.get('/quotes')
    assert b'Tier 5 (End User)' in quotes.data
    assert b'<input type="hidden" id="tier" value="T5">' in quotes.data
    assert b'<input type="hidden" id="s-tier" value="T5">' in quotes.data
    customers = client.get('/api/customers')
    assert customers.status_code == 403
    for needle in forbidden:
        assert needle not in customers.data
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T2'})
    assert blocked.status_code == 400
    assert b'price_sqf' not in blocked.data
    for needle in forbidden:
        assert needle not in blocked.data
    from app import PRICING
    payload = {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'}
    allowed = client.post('/api/calculate', json={**payload, 'tier': 'T5'})
    assert allowed.status_code == 200
    assert allowed.get_json()['tier'] == 'T5'
    assert allowed.get_json()['price_sqf'] == calculate_price_sqf(payload, 'T5', PRICING, load_tiers())
    assert allowed.get_json()['price_sqf'] != calculate_price_sqf(payload, 'T2', PRICING, load_tiers())
    rejected = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '1'}],
        'currency': 'usd',
        'project': 'Company tier',
        'type': 'sqf',
        'tier': 'T2',
    })
    assert rejected.status_code == 400
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '1'}],
        'currency': 'usd',
        'project': 'End user',
        'type': 'sqf',
        'tier': 'T5',
    })
    assert saved.status_code == 200
    history = client.get('/api/quotes')
    assert history.status_code == 200
    for needle in forbidden:
        assert needle not in history.data
    body = history.get_json()
    assert {quote['project'] for quote in body} == {'Old company price', 'End user'}
    assert all(quote.get('tier') in (None, 'T5') for quote in body)
    pdf = client.post('/api/export-quote', json={
        'items': [{'name': 'Clear Glass', 'price': '10.00'}],
        'currency': 'usd',
        'project': 'End user',
        'tier': 'T2',
    })
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    drawn = b'\n'.join(re.findall(rb'\((?:\\.|[^\\)])*\)\s*Tj', pdf.data))
    assert b'Tier 2' not in drawn
    assert b'(T2)' not in drawn
    assert b'T2' not in drawn


def test_sealant_note_renders_below_the_spacer_row(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert page.status_code == 200
    html = page.data
    for note_id in (b'sealant1-note', b'sealant2-note', b's-sealant1-note', b's-sealant2-note'):
        marker = b'<div class="sealant-note" id="' + note_id + b'"></div>'
        assert marker in html
        # The note follows the spacer/sealant row instead of sitting inside the sealant field.
        assert b'</select></div>\n</div>' + marker in html or b'</select></div></div>' + marker in html


def test_customer_with_only_the_company_tier_sees_it_read_only(client):
    write_user(
        'kiu@open-windows.ca', 'customer', customer_id='open-windows',
        customer_name='Open Windows', customer_tier='T3',
    )
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    page = client.get('/quotes')
    assert b'<select id="tier">' not in page.data
    assert b'<select id="s-tier">' not in page.data
    assert page.data.count(b'value="T3"') == 2
    assert b'Tier 3' in page.data
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'})
    assert blocked.status_code == 400
    priced = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'temper1': 'n/a'})
    assert priced.get_json()['tier'] == 'T3'


def test_admin_sets_allowed_tiers_when_creating_and_editing(client):
    _admin(client)
    customer_id = _company(client, 'West Glass', 'T3')
    created = client.post('/api/users', json={
        'name': 'Pat West',
        'email': 'pat@west.example',
        'role': 'customer',
        'customer_id': customer_id,
        'tier_override': 'T5',
        'allowed_tiers': ['T5', 'T1'],
    })
    assert created.status_code == 200
    assert load_users()['pat@west.example']['allowed_tiers'] == ['T1', 'T5']
    row = next(item for item in client.get('/api/users').get_json() if item['email'] == 'pat@west.example')
    assert row['company_tier_label'] == 'Tier 3'
    assert row['effective_tier_label'] == 'Tier 5 (End User)'
    assert row['allowed_tier_labels'] == ['Tier 1', 'Tier 5 (End User)']
    bad = client.put('/api/users/pat@west.example', json={'allowed_tiers': ['T4', 'NOPE']})
    assert bad.status_code == 400
    assert b'Unknown pricing tier' in bad.data
    assert load_users()['pat@west.example']['allowed_tiers'] == ['T1', 'T5']
    needs_default = client.put('/api/users/pat@west.example', json={
        'tier_override': '',
        'allowed_tiers': ['T5'],
    })
    assert needs_default.status_code == 400
    assert b'Choose one of the allowed tiers' in needs_default.data
    empty = client.put('/api/users/pat@west.example', json={'allowed_tiers': []})
    assert empty.status_code == 400
    assert b'at least one allowed tier' in empty.data
    edited = client.put('/api/users/pat@west.example', json={'allowed_tiers': ['T4', 'T5'], 'tier_override': 'T5'})
    assert edited.status_code == 200
    assert load_users()['pat@west.example']['allowed_tiers'] == ['T4', 'T5']
    not_a_list = client.put('/api/users/pat@west.example', json={'allowed_tiers': 'T4'})
    assert not_a_list.status_code == 400
    assert load_users()['pat@west.example']['allowed_tiers'] == ['T4', 'T5']


def test_create_user_keeps_csrf_protection(data_dir):
    import re
    from app import app
    write_user('admin@oraglass.us', 'admin')
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        test_client = app.test_client()
        login_page = test_client.get('/login')
        token = re.search(br'name="csrf_token" value="([^"]+)"', login_page.data).group(1).decode()
        signed_in = test_client.post('/login', data={
            'email': 'admin@oraglass.us',
            'password': 'Validpass1',
            'csrf_token': token,
        }, follow_redirects=False)
        assert signed_in.status_code == 302
        users_page = test_client.get('/users')
        fresh = re.search(br'name="csrf-token" content="([^"]+)"', users_page.data).group(1).decode()
        blocked = test_client.post('/api/users', json={
            'name': 'No Token',
            'email': 'notoken@oraglass.us',
            'role': 'sales',
            'tier_override': 'T4',
        })
        assert blocked.status_code == 400
        assert b'CSRF' in blocked.data
        assert 'notoken@oraglass.us' not in load_users()
        created = test_client.post('/api/users', json={
            'name': 'With Token',
            'email': 'withtoken@oraglass.us',
            'role': 'sales',
            'tier_override': 'T4',
        }, headers={'X-CSRFToken': fresh})
        assert created.status_code == 200
        assert load_users()['withtoken@oraglass.us']['tier_override'] == 'T4'
        assert load_users()['withtoken@oraglass.us']['customer_id'] is None
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_setup_link_is_single_use(client, data_dir):
    tokens = apply_seed(issue_links=True)
    token = tokens['vlad@open-windows.ca']
    assert load_users()['vlad@open-windows.ca']['invite_token_hash'] == hash_invite_token(token)
    weak = client.post(f'/setup-password/{token}', data={'password': 'short', 'confirm': 'short'})
    assert b'at least 10' in weak.data
    saved = client.post(
        f'/setup-password/{token}',
        data={'password': 'Newpassword1', 'confirm': 'Newpassword1'},
        follow_redirects=False,
    )
    assert saved.status_code == 302
    user = load_users()['vlad@open-windows.ca']
    assert verify_password(user['password_hash'], 'Newpassword1')
    assert user['invite_token_hash'] is None
    reused = client.post(f'/setup-password/{token}', data={'password': 'Newpassword1', 'confirm': 'Newpassword1'})
    assert reused.status_code == 400
    assert login(client, 'vlad@open-windows.ca', 'Newpassword1').status_code == 302


def test_temporary_password_must_be_changed(client):
    write_user('temp@oraglass.us', 'sales', password='Temp-password1', must_change_password=True)
    response = login(client, 'temp@oraglass.us', 'Temp-password1')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/account/password')
    blocked = client.post('/api/calculate', json={'glass1': 'Clear Glass 4mm', 'tier': 'T1'})
    assert blocked.status_code == 403
    changed = client.post('/account/password', data={
        'current_password': 'Temp-password1',
        'password': 'Replaced-pass1',
        'confirm': 'Replaced-pass1',
    }, follow_redirects=False)
    assert changed.status_code == 302
    quote = client.get('/quotes')
    assert quote.status_code == 200


def test_export_pdf_still_works(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    response = client.post('/api/export-quote', json={
        'items': [{'name': 'Clear Glass', 'price': '10.00'}],
        'currency': 'usd',
        'project': 'Lobby',
    })
    assert response.status_code == 200
    assert response.data.startswith(b'%PDF')


def test_csrf_blocks_login_without_token(data_dir):
    from app import app
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        test_client = app.test_client()
        response = test_client.post(
            '/login',
            data={'email': 'a@oraglass.us', 'password': 'Validpass1'},
            follow_redirects=True,
        )
        assert b'session expired' in response.data.lower()
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_bootstrap_admin_from_env_and_cli(data_dir, monkeypatch):
    from app import app, bootstrap_admin
    monkeypatch.setenv('ADMIN_EMAIL', 'boss@oraglass.us')
    monkeypatch.setenv('ADMIN_PASSWORD', 'admin' + '123')
    assert bootstrap_admin() == 'rejected'
    assert 'boss@oraglass.us' not in load_users()
    monkeypatch.setenv('ADMIN_PASSWORD', 'Sup3rsecret1')
    assert bootstrap_admin() == 'created'
    assert verify_password(load_users()['boss@oraglass.us']['password_hash'], 'Sup3rsecret1')
    users = load_users()
    users['boss@oraglass.us']['name'] = 'Kept'
    save_users(users)
    assert bootstrap_admin() == 'exists'
    assert load_users()['boss@oraglass.us']['name'] == 'Kept'

    monkeypatch.setenv('ADMIN_EMAIL', 'second@oraglass.us')
    monkeypatch.setenv('ADMIN_PASSWORD', 'Another-pass1')
    result = app.test_cli_runner().invoke(args=['create-admin', '--email', 'second@oraglass.us'])
    assert result.exit_code == 0, result.output
    assert 'Another-pass1' not in result.output
    assert verify_password(load_users()['second@oraglass.us']['password_hash'], 'Another-pass1')
    links = app.test_cli_runner().invoke(args=['seed', '--links', '--base-url', 'https://quotes.example'])
    assert links.exit_code == 0, links.output
    assert 'https://quotes.example/setup-password/' in links.output
    assert 'kiu@open-windows.ca' in links.output
    assert load_users()['kiu@open-windows.ca']['password_hash'] is None


def test_repo_has_no_demo_secrets():
    banned = [
        'ora-glass-erp-2026-secret',
        'admin' + '123',
        'sales' + '123',
        'prod' + '123',
        'customer' + '123',
    ]
    root = os_path_root()
    skip = {'.git', '.pytest_cache', '__pycache__', 'tests', 'node_modules'}
    for dirpath, dirnames, filenames in os_walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip]
        for name in filenames:
            path = root_join(dirpath, name)
            if not name.endswith(('.py', '.html', '.md', '.yml', '.yaml', '.txt', '.json')):
                continue
            text = open(path, encoding='utf-8', errors='ignore').read()
            for secret in banned:
                assert secret not in text, f'{secret} found in {path}'


def os_path_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parents[1]


def os_walk(root):
    import os
    return os.walk(root)


def root_join(dirpath, name):
    import os
    return os.path.join(dirpath, name)

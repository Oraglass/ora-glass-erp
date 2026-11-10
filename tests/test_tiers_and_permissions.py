import json

from quoting import calculate_price_sqf, resolve_effective_tier
from seed_data import SEED_USERS, apply_seed
from security import hash_invite_token, verify_password
from storage import load_customers, load_quotes, load_tiers, load_users, save_users
from tests.conftest import login, write_user


def test_resolve_effective_tier_override_then_customer_then_default():
    customers = {'c': {'tier': 'EU'}}
    valid = {'T1': {}, 'T2': {}, 'EU': {}}
    assert resolve_effective_tier({'tier_override': 'T2', 'customer_id': 'c'}, customers, valid) == 'T2'
    assert resolve_effective_tier({'tier_override': '', 'customer_id': 'c'}, customers, valid) == 'EU'
    assert resolve_effective_tier({'tier_override': None, 'customer_id': 'c'}, customers, valid) == 'EU'
    assert resolve_effective_tier({'tier_override': 'NOPE', 'customer_id': 'c'}, customers, valid) == 'EU'
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


def test_end_user_tier_is_separate_from_t5_and_marked_placeholder(data_dir):
    tiers = load_tiers()
    assert tiers['EU']['label'] == 'End User'
    assert tiers['EU']['placeholder'] is True
    assert tiers['EU']['glass_markup'] == 1.0
    assert tiers['EU']['spacer_markup'] == 1.0
    assert tiers['T5']['label'] == 'Tier 5'
    assert tiers['T5']['glass_markup'] == 4.25
    assert tiers['T1']['glass_markup'] == 0.30
    assert tiers['T1']['spacer_markup'] == 0.20


def test_seed_has_no_passwords_and_assigns_tiers(data_dir):
    apply_seed()
    users = load_users()
    customers = load_customers()
    tiers = load_tiers()
    assert customers['open-windows']['name'] == 'Open Windows'
    assert customers['open-windows']['domain'] == 'open-windows.ca'
    assert customers['open-windows']['tier'] == 'EU'
    assert resolve_effective_tier(users['alex@oraglass.us'], customers, tiers) == 'T2'
    for spec in SEED_USERS:
        user = users[spec['email']]
        assert user['password_hash'] is None
        assert user['customer_id'] == 'open-windows'
        if spec['email'] == 'alex@oraglass.us':
            assert user['tier_override'] == 'T2'
        else:
            assert user['tier_override'] is None
            assert resolve_effective_tier(user, customers, tiers) == 'EU'
    apply_seed()
    assert load_users()['kiu@open-windows.ca']['password_hash'] is None


def test_customer_cannot_choose_a_different_tier(client):
    write_user(
        'kiu@open-windows.ca', 'customer', customer_id='open-windows',
        customer_name='Open Windows', customer_tier='EU', tier_override='T2',
    )
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    page = client.get('/quotes')
    assert b'<select id="tier">' not in page.data
    assert b'value="T2"' in page.data
    payload = {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'T1'}
    response = client.post('/api/calculate', json=payload)
    body = response.get_json()
    assert response.status_code == 200
    assert body['tier'] == 'T2'
    from app import PRICING
    expected = calculate_price_sqf(payload, 'T2', PRICING, load_tiers())
    other = calculate_price_sqf(payload, 'T1', PRICING, load_tiers())
    assert body['price_sqf'] == expected
    assert expected != other


def test_staff_can_choose_tier_including_end_user(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quotes')
    assert b'<select id="tier">' in page.data
    assert b'End User' in page.data
    assert b'Tier 5' in page.data
    payload = {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'tier': 'EU'}
    response = client.post('/api/calculate', json=payload)
    body = response.get_json()
    assert body['tier'] == 'EU'
    from app import PRICING
    assert body['price_sqf'] == calculate_price_sqf(payload, 'EU', PRICING, load_tiers())


def test_customer_sees_only_own_company_quotes_and_storage_path(client, data_dir):
    write_user('admin@oraglass.us', 'admin')
    write_user('kiu@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows', customer_tier='EU')
    write_user('other@example.com', 'customer', customer_id='other-co', customer_name='Other Co', customer_tier='T1')
    assert login(client, 'kiu@open-windows.ca').status_code == 302
    saved = client.post('/api/quotes', json={
        'items': [{'name': 'Unit', 'price': '12.50'}],
        'currency': 'usd',
        'project': 'Open Job',
        'type': 'sqf',
        'tier': 'T1',
    })
    assert saved.status_code == 200
    on_disk = json.loads((data_dir / 'quotes.json').read_text())
    assert on_disk[0]['project'] == 'Open Job'
    assert on_disk[0]['customer_id'] == 'open-windows'
    assert on_disk[0]['company'] == 'Open Windows'
    assert on_disk[0]['user'] == 'kiu@open-windows.ca'
    assert on_disk[0]['tier'] == 'EU'
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
    write_user('kiu@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows', customer_tier='EU')

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
        'tier_override': 'EU',
    })
    body = created_user.get_json()
    assert created_user.status_code == 200
    assert body['setup_url'].startswith('http')
    assert 'password' not in body
    stored = load_users()['pat@north.example']
    assert stored['password_hash'] is None
    assert stored['tier_override'] == 'EU'
    assert stored['invite_token_hash']
    assert verify_password(stored['password_hash'], 'anything') is False
    removed = client.delete('/api/users/' + 'admin@oraglass.us')
    assert removed.status_code == 400


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

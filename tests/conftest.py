import os
import tempfile

os.environ.setdefault('APP_ENV', 'test')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production-use')
os.environ.setdefault('AUTO_SEED', '0')
os.environ.setdefault('SESSION_COOKIE_SECURE', '0')
os.environ.setdefault('TRUST_PROXY', '0')
_SHARED = tempfile.mkdtemp(prefix='ora-erp-tests-')
os.environ.setdefault('DATA_DIR', _SHARED)
os.environ.setdefault('QUOTES_PATH', os.path.join(_SHARED, 'quotes.json'))

import pytest

import app as app_module
from security import hash_password
from storage import load_customers, load_users, save_customers, save_users


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('QUOTES_PATH', str(tmp_path / 'quotes.json'))
    monkeypatch.setenv('CUSTOMERS_PATH', str(tmp_path / 'customers.json'))
    monkeypatch.setenv('USERS_PATH', str(tmp_path / 'users.json'))
    monkeypatch.setenv('TIERS_PATH', str(tmp_path / 'tiers.json'))
    monkeypatch.setenv('AUTO_SEED', '0')
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.delenv('ADMIN_EMAIL', raising=False)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    monkeypatch.delenv('PRICING_PATH', raising=False)
    app_module.reset_runtime_state()
    return tmp_path


@pytest.fixture
def client(data_dir):
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as test_client:
        yield test_client


def write_user(email, role, password='Validpass1', customer_id=None, tier_override=None,
               must_change_password=False, active=True, customer_tier='T2', customer_name=None,
               allowed_tiers=None, customer_subrole=None):
    if customer_id:
        customers = load_customers()
        customers.setdefault(customer_id, {
            'id': customer_id,
            'name': customer_name or customer_id,
            'domain': '',
            'address': '',
            'city': '',
            'state': '',
            'zip': '',
            'phone': '',
            'email': '',
            'credit_approved': False,
            'payment_terms': 'Net 30',
            'tier': customer_tier,
            'status': 'active',
        })
        customers[customer_id]['tier'] = customer_tier
        if customer_name:
            customers[customer_id]['name'] = customer_name
        save_customers(customers)
    users = load_users()
    users[email] = {
        'email': email,
        'name': email.split('@')[0].title(),
        'role': role,
        'customer_id': customer_id,
        'tier_override': tier_override,
        'password_hash': hash_password(password) if password else None,
        'must_change_password': must_change_password,
        'active': active,
        'invite_token_hash': None,
        'invite_expires': None,
    }
    if allowed_tiers is not None:
        users[email]['allowed_tiers'] = list(allowed_tiers)
    if customer_subrole:
        users[email]['customer_subrole'] = customer_subrole
    save_users(users)
    return password


def login(client, email, password='Validpass1'):
    return client.post('/login', data={'email': email, 'password': password}, follow_redirects=False)

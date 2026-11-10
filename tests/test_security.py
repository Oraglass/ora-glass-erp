import hashlib

from security import (
    hash_password,
    is_development,
    password_is_strong,
    resolve_secret_key,
    session_cookie_secure,
    verify_password,
)
from tests.conftest import login, write_user


def test_password_hash_is_salted_and_verifies():
    first = hash_password('Validpass1')
    second = hash_password('Validpass1')
    assert first != second
    assert first != 'Validpass1'
    assert not first.startswith(hashlib.sha256(b'Validpass1').hexdigest())
    assert verify_password(first, 'Validpass1')
    assert not verify_password(first, 'Validpass2')


def test_legacy_unsalted_sha256_is_rejected():
    legacy = hashlib.sha256(b'Validpass1').hexdigest()
    assert not verify_password(legacy, 'Validpass1')
    assert not verify_password(None, 'Validpass1')
    assert not verify_password('', 'Validpass1')


def test_password_strength_rules():
    assert password_is_strong('Validpass1')[0]
    assert not password_is_strong('short1')[0]
    assert not password_is_strong('longpassword')[0]
    assert not password_is_strong('admin' + '123')[0]


def test_secret_key_required_in_production(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.delenv('SECRET_KEY', raising=False)
    assert is_development() is False
    try:
        resolve_secret_key()
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_short_secret_rejected_in_production(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('SECRET_KEY', 'too-short')
    try:
        resolve_secret_key()
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_development_secret_is_ephemeral_when_unset(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'development')
    monkeypatch.delenv('SECRET_KEY', raising=False)
    key = resolve_secret_key()
    assert len(key) >= 32
    assert key != 'ora-glass-erp-2026-secret'


def test_secure_cookie_defaults_on_in_production(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.delenv('SESSION_COOKIE_SECURE', raising=False)
    assert session_cookie_secure() is True
    monkeypatch.setenv('APP_ENV', 'test')
    monkeypatch.delenv('SESSION_COOKIE_SECURE', raising=False)
    assert session_cookie_secure() is False


def test_login_by_email_and_cookie_flags(client):
    write_user('sales@oraglass.us', 'sales')
    rejected = client.post('/login', data={'username': 'sales@oraglass.us', 'password': 'Validpass1'})
    assert rejected.status_code == 200
    assert b'Invalid credentials' in rejected.data
    response = login(client, 'sales@oraglass.us')
    assert response.status_code == 302
    cookie = response.headers['Set-Cookie']
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie
    page = client.get('/dashboard')
    assert page.status_code == 200
    assert b'sales@oraglass.us' not in page.data or b'Sales' in page.data


def test_login_rate_limit(client):
    write_user('limited@oraglass.us', 'sales')
    for _ in range(5):
        response = login(client, 'limited@oraglass.us', 'wrong-password-1')
        assert response.status_code == 200
    blocked = login(client, 'limited@oraglass.us', 'Validpass1')
    assert blocked.status_code == 429
    assert b'Too many login attempts' in blocked.data


def test_app_secret_is_not_the_old_hardcoded_value():
    from app import app
    assert app.secret_key != 'ora-glass-erp-2026-secret'
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'

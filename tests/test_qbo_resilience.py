import json
import urllib.error

import pytest

import quickbooks
from tests.conftest import login, write_user


def _tokens():
    quickbooks.save_tokens({
        'access_token': 'old-access',
        'refresh_token': 'refresh-1',
        'realm_id': '123456',
        'expires_at': 9_999_999_999,
        'company_name': 'Ora Glass',
    })


def _use(monkeypatch, transport):
    monkeypatch.setattr(quickbooks, 'SLEEP', lambda seconds: sleeps.append(seconds))
    quickbooks.TRANSPORT = transport


sleeps = []


@pytest.fixture(autouse=True)
def _reset_sleep():
    sleeps.clear()
    previous = quickbooks.SLEEP
    yield
    quickbooks.SLEEP = previous
    quickbooks.TRANSPORT = None


def test_invalid_grant_clears_tokens_and_shows_reconnect(client, monkeypatch):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor')
    _tokens()

    def transport(method, url, headers, body):
        return 400, {'intuit_tid': 'tid-grant'}, json.dumps({
            'error': 'invalid_grant',
            'error_description': 'Token revoked',
        }).encode()

    _use(monkeypatch, transport)
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.ensure_access_token(force=True)
    assert quickbooks.load_tokens() == {}
    assert quickbooks.connection_expired() is True
    assert any('intuit_tid=tid-grant' in row['message'] for row in quickbooks.load_log())
    assert sleeps == []

    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/settings/quickbooks').data
    section = page[page.index(b'Settings'):page.index(b'Item mapping')]
    assert b'QuickBooks connection expired - Reconnect' in section
    assert b'href="/quickbooks/connect"' in section
    dashboard = client.get('/dashboard')
    assert b'QuickBooks connection expired - Reconnect' in dashboard.data
    assert b'href="/quickbooks/connect"' in dashboard.data
    client.post('/logout')
    assert login(client, 'lee@harbor.example').status_code == 302
    assert b'QuickBooks connection expired - Reconnect' not in client.get('/dashboard').data


def test_expired_access_token_refreshes_once_and_retries(monkeypatch):
    _tokens()
    calls = []

    def transport(method, url, headers, body):
        calls.append((url, headers.get('Authorization')))
        if 'tokens/bearer' in url:
            return 200, {}, json.dumps({
                'access_token': 'new-access',
                'refresh_token': 'refresh-2',
                'expires_in': 3600,
            }).encode()
        if headers.get('Authorization') == 'Bearer old-access':
            return 401, {'intuit_tid': 'tid-401'}, b'{"Fault":{"Error":[{"Message":"AuthenticationFailed"}]}}'
        return 200, {}, b'{"CompanyInfo":{"CompanyName":"Still connected"}}'

    _use(monkeypatch, transport)
    body = quickbooks.api_get('/v3/company/123456/companyinfo/123456')
    assert body['CompanyInfo']['CompanyName'] == 'Still connected'
    assert sum(1 for url, _auth in calls if 'tokens/bearer' in url) == 1
    assert quickbooks.load_tokens().get('access_token') == 'new-access'
    assert quickbooks.connection_expired() is False


def test_401_after_refresh_disconnects(monkeypatch):
    _tokens()

    def transport(method, url, headers, body):
        if 'tokens/bearer' in url:
            return 200, {'intuit_tid': 'tid-refresh'}, json.dumps({
                'access_token': 'new-access',
                'refresh_token': 'refresh-2',
                'expires_in': 3600,
            }).encode()
        return 401, {'intuit_tid': 'tid-still'}, b'{"Fault":{"Error":[{"Message":"AuthenticationFailed"}]}}'

    _use(monkeypatch, transport)
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.api_get('/v3/company/123456/companyinfo/123456')
    assert quickbooks.load_tokens() == {}
    assert quickbooks.connection_expired() is True
    assert any('intuit_tid=tid-still' in row['message'] for row in quickbooks.load_log())


def test_transient_failures_retry_with_backoff_and_retry_after(monkeypatch):
    _tokens()
    calls = {'n': 0}

    def transport(method, url, headers, body):
        calls['n'] += 1
        if calls['n'] < 3:
            return 503, {'intuit_tid': 'tid-503'}, b'{}'
        return 200, {}, b'{"ok": true}'

    _use(monkeypatch, transport)
    assert quickbooks.api_get('/v3/company/123456/companyinfo/123456') == {'ok': True}
    assert calls['n'] == 3
    assert sleeps == [1, 2]

    calls['n'] = 0
    sleeps.clear()

    def always_down(method, url, headers, body):
        calls['n'] += 1
        return 500, {'intuit_tid': 'tid-500'}, b'{}'

    quickbooks.TRANSPORT = always_down
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.api_get('/v3/company/123456/companyinfo/123456')
    assert calls['n'] == 4
    assert sleeps == [1, 2, 4]
    assert any('intuit_tid=tid-500' in row['message'] for row in quickbooks.load_log())

    sleeps.clear()
    state = {'n': 0}

    def limited(method, url, headers, body):
        state['n'] += 1
        if state['n'] == 1:
            return 429, {'Retry-After': '9', 'intuit_tid': 'tid-429'}, b'{}'
        return 200, {}, b'{"ok": true}'

    quickbooks.TRANSPORT = limited
    assert quickbooks.api_get('/v3/company/123456/companyinfo/123456') == {'ok': True}
    assert state['n'] == 2
    assert sleeps == [9]


def test_other_client_errors_and_network_errors(monkeypatch):
    _tokens()
    calls = {'n': 0}

    def bad_request(method, url, headers, body):
        calls['n'] += 1
        return 400, {'intuit_tid': 'tid-400'}, b'{"Fault":{"Error":[{"Message":"bad request"}]}}'

    _use(monkeypatch, bad_request)
    with pytest.raises(quickbooks.QuickBooksError):
        quickbooks.api_get('/v3/company/123456/companyinfo/123456')
    assert calls['n'] == 1
    assert sleeps == []
    assert any('intuit_tid=tid-400' in row['message'] for row in quickbooks.load_log())
    assert quickbooks.load_tokens().get('refresh_token') == 'refresh-1'

    calls['n'] = 0

    def flaky(method, url, headers, body):
        calls['n'] += 1
        if calls['n'] < 3:
            raise urllib.error.URLError('down')
        return 200, {}, b'{"ok": true}'

    quickbooks.TRANSPORT = flaky
    assert quickbooks.api_get('/v3/company/123456/companyinfo/123456') == {'ok': True}
    assert calls['n'] == 3
    assert sleeps == [1, 2]

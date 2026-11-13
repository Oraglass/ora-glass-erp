import quickbooks
from tests.conftest import login, write_user


def test_eula_and_privacy_are_public_and_linked_from_login(client):
    eula = client.get('/eula')
    privacy = client.get('/privacy')
    assert eula.status_code == 200
    assert privacy.status_code == 200
    assert b'End-User License Agreement' in eula.data
    assert b'alt="Ora Glass"' in eula.data
    assert b'oraglass.us' in eula.data
    assert b'Privacy Policy' in privacy.data
    assert b'quotes' in privacy.data.lower()
    assert b'orders' in privacy.data.lower()
    assert b'invoices' in privacy.data.lower()
    assert b'payments' in privacy.data.lower()
    assert b'punches' in privacy.data.lower() or b'punch' in privacy.data.lower()
    assert b'photos' in privacy.data.lower()
    assert b'QuickBooks' in privacy.data
    assert b'not sell' in privacy.data.lower() or b'does not sell' in privacy.data.lower()
    assert b'Render' in privacy.data
    assert b'Intuit' in privacy.data
    assert b'disconnect' in privacy.data.lower()
    assert b'oraglass.us' in privacy.data
    login_page = client.get('/login')
    assert b'href="/eula"' in login_page.data
    assert b'href="/privacy"' in login_page.data


def test_quickbooks_launch_connect_and_disconnect_urls(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor')
    launch = client.get('/quickbooks/launch')
    assert launch.status_code == 302
    assert '/login' in launch.headers['Location']
    assert 'next=/settings/quickbooks' in launch.headers['Location']
    same = client.get('/quickbooks')
    assert same.status_code == 302
    assert 'next=/settings/quickbooks' in same.headers['Location']
    connect = client.get('/quickbooks/connect')
    assert connect.status_code == 302
    assert '/login' in connect.headers['Location']
    assert 'next=/quickbooks/connect' in connect.headers['Location']

    quickbooks.save_tokens({
        'access_token': 'access',
        'refresh_token': 'refresh',
        'realm_id': '123456',
        'expires_at': 9_999_999_999,
        'company_name': 'Ora Glass',
    })
    other = client.get('/quickbooks/disconnected?realmId=999')
    assert other.status_code == 200
    assert b'not the one connected' in other.data
    assert quickbooks.load_tokens().get('realm_id') == '123456'
    page = client.get('/quickbooks/disconnected?realmId=123456')
    assert page.status_code == 200
    assert b'no longer connected' in page.data
    assert quickbooks.load_tokens() == {}
    idle = client.get('/quickbooks/disconnected')
    assert idle.status_code == 200
    assert b'does not have an active QuickBooks connection' in idle.data

    assert login(client, 'lee@harbor.example').status_code == 302
    denied = client.get('/quickbooks/launch')
    assert denied.status_code in (302, 403)
    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    opened = client.get('/quickbooks/launch')
    assert opened.status_code == 302
    assert opened.headers['Location'].endswith('/settings/quickbooks')
    assert client.post('/quickbooks/webhook', data=b'{}').status_code != 404

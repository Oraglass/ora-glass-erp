from tests.conftest import login, write_user


def test_dashboard_renders_the_ora_glass_logo(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('kiu@open-windows.ca', 'customer', customer_id='open-windows', customer_name='Open Windows')
    for email in ('admin@oraglass.us', 'kiu@open-windows.ca'):
        assert login(client, email).status_code == 302
        page = client.get('/dashboard')
        assert page.status_code == 200
        assert b'alt="ORA Glass"' in page.data
        assert b'/static/logo.png' in page.data
        client.post('/logout')
    logo = client.get('/static/logo.png')
    assert logo.status_code == 200
    assert logo.data.startswith(b'\x89PNG')
    assert len(logo.data) > 1000

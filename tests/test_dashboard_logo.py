from tests.conftest import login, write_user


def test_header_and_login_use_the_logo_instead_of_the_text_badge(client):
    write_user('admin@oraglass.us', 'admin')
    login_page = client.get('/login')
    assert b'ORA GLASS ERP' not in login_page.data
    assert b'GLASS ERP' not in login_page.data
    assert b'alt="Ora Glass"' in login_page.data
    assert b'/static/logo.png' in login_page.data
    assert login(client, 'admin@oraglass.us').status_code == 302
    quotes = client.get('/quotes')
    assert quotes.status_code == 200
    assert b'ORA GLASS ERP' not in quotes.data
    assert b'alt="Ora Glass"' in quotes.data
    assert b'/static/logo.png' in quotes.data
    assert b'href="/dashboard"' in quotes.data
    assert b'.header { background: #fff; color: #111;' in quotes.data
    assert b'.header a.brand { display: inline-flex; align-items: center; background: transparent;' in quotes.data
    assert b'.header button.linkish { background: none; border: 0; color: #111;' in quotes.data


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

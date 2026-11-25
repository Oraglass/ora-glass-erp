"""A driver can sign in with a username when they have no email."""

from storage import load_users
from tests.conftest import login, write_user


def test_driver_username_is_a_login_and_stays_unique(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/users')
    assert b'Username' in page.data
    assert b'Required for a new driver' in page.data
    missing_password = client.post('/api/users', json={
        'name': 'No Pass', 'role': 'driver', 'username': 'nopass',
    })
    assert missing_password.status_code == 400
    assert 'temporary password' in missing_password.get_json()['error'].lower()
    created = client.post('/api/users', json={
        'name': 'Dana Driver', 'role': 'driver', 'username': 'Dana-1', 'password': 'Validpass1',
    })
    assert created.status_code == 200, created.get_json()
    assert created.get_json()['username'] == 'dana-1'
    stored = load_users()['dana-1']
    assert stored.get('email') in ('', None)
    assert stored['role'] == 'driver'
    assert stored['must_change_password'] is True
    assert stored['username'] == 'dana-1'
    same = client.post('/api/users', json={
        'name': 'Other Dana', 'role': 'driver', 'username': 'dana-1', 'password': 'Validpass1',
    })
    assert same.status_code == 409
    email_taken = client.post('/api/users', json={
        'name': 'Copy', 'role': 'driver', 'username': 'Boss@Harbor.example', 'password': 'Validpass1',
    })
    assert email_taken.status_code == 409
    phone = client.post('/api/users', json={
        'name': 'Pat Driver', 'role': 'driver', 'username': '(360) 555-0199', 'password': 'Validpass1',
    })
    assert phone.status_code == 200, phone.get_json()
    phone_again = client.post('/api/users', json={
        'name': 'Pat Again', 'role': 'driver', 'username': '3605550199', 'password': 'Validpass1',
    })
    assert phone_again.status_code == 409
    still_needs_email = client.post('/api/users', json={'name': 'No Mail', 'role': 'employee'})
    assert still_needs_email.status_code == 400
    assert 'email' in still_needs_email.get_json()['error'].lower()
    client.post('/logout')

    signed = client.post('/login', data={'email': 'Dana-1', 'password': 'Validpass1'})
    assert signed.status_code == 302
    assert signed.headers['Location'].endswith('/account/password')
    blocked = client.get('/racks/scan')
    assert blocked.status_code == 302
    assert blocked.headers['Location'].endswith('/account/password')
    changed = client.post('/account/password', data={
        'current_password': 'Validpass1', 'password': 'Validpass2', 'confirm': 'Validpass2',
    })
    assert changed.status_code == 302
    assert changed.headers['Location'].endswith('/racks/scan')
    assert client.get('/racks/scan').status_code == 200
    client.post('/logout')
    again = client.post('/login', data={'email': 'dana-1', 'password': 'Validpass2'})
    assert again.status_code == 302
    assert again.headers['Location'].endswith('/racks/scan')
    client.post('/logout')

    existing = login(client, 'boss@harbor.example')
    assert existing.status_code == 302
    assert client.get('/dashboard').status_code == 200

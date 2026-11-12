"""Employee time clock: punches, hours, permissions, and corrections."""

from datetime import datetime, timedelta, timezone

from storage import load_punches, load_users, save_punches, save_users
from tests.conftest import login, write_user
from timeclock import pacific_date, replay, stamp, summarize


def _employee(email, modules, manage_time=False, uses_time_clock=False):
    write_user(email, 'employee', password='Validpass1')
    users = load_users()
    users[email]['modules'] = list(modules)
    users[email]['manage_time'] = manage_time
    users[email]['uses_time_clock'] = uses_time_clock
    if uses_time_clock and 'time-clock' not in users[email]['modules']:
        users[email]['modules'].append('time-clock')
    save_users(users)


def _set_time(punch_id, moment):
    rows = load_punches()
    for row in rows:
        if row['id'] == punch_id:
            row['at'] = stamp(moment)
            row['original']['at'] = stamp(moment)
    save_punches(rows)


def test_hours_deduct_breaks_and_flag_a_missing_punch_out():
    start = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)  # 08:00 Pacific
    punches = [
        {'id': 1, 'action': 'in', 'at': stamp(start)},
        {'id': 2, 'action': 'break_start', 'at': stamp(start + timedelta(hours=4))},
        {'id': 3, 'action': 'break_end', 'at': stamp(start + timedelta(hours=4, minutes=30))},
        {'id': 4, 'action': 'out', 'at': stamp(start + timedelta(hours=8))},
        {'id': 5, 'action': 'in', 'at': stamp(start + timedelta(days=1))},
    ]
    shifts = replay(punches)
    assert shifts[0]['out'] is not None
    assert shifts[1]['out'] is None
    days, total = summarize(punches)
    assert days[0]['hours'] == 7.5
    assert days[0]['missing_out'] is False
    assert days[1]['hours'] == 0
    assert days[1]['missing_out'] is True
    assert total == 7.5


def test_punch_flow_blocks_a_second_punch_in_and_keeps_location_optional(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('boss@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor')
    _employee('casey@oraglass.us', ['dashboard', 'time-clock'], uses_time_clock=True)
    assert login(client, 'admin@oraglass.us').status_code == 302
    rejected = client.post('/api/users', json={
        'name': 'Pat',
        'email': 'pat@harbor.example',
        'role': 'customer',
        'customer_id': 'harbor',
        'modules': ['quotes', 'time-clock'],
        'password': 'Validpass1',
        'temporary': False,
    })
    assert rejected.status_code == 400
    client.post('/logout')

    assert login(client, 'casey@oraglass.us').status_code == 302
    page = client.get('/punch')
    assert page.status_code == 200
    assert b'apple-mobile-web-app-capable' in page.data
    assert b'Ora Glass Time Clock' in page.data
    assert b'/manifest.webmanifest' in page.data
    with client.session_transaction() as sess:
        assert sess.get('long_session') is True
    first = client.post('/api/punches', json={'action': 'in', 'note': 'Shop'})
    assert first.status_code == 200
    assert first.get_json()['status'] == 'in'
    assert first.get_json()['history'][0]['no_location'] is True
    again = client.post('/api/punches', json={'action': 'in', 'lat': 48.94, 'lng': -122.48, 'accuracy': 12})
    assert again.status_code == 409
    assert b'Already punched in' in again.data

    start = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
    _set_time(1, start)
    started = client.post('/api/punches', json={'action': 'break_start'})
    assert started.status_code == 200
    _set_time(load_punches()[-1]['id'], start + timedelta(hours=1))
    ended = client.post('/api/punches', json={'action': 'break_end'})
    assert ended.status_code == 200
    _set_time(load_punches()[-1]['id'], start + timedelta(hours=2))
    closed = client.post('/api/punches', json={'action': 'out', 'note': 'Done'})
    assert closed.status_code == 200
    _set_time(load_punches()[-1]['id'], start + timedelta(hours=3))
    me = client.get('/api/punches/me').get_json()
    assert me['status'] == 'out'
    worked = next(day for day in me['days'] if day['date'] == '2026-11-10')
    assert worked['hours'] == 2.0
    assert worked['missing_out'] is False
    assert client.get('/time-clock').status_code in (302, 403)
    assert client.get('/api/time-clock').status_code == 403
    manifest = client.get('/manifest.webmanifest')
    assert manifest.status_code == 200
    assert manifest.get_json()['name'] == 'Ora Glass Time Clock'
    assert manifest.get_json()['display'] == 'standalone'
    assert manifest.get_json()['theme_color'] == '#9f1830'
    assert manifest.get_json()['start_url'] == '/kiosk'
    assert client.get('/sw.js').status_code == 200
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.get('/punch').status_code in (302, 403)
    assert client.post('/api/punches', json={'action': 'in'}).status_code == 403


def test_shop_worker_uses_a_pin_and_cannot_open_other_modules(client):
    write_user('admin@oraglass.us', 'admin')
    _employee('riley@oraglass.us', ['dashboard', 'quotes', 'time-clock'], uses_time_clock=False)
    assert login(client, 'admin@oraglass.us').status_code == 302
    blocked = client.post('/api/users', json={
        'name': 'Sam',
        'role': 'shop',
        'employee_number': '441',
        'pin': '1234',
        'modules': ['quotes', 'orders'],
    })
    assert blocked.status_code == 400
    created = client.post('/api/users', json={
        'name': 'Sam Shop',
        'role': 'shop',
        'employee_number': '441',
        'pin': '1234',
    })
    assert created.status_code == 200
    body = created.get_json()
    assert body['account'] == 'shop:441'
    assert 'setup_url' not in body
    emailed = client.post('/api/users', json={
        'name': 'Pat Shop',
        'role': 'shop',
        'email': 'pat.shop@oraglass.us',
        'employee_number': '442',
    })
    assert emailed.status_code == 200
    assert 'setup_url' in emailed.get_json()
    reset = client.post('/api/users/shop:441/pin', json={'pin': '98765'})
    assert reset.status_code == 200
    assert client.post('/api/users/shop:441/pin', json={'pin': '12'}).status_code == 400
    client.post('/logout')

    assert login(client, 'riley@oraglass.us').status_code == 302
    assert client.get('/punch').status_code in (302, 403)
    assert client.post('/api/punches', json={'action': 'in'}).status_code == 403
    client.post('/logout')

    denied = client.post('/punch/login', data={'employee_number': '441', 'pin': '98765'})
    assert denied.status_code == 403
    assert b'shop tablet' in denied.data
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.put('/api/users/shop:441', json={'allow_phone_punch': True}).status_code == 200
    client.post('/logout')

    gate = client.post('/punch/login', data={'employee_number': '441', 'pin': '1234'})
    assert gate.status_code == 200
    assert b'Invalid employee number or PIN' in gate.data
    signed = client.post('/punch/login', data={'employee_number': '441', 'pin': '98765'})
    assert signed.status_code == 302
    assert signed.headers['Location'].endswith('/punch')
    with client.session_transaction() as sess:
        assert sess.get('long_session') is True
        assert sess.get('user') == 'shop:441'
    page = client.get('/punch')
    assert page.status_code == 200
    assert b'href="/dashboard"' not in page.data
    assert b'Sign out' in page.data
    assert b'Sam Shop' in page.data
    home = client.get('/')
    assert home.status_code == 302
    assert home.headers['Location'].endswith('/punch')
    assert client.get('/dashboard').headers['Location'].endswith('/punch')
    assert client.get('/quotes').headers['Location'].endswith('/punch')
    assert client.get('/api/quotes').status_code == 403
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/punches', json={'action': 'in', 'note': 'Bench'}).status_code == 200
    assert client.post('/api/punches', json={'action': 'in'}).status_code == 409
    for _ in range(5):
        client.post('/punch/login', data={'employee_number': '999', 'pin': '0000'})
    limited = client.post('/punch/login', data={'employee_number': '999', 'pin': '0000'})
    assert limited.status_code == 429


def test_manager_corrections_keep_the_original_and_exports(client):
    write_user('admin@oraglass.us', 'admin')
    _employee('casey@oraglass.us', ['dashboard', 'time-clock'], uses_time_clock=True)
    _employee('payroll@oraglass.us', ['dashboard', 'time-clock'], manage_time=False)
    _employee('lead@oraglass.us', ['dashboard', 'time-clock'], manage_time=True, uses_time_clock=True)
    assert login(client, 'casey@oraglass.us').status_code == 302
    assert client.post('/api/punches', json={'action': 'in'}).status_code == 200
    punch_id = load_punches()[-1]['id']
    original_at = load_punches()[-1]['at']
    client.post('/logout')

    assert login(client, 'payroll@oraglass.us').status_code == 302
    assert client.post(f'/api/time-clock/{punch_id}/correct', json={'reason': 'Fix', 'action': 'out'}).status_code == 403
    client.post('/logout')

    assert login(client, 'lead@oraglass.us').status_code == 302
    missing = client.post(f'/api/time-clock/{punch_id}/correct', json={'action': 'out'})
    assert missing.status_code == 400
    bad = client.post(f'/api/time-clock/{punch_id}/correct', json={'reason': 'Not really out', 'action': 'out'})
    assert bad.status_code == 400
    corrected = client.post(f'/api/time-clock/{punch_id}/correct', json={
        'reason': 'Clock was fast',
        'at_pacific': '2026-11-10T08:05',
        'note': 'Adjusted',
    })
    assert corrected.status_code == 200
    saved = next(row for row in load_punches() if row['id'] == punch_id)
    assert saved['original']['at'] == original_at
    assert saved['original']['note'] == ''
    assert saved['note'] == 'Adjusted'
    assert saved['at'] != original_at
    assert saved['corrections'][-1]['by'] == 'lead@oraglass.us'
    assert saved['corrections'][-1]['reason'] == 'Clock was fast'
    assert saved['corrections'][-1]['before']['at'] == original_at
    sheet = client.get('/api/time-clock?user=casey@oraglass.us').get_json()
    assert sheet['punches'][0]['user'] == 'casey@oraglass.us'
    assert sheet['days'][0]['missing_out'] is True
    exported = client.get('/api/time-clock/export.csv?user=casey@oraglass.us')
    assert exported.status_code == 200
    assert b'casey@oraglass.us' in exported.data
    assert b'missing_punch_out' in exported.data
    pdf = client.get('/api/time-clock/timesheet.pdf?user=casey@oraglass.us&from=2026-11-01&to=2026-11-30')
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    assert b'Lynden' in pdf.data or b'Casey' in pdf.data or len(pdf.data) > 1000
    page = client.get('/payroll')
    assert page.status_code == 200
    assert b'Time & Kiosk' in page.data
    client.post('/logout')
    assert login(client, 'casey@oraglass.us').status_code == 302
    own = client.get('/api/punches/me').get_json()
    assert len(own['history']) == 1
    assert own['history'][0]['user'] == 'casey@oraglass.us'


def _jpeg(width=800, height=600):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', (width, height), (180, 20, 40)).save(buf, 'JPEG', quality=90)
    buf.seek(0)
    return buf


def _shop_and_kiosk(client, number='441', pin='1234'):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/users', json={
        'name': 'Sam Shop',
        'role': 'shop',
        'employee_number': number,
        'pin': pin,
    })
    assert created.status_code == 200
    activated = client.post('/api/time-clock/kiosks', json={'name': 'Shop tablet'})
    assert activated.status_code == 200
    body = activated.get_json()
    assert body['kiosk']['name'] == 'Shop tablet'
    assert body['token']
    with client.session_transaction() as sess:
        assert 'user' not in sess
    return body['token'], body['kiosk']['id']


def test_kiosk_activation_and_revocation(client):
    token, kiosk_id = _shop_and_kiosk(client)
    page = client.get('/kiosk')
    assert page.status_code == 200
    assert b'Employee number' in page.data
    assert b'Punch In' in page.data
    assert b'href="/dashboard"' not in page.data
    assert b'Activate this device as kiosk' not in page.data
    workers = client.get('/api/kiosk/workers')
    assert workers.status_code == 200
    assert workers.get_json() == [{'name': 'Sam Shop', 'employee_number': '441'}]
    assert login(client, 'admin@oraglass.us').status_code == 302
    listed = client.get('/api/time-clock/kiosks').get_json()
    assert listed[0]['id'] == kiosk_id
    assert listed[0]['revoked'] is False
    assert 'token' not in listed[0]
    assert 'token_hash' not in listed[0]
    assert client.post(f'/api/time-clock/kiosks/{kiosk_id}/revoke').status_code == 200
    revoked = client.get('/kiosk')
    assert b'not activated' in revoked.data
    assert client.get('/api/kiosk/workers', headers={'X-Kiosk-Token': token}).status_code == 403
    assert client.post('/api/time-clock/kiosks/99/revoke').status_code == 404


def test_kiosk_punch_stores_a_private_photo_and_flags_a_missing_one(client, data_dir):
    import os
    from io import BytesIO
    from PIL import Image
    import app as app_module
    from storage import punch_photo_dir

    token, kiosk_id = _shop_and_kiosk(client)
    app_module.app.config['WTF_CSRF_ENABLED'] = True
    try:
        fresh = app_module.app.test_client()
        refused = fresh.post('/login', data={'email': 'admin@oraglass.us', 'password': 'Validpass1'})
        assert refused.status_code == 302
        assert refused.headers['Location'].endswith('/login')
        with fresh.session_transaction() as sess:
            assert 'user' not in sess
        punched = fresh.post('/api/kiosk/punch', data={
            'employee_number': '441',
            'pin': '1234',
            'action': 'in',
            'lat': '48.94',
            'lng': '-122.48',
            'photo': (_jpeg(), 'punch.jpg'),
        }, headers={'X-Kiosk-Token': token})
        assert punched.status_code == 200
    finally:
        app_module.app.config['WTF_CSRF_ENABLED'] = False
    body = punched.get_json()
    assert body['name'] == 'Sam Shop'
    assert body['action'] == 'in'
    assert body['no_photo'] is False
    assert body['kiosk_id'] == kiosk_id
    punch_id = load_punches()[-1]['id']
    saved = load_punches()[-1]
    assert saved['user'] == 'shop:441'
    assert saved['photo'] is True
    assert saved['kiosk_id'] == kiosk_id
    path = os.path.join(punch_photo_dir(), f'{punch_id}.jpg')
    assert os.path.isfile(path)
    assert Image.open(path).size[0] <= 640
    assert not str(path).startswith(os.path.join(os.getcwd(), 'static'))
    assert client.get(f'/static/punch_photos/{punch_id}.jpg').status_code == 404
    assert client.get(f'/api/time-clock/photos/{punch_id}').status_code == 401

    _employee('payroll@oraglass.us', ['dashboard', 'time-clock'], manage_time=False)
    _employee('lead@oraglass.us', ['dashboard', 'time-clock'], manage_time=True)
    assert login(client, 'payroll@oraglass.us').status_code == 302
    assert client.get(f'/api/time-clock/photos/{punch_id}').status_code == 403
    client.post('/logout')
    assert login(client, 'lead@oraglass.us').status_code == 302
    photo = client.get(f'/api/time-clock/photos/{punch_id}')
    assert photo.status_code == 200
    assert photo.mimetype == 'image/jpeg'
    assert photo.data[:2] == b'\xff\xd8'
    sheet = client.get('/api/time-clock').get_json()
    row = next(item for item in sheet['punches'] if item['id'] == punch_id)
    assert row['has_photo'] is True
    assert row['name'] == 'Sam Shop'
    assert row['kiosk_id'] == kiosk_id
    page = client.get('/payroll')
    assert b'No photo' in page.data or b'data-photo' in page.data
    client.post('/logout')

    missing = client.post('/api/kiosk/punch', data={
        'employee_number': '441',
        'pin': '1234',
        'action': 'out',
        'photo': (BytesIO(b'not-a-photo'), 'punch.jpg'),
    }, headers={'X-Kiosk-Token': token})
    assert missing.status_code == 200
    assert missing.get_json()['no_photo'] is True
    bare = load_punches()[-1]
    assert bare['photo'] is False
    assert bare['no_photo'] is True
    assert bare['kiosk_id'] == kiosk_id

    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.put('/api/time-clock/settings', json={'photo_retention_days': 30}).status_code == 400
    assert client.put('/api/time-clock/settings', json={'photo_retention_days': 90}).status_code == 200
    old = load_punches()
    for item in old:
        if item['id'] == punch_id:
            item['at'] = stamp(datetime.now(timezone.utc) - timedelta(days=91))
    save_punches(old)
    assert client.get('/api/time-clock').status_code == 200
    assert not os.path.exists(path)
    purged = next(item for item in load_punches() if item['id'] == punch_id)
    assert purged['photo'] is False
    assert purged.get('photo_purged') is True


def test_kiosk_pin_locks_the_worker_and_the_tablet(client):
    token, kiosk_id = _shop_and_kiosk(client)
    headers = {'X-Kiosk-Token': token}
    for _ in range(4):
        bad = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '0000'}, headers=headers)
        assert bad.status_code == 401
    locked = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '0000'}, headers=headers)
    assert locked.status_code == 423
    still = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '1234'}, headers=headers)
    assert still.status_code == 423
    assert login(client, 'admin@oraglass.us').status_code == 302
    assert client.post('/api/users/shop:441/pin/unlock').status_code == 200
    opened = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '1234'}, headers=headers)
    assert opened.status_code == 200
    assert opened.get_json()['name'] == 'Sam Shop'
    for _ in range(9):
        miss = client.post('/api/kiosk/identify', json={'employee_number': '999', 'pin': '0000'}, headers=headers)
        assert miss.status_code == 401
    tablet = client.post('/api/kiosk/identify', json={'employee_number': '999', 'pin': '0000'}, headers=headers)
    assert tablet.status_code == 423
    blocked = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '1234'}, headers=headers)
    assert blocked.status_code == 423
    assert client.post(f'/api/time-clock/kiosks/{kiosk_id}/unlock').status_code == 200
    again = client.post('/api/kiosk/identify', json={'employee_number': '441', 'pin': '1234'}, headers=headers)
    assert again.status_code == 200


def test_time_clock_stays_off_the_office_nav_and_setup_is_a_bare_page(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('boss@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor', customer_subrole='management')
    _employee('lead@oraglass.us', ['dashboard', 'invoices'], manage_time=True)
    assert login(client, 'admin@oraglass.us').status_code == 302
    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert b'>Time Clock<' not in dashboard.data
    assert b'href="/punch"' not in dashboard.data
    users = client.get('/users')
    assert b'value="time-clock"' not in users.data
    payroll = client.get('/payroll')
    assert b'Set up shop kiosk' in payroll.data
    assert b'Time & Kiosk' in payroll.data
    setup = client.get('/kiosk/setup')
    assert setup.status_code == 200
    assert b'Set up shop kiosk' in setup.data
    assert b'class="module-nav"' not in setup.data
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    customer_home = client.get('/dashboard')
    assert b'>Time Clock<' not in customer_home.data
    client.post('/logout')

    assert client.get('/kiosk/setup').status_code == 302
    assert login(client, 'lead@oraglass.us').status_code == 302
    assert b'Set up shop kiosk' in client.get('/kiosk/setup').data
    assert b'Set up shop kiosk' in client.get('/payroll').data

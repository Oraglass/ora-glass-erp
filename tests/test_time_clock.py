"""Employee time clock: punches, hours, permissions, and corrections."""

from datetime import datetime, timedelta, timezone

from storage import load_punches, load_users, save_punches, save_users
from tests.conftest import login, write_user
from timeclock import pacific_date, replay, stamp, summarize


def _employee(email, modules, manage_time=False):
    write_user(email, 'employee', password='Validpass1')
    users = load_users()
    users[email]['modules'] = list(modules)
    users[email]['manage_time'] = manage_time
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
    _employee('casey@oraglass.us', ['dashboard', 'time-clock'])
    write_user('boss@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor')
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
    assert client.get('/sw.js').status_code == 200
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    assert client.get('/punch').status_code in (302, 403)
    assert client.post('/api/punches', json={'action': 'in'}).status_code == 403


def test_manager_corrections_keep_the_original_and_exports(client):
    write_user('admin@oraglass.us', 'admin')
    _employee('casey@oraglass.us', ['dashboard', 'time-clock'])
    _employee('payroll@oraglass.us', ['dashboard', 'time-clock'], manage_time=False)
    _employee('lead@oraglass.us', ['dashboard', 'time-clock'], manage_time=True)
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
    page = client.get('/time-clock')
    assert page.status_code == 200
    assert b'Time Clock' in page.data
    client.post('/logout')
    assert login(client, 'casey@oraglass.us').status_code == 302
    own = client.get('/api/punches/me').get_json()
    assert len(own['history']) == 1
    assert own['history'][0]['user'] == 'casey@oraglass.us'

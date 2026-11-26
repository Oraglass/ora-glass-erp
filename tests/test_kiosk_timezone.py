"""Kiosk time zones. Punches use the server clock, and the day follows the tablet's zone."""

from datetime import datetime, timezone

from storage import load_kiosks, load_punches, load_time_settings
from tests.conftest import login, write_user
from timeclock import local_date, local_label, summarize


def _manager(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    client.post('/api/users', json={
        'name': 'Sam Shop',
        'email': 'sam-shop@oraglass.us',
        'role': 'shop',
        'employee_number': '441',
        'pin': '1234',
        'password': 'Validpass1',
        'temporary': False,
    })


def test_a_new_kiosk_defaults_to_vancouver_and_can_use_another_zone(client):
    _manager(client)
    page = client.get('/kiosk/setup')
    assert b'kiosk-zone' in page.data
    payroll = client.get('/payroll')
    assert b'id="kiosk-zone"' in payroll.data
    assert b'id="default-zone"' in payroll.data
    assert b'id="time-kiosk"' in payroll.data
    zones = client.get('/api/time-zones').get_json()
    assert zones['default'] == 'America/Vancouver'
    assert zones['common'][0] == 'America/Vancouver'
    assert zones['zones'][0] == 'America/Vancouver'
    assert 'America/Toronto' in zones['common']
    assert zones['zones'].index('America/Toronto') < zones['zones'].index('Europe/London')
    created = client.post('/api/time-clock/kiosks', json={'name': 'Shop tablet'})
    assert created.status_code == 200
    assert created.get_json()['kiosk']['timezone'] == 'America/Vancouver'
    assert load_kiosks()[0]['timezone'] == 'America/Vancouver'
    assert login(client, 'admin@oraglass.us').status_code == 302
    rejected = client.post('/api/time-clock/kiosks', json={'name': 'Bad', 'timezone': 'Not/AZone'})
    assert rejected.status_code == 400
    toronto = client.post('/api/time-clock/kiosks', json={'name': 'East tablet', 'timezone': 'America/Toronto'})
    assert toronto.get_json()['kiosk']['timezone'] == 'America/Toronto'
    bare = {'id': 9, 'name': 'Old tablet', 'token_hash': 'x', 'revoked': False}
    from app import _public_kiosk
    assert _public_kiosk(bare)['timezone'] == 'America/Vancouver'


def test_kiosk_punch_uses_the_server_clock_and_stores_the_tablet_zone(client):
    _manager(client)
    created = client.post('/api/time-clock/kiosks', json={'name': 'East tablet', 'timezone': 'America/Toronto'})
    token = created.get_json()['token']
    client.post('/logout')
    headers = {'X-Kiosk-Token': token}
    punched = client.post('/api/kiosk/punch', data={
        'employee_number': '441',
        'pin': '1234',
        'action': 'in',
        'at': '1999-01-01T00:00:00Z',
    }, headers=headers)
    assert punched.status_code == 200, punched.get_json()
    body = punched.get_json()
    saved = load_punches()[0]
    assert saved['at'].startswith('1999') is False
    assert saved['timezone'] == 'America/Toronto'
    assert body['timezone'] == 'America/Toronto'
    assert body['local'] == local_label(saved['at'], 'America/Toronto')
    screen = client.get('/kiosk', headers=headers)
    assert b'America/Toronto' in screen.data
    assert b'id="shop-clock"' in screen.data
    assert b'id="shop-zone"' in screen.data


def test_phone_punches_use_the_default_zone(client):
    _manager(client)
    saved = client.put('/api/time-clock/settings', json={'default_timezone': 'America/Halifax'})
    assert saved.status_code == 200
    assert load_time_settings()['default_timezone'] == 'America/Halifax'
    users = __import__('storage').load_users()
    users['sam-shop@oraglass.us']['allow_phone_punch'] = True
    __import__('storage').save_users(users)
    client.post('/logout')
    assert login(client, 'sam-shop@oraglass.us').status_code == 302
    punched = client.post('/api/punches', json={'action': 'in', 'at': '1999-01-01T00:00:00Z'})
    assert punched.status_code == 200, punched.get_json()
    stored = load_punches()[0]
    assert stored['timezone'] == 'America/Halifax'
    assert stored['at'].startswith('1999') is False
    assert stored['kiosk_id'] is None


def test_dst_boundaries_follow_the_punch_zone():
    spring_before = '2026-03-08T09:30:00+00:00'
    spring_after = '2026-03-08T10:30:00+00:00'
    assert local_label(spring_before, 'America/Vancouver') == '2026-03-08 01:30'
    assert local_label(spring_after, 'America/Vancouver') == '2026-03-08 03:30'
    fall_first = '2025-11-02T08:30:00+00:00'
    fall_second = '2025-11-02T09:30:00+00:00'
    assert local_label(fall_first, 'America/Vancouver') == '2025-11-02 01:30'
    assert local_label(fall_second, 'America/Vancouver') == '2025-11-02 01:30'
    from zoneinfo import ZoneInfo
    from timeclock import as_utc
    assert as_utc(fall_first).astimezone(ZoneInfo('America/Vancouver')).utcoffset() != as_utc(fall_second).astimezone(ZoneInfo('America/Vancouver')).utcoffset()
    # 07:30 UTC is still the previous evening in Vancouver and already morning in Toronto.
    boundary = '2026-03-08T07:30:00+00:00'
    assert local_date(boundary, 'America/Vancouver').isoformat() == '2026-03-07'
    assert local_date(boundary, 'America/Toronto').isoformat() == '2026-03-08'
    vancouver = summarize([
        {'id': 1, 'action': 'in', 'at': boundary, 'timezone': 'America/Vancouver'},
        {'id': 2, 'action': 'out', 'at': '2026-03-08T16:00:00+00:00', 'timezone': 'America/Vancouver'},
    ])
    toronto = summarize([
        {'id': 1, 'action': 'in', 'at': boundary, 'timezone': 'America/Toronto'},
        {'id': 2, 'action': 'out', 'at': '2026-03-08T16:00:00+00:00', 'timezone': 'America/Toronto'},
    ])
    assert vancouver[0][0]['date'] == '2026-03-07'
    assert toronto[0][0]['date'] == '2026-03-08'
    from payroll import attendance_range, build_stub, normalize_settings
    settings = normalize_settings({'default_timezone': 'America/Toronto', 'period_anchor': '2026-03-02', 'pay_frequency': 'weekly'})
    days = attendance_range([
        {'id': 1, 'action': 'in', 'at': '2026-03-02T13:00:00+00:00', 'timezone': 'America/Toronto'},
        {'id': 2, 'action': 'out', 'at': '2026-03-02T21:00:00+00:00', 'timezone': 'America/Toronto'},
    ], [], {}, settings, datetime(2026, 3, 2).date(), datetime(2026, 3, 8).date())
    worked = [row for row in days if row['paid_hours'] != '0.00']
    assert worked[0]['date'] == '2026-03-02'
    assert worked[0]['timezone'] == 'America/Toronto'
    assert float(worked[0]['paid_hours']) == 8.0
    stub = build_stub('shop:1', '2026-03-02', '2026-03-08', days, [
        {'id': 1, 'hourly_rate': '20.00', 'currency': 'USD', 'effective_from': '2026-01-01'},
    ], [], settings)
    assert stub['timezones'] == ['America/Toronto']
    assert stub['regular_hours'] == '8.00'


def test_shift_workbook_uses_excel_times_and_the_same_filters(client):
    from datetime import timedelta
    from io import BytesIO
    from openpyxl import load_workbook
    from storage import save_kiosks, save_punches
    write_user('admin@oraglass.us', 'admin')
    write_user('lead@oraglass.us', 'employee')
    write_user('clerk@oraglass.us', 'employee')
    from storage import load_users, save_users
    users = load_users()
    users['lead@oraglass.us']['manage_time'] = True
    users['sam-shop@oraglass.us'] = {
        'email': 'sam-shop@oraglass.us',
        'name': 'Sam Shop',
        'role': 'shop',
        'employee_number': '441',
        'active': True,
    }
    save_users(users)
    save_kiosks([{'id': 3, 'name': 'East tablet', 'timezone': 'America/Toronto', 'revoked': False}])
    monday = datetime(2026, 3, 2).date()
    punches = []
    next_id = 1
    for offset in range(6):
        day = monday + timedelta(days=offset)
        start = datetime(day.year, day.month, day.day, 8, 0, tzinfo=__import__('zoneinfo').ZoneInfo('America/Toronto'))
        end = start + timedelta(hours=8)
        punches.append({
            'id': next_id, 'user': 'sam-shop@oraglass.us', 'action': 'in',
            'at': start.astimezone(timezone.utc).isoformat(), 'timezone': 'America/Toronto',
            'kiosk_id': 3, 'corrections': [],
        })
        next_id += 1
        out = {
            'id': next_id, 'user': 'sam-shop@oraglass.us', 'action': 'out',
            'at': end.astimezone(timezone.utc).isoformat(), 'timezone': 'America/Toronto',
            'kiosk_id': 3, 'corrections': [],
        }
        if offset == 0:
            out['corrections'] = [{'by': 'lead@oraglass.us', 'reason': 'Clock was fast'}]
        punches.append(out)
        next_id += 1
    save_punches(punches)
    assert login(client, 'clerk@oraglass.us').status_code == 302
    assert client.get('/api/time-clock/shifts.xlsx').status_code == 403
    client.post('/logout')
    assert login(client, 'lead@oraglass.us').status_code == 302
    denied_page = client.get('/payroll')
    assert b'timesheet-xlsx' in denied_page.data or b'xlsx-link' in denied_page.data or b'Excel shifts' in client.get('/time-clock').data
    workbook = client.get('/api/time-clock/shifts.xlsx?user=sam-shop@oraglass.us&from=2026-03-02&to=2026-03-06')
    assert workbook.status_code == 200
    assert 'spreadsheet' in workbook.mimetype
    book = load_workbook(BytesIO(workbook.data))
    assert book.sheetnames == ['Shifts', 'Summary']
    shifts = book['Shifts']
    assert shifts.freeze_panes == 'A3'
    assert shifts['A1'].value.startswith('Shifts 2026-03-02 to 2026-03-06')
    assert 'America/Toronto' in shifts['A1'].value
    assert [shifts.cell(2, col).value for col in range(1, 12)] == [
        'Worker', 'Employee #', 'Date', 'Clock in', 'Clock out', 'Break',
        'Hours worked', 'Regular hours', 'Overtime hours', 'Kiosk/phone', 'Edited',
    ]
    assert shifts.max_row == 7
    assert shifts['A3'].value == 'Sam Shop'
    assert shifts['B3'].value == '441'
    assert shifts['C3'].value.date() == monday
    assert isinstance(shifts['D3'].value, datetime)
    assert shifts['D3'].value.hour == 8 and shifts['D3'].value.tzinfo is None
    assert isinstance(shifts['E3'].value, datetime)
    assert isinstance(shifts['F3'].value, timedelta)
    assert shifts['G3'].value == 8
    assert shifts['H3'].value == 8
    assert shifts['I3'].value == 0
    assert shifts['J3'].value == 'Kiosk East tablet'
    assert shifts['K3'].value == 'Yes, lead@oraglass.us'
    assert shifts['K4'].value == 'No'
    assert shifts.column_dimensions['D'].width >= 18
    later = client.get('/api/time-clock/shifts.xlsx?from=2026-03-07&to=2026-03-07')
    saturday = load_workbook(BytesIO(later.data))['Shifts']
    assert saturday.max_row == 3
    assert saturday['H3'].value == 0
    assert saturday['I3'].value == 8
    summary = book['Summary']
    assert summary.freeze_panes == 'A3'
    assert summary['A1'].value == shifts['A1'].value
    assert summary['A3'].value == 'Sam Shop'
    assert summary['C3'].value == 5
    assert summary['D3'].value == 40
    assert summary['E3'].value == 0
    assert summary['F3'].value == 40

"""Gross pay: weekly overtime, shifts, rates, and approval. No tax withholding."""

from datetime import datetime, timedelta, timezone

from payroll import attendance_range, build_stub, normalize_settings, period_containing
from storage import load_paystubs, load_payroll_audit, load_punches, save_punches
from tests.conftest import login, write_user
from timeclock import PACIFIC, stamp


def _at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PACIFIC)


def _span(start_id, day, start_hour, end_hour, end_minute=0, breaks=()):
    rows = [
        {'id': start_id, 'user': 'shop:441', 'action': 'in', 'at': stamp(_at(day, start_hour))},
    ]
    next_id = start_id + 1
    for began, ended in breaks:
        rows.append({'id': next_id, 'user': 'shop:441', 'action': 'break_start', 'at': stamp(_at(day, began[0], began[1]))})
        next_id += 1
        rows.append({'id': next_id, 'user': 'shop:441', 'action': 'break_end', 'at': stamp(_at(day, ended[0], ended[1]))})
        next_id += 1
    end = _at(day, end_hour, end_minute)
    if (end_hour, end_minute) <= (start_hour, 0):
        end += timedelta(days=1)
    rows.append({'id': next_id, 'user': 'shop:441', 'action': 'out', 'at': stamp(end)})
    return rows


def _days(start, count, start_hour=8, end_hour=16):
    punches = []
    next_id = 1
    for offset in range(count):
        chunk = _span(next_id, start + timedelta(days=offset), start_hour, end_hour)
        punches.extend(chunk)
        next_id = chunk[-1]['id'] + 1
    return punches


def _paid(punches, assignments=None, shifts=None, settings=None, start=None, end=None):
    settings = settings or normalize_settings({})
    days = attendance_range(punches, assignments or [], shifts or {}, settings, start, end)
    return days


def test_weekly_overtime_starts_after_forty_hours():
    settings = normalize_settings({'period_anchor': '2026-11-02', 'pay_frequency': 'weekly'})
    start, end = period_containing(datetime(2026, 11, 2).date(), settings)
    assert start.isoformat() == '2026-11-02'
    assert end.isoformat() == '2026-11-08'
    monday = start
    punches = _days(monday, 6)  # Mon-Sat, 8 hours each
    days = _paid(punches, start=monday - timedelta(days=7), end=end)
    stub = build_stub('shop:441', start, end, days, [
        {'id': 1, 'hourly_rate': '20.00', 'currency': 'USD', 'effective_from': '2026-01-01'},
    ], [], settings)
    assert stub['regular_hours'] == '40.00'
    assert stub['overtime_hours'] == '8.00'
    assert stub['regular_pay'] == '800.00'
    assert stub['overtime_pay'] == '240.00'
    assert stub['gross_pay'] == '1040.00'
    assert stub['label'] == 'Gross pay (before taxes)'
    assert stub['ytd_gross'] == '1040.00'


def test_overtime_keeps_counting_across_a_biweekly_boundary():
    settings = normalize_settings({'period_anchor': '2026-11-04', 'pay_frequency': 'biweekly', 'work_week_start': 0})
    period_start, period_end = period_containing(datetime(2026, 11, 4).date(), settings)
    assert period_start.isoformat() == '2026-11-04'
    assert period_end.isoformat() == '2026-11-17'
    monday = datetime(2026, 11, 2).date()
    punches = _days(monday, 6)
    days = _paid(punches, start=monday, end=period_end)
    stub = build_stub('shop:441', period_start, period_end, days, [
        {'id': 1, 'hourly_rate': '20.00', 'currency': 'USD', 'effective_from': '2026-01-01'},
    ], [], settings)
    # Monday and Tuesday (16h) sit in the previous period and use up part of the 40-hour week.
    assert stub['regular_hours'] == '24.00'
    assert stub['overtime_hours'] == '8.00'
    assert stub['regular_pay'] == '480.00'
    assert stub['overtime_pay'] == '240.00'
    assert stub['gross_pay'] == '720.00'


def test_a_mid_period_rate_change_applies_only_forward():
    settings = normalize_settings({'period_anchor': '2026-11-02', 'pay_frequency': 'weekly'})
    start, end = period_containing(datetime(2026, 11, 2).date(), settings)
    punches = _days(start, 6)
    days = _paid(punches, start=start, end=end)
    stub = build_stub('shop:441', start, end, days, [
        {'id': 1, 'hourly_rate': '20.00', 'currency': 'USD', 'effective_from': '2026-01-01'},
        {'id': 2, 'hourly_rate': '30.00', 'currency': 'USD', 'effective_from': '2026-11-05'},
    ], [
        {'id': 1, 'worker': 'shop:441', 'period_start': '2026-11-02', 'kind': 'bonus', 'amount': '50.00', 'reason': 'Install'},
    ], settings)
    # Mon-Wed 24h at 20, Thu-Fri 16h at 30, Saturday 8h OT at 30.
    assert stub['regular_pay'] == '960.00'
    assert stub['overtime_pay'] == '360.00'
    assert stub['adjustment_total'] == '50.00'
    assert stub['gross_pay'] == '1370.00'
    assert [row['hourly_rate'] for row in stub['rates']] == ['20.00', '30.00']
    assert stub['adjustments'][0]['reason'] == 'Install'


def test_break_punches_win_and_the_shift_break_applies_when_the_span_covers_it():
    settings = normalize_settings({'grace_minutes': 5})
    day = datetime(2026, 11, 2).date()
    shift = {
        'id': 1, 'name': 'Day', 'start': '07:00', 'end': '15:30',
        'weekdays': [0, 1, 2, 3, 4], 'unpaid_break_minutes': 30, 'active': True,
    }
    assignments = [{'id': 1, 'shift_id': 1, 'effective_from': '2026-01-01'}]
    full = _span(1, day, 7, 15, 30)
    covered = _paid(full, assignments, {1: shift}, settings, day, day)[0]
    assert covered['actual_hours'] == '8.50'
    assert covered['paid_hours'] == '8.00'
    assert covered['scheduled_hours'] == '8.00'
    assert covered['flags'] == []

    with_break = _span(1, day, 7, 15, 30, breaks=(((12, 0), (12, 30)),))
    punched = _paid(with_break, assignments, {1: shift}, settings, day, day)[0]
    assert punched['paid_hours'] == '8.00'
    assert punched['actual_hours'] == '8.00'

    short = _span(1, day, 7, 11)
    morning = _paid(short, assignments, {1: shift}, settings, day, day)[0]
    assert morning['paid_hours'] == '4.00'
    assert morning['early_minutes'] > 0
    assert any(flag.startswith('Left') for flag in morning['flags'])

    late = _span(1, day, 7, 15, 30)
    late[0]['at'] = stamp(_at(day, 7, 20))
    arrived = _paid(late, assignments, {1: shift}, settings, day, day)[0]
    assert arrived['late_minutes'] == 20
    assert 'Late 20 min' in arrived['flags']

    missed = _paid([], assignments, {1: shift}, settings, day, day)[0]
    assert missed['missed_shift'] is True
    assert 'Missed shift' in missed['flags']


def test_approval_locks_hours_until_the_period_is_reopened(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('clerk@oraglass.us', 'employee')
    from storage import load_users, save_users
    users = load_users()
    users['clerk@oraglass.us']['modules'] = ['dashboard', 'time-clock']
    users['clerk@oraglass.us']['manage_time'] = True
    users['clerk@oraglass.us']['manage_payroll'] = False
    users['clerk@oraglass.us']['uses_time_clock'] = False
    save_users(users)
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/users', json={
        'name': 'Sam Shop', 'role': 'shop', 'employee_number': '441', 'pin': '1234',
    })
    assert created.status_code == 200
    client.post('/logout')
    assert login(client, 'clerk@oraglass.us').status_code == 302
    blocked = client.get('/api/payroll/period')
    assert blocked.status_code == 403
    clock_page = client.get('/payroll')
    assert clock_page.status_code == 200
    assert b'Time & Kiosk' in clock_page.data
    assert b'Save settings' not in clock_page.data
    client.post('/logout')

    assert login(client, 'admin@oraglass.us').status_code == 302
    payroll_user = client.post('/api/users', json={
        'name': 'Pat Payroll',
        'email': 'pat.payroll@oraglass.us',
        'role': 'employee',
        'password': 'Validpass1',
        'temporary': False,
        'manage_payroll': True,
        'modules': ['dashboard'],
    })
    assert payroll_user.status_code == 200
    client.post('/logout')
    assert login(client, 'pat.payroll@oraglass.us').status_code == 302
    rate = client.post('/api/payroll/rates', json={
        'worker': 'shop:441', 'hourly_rate': '22.00', 'currency': 'USD', 'effective_from': '2026-11-02',
    })
    assert rate.status_code == 200
    client.post('/logout')

    monday = datetime(2026, 11, 2).date()
    save_punches(_days(monday, 6))
    assert login(client, 'admin@oraglass.us').status_code == 302
    sheet = client.get('/api/time-clock?from=2026-11-02&to=2026-11-08&user=shop:441')
    assert sheet.status_code == 200
    attendance = sheet.get_json()['attendance']
    assert attendance[0]['actual_hours'] == '8.00'
    period = client.get('/api/payroll/period?start=2026-11-02').get_json()
    stub = next(row for row in period['stubs'] if row['worker'] == 'shop:441')
    assert stub['regular_hours'] == '40.00'
    assert stub['overtime_hours'] == '8.00'
    assert stub['gross_pay'] == '1144.00'
    assert period['label'] == 'Gross pay (before taxes)'
    bonus = client.post('/api/payroll/adjustments', json={
        'worker': 'shop:441', 'period_start': '2026-11-02', 'kind': 'reimbursement',
        'amount': '15.00', 'reason': 'Boots',
    })
    assert bonus.status_code == 200
    approved = client.post('/api/payroll/approve', json={'worker': 'shop:441', 'period_start': '2026-11-02'})
    assert approved.status_code == 200
    assert approved.get_json()['gross_pay'] == '1159.00'
    assert approved.get_json()['status'] == 'approved'
    pdf = client.get('/api/payroll/paystub.pdf?worker=shop:441&start=2026-11-02')
    assert pdf.status_code == 200
    assert pdf.data.startswith(b'%PDF')
    assert len(pdf.data) > 1000
    summary = client.get('/api/payroll/summary.pdf?start=2026-11-02')
    assert summary.status_code == 200
    assert summary.data.startswith(b'%PDF')
    locked = client.post(f"/api/time-clock/{load_punches()[0]['id']}/correct", json={
        'reason': 'Change a locked day', 'at_pacific': '2026-11-02T08:05',
    })
    assert locked.status_code == 409
    assert b'Reopen' in locked.data
    assert client.post('/api/payroll/adjustments', json={
        'worker': 'shop:441', 'period_start': '2026-11-02', 'kind': 'bonus', 'amount': '5.00', 'reason': 'Too late',
    }).status_code == 409
    reopened = client.post('/api/payroll/reopen', json={
        'worker': 'shop:441', 'period_start': '2026-11-02', 'reason': 'Missed punch',
    })
    assert reopened.status_code == 200
    assert reopened.get_json()['status'] == 'draft'
    fixed = client.post(f"/api/time-clock/{load_punches()[0]['id']}/correct", json={
        'reason': 'Clock was fast', 'at_pacific': '2026-11-02T08:05',
    })
    assert fixed.status_code == 200
    actions = [row['action'] for row in load_payroll_audit()]
    assert 'pay_rate' in actions
    assert 'approve' in actions
    assert 'reopen' in actions
    assert any(row.get('status') == 'draft' for row in load_paystubs())
    assert client.put('/api/users/shop:441', json={'allow_phone_punch': True}).status_code == 200
    client.post('/logout')
    shop = client.post('/punch/login', data={'employee_number': '441', 'pin': '1234'})
    assert shop.status_code == 302
    assert client.get('/api/payroll/period').status_code == 403
    assert client.get('/payroll').status_code == 302
    assert b'Gross pay' not in client.get('/kiosk').data

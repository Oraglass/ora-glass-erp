"""Punch state and payroll hours. Times are stored in UTC and counted on Pacific dates."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')
ACTIONS = ('in', 'out', 'break_start', 'break_end')


def as_utc(value, naive_is_pacific=False):
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or '').strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=PACIFIC if naive_is_pacific else timezone.utc)
    return moment.astimezone(timezone.utc)


def stamp(moment):
    return as_utc(moment).replace(microsecond=0).isoformat()


def pacific_date(moment):
    return as_utc(moment).astimezone(PACIFIC).date()


def pacific_clock(moment):
    return as_utc(moment).astimezone(PACIFIC).strftime('%H:%M')


def pacific_label(moment):
    return as_utc(moment).astimezone(PACIFIC).strftime('%Y-%m-%d %H:%M')


def _hours(start, end):
    return max(0.0, (end - start).total_seconds() / 3600.0)


def replay(punches):
    """Turn a user's punches into shifts. An open shift has no punch-out."""
    ordered = sorted(punches, key=lambda row: (row.get('at') or '', row.get('id') or 0))
    shifts = []
    current = None
    for punch in ordered:
        action = punch.get('action')
        moment = as_utc(punch.get('at'))
        if action == 'in':
            if current is not None:
                raise ValueError('Already punched in.')
            current = {
                'in': moment,
                'in_id': punch.get('id'),
                'breaks': [],
                'open_break': None,
                'out': None,
                'out_id': None,
            }
        elif action == 'break_start':
            if current is None or current['out'] or current['open_break']:
                raise ValueError('Start a break only while punched in.')
            current['open_break'] = moment
        elif action == 'break_end':
            if current is None or current['open_break'] is None:
                raise ValueError('There is no break to end.')
            current['breaks'].append((current['open_break'], moment))
            current['open_break'] = None
        elif action == 'out':
            if current is None or current['out']:
                raise ValueError('Punch in before punching out.')
            if current['open_break']:
                current['breaks'].append((current['open_break'], moment))
                current['open_break'] = None
            current['out'] = moment
            current['out_id'] = punch.get('id')
            shifts.append(current)
            current = None
        else:
            raise ValueError('Unknown punch.')
    if current is not None:
        shifts.append(current)
    return shifts


def clock_status(punches):
    shifts = replay(punches)
    if not shifts or shifts[-1]['out'] is not None:
        return 'out', None
    current = shifts[-1]
    if current['open_break'] is not None:
        return 'break', current['open_break']
    return 'in', current['in']


def shift_hours(shift, now=None, live=False):
    """Closed shifts always count. Open shifts count only when live is set."""
    end = shift['out']
    if end is None:
        if not live or now is None:
            return None
        end = now
    elapsed = _hours(shift['in'], end)
    for start, stop in shift['breaks']:
        elapsed -= _hours(start, stop or end)
    if shift['open_break'] is not None and live and now is not None:
        elapsed -= _hours(shift['open_break'], now)
    return round(max(0.0, elapsed), 2)


def summarize(punches, start=None, end=None, now=None):
    """Hours per Pacific day of punch-in. Breaks are deducted. Open shifts are flagged."""
    now = as_utc(now or datetime.now(timezone.utc))
    shifts = replay(punches)
    days = {}
    for shift in shifts:
        day = pacific_date(shift['in'])
        if start and day < start:
            continue
        if end and day > end:
            continue
        bucket = days.setdefault(day, {'date': day.isoformat(), 'hours': 0.0, 'missing_out': False, 'shifts': 0})
        bucket['shifts'] += 1
        closed = shift_hours(shift, now=now, live=False)
        if closed is None:
            bucket['missing_out'] = True
        else:
            bucket['hours'] = round(bucket['hours'] + closed, 2)
    ordered = [days[key] for key in sorted(days)]
    week_hours = round(sum(day['hours'] for day in ordered), 2)
    return ordered, week_hours


def live_hours(punches, start, end, now=None):
    """Closed hours plus the current open shift, for the employee's own display."""
    now = as_utc(now or datetime.now(timezone.utc))
    total = 0.0
    for shift in replay(punches):
        day = pacific_date(shift['in'])
        if day < start or day > end:
            continue
        amount = shift_hours(shift, now=now, live=True)
        if amount:
            total += amount
    return round(total, 2)


def week_start(day):
    return day - timedelta(days=day.weekday())

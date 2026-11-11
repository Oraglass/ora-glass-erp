"""Gross pay from punches, shifts, and weekly overtime. No tax withholding."""

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from timeclock import PACIFIC, pacific_date, replay

CENT = Decimal('0.01')
HOUR = Decimal('0.01')
WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
GROSS_LABEL = 'Gross pay (before taxes)'


def _dec(value, default='0'):
    try:
        return Decimal(str(value if value not in (None, '') else default))
    except Exception:
        return Decimal(default)


def q_hours(value):
    return f"{_dec(value).quantize(HOUR, rounding=ROUND_HALF_UP):.2f}"


def q_money(value):
    return f"{_dec(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def normalize_settings(raw):
    """Fill payroll defaults. Photo retention is left to the caller."""
    raw = raw if isinstance(raw, dict) else {}
    rounding = str(raw.get('rounding') or 'none')
    if rounding not in {'none', '5', '15'}:
        rounding = 'none'
    try:
        grace = int(raw.get('grace_minutes') or 0)
    except (TypeError, ValueError):
        grace = 0
    grace = min(120, max(0, grace))
    try:
        week = int(0 if raw.get('work_week_start') in (None, '') else raw.get('work_week_start'))
    except (TypeError, ValueError):
        week = 0
    if week not in range(7):
        week = 0
    threshold = _dec(raw.get('overtime_threshold_hours') if raw.get('overtime_threshold_hours') not in (None, '') else 40, '40')
    if threshold <= 0 or threshold > 168:
        threshold = Decimal('40')
    frequency = str(raw.get('pay_frequency') or 'weekly')
    if frequency not in {'weekly', 'biweekly', 'semimonthly'}:
        frequency = 'weekly'
    anchor_text = str(raw.get('period_anchor') or '2026-01-05')
    try:
        anchor = date.fromisoformat(anchor_text)
    except ValueError:
        anchor = date(2026, 1, 5)
    return {
        'rounding': rounding,
        'grace_minutes': grace,
        'work_week_start': week,
        'work_week_start_name': WEEKDAY_NAMES[week],
        'overtime_threshold_hours': q_hours(threshold),
        'pay_frequency': frequency,
        'period_anchor': anchor.isoformat(),
    }


def validate_settings(raw):
    """Return (settings, error). Semi-monthly anchors must fall on the 1st or the 16th."""
    settings = normalize_settings(raw)
    if settings['pay_frequency'] == 'semimonthly':
        anchor = date.fromisoformat(settings['period_anchor'])
        if anchor.day not in (1, 16):
            return None, 'Semi-monthly periods start on the 1st or the 16th.'
    try:
        threshold = _dec(settings['overtime_threshold_hours'])
    except Exception:
        return None, 'Overtime threshold must be a number of hours.'
    if threshold <= 0 or threshold > 168:
        return None, 'Overtime threshold must be between 0 and 168 hours.'
    return settings, None


def period_containing(day, settings):
    """Return the pay period (start, end) that contains day."""
    settings = normalize_settings(settings)
    if isinstance(day, datetime):
        day = day.date()
    if settings['pay_frequency'] == 'semimonthly':
        if day.day <= 15:
            return day.replace(day=1), day.replace(day=15)
        next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
        return day.replace(day=16), last
    length = 7 if settings['pay_frequency'] == 'weekly' else 14
    anchor = date.fromisoformat(settings['period_anchor'])
    periods = (day - anchor).days // length
    start = anchor + timedelta(days=periods * length)
    return start, start + timedelta(days=length - 1)


def week_bounds(day, week_start):
    if isinstance(day, datetime):
        day = day.date()
    delta = (day.weekday() - int(week_start)) % 7
    start = day - timedelta(days=delta)
    return start, start + timedelta(days=6)


def weeks_covering(period_start, period_end, week_start):
    first, _end = week_bounds(period_start, week_start)
    _start, last = week_bounds(period_end, week_start)
    return first, last


def _hhmm(value):
    text = str(value or '').strip()
    hour, minute = text.split(':')
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError
    return hour, minute


def shift_bounds(day, shift):
    sh, sm = _hhmm(shift.get('start'))
    eh, em = _hhmm(shift.get('end'))
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=PACIFIC)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=PACIFIC)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def break_window(day, shift):
    minutes = int(shift.get('unpaid_break_minutes') or 0)
    if minutes <= 0:
        return None
    start, end = shift_bounds(day, shift)
    midpoint = start + (end - start) / 2
    half = timedelta(minutes=minutes) / 2
    return midpoint - half, midpoint + half


def scheduled_hours(day, shift):
    if not shift:
        return Decimal('0')
    start, end = shift_bounds(day, shift)
    minutes = Decimal(str((end - start).total_seconds())) / Decimal('60')
    minutes -= Decimal(int(shift.get('unpaid_break_minutes') or 0))
    if minutes < 0:
        minutes = Decimal('0')
    return (minutes / Decimal('60')).quantize(HOUR, rounding=ROUND_HALF_UP)


def _round_minutes(minutes, step):
    if not step:
        return minutes
    step = Decimal(int(step))
    units = (minutes / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return units * step


def effective_row(rows, day):
    """Latest row whose effective_from is on or before day. Same date: higher id wins."""
    chosen = None
    chosen_key = None
    for row in rows or []:
        try:
            effective = date.fromisoformat(str(row.get('effective_from')))
        except ValueError:
            continue
        if effective > day:
            continue
        key = (effective, int(row.get('id') or 0))
        if chosen_key is None or key >= chosen_key:
            chosen = row
            chosen_key = key
    return chosen


def shift_for_day(assignments, shifts_by_id, day):
    assigned = effective_row(assignments, day)
    if not assigned or not assigned.get('shift_id'):
        return None
    shift = shifts_by_id.get(assigned.get('shift_id'))
    if not shift or shift.get('active') is False:
        return None
    weekdays = [int(item) for item in (shift.get('weekdays') or [])]
    if day.weekday() not in weekdays:
        return None
    return shift


def rate_for_day(rates, day):
    return effective_row(rates, day)


def attendance_range(punches, assignments, shifts_by_id, settings, start, end):
    """One row per day from start through end. Paid hours deduct breaks, then round."""
    settings = normalize_settings(settings)
    step = 0 if settings['rounding'] == 'none' else int(settings['rounding'])
    grace = timedelta(minutes=int(settings['grace_minutes']))
    try:
        spans = replay(punches or [])
    except ValueError:
        spans = []
    rows = []
    day = start
    while day <= end:
        shift = shift_for_day(assignments, shifts_by_id, day)
        day_spans = [span for span in spans if pacific_date(span['in']) == day]
        closed = [span for span in day_spans if span.get('out')]
        missing_out = any(span.get('out') is None for span in day_spans)
        actual_seconds = 0.0
        used_break_punches = False
        for span in closed:
            actual_seconds += max(0.0, (span['out'] - span['in']).total_seconds())
            for began, ended in span['breaks']:
                actual_seconds -= max(0.0, (ended - began).total_seconds())
            if span['breaks']:
                used_break_punches = True
        actual_seconds = max(0.0, actual_seconds)
        paid_seconds = actual_seconds
        if shift and closed and not used_break_punches:
            window = break_window(day, shift)
            if window:
                for span in closed:
                    if span['in'] <= window[0] and span['out'] >= window[1]:
                        paid_seconds -= int(shift.get('unpaid_break_minutes') or 0) * 60
                        break
        paid_seconds = max(0.0, paid_seconds)
        paid_minutes = _round_minutes(Decimal(str(paid_seconds)) / Decimal('60'), step)
        paid = (paid_minutes / Decimal('60')).quantize(HOUR, rounding=ROUND_HALF_UP)
        actual = (Decimal(str(actual_seconds)) / Decimal('3600')).quantize(HOUR, rounding=ROUND_HALF_UP)
        late_minutes = 0
        early_minutes = 0
        if shift and day_spans:
            shift_start, shift_end = shift_bounds(day, shift)
            first_in = day_spans[0]['in']
            if first_in > shift_start + grace:
                late_minutes = int((first_in - shift_start).total_seconds() // 60)
            if closed and not missing_out:
                last_out = closed[-1]['out']
                if last_out + grace < shift_end:
                    early_minutes = int((shift_end - last_out).total_seconds() // 60)
        scheduled = scheduled_hours(day, shift)
        missed_shift = bool(shift) and not day_spans
        flags = []
        if missing_out:
            flags.append('Missing punch-out')
        if missed_shift:
            flags.append('Missed shift')
        if late_minutes:
            flags.append(f'Late {late_minutes} min')
        if early_minutes:
            flags.append(f'Left {early_minutes} min early')
        rows.append({
            'date': day.isoformat(),
            'scheduled_hours': q_hours(scheduled),
            'actual_hours': q_hours(actual),
            'paid_hours': q_hours(paid),
            'late_minutes': late_minutes,
            'early_minutes': early_minutes,
            'missing_out': missing_out,
            'missed_shift': missed_shift,
            'flags': flags,
            'shift_name': (shift or {}).get('name') or '',
        })
        day += timedelta(days=1)
    return rows


def build_stub(worker, period_start, period_end, days, rates, adjustments, settings, prior_ytd='0'):
    """Gross pay for one worker. Overtime is 1.5x after the weekly threshold, including hours before the period."""
    settings = normalize_settings(settings)
    threshold = _dec(settings['overtime_threshold_hours'])
    if isinstance(period_start, str):
        period_start = date.fromisoformat(period_start)
    if isinstance(period_end, str):
        period_end = date.fromisoformat(period_end)
    grouped = {}
    for row in days or []:
        day = row['date']
        if isinstance(day, str):
            day = date.fromisoformat(day)
        week_start, _week_end = week_bounds(day, settings['work_week_start'])
        grouped.setdefault(week_start, []).append((day, _dec(row.get('paid_hours'))))
    pieces = []
    week_totals = []
    missing_rate = False
    for week_start in sorted(grouped):
        regular_left = threshold
        week_regular = Decimal('0')
        week_ot = Decimal('0')
        for day, hours in sorted(grouped[week_start]):
            regular = min(hours, max(Decimal('0'), regular_left))
            overtime = hours - regular
            regular_left -= regular
            week_regular += regular
            week_ot += overtime
            if not (period_start <= day <= period_end):
                continue
            rate_row = rate_for_day(rates, day)
            if hours > 0 and not rate_row:
                missing_rate = True
            rate = _dec(rate_row.get('hourly_rate')) if rate_row else Decimal('0')
            currency = str((rate_row or {}).get('currency') or 'USD').upper()
            pieces.append({
                'date': day.isoformat(),
                'hourly_rate': q_money(rate),
                'currency': currency,
                'regular_hours': regular,
                'overtime_hours': overtime,
            })
        week_totals.append({
            'week_start': week_start.isoformat(),
            'regular_hours': q_hours(week_regular),
            'overtime_hours': q_hours(week_ot),
        })
    by_rate = {}
    regular_hours = Decimal('0')
    overtime_hours = Decimal('0')
    regular_pay = Decimal('0')
    overtime_pay = Decimal('0')
    currencies = set()
    for piece in pieces:
        regular_hours += piece['regular_hours']
        overtime_hours += piece['overtime_hours']
        rate = _dec(piece['hourly_rate'])
        currencies.add(piece['currency'])
        reg_pay = piece['regular_hours'] * rate
        ot_pay = piece['overtime_hours'] * rate * Decimal('1.5')
        regular_pay += reg_pay
        overtime_pay += ot_pay
        bucket = by_rate.setdefault((piece['currency'], piece['hourly_rate']), {
            'currency': piece['currency'],
            'hourly_rate': piece['hourly_rate'],
            'regular_hours': Decimal('0'),
            'overtime_hours': Decimal('0'),
            'regular_pay': Decimal('0'),
            'overtime_pay': Decimal('0'),
        })
        bucket['regular_hours'] += piece['regular_hours']
        bucket['overtime_hours'] += piece['overtime_hours']
        bucket['regular_pay'] += reg_pay
        bucket['overtime_pay'] += ot_pay
    kept_adjustments = []
    adjustment_total = Decimal('0')
    for row in adjustments or []:
        if row.get('worker') != worker or str(row.get('period_start')) != period_start.isoformat():
            continue
        amount = _dec(row.get('amount'))
        adjustment_total += amount
        kept_adjustments.append({
            'id': row.get('id'),
            'kind': row.get('kind'),
            'amount': q_money(amount),
            'reason': row.get('reason') or '',
        })
    mixed = len(currencies) > 1
    currency = 'USD' if not currencies else (sorted(currencies)[0] if not mixed else 'MIXED')
    gross = regular_pay + overtime_pay + adjustment_total
    ytd = _dec(prior_ytd) + gross
    rates_out = []
    for bucket in by_rate.values():
        if bucket['regular_hours'] == 0 and bucket['overtime_hours'] == 0:
            continue
        rates_out.append({
            'currency': bucket['currency'],
            'hourly_rate': bucket['hourly_rate'],
            'regular_hours': q_hours(bucket['regular_hours']),
            'overtime_hours': q_hours(bucket['overtime_hours']),
            'regular_pay': q_money(bucket['regular_pay']),
            'overtime_pay': q_money(bucket['overtime_pay']),
        })
    rates_out.sort(key=lambda row: (row['currency'], _dec(row['hourly_rate'])))
    return {
        'worker': worker,
        'period_start': period_start.isoformat(),
        'period_end': period_end.isoformat(),
        'currency': currency,
        'regular_hours': q_hours(regular_hours),
        'overtime_hours': q_hours(overtime_hours),
        'regular_pay': q_money(regular_pay),
        'overtime_pay': q_money(overtime_pay),
        'adjustments': kept_adjustments,
        'adjustment_total': q_money(adjustment_total),
        'gross_pay': None if mixed else q_money(gross),
        'ytd_gross': None if mixed else q_money(ytd),
        'rates': rates_out,
        'weeks': week_totals,
        'missing_rate': missing_rate,
        'mixed_currency': mixed,
        'label': GROSS_LABEL,
    }

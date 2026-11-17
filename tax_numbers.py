"""Append-only PST and QST history.

A number counts for sales tax only when it looks like a saved registration.
Blank, whitespace, and short values do not. History rows are inserted and
never rewritten.
"""

import re
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

from storage import append_tax_number_history, load_customers, load_tax_number_history

FIELDS = {
    'pst_number': 'PST number',
    'qst_number': 'QST number',
    'gst_number': 'GST/HST number',
}
# Statutory rates. A saved PST number drops the provincial tax in BC, SK, and MB.
# A saved QST number drops QST. HST provinces stay HST.
TAX_RATES = {
    'BC': (('GST', '5'), ('PST', '7')),
    'SK': (('GST', '5'), ('PST', '6')),
    'MB': (('GST', '5'), ('RST', '7')),
    'QC': (('GST', '5'), ('QST', '9.975')),
    'ON': (('HST', '13'),),
    'NB': (('HST', '15'),),
    'NL': (('HST', '15'),),
    'NS': (('HST', '14'),),
    'PE': (('HST', '15'),),
    'AB': (('GST', '5'),),
    'NT': (('GST', '5'),),
    'NU': (('GST', '5'),),
    'YT': (('GST', '5'),),
}
PST_PROVINCES = {'BC', 'SK', 'MB'}
SELLER_GST_ID = 'ora-glass-canada'
SELLER_GST_INITIAL = '729631119 RT0001'
# At least five characters, one digit, and no stray punctuation.
_VALID = re.compile(r'^(?=.*\d)[A-Za-z0-9][A-Za-z0-9 -]{4,39}$')


def normalize(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def rate_schedule(province, pst_number='', qst_number=''):
    """Name and percent for each tax that applies. Empty when the province is not Canadian."""
    code = str(province or '').strip().upper()
    rows = list(TAX_RATES.get(code) or ())
    if code in PST_PROVINCES and saved_number(pst_number):
        rows = [row for row in rows if row[0] == 'GST']
    if code == 'QC' and saved_number(qst_number):
        rows = [row for row in rows if row[0] == 'GST']
    return [{'name': name, 'rate': rate} for name, rate in rows]


def tax_amount(taxable, rate):
    """Round half up to the cent. 100 at 9.975% is 9.98."""
    from decimal import Decimal, ROUND_HALF_UP
    base = Decimal(str(taxable or 0))
    percent = Decimal(str(rate))
    amount = (base * percent / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(amount)


def tax_breakdown(province, taxable, pst_number='', qst_number='', rates=None):
    """Dollar lines for a pretax total. A stored rate list freezes the regime."""
    chosen = rates if rates is not None else rate_schedule(province, pst_number, qst_number)
    lines = []
    for row in chosen or []:
        name = str(row.get('name') or '')
        rate = str(row.get('rate') or '')
        if not name or not rate:
            continue
        lines.append({
            'name': name,
            'rate': rate,
            'amount': tax_amount(taxable, rate),
        })
    return lines


def tax_total(lines):
    from decimal import Decimal, ROUND_HALF_UP
    total = sum((Decimal(str(row.get('amount') or 0)) for row in lines or []), Decimal('0'))
    return float(total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def saved_number(value):
    """The number to use for tax. Empty when nothing valid is on file."""
    text = normalize(value)
    if _VALID.fullmatch(text):
        return text
    return ''


def validate_change(current, new_value, confirm, reason, remove=False):
    """Return (stored_value, error). An empty stored value removes the number."""
    why = normalize(reason)
    if not why:
        return None, 'A reason is required.'
    current_text = normalize(current)
    if remove:
        entered = normalize(new_value)
        confirmed = normalize(confirm)
        if entered or confirmed:
            return None, 'Leave both number fields blank to remove the saved number.'
        if not current_text:
            return None, 'There is no saved number to remove.'
        return '', None
    new_text = normalize(new_value)
    confirm_text = normalize(confirm)
    if not new_text or not confirm_text:
        return None, 'Enter the new number twice. Spaces alone are not a number.'
    if new_text != confirm_text:
        return None, 'Enter the new number twice, and make both entries match.'
    if not saved_number(new_text):
        return None, 'Enter a number with at least 5 characters and one digit.'
    if saved_number(new_text) == saved_number(current):
        return None, 'That is already the saved number.'
    return saved_number(new_text), None


def history_for(customer_id):
    wanted = str(customer_id or '')
    return [row for row in load_tax_number_history() if row.get('customer_id') == wanted]


def record_change(customer, customer_id, field, new_value, user_email, reason, ip):
    """Append history and the customer changelog, then store the new number."""
    old = normalize(customer.get(field))
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entry = {
        'customer_id': customer_id,
        'field': field,
        'old_value': old,
        'new_value': new_value,
        'changed_by': user_email or '',
        'at': at,
        'reason': normalize(reason),
        'ip': ip or '',
    }
    append_tax_number_history(entry)
    log = list(customer.get('changelog') or [])
    log.append({
        'at': at,
        'user': user_email or '',
        'ip': ip or '',
        'reason': entry['reason'],
        'changes': [{'field': field, 'old': old, 'new': new_value}],
    })
    customer['changelog'] = log
    customer[field] = new_value
    return entry


def seed_current_numbers():
    """One initial history row for each number already on file. A second run adds nothing."""
    customers = load_customers()
    existing = {(row.get('customer_id'), row.get('field')) for row in load_tax_number_history()}
    added = 0
    for customer_id, customer in customers.items():
        if not isinstance(customer, dict):
            continue
        for field in FIELDS:
            number = saved_number(customer.get(field))
            if not number or (customer_id, field) in existing:
                continue
            customer[field] = ''
            record_change(
                customer, customer_id, field, number, '',
                'Initial number on file', '',
            )
            added += 1
    if added:
        from storage import save_customers
        save_customers(customers)
    return added


def seller_gst_number():
    """Ora Glass (Canada) Inc GST/HST registration, or empty when it has not been set."""
    import quickbooks
    with quickbooks.connection_scope('ca'):
        settings = quickbooks.load_settings()
    return saved_number(settings.get('gst_number'))


def seed_seller_gst():
    """Store Adrian's public GST number once, only while the Canada setting is empty."""
    import quickbooks
    if history_for(SELLER_GST_ID):
        return 0
    with quickbooks.connection_scope('ca'):
        settings = quickbooks.load_settings()
        if saved_number(settings.get('gst_number')):
            return 0
        settings['gst_number'] = SELLER_GST_INITIAL
        quickbooks.save_settings(settings)
    record_change(
        {'gst_number': ''}, SELLER_GST_ID, 'gst_number', SELLER_GST_INITIAL, '',
        'Initial value', '',
    )
    return 1


def history_pdf(customer_id, company, rows):
    """A PDF of the append-only tax-number history."""
    styles = getSampleStyleSheet()
    body = styles['BodyText']
    body.fontSize = 8
    body.leading = 10
    story = [
        Paragraph(escape(f'Tax number history — {company or customer_id}'), styles['Title']),
        Spacer(1, 8),
        Paragraph(
            'This record is append-only. Entries cannot be edited or deleted.',
            body,
        ),
        Spacer(1, 8),
    ]
    header = ['When', 'Field', 'Old', 'New', 'Changed by', 'Reason', 'IP']
    data = [header]
    for row in rows:
        data.append([
            str(row.get('at') or ''),
            FIELDS.get(row.get('field'), row.get('field') or ''),
            str(row.get('old_value') or '—'),
            str(row.get('new_value') or '—'),
            str(row.get('changed_by') or '—'),
            str(row.get('reason') or ''),
            str(row.get('ip') or ''),
        ])
    if len(data) == 1:
        data.append(['—', '—', '—', '—', '—', 'No changes recorded', '—'])
    table = Table(data, repeatRows=1)
    story.append(table)
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36).build(story)
    return buffer.getvalue()

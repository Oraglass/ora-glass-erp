"""Resale certificates. Files stay on the data disk. The history is append-only."""

import os
import re
import secrets
from datetime import date, datetime

from zoneinfo import ZoneInfo

from storage import US_STATE_CODES, data_dir, load_customers, save_customers

MAX_BYTES = 8 * 1024 * 1024
ALLOWED = {
    'pdf': ('application/pdf', lambda payload: payload.startswith(b'%PDF')),
    'png': ('image/png', lambda payload: payload.startswith(b'\x89PNG\r\n\x1a\n')),
    'jpg': ('image/jpeg', lambda payload: payload.startswith(b'\xff\xd8\xff')),
    'jpeg': ('image/jpeg', lambda payload: payload.startswith(b'\xff\xd8\xff')),
    'gif': ('image/gif', lambda payload: payload.startswith(b'GIF8')),
    'webp': ('image/webp', lambda payload: payload.startswith(b'RIFF') and payload[8:12] == b'WEBP'),
}


class ResaleError(Exception):
    pass


def tax_today():
    return datetime.now(ZoneInfo('America/Los_Angeles')).date()


def _folder(customer_id):
    safe = re.sub(r'[^A-Za-z0-9_-]', '', str(customer_id or '')) or 'customer'
    path = os.path.join(data_dir(), 'resale_certificates', safe)
    os.makedirs(path, exist_ok=True)
    return path


def check_certificate_file(filename, payload):
    ext = os.path.splitext(filename or '')[1].lower().lstrip('.')
    spec = ALLOWED.get(ext)
    if not spec:
        raise ResaleError('Upload a PDF or an image (PNG, JPEG, GIF, or WebP).')
    if not payload:
        raise ResaleError('The file is empty.')
    if len(payload) > MAX_BYTES:
        raise ResaleError('The file is larger than 8 MB.')
    content_type, sniff = spec
    if not sniff(payload):
        raise ResaleError('That file does not match its extension.')
    return ext, content_type


def store_certificate_file(customer_id, filename, payload):
    ext, content_type = check_certificate_file(filename, payload)
    stored = f'{secrets.token_hex(8)}.{ext}'
    path = os.path.join(_folder(customer_id), stored)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return stored, content_type


def certificate_path(customer_id, stored_name):
    name = os.path.basename(str(stored_name or ''))
    if not name or name != stored_name or not re.fullmatch(r'[a-f0-9]{16}\.[a-z0-9]+', name):
        return None
    root = os.path.realpath(_folder(customer_id))
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def _certificates(customer):
    rows = customer.get('resale_certificates')
    return list(rows) if isinstance(rows, list) else []


def latest_verified(customer):
    verified = [row for row in _certificates(customer) if row.get('status') == 'verified']
    return verified[-1] if verified else None


def resale_covers_tax(customer, today=None):
    """A verified certificate with no expiry, or an expiry on or after today, is untaxed."""
    cert = latest_verified(customer)
    if not cert:
        return False
    expires = str(cert.get('expires') or '').strip()
    if not expires:
        return True
    day = today or tax_today()
    if isinstance(day, datetime):
        day = day.date()
    return expires >= day.isoformat()


def certificate_alert(customer, today=None):
    """None, or ('expiring'|'expired', days, certificate). Days are negative once expired."""
    cert = latest_verified(customer)
    expires = str((cert or {}).get('expires') or '').strip()
    if not expires:
        return None
    day = today or tax_today()
    if isinstance(day, datetime):
        day = day.date()
    try:
        expiry = date.fromisoformat(expires)
    except ValueError:
        return None
    days = (expiry - day).days
    if days < 0:
        return 'expired', days, cert
    if days <= 30:
        return 'expiring', days, cert
    return None


def resale_label(customer, today=None):
    rows = _certificates(customer)
    if not rows:
        return ''
    alert = certificate_alert(customer, today)
    if alert and alert[0] == 'expired':
        return 'Expired'
    if alert and alert[0] == 'expiring':
        return f'Expires in {alert[1]} days'
    latest = rows[-1]
    if latest.get('status') == 'verified' and resale_covers_tax(customer, today):
        return 'Verified'
    if latest.get('status') == 'pending':
        return 'Pending'
    if latest.get('status') == 'rejected':
        return 'Rejected'
    return latest.get('status') or ''


def warnings_for(customers, today=None):
    notes = []
    day = today or tax_today()
    for customer_id, customer in customers.items():
        if not isinstance(customer, dict) or customer.get('books_entity'):
            continue
        alert = certificate_alert(customer, day)
        if not alert:
            continue
        kind, days, cert = alert
        name = customer.get('name') or customer_id
        if kind == 'expired':
            text = f'{name} resale certificate expired. Invoices from Ora Glass Inc are now taxable.'
        else:
            text = f'{name} resale certificate expires in {days} days.'
        notes.append({
            'customer_id': customer_id,
            'name': name,
            'kind': kind,
            'days': days,
            'expires': cert.get('expires') or '',
            'text': text,
        })
    notes.sort(key=lambda row: (row['kind'] != 'expired', row['days'], row['name'].lower()))
    return notes


def public_certificate(row):
    return {
        'id': row.get('id') or '',
        'number': row.get('number') or '',
        'state': row.get('state') or '',
        'expires': row.get('expires') or '',
        'filename': row.get('filename') or '',
        'content_type': row.get('content_type') or '',
        'uploaded_at': row.get('uploaded_at') or '',
        'uploaded_by': row.get('uploaded_by') or '',
        'status': row.get('status') or 'pending',
        'reviewed_at': row.get('reviewed_at') or '',
        'reviewed_by': row.get('reviewed_by') or '',
        'review_note': row.get('review_note') or '',
    }


def add_certificate(customer_id, filename, payload, number, state, expires, user_email, verified=False):
    if not number:
        raise ResaleError('Certificate number is required.')
    state = str(state or '').strip().upper()
    if state not in US_STATE_CODES:
        raise ResaleError('Choose the state that issued the certificate.')
    expires = str(expires or '').strip()
    if expires:
        try:
            date.fromisoformat(expires)
        except ValueError:
            raise ResaleError('Expiry must be a date, or left blank.')
    customers = load_customers()
    customer = customers.get(customer_id)
    if not customer:
        raise ResaleError('Unknown customer.')
    stored, content_type = store_certificate_file(customer_id, filename, payload)
    record = {
        'id': secrets.token_hex(6),
        'number': number[:80],
        'state': state,
        'expires': expires,
        'filename': os.path.basename(str(filename or 'certificate'))[:180],
        'stored': stored,
        'content_type': content_type,
        'uploaded_at': datetime.now(ZoneInfo('America/Los_Angeles')).replace(microsecond=0).isoformat(),
        'uploaded_by': user_email or '',
        'status': 'verified' if verified else 'pending',
        'reviewed_at': '',
        'reviewed_by': '',
        'review_note': '',
    }
    if verified:
        record['reviewed_at'] = record['uploaded_at']
        record['reviewed_by'] = user_email or ''
    history = _certificates(customer)
    history.append(record)
    customer['resale_certificates'] = history
    customers[customer_id] = customer
    save_customers(customers)
    return record


def review_certificate(customer_id, certificate_id, status, reviewer, note=''):
    if status not in {'verified', 'rejected'}:
        raise ResaleError('Mark the certificate verified or rejected.')
    customers = load_customers()
    customer = customers.get(customer_id)
    if not customer:
        raise ResaleError('Unknown customer.')
    found = None
    for row in _certificates(customer):
        if row.get('id') == certificate_id:
            found = row
            break
    if not found:
        raise ResaleError('Unknown certificate.')
    found['status'] = status
    found['reviewed_at'] = datetime.now(ZoneInfo('America/Los_Angeles')).replace(microsecond=0).isoformat()
    found['reviewed_by'] = reviewer or ''
    found['review_note'] = str(note or '')[:300]
    save_customers(customers)
    return found

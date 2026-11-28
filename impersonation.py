"""Admin view-as-customer audit. Does not change passwords or send mail."""

import os
from datetime import datetime, timezone

from storage import atomic_write_json, data_dir, lock_for, read_json


def _path():
    return os.path.join(data_dir(), 'impersonation_audit.json')


def _stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def acting_label(admin_name, customer_user_name):
    admin = admin_name or 'Admin'
    person = customer_user_name or 'customer user'
    return f'Created by {admin} (admin) on behalf of {person}'


def record(admin, viewer, action, detail=''):
    """Append one impersonation event. Start, stop, and each created record."""
    admin = admin or {}
    viewer = viewer or {}
    entry = {
        'at': _stamp(),
        'action': action,
        'admin': admin.get('email') or admin.get('name') or '',
        'admin_name': admin.get('name') or admin.get('email') or 'Admin',
        'user': viewer.get('email') or viewer.get('name') or '',
        'user_name': viewer.get('name') or viewer.get('email') or '',
        'customer_id': viewer.get('customer_id') or '',
        'detail': str(detail or '')[:300],
    }
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock_for(path):
        rows = read_json(path, [])
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        atomic_write_json(path, rows)
    return entry


def load_audit():
    rows = read_json(_path(), [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

"""Drawing files for irregular-shape lines. The bytes stay on the data disk."""

import os
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from storage import data_dir

MAX_BYTES = 25 * 1024 * 1024


class DrawingError(Exception):
    pass


def is_dwg(filename):
    return os.path.splitext(filename or '')[1].lower() == '.dwg'


def _folder(order_id, line_id):
    order_key = re.sub(r'[^A-Za-z0-9_-]', '', str(order_id or '')) or 'order'
    line_key = re.sub(r'[^A-Za-z0-9_-]', '', str(line_id or '')) or 'line'
    path = os.path.join(data_dir(), 'order_drawings', order_key, line_key)
    os.makedirs(path, exist_ok=True)
    return path


def store_drawing(order_id, line_id, filename, payload):
    if not payload:
        raise DrawingError('The drawing file is empty.')
    if len(payload) > MAX_BYTES:
        raise DrawingError('The drawing is larger than 25 MB.')
    name = os.path.basename(str(filename or 'drawing')).replace('\x00', '')
    if not name or name in {'.', '..'}:
        raise DrawingError('Choose a drawing file.')
    ext = os.path.splitext(name)[1].lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,8}', ext or ''):
        ext = '.bin'
    stamp = datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y%m%d%H%M%S')
    stored = f'{stamp}-{secrets.token_hex(3)}{ext}'
    path = os.path.join(_folder(order_id, line_id), stored)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return {
        'line_id': line_id,
        'filename': name[:180],
        'stored': stored,
        'is_dwg': is_dwg(name),
        'uploaded_at': datetime.now(ZoneInfo('America/Los_Angeles')).replace(microsecond=0).isoformat(),
    }


def drawing_path(order_id, line_id, stored_name):
    name = os.path.basename(str(stored_name or ''))
    if not name or name != stored_name:
        return None
    root = os.path.realpath(_folder(order_id, line_id))
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path

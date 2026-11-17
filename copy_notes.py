"""Remove 'Copied from quote #N' from text customers can see.

Copy quote used to append that sentence to the notes, and each later copy
added another one. The source id can stay on copied_from. This pass does
not print it, and it leaves real remarks, including the drawing note, in place.
It is safe to run on every startup.
"""

import logging
import re

from storage import load_orders, load_quotes, save_orders, save_quotes

logger = logging.getLogger('ora_glass.copy_notes')

_COPY_NOTE = re.compile(r'copied from quote #\d+\.?', re.IGNORECASE)


def strip_copy_history(value):
    """Drop every 'Copied from quote #N' sentence. Other remarks stay."""
    text = '' if value is None else str(value)
    if not _COPY_NOTE.search(text):
        return text
    cleaned = _COPY_NOTE.sub('', text).replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in cleaned.split('\n'):
        line = re.sub(r'[ \t]{2,}', ' ', line).strip(' \t,;')
        if line:
            lines.append(line)
    return '\n'.join(lines).strip()


def _clean_field(record, key):
    current = record.get(key)
    if not isinstance(current, str) or not _COPY_NOTE.search(current):
        return False
    updated = strip_copy_history(current)
    if updated == current:
        return False
    record[key] = updated
    return True


def _clean_record(record):
    if not isinstance(record, dict):
        return False
    changed = _clean_field(record, 'notes')
    changed = _clean_field(record, 'remarks') or changed
    if 'copy_note' in record:
        record.pop('copy_note', None)
        changed = True
    for item in record.get('items') or []:
        if isinstance(item, dict) and _clean_field(item, 'remarks'):
            changed = True
    return changed


def strip_stored_copy_notes():
    """Rewrite stored quotes and orders once. A second run changes nothing."""
    quotes = load_quotes()
    orders = load_orders()
    quote_changes = sum(1 for row in quotes if _clean_record(row))
    order_changes = sum(1 for row in orders if _clean_record(row))
    if quote_changes:
        save_quotes(quotes)
    if order_changes:
        save_orders(orders)
    if quote_changes or order_changes:
        logger.info(
            'Copy history cleanup: removed the copy note from %s quote(s) and %s order(s).',
            quote_changes, order_changes,
        )
    return {'quotes': quote_changes, 'orders': order_changes}

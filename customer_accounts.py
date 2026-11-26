"""One receivable account per issuing company.

A customer can owe Ora Glass Inc and Ora Glass (Canada) Inc separately.
Each account has its own currency, tax, balance, and QuickBooks customer id.
Migration only reads records already stored here. It does not call QuickBooks
and it does not create a QuickBooks customer.
"""

from billing import money

ENTITIES = ('us', 'ca')
LABELS = {
    'us': 'Ora Glass Inc',
    'ca': 'Ora Glass (Canada) Inc',
}
CURRENCIES = {'us': 'usd', 'ca': 'cad'}
OPEN_WINDOWS_NAMES = {'open windows', 'open windows inc'}


def is_open_windows(customer):
    customer = customer or {}
    identity = str(customer.get('id') or '').strip().lower()
    name = str(customer.get('name') or '').strip().casefold()
    return identity == 'open-windows' or name in OPEN_WINDOWS_NAMES


def inferred_entity(customer):
    """The company this customer is used with, before a second account is added."""
    if is_open_windows(customer):
        return 'us'
    import quickbooks
    return 'ca' if quickbooks.is_canadian(customer) else 'us'


def home_entity(customer):
    """The company this customer is already invoiced by."""
    customer = customer or {}
    stored = str(customer.get('home_entity') or '').strip().lower()
    accounts = customer.get('accounts') if isinstance(customer.get('accounts'), dict) else {}
    if stored in ENTITIES and stored in accounts:
        return stored
    return inferred_entity(customer)


def _stored_qbo_id(customer_id, entity):
    """The QuickBooks id already saved for this company. No network call."""
    import quickbooks
    for row in quickbooks.load_links():
        if row.get('kind') != 'customer':
            continue
        if str(row.get('erp_id') or '') != str(customer_id or ''):
            continue
        if (row.get('connection') or 'us') != entity:
            continue
        qbo_id = str(row.get('qbo_id') or '').strip()
        if qbo_id:
            return qbo_id
    return ''


def _account_body(customer, entity):
    currency = CURRENCIES[entity]
    gst = entity == 'ca'
    limit_currency = str(customer.get('credit_currency') or currency).lower()
    limit = money(customer.get('credit_limit') or 0) if limit_currency == currency else 0.0
    if is_open_windows(customer) and entity == 'us':
        currency = 'usd'
        gst = False
        limit = money(customer.get('credit_limit') or 0)
    return {
        'entity': entity,
        'label': LABELS[entity],
        'currency': currency,
        'gst': gst,
        'export_no_tax': bool(customer.get('export_no_tax')) if entity == 'us' else False,
        'tax_exempt': bool(customer.get('tax_exempt')) if entity == 'ca' else False,
        'qbo_tax_code': (customer.get('qbo_tax_code') or '') if entity == 'ca' else '',
        'tax_code_override': bool(customer.get('tax_code_override')) if entity == 'ca' else False,
        'credit_limit': limit,
        'credit_currency': currency,
        'qbo_id': _stored_qbo_id(customer.get('id'), entity),
    }


def ensure_accounts(customer):
    """Turn the current customer record into its existing company account.

    Returns True when the record changed. Does not write invoices, payments,
    or QuickBooks.
    """
    if not isinstance(customer, dict) or customer.get('books_entity'):
        return False
    accounts = customer.get('accounts') if isinstance(customer.get('accounts'), dict) else None
    if customer.get('accounts_version') == 1 and accounts:
        changed = False
        if is_open_windows(customer):
            customer['export_no_tax'] = True
            us = accounts.get('us') if isinstance(accounts.get('us'), dict) else None
            if us:
                limit = money(customer.get('credit_limit') or 0)
                if us.get('currency') != 'usd' or us.get('gst') or us.get('credit_currency') != 'usd' or us.get('credit_limit') != limit:
                    us['currency'] = 'usd'
                    us['gst'] = False
                    us['credit_currency'] = 'usd'
                    us['credit_limit'] = limit
                    changed = True
            return changed
        # A customer saved before the country was known has one automatic account.
        # Move it when the country now says which company they belong to.
        # A second account is never moved.
        if len(accounts) == 1:
            inferred = inferred_entity(customer)
            current = next(iter(accounts))
            if current != inferred:
                customer['accounts'] = {inferred: _account_body(customer, inferred)}
                customer['home_entity'] = inferred
                customer['credit_currency'] = CURRENCIES[inferred]
                return True
        return False
    entity = 'us' if is_open_windows(customer) else inferred_entity(customer)
    customer['accounts'] = {entity: _account_body(customer, entity)}
    customer['accounts_version'] = 1
    customer['home_entity'] = entity
    if is_open_windows(customer):
        customer['credit_currency'] = 'usd'
        customer['export_no_tax'] = True
    else:
        customer['credit_currency'] = CURRENCIES[entity]
    return True


def account(customer, entity):
    accounts = (customer or {}).get('accounts') if isinstance((customer or {}).get('accounts'), dict) else {}
    row = accounts.get(entity)
    return row if isinstance(row, dict) else None


def home_account(customer):
    return account(customer, home_entity(customer))


def add_account(customer, entity):
    """Add the other issuing company. Does not create a QuickBooks customer."""
    if entity not in ENTITIES:
        return 'Choose Ora Glass Inc or Ora Glass (Canada) Inc.'
    if not isinstance(customer, dict):
        return 'Unknown customer.'
    ensure_accounts(customer)
    if account(customer, entity):
        return f'{LABELS[entity]} is already an account for this customer.'
    customer['accounts'][entity] = _account_body(customer, entity)
    customer['accounts'][entity]['qbo_id'] = ''
    customer['accounts'][entity]['credit_limit'] = 0.0
    return None


def remember_qbo_id(customer, entity, qbo_id):
    """Store an id that an explicit link already saved. Does not call QuickBooks."""
    row = account(customer, entity)
    if not row:
        return
    row['qbo_id'] = str(qbo_id or '')


def require_account(customer, entity, currency=None):
    if isinstance(customer, dict) and not customer.get('books_entity'):
        ensure_accounts(customer)
    row = account(customer, entity)
    if not row:
        return None, f'Add an {LABELS.get(entity, "Ora Glass")} account before using that company.'
    if currency and str(currency).lower() != row.get('currency'):
        other = 'ca' if entity == 'us' else 'us'
        return None, (
            f'An {LABELS[entity]} invoice uses {str(row.get("currency") or "").upper()} '
            f'and cannot post to the {LABELS.get(other, "other")} account.'
        )
    return row, None


def entity_for_order(customer, order):
    """The account an order posts to. A stored issuer wins. Otherwise the currency decides."""
    stored = str((order or {}).get('issuer') or '').strip().lower()
    if stored in ENTITIES and account(customer, stored):
        return stored
    currency = str((order or {}).get('currency') or '').lower()
    matches = [
        entity for entity in ENTITIES
        if (account(customer, entity) or {}).get('currency') == currency
    ]
    if len(matches) == 1:
        return matches[0]
    return home_entity(customer)


def document_entity(row, home='us'):
    issuer = str((row or {}).get('issuer') or '').strip().lower()
    if issuer in ENTITIES:
        return issuer
    currency = str((row or {}).get('currency') or '').lower()
    if currency == 'cad':
        return 'ca'
    if currency == 'usd':
        return 'us'
    return home if home in ENTITIES else 'us'

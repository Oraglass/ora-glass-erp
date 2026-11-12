"""QuickBooks Online Accounting API. Tokens stay on the data disk. Secrets stay in the environment."""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from billing import TERM_DAYS, invoice_balance, invoice_status, money
from storage import (
    append_invoice,
    append_payment,
    atomic_write_json,
    data_dir,
    load_customers,
    load_invoices,
    load_payments,
    lock_for,
    read_json,
    save_customers,
    save_invoices,
    save_payments,
)

logger = logging.getLogger('ora_glass.quickbooks')

ACCOUNTING_SCOPE = 'com.intuit.quickbooks.accounting'
AUTH_URL = 'https://appcenter.intuit.com/connect/oauth2'
TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'
MINOR_VERSION = '75'
CDC_INTERVAL = 15 * 60
DEFAULT_ITEM = 'Insulated Glass Units'
DEFAULT_TAX = 'NON'
TRANSPORT = None


class QuickBooksError(Exception):
    def __init__(self, message, status=0, sync_conflict=False):
        super().__init__(message)
        self.status = status
        self.sync_conflict = sync_conflict


def _path(name):
    return os.path.join(data_dir(), name)


def _load(name, default):
    path = _path(name)
    with lock_for(path):
        data = read_json(path, default)
    return data


def _save(name, data):
    path = _path(name)
    with lock_for(path):
        atomic_write_json(path, data)


def environment():
    return (os.environ.get('QBO_ENVIRONMENT') or '').strip().lower()


def configured():
    missing = []
    for key in ('QBO_CLIENT_ID', 'QBO_CLIENT_SECRET', 'QBO_REDIRECT_URI', 'QBO_ENVIRONMENT', 'QBO_WEBHOOK_VERIFIER'):
        if not (os.environ.get(key) or '').strip():
            missing.append(key)
    if environment() not in {'', 'sandbox', 'production'}:
        if 'QBO_ENVIRONMENT' not in missing:
            missing.append('QBO_ENVIRONMENT')
    return missing


def api_host():
    override = (os.environ.get('QBO_API_BASE') or '').strip().rstrip('/')
    if override:
        return override
    if environment() == 'production':
        return 'https://quickbooks.api.intuit.com'
    return 'https://sandbox-quickbooks.api.intuit.com'


def load_tokens():
    data = _load('qbo_tokens.json', {})
    return data if isinstance(data, dict) else {}


def save_tokens(tokens):
    _save('qbo_tokens.json', tokens)


def disconnect():
    save_tokens({})
    log('info', 'Disconnected from QuickBooks.')


def load_settings():
    data = _load('qbo_settings.json', {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault('default_item', DEFAULT_ITEM)
    data.setdefault('tax_code', DEFAULT_TAX)
    data.setdefault('extra_items', {})
    return data


def save_settings(settings):
    _save('qbo_settings.json', settings)


def load_links():
    data = _load('qbo_links.json', [])
    return data if isinstance(data, list) else []


def save_links(rows):
    _save('qbo_links.json', rows)


def load_log():
    data = _load('qbo_log.json', [])
    return data if isinstance(data, list) else []


def log(level, message):
    rows = load_log()
    rows.append({
        'at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        'level': level,
        'message': message,
    })
    _save('qbo_log.json', rows[-200:])
    settings = load_settings()
    settings['last_sync'] = rows[-1]['at']
    save_settings(settings)


def connected():
    tokens = load_tokens()
    return bool(tokens.get('refresh_token') and tokens.get('realm_id') and not configured())


def find_link(kind, erp_id=None, qbo_id=None):
    for row in load_links():
        if row.get('kind') != kind:
            continue
        if erp_id is not None and str(row.get('erp_id')) == str(erp_id):
            return row
        if qbo_id is not None and str(row.get('qbo_id')) == str(qbo_id):
            return row
    return None


def upsert_link(kind, erp_id, qbo_id, sync_token, origin):
    rows = load_links()
    for row in rows:
        if row.get('kind') == kind and (str(row.get('erp_id')) == str(erp_id) or str(row.get('qbo_id')) == str(qbo_id)):
            row['erp_id'] = str(erp_id)
            row['qbo_id'] = str(qbo_id)
            row['sync_token'] = str(sync_token or '0')
            row['origin'] = origin or row.get('origin') or 'erp'
            save_links(rows)
            return row
    record = {
        'kind': kind,
        'erp_id': str(erp_id),
        'qbo_id': str(qbo_id),
        'sync_token': str(sync_token or '0'),
        'origin': origin or 'erp',
    }
    rows.append(record)
    save_links(rows)
    return record


def status_view():
    tokens = load_tokens()
    settings = load_settings()
    missing = configured()
    return {
        'configured': not missing,
        'missing': missing,
        'connected': bool(tokens.get('refresh_token') and tokens.get('realm_id') and not missing),
        'company_name': tokens.get('company_name') or '',
        'realm_id': tokens.get('realm_id') or '',
        'environment': environment(),
        'last_sync': settings.get('last_sync') or '',
        'last_cdc': settings.get('last_cdc') or '',
        'default_item': settings.get('default_item') or DEFAULT_ITEM,
        'tax_code': settings.get('tax_code') or DEFAULT_TAX,
        'extra_items': settings.get('extra_items') or {},
        'log': list(reversed(load_log()[-50:])),
    }


def authorize_url(state):
    missing = configured()
    if missing:
        raise QuickBooksError('QuickBooks is not configured: ' + ', '.join(missing))
    query = urllib.parse.urlencode({
        'client_id': os.environ['QBO_CLIENT_ID'].strip(),
        'response_type': 'code',
        'scope': ACCOUNTING_SCOPE,
        'redirect_uri': os.environ['QBO_REDIRECT_URI'].strip(),
        'state': state,
    })
    return AUTH_URL + '?' + query


def _basic_auth():
    raw = (os.environ.get('QBO_CLIENT_ID') or '').strip() + ':' + (os.environ.get('QBO_CLIENT_SECRET') or '').strip()
    return 'Basic ' + base64.b64encode(raw.encode()).decode()


def _http(method, url, headers, body):
    if TRANSPORT is not None:
        return TRANSPORT(method, url, headers, body)
    data = body if isinstance(body, bytes) or body is None else body.encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _json_call(method, url, headers, payload):
    encoded = None if payload is None else json.dumps(payload).encode()
    headers = dict(headers)
    if encoded is not None:
        headers['Content-Type'] = 'application/json'
    status, _headers, raw = _http(method, url, headers, encoded)
    try:
        data = json.loads(raw.decode() or '{}')
    except json.JSONDecodeError:
        data = {'raw': (raw or b'')[:500].decode(errors='replace')}
    if status >= 400:
        fault = ((data.get('Fault') or {}).get('Error') or [{}])[0]
        code = str(fault.get('code') or '')
        message = fault.get('Message') or fault.get('Detail') or data.get('error') or 'QuickBooks request failed'
        raise QuickBooksError(str(message), status=status, sync_conflict=code == '5010')
    return data


def exchange_code(code, realm_id):
    form = urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': os.environ['QBO_REDIRECT_URI'].strip(),
    }).encode()
    status, _headers, raw = _http('POST', os.environ.get('QBO_TOKEN_URL') or TOKEN_URL, {
        'Authorization': _basic_auth(),
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }, form)
    data = json.loads(raw.decode() or '{}')
    if status >= 400:
        raise QuickBooksError(data.get('error_description') or 'QuickBooks did not accept the authorization code.', status)
    tokens = _store_token_response(data, realm_id)
    info = api_get(f"/v3/company/{realm_id}/companyinfo/{realm_id}")
    company = ((info.get('CompanyInfo') or {}).get('CompanyName')) or ''
    tokens['company_name'] = company
    save_tokens(tokens)
    settings = load_settings()
    settings['last_cdc'] = _now()
    save_settings(settings)
    log('info', f'Connected to {company or realm_id}.')
    return tokens


def _store_token_response(data, realm_id):
    tokens = load_tokens()
    tokens.update({
        'access_token': data.get('access_token') or '',
        'refresh_token': data.get('refresh_token') or tokens.get('refresh_token') or '',
        'expires_at': time.time() + int(data.get('expires_in') or 3600),
        'realm_id': str(realm_id or tokens.get('realm_id') or ''),
        'token_type': data.get('token_type') or 'bearer',
    })
    save_tokens(tokens)
    return tokens


def ensure_access_token():
    tokens = load_tokens()
    if not tokens.get('refresh_token') or not tokens.get('realm_id'):
        raise QuickBooksError('QuickBooks is not connected.')
    if float(tokens.get('expires_at') or 0) - 60 > time.time() and tokens.get('access_token'):
        return tokens
    form = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token'],
    }).encode()
    status, _headers, raw = _http('POST', os.environ.get('QBO_TOKEN_URL') or TOKEN_URL, {
        'Authorization': _basic_auth(),
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }, form)
    data = json.loads(raw.decode() or '{}')
    if status >= 400:
        raise QuickBooksError(data.get('error_description') or 'QuickBooks refused to refresh the connection.', status)
    tokens = _store_token_response(data, tokens.get('realm_id'))
    log('info', 'Refreshed the QuickBooks access token.')
    return tokens


def _auth_headers():
    tokens = ensure_access_token()
    return {
        'Authorization': 'Bearer ' + tokens['access_token'],
        'Accept': 'application/json',
    }, tokens['realm_id']


def api_get(path):
    headers, _realm = _auth_headers()
    joiner = '&' if '?' in path else '?'
    return _json_call('GET', api_host() + path + joiner + 'minorversion=' + MINOR_VERSION, headers, None)


def api_post(path, payload):
    headers, _realm = _auth_headers()
    joiner = '&' if '?' in path else '?'
    return _json_call('POST', api_host() + path + joiner + 'minorversion=' + MINOR_VERSION, headers, payload)


def query(statement):
    headers, realm = _auth_headers()
    url = api_host() + f'/v3/company/{realm}/query?' + urllib.parse.urlencode({
        'query': statement,
        'minorversion': MINOR_VERSION,
    })
    data = _json_call('GET', url, headers, None)
    response = data.get('QueryResponse') or {}
    for key, value in response.items():
        if isinstance(value, list):
            return value
    return []


def _entity_path(name):
    return {
        'Customer': 'customer',
        'Invoice': 'invoice',
        'Payment': 'payment',
        'CreditMemo': 'creditmemo',
        'Item': 'item',
        'Term': 'term',
    }[name]


def _normalize_name(value):
    return ' '.join(str(value or '').casefold().replace('.', ' ').split())


def names_match(left, right):
    a = _normalize_name(left)
    b = _normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 8 and (long == short or long.startswith(short + ' '))


def suggest_match(qbo_customer, customers):
    email = (((qbo_customer.get('PrimaryEmailAddr') or {}).get('Address')) or '').strip().lower()
    name = qbo_customer.get('DisplayName') or qbo_customer.get('CompanyName') or ''
    for customer_id, customer in customers.items():
        if email and (customer.get('email') or '').strip().lower() == email:
            return customer_id, 'email'
        if names_match(name, customer.get('name') or ''):
            return customer_id, 'name'
    return '', ''


def preview_import():
    rows = query('select * from Customer maxresults 1000')
    customers = load_customers()
    preview = []
    for row in rows:
        customer_id, reason = suggest_match(row, customers)
        currency = str((row.get('CurrencyRef') or {}).get('value') or 'USD').lower()
        preview.append({
            'qbo_id': str(row.get('Id')),
            'name': row.get('DisplayName') or '',
            'email': ((row.get('PrimaryEmailAddr') or {}).get('Address')) or '',
            'currency': currency if currency in {'usd', 'cad'} else 'usd',
            'open_balance': money(row.get('Balance')),
            'sync_token': str(row.get('SyncToken') or '0'),
            'suggested_customer_id': customer_id,
            'match_reason': reason,
            'action': 'link' if customer_id else 'create',
        })
    _save('qbo_import_preview.json', preview)
    log('info', f'Prepared an import of {len(preview)} QuickBooks customers.')
    return preview


def _customer_from_qbo(row, customer_id, tier='T2'):
    currency = str((row.get('CurrencyRef') or {}).get('value') or 'USD').lower()
    if currency not in {'usd', 'cad'}:
        currency = 'usd'
    address = row.get('BillAddr') or {}
    term = (row.get('SalesTermRef') or {}).get('name') or 'Net 30'
    if term not in TERM_DAYS:
        term = 'Net 30'
    return {
        'id': customer_id,
        'name': row.get('DisplayName') or row.get('CompanyName') or customer_id,
        'domain': '',
        'address': address.get('Line1') or '',
        'city': address.get('City') or '',
        'state': address.get('CountrySubDivisionCode') or '',
        'zip': address.get('PostalCode') or '',
        'phone': ((row.get('PrimaryPhone') or {}).get('FreeFormNumber')) or '',
        'email': ((row.get('PrimaryEmailAddr') or {}).get('Address')) or '',
        'credit_approved': False,
        'credit_limit': 0,
        'credit_currency': currency,
        'payment_terms': term,
        'payment_terms_days': TERM_DAYS[term],
        'tier': tier,
        'status': 'active',
        'kind': 'account',
        'qbo_origin': True,
    }


def apply_import(decisions):
    """decisions: {qbo_id: {action: link|create|skip, customer_id?}}"""
    preview = _load('qbo_import_preview.json', [])
    if not isinstance(preview, list) or not preview:
        raise QuickBooksError('Review the QuickBooks customers before applying them.')
    by_id = {str(row.get('qbo_id')): row for row in preview}
    customers = load_customers()
    linked = []
    for qbo_id, choice in (decisions or {}).items():
        row = by_id.get(str(qbo_id))
        if not row:
            continue
        action = (choice or {}).get('action') or row.get('action')
        if action == 'skip':
            continue
        if action == 'link':
            customer_id = (choice or {}).get('customer_id') or row.get('suggested_customer_id')
            if customer_id not in customers:
                raise QuickBooksError(f'No ERP customer matches {row.get("name")}.')
        else:
            from app import new_customer_id
            customer_id = new_customer_id(row.get('name') or 'customer', customers)
            remote = _customer_record(qbo_id)
            customers[customer_id] = _customer_from_qbo(remote, customer_id)
            customers[customer_id]['qbo_origin'] = True
        upsert_link('customer', customer_id, qbo_id, row.get('sync_token'), 'qbo')
        customers[customer_id]['qbo_id'] = str(qbo_id)
        linked.append(customer_id)
    save_customers(customers)
    imported = {'invoices': 0, 'payments': 0}
    for customer_id in linked:
        counts = _import_customer_activity(customer_id)
        imported['invoices'] += counts['invoices']
        imported['payments'] += counts['payments']
    log('info', f"Imported {imported['invoices']} invoices and {imported['payments']} payments from QuickBooks.")
    return {'customers': len(linked), **imported}


def _customer_record(qbo_id):
    data = api_get(f"/v3/company/{load_tokens()['realm_id']}/customer/{qbo_id}")
    return data.get('Customer') or {}


def _import_customer_activity(customer_id):
    link = find_link('customer', erp_id=customer_id)
    qbo_id = link['qbo_id']
    invoices = query(f"select * from Invoice where CustomerRef = '{qbo_id}' maxresults 1000")
    credits = query(f"select * from CreditMemo where CustomerRef = '{qbo_id}' maxresults 1000")
    payments = query(f"select * from Payment where CustomerRef = '{qbo_id}' maxresults 1000")
    counts = {'invoices': 0, 'payments': 0}
    for row in invoices:
        if _ingest_invoice(row):
            counts['invoices'] += 1
    for row in credits:
        if _ingest_credit(row):
            counts['invoices'] += 1
    for row in payments:
        if _ingest_payment(row):
            counts['payments'] += 1
    _reconcile_open_balances(customer_id, invoices)
    return counts


def _reconcile_open_balances(customer_id, qbo_invoices):
    """If historical payments do not explain the open balance, record the difference once."""
    payments = load_payments()
    invoices = load_invoices()
    changed = False
    for remote in qbo_invoices:
        link = find_link('invoice', qbo_id=remote.get('Id'))
        if not link:
            continue
        local = next((row for row in invoices if str(row.get('id')) == str(link['erp_id'])), None)
        if not local:
            continue
        target = money(remote.get('Balance'))
        current = invoice_balance(local, payments)
        gap = round(current - target, 2)
        if gap > 0.009:
            append_payment({
                'customer_id': customer_id,
                'amount': gap,
                'currency': local.get('currency') or 'usd',
                'method': 'wire',
                'reference': 'QuickBooks history',
                'date': remote.get('TxnDate') or _today(),
                'notes': 'Imported so the open balance matches QuickBooks.',
                'allocations': [{'target_type': 'invoice', 'target_id': local.get('id'), 'amount': gap}],
                'voided': False,
                'qbo_origin': True,
            })
            changed = True
    if changed:
        payments = load_payments()
        for invoice in invoices:
            invoice['balance'] = invoice_balance(invoice, payments)
            invoice['status'] = invoice_status(invoice, payments)
        save_invoices(invoices)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ingest_invoice(row):
    if find_link('invoice', qbo_id=row.get('Id')):
        link = find_link('invoice', qbo_id=row.get('Id'))
        link_rows = load_links()
        for item in link_rows:
            if item is link or (item.get('kind') == 'invoice' and item.get('qbo_id') == str(row.get('Id'))):
                item['sync_token'] = str(row.get('SyncToken') or item.get('sync_token') or '0')
        save_links(link_rows)
        return False
    customer = find_link('customer', qbo_id=(row.get('CustomerRef') or {}).get('value'))
    if not customer:
        return False
    currency = str((row.get('CurrencyRef') or {}).get('value') or 'USD').lower()
    total = money(row.get('TotalAmt'))
    description = 'Insulated glass'
    for line in row.get('Line') or []:
        if line.get('Description'):
            description = line['Description']
            break
    stored = append_invoice({
        'doc_type': 'invoice',
        'source': 'quickbooks',
        'qbo_origin': True,
        'customer_id': customer['erp_id'],
        'company': (load_customers().get(customer['erp_id']) or {}).get('name') or '',
        'project': row.get('DocNumber') or '',
        'currency': currency if currency in {'usd', 'cad'} else 'usd',
        'lines': [{'description': description, 'qty': 1, 'unit_price': total, 'total': total, 'name': description, 'pcs': 1, 'price_pc': total}],
        'items': [{'description': description, 'qty': 1, 'unit_price': total, 'total': total, 'name': description, 'pcs': 1, 'price_pc': total}],
        'total': total,
        'subtotal': total,
        'balance': money(row.get('Balance') if row.get('Balance') is not None else total),
        'status': 'unpaid',
        'invoice_date': row.get('TxnDate') or _today(),
        'due_date': row.get('DueDate') or '',
        'payment_terms': 'Net 30',
        'voided': False,
        'created_at': _now(),
        'created_by': 'quickbooks',
    })
    stored['status'] = invoice_status(stored, load_payments())
    stored['balance'] = invoice_balance(stored, load_payments())
    invoices = load_invoices()
    for index, item in enumerate(invoices):
        if item.get('id') == stored['id']:
            invoices[index] = stored
    save_invoices(invoices)
    upsert_link('invoice', stored['id'], row.get('Id'), row.get('SyncToken'), 'qbo')
    return True


def _ingest_credit(row):
    if find_link('credit', qbo_id=row.get('Id')):
        return False
    customer = find_link('customer', qbo_id=(row.get('CustomerRef') or {}).get('value'))
    if not customer:
        return False
    total = money(row.get('TotalAmt'))
    currency = str((row.get('CurrencyRef') or {}).get('value') or 'USD').lower()
    stored = append_invoice({
        'doc_type': 'credit_note',
        'source': 'quickbooks',
        'qbo_origin': True,
        'customer_id': customer['erp_id'],
        'company': (load_customers().get(customer['erp_id']) or {}).get('name') or '',
        'currency': currency if currency in {'usd', 'cad'} else 'usd',
        'lines': [{'description': 'Credit memo', 'qty': 1, 'unit_price': total, 'total': total}],
        'items': [{'description': 'Credit memo', 'qty': 1, 'unit_price': total, 'total': total}],
        'total': total,
        'reason': 'Imported from QuickBooks',
        'applications': [],
        'status': 'open',
        'voided': False,
        'invoice_date': row.get('TxnDate') or _today(),
        'created_at': _now(),
        'created_by': 'quickbooks',
    })
    upsert_link('credit', stored['id'], row.get('Id'), row.get('SyncToken'), 'qbo')
    return True


def _ingest_payment(row):
    if find_link('payment', qbo_id=row.get('Id')):
        _remember_token('payment', row)
        return False
    customer = find_link('customer', qbo_id=(row.get('CustomerRef') or {}).get('value'))
    if not customer:
        return False
    allocations = []
    for line in row.get('Line') or []:
        for linked in line.get('LinkedTxn') or []:
            if linked.get('TxnType') != 'Invoice':
                continue
            invoice_link = find_link('invoice', qbo_id=linked.get('TxnId'))
            if not invoice_link:
                continue
            allocations.append({
                'target_type': 'invoice',
                'target_id': int(invoice_link['erp_id']),
                'amount': money(line.get('Amount')),
            })
    currency = str((row.get('CurrencyRef') or {}).get('value') or 'USD').lower()
    stored = append_payment({
        'customer_id': customer['erp_id'],
        'company': (load_customers().get(customer['erp_id']) or {}).get('name') or '',
        'amount': money(row.get('TotalAmt')),
        'currency': currency if currency in {'usd', 'cad'} else 'usd',
        'method': 'wire',
        'reference': 'QuickBooks ' + str(row.get('Id')),
        'date': row.get('TxnDate') or _today(),
        'notes': 'Imported from QuickBooks',
        'allocations': allocations,
        'voided': False,
        'qbo_origin': True,
        'created_by': 'quickbooks',
    })
    upsert_link('payment', stored['id'], row.get('Id'), row.get('SyncToken'), 'qbo')
    payments = load_payments()
    invoices = load_invoices()
    for invoice in invoices:
        if invoice.get('customer_id') != customer['erp_id']:
            continue
        invoice['balance'] = invoice_balance(invoice, payments)
        invoice['status'] = invoice_status(invoice, payments)
    save_invoices(invoices)
    return True


def _remember_token(kind, row):
    rows = load_links()
    for item in rows:
        if item.get('kind') == kind and str(item.get('qbo_id')) == str(row.get('Id')):
            item['sync_token'] = str(row.get('SyncToken') or item.get('sync_token') or '0')
    save_links(rows)


def _item_id(name):
    settings = load_settings()
    cache = settings.setdefault('item_ids', {})
    if cache.get(name):
        return cache[name]
    safe = name.replace("'", "\\'")
    found = query(f"select * from Item where Name = '{safe}'")
    if not found:
        raise QuickBooksError(f'QuickBooks has no item named {name}.')
    cache[name] = str(found[0].get('Id'))
    save_settings(settings)
    return cache[name]


def _term_ref(label):
    settings = load_settings()
    cache = settings.setdefault('term_ids', {})
    label = label if label in TERM_DAYS else 'Net 30'
    if cache.get(label):
        return {'value': cache[label]}
    safe = label.replace("'", "\\'")
    found = query(f"select * from Term where Name = '{safe}'")
    if not found:
        return None
    cache[label] = str(found[0].get('Id'))
    save_settings(settings)
    return {'value': cache[label]}


def _sales_line(description, qty, unit_price, amount, item_name):
    settings = load_settings()
    return {
        'Amount': round(float(amount), 2),
        'Description': description,
        'DetailType': 'SalesItemLineDetail',
        'SalesItemLineDetail': {
            'ItemRef': {'value': _item_id(item_name), 'name': item_name},
            'Qty': float(qty or 1),
            'UnitPrice': round(float(unit_price), 2),
            'TaxCodeRef': {'value': settings.get('tax_code') or DEFAULT_TAX},
        },
    }


def _document_lines(invoice):
    settings = load_settings()
    default_item = settings.get('default_item') or DEFAULT_ITEM
    extras = settings.get('extra_items') or {}
    lines = []
    source = invoice.get('lines') or invoice.get('items') or []
    for item in source:
        amount = money(item.get('total') if item.get('total') not in (None, '') else item.get('amount'))
        if amount <= 0 and item.get('price_pc') and item.get('pcs'):
            amount = round(money(item.get('price_pc')) * money(item.get('pcs')), 2)
        if amount <= 0:
            continue
        qty = item.get('qty') or item.get('pcs') or 1
        unit = item.get('unit_price') if item.get('unit_price') not in (None, '') else item.get('price_pc') or amount
        description = item.get('description') or item.get('name') or default_item
        lines.append(_sales_line(description, qty, unit, amount, default_item))
    for charge in invoice.get('extra_charges') or []:
        label = charge.get('label') or charge.get('name') or 'Extra'
        amount = money(charge.get('amount'))
        if amount <= 0:
            continue
        lines.append(_sales_line(label, 1, amount, amount, extras.get(label) or default_item))
    if not lines and money(invoice.get('total')) > 0:
        total = money(invoice.get('total'))
        lines.append(_sales_line(default_item, 1, total, total, default_item))
    discount = money(invoice.get('discount_amount'))
    if discount > 0:
        lines.append({
            'Amount': discount,
            'DetailType': 'DiscountLineDetail',
            'DiscountLineDetail': {'PercentBased': False},
        })
    return lines


def push_customer(customer_id):
    if not connected():
        return None
    customers = load_customers()
    customer = customers.get(customer_id)
    if not customer:
        return None
    existing = find_link('customer', erp_id=customer_id)
    currency = str(customer.get('credit_currency') or 'usd').upper()
    if currency not in {'USD', 'CAD'}:
        currency = 'USD'
    payload = {
        'DisplayName': customer.get('name') or customer_id,
        'CompanyName': customer.get('name') or '',
        'PrimaryEmailAddr': {'Address': customer.get('email') or ''},
        'PrimaryPhone': {'FreeFormNumber': customer.get('phone') or ''},
        'BillAddr': {
            'Line1': customer.get('address') or '',
            'City': customer.get('city') or '',
            'CountrySubDivisionCode': customer.get('state') or '',
            'PostalCode': customer.get('zip') or '',
        },
        'CurrencyRef': {'value': currency},
    }
    term = _term_ref(customer.get('payment_terms') or 'Net 30')
    if term:
        payload['SalesTermRef'] = term
    try:
        if existing:
            payload['Id'] = existing['qbo_id']
            payload['SyncToken'] = existing['sync_token']
            payload['sparse'] = True
            data = _post_entity('customer', payload)
        else:
            data = _post_entity('customer', payload)
        remote = data.get('Customer') or {}
        if remote:
            upsert_link('customer', customer_id, remote.get('Id'), remote.get('SyncToken'), existing.get('origin') if existing else 'erp')
            log('info', f"Saved {customer.get('name')} in QuickBooks.")
        return remote
    except QuickBooksError as exc:
        log('error', f'Customer {customer_id}: {exc}')
        return None


def deactivate_customer(customer_id):
    if not connected():
        return None
    existing = find_link('customer', erp_id=customer_id)
    if not existing or existing.get('origin') == 'qbo':
        return None
    try:
        _post_entity('customer', {
            'Id': existing['qbo_id'],
            'SyncToken': existing['sync_token'],
            'sparse': True,
            'Active': False,
        })
        log('info', f'Marked QuickBooks customer {existing["qbo_id"]} inactive.')
    except QuickBooksError as exc:
        log('error', f'Customer {customer_id}: {exc}')
    return None


def _post_entity(entity, payload):
    realm = load_tokens()['realm_id']
    try:
        return api_post(f'/v3/company/{realm}/{entity}', payload)
    except QuickBooksError as exc:
        if exc.sync_conflict and payload.get('Id'):
            current = api_get(f"/v3/company/{realm}/{entity}/{payload['Id']}")
            keys = {'customer': 'Customer', 'invoice': 'Invoice', 'creditmemo': 'CreditMemo', 'payment': 'Payment'}
            remote = current.get(keys.get(entity, '')) or {}
            if not remote:
                for value in current.values():
                    if isinstance(value, dict) and value.get('Id'):
                        remote = value
                        break
            kind = {'customer': 'customer', 'invoice': 'invoice', 'creditmemo': 'credit', 'payment': 'payment'}[entity]
            if remote.get('Id'):
                rows = load_links()
                for item in rows:
                    if item.get('kind') == kind and str(item.get('qbo_id')) == str(remote.get('Id')):
                        item['sync_token'] = str(remote.get('SyncToken') or '0')
                save_links(rows)
            log('error', f'QuickBooks SyncToken conflict on {entity} {payload.get("Id")}. The ERP copy was left as saved here.')
            return {}
        raise


def push_document(invoice):
    if not connected() or not invoice or invoice.get('qbo_origin') or invoice.get('source') == 'quickbooks':
        return None
    kind = 'credit' if invoice.get('doc_type') == 'credit_note' else 'invoice'
    entity = 'creditmemo' if kind == 'credit' else 'invoice'
    response_key = 'CreditMemo' if kind == 'credit' else 'Invoice'
    if find_link(kind, erp_id=invoice.get('id')):
        return None
    customer_link = find_link('customer', erp_id=invoice.get('customer_id'))
    if not customer_link:
        push_customer(invoice.get('customer_id'))
        customer_link = find_link('customer', erp_id=invoice.get('customer_id'))
    if not customer_link:
        log('error', f"Invoice {invoice.get('id')} was not sent because the customer is not in QuickBooks.")
        return None
    payload = {
        'CustomerRef': {'value': customer_link['qbo_id']},
        'CurrencyRef': {'value': str(invoice.get('currency') or 'usd').upper()},
        'TxnDate': invoice.get('invoice_date') or _today(),
        'Line': _document_lines(invoice),
        'PrivateNote': invoice.get('reason') or invoice.get('project') or '',
    }
    if kind == 'invoice' and invoice.get('due_date'):
        payload['DueDate'] = invoice['due_date']
    customer = load_customers().get(invoice.get('customer_id') or '') or {}
    term = _term_ref(invoice.get('payment_terms') or customer.get('payment_terms') or 'Net 30')
    if term:
        payload['SalesTermRef'] = term
    try:
        data = _post_entity(entity, payload)
    except QuickBooksError as exc:
        log('error', f"Document {invoice.get('number') or invoice.get('id')}: {exc}")
        return None
    remote = data.get(response_key) or {}
    if remote.get('Id'):
        upsert_link(kind, invoice.get('id'), remote.get('Id'), remote.get('SyncToken'), 'erp')
        log('info', f"Sent {invoice.get('number') or invoice.get('id')} to QuickBooks.")
    return remote


def push_document_id(invoice_id):
    invoice = next((row for row in load_invoices() if row.get('id') == invoice_id), None)
    return push_document(invoice)


def void_document(invoice):
    if not connected() or not invoice:
        return None
    kind = 'credit' if invoice.get('doc_type') == 'credit_note' else 'invoice'
    entity = 'creditmemo' if kind == 'credit' else 'invoice'
    link = find_link(kind, erp_id=invoice.get('id'))
    if not link or link.get('origin') == 'qbo':
        return None
    try:
        realm = load_tokens()['realm_id']
        api_post(f"/v3/company/{realm}/{entity}?operation=void", {'Id': link['qbo_id'], 'SyncToken': link['sync_token']})
        log('info', f"Voided QuickBooks {entity} {link['qbo_id']}.")
    except QuickBooksError as exc:
        log('error', f'Void {invoice.get("id")}: {exc}')
    return None


def push_payment(payment):
    if not connected() or not payment or payment.get('qbo_origin') or payment.get('voided'):
        return None
    if payment.get('method') == 'credit_note':
        return _push_credit_application(payment)
    existing = find_link('payment', erp_id=payment.get('id'))
    customer_link = find_link('customer', erp_id=payment.get('customer_id'))
    if not customer_link:
        push_customer(payment.get('customer_id'))
        customer_link = find_link('customer', erp_id=payment.get('customer_id'))
    if not customer_link:
        log('error', f"Payment {payment.get('id')} was not sent because the customer is not in QuickBooks.")
        return None
    lines = []
    for allocation in payment.get('allocations') or []:
        if allocation.get('voided') or allocation.get('target_type') != 'invoice':
            continue
        invoice_link = find_link('invoice', erp_id=allocation.get('target_id'))
        if not invoice_link:
            push_document_id(allocation.get('target_id'))
            invoice_link = find_link('invoice', erp_id=allocation.get('target_id'))
        if not invoice_link:
            continue
        lines.append({
            'Amount': money(allocation.get('amount')),
            'LinkedTxn': [{'TxnId': invoice_link['qbo_id'], 'TxnType': 'Invoice'}],
        })
    payload = {
        'CustomerRef': {'value': customer_link['qbo_id']},
        'TotalAmt': money(payment.get('amount')),
        'TxnDate': payment.get('date') or _today(),
        'CurrencyRef': {'value': str(payment.get('currency') or 'usd').upper()},
        'PrivateNote': payment.get('reference') or payment.get('notes') or '',
    }
    if lines:
        payload['Line'] = lines
    try:
        if existing:
            payload['Id'] = existing['qbo_id']
            payload['SyncToken'] = existing['sync_token']
            payload['sparse'] = True
        data = _post_entity('payment', payload)
    except QuickBooksError as exc:
        log('error', f"Payment {payment.get('id')}: {exc}")
        return None
    remote = data.get('Payment') or {}
    if remote.get('Id'):
        upsert_link('payment', payment.get('id'), remote.get('Id'), remote.get('SyncToken'), existing.get('origin') if existing else 'erp')
        log('info', f"Sent payment {payment.get('id')} to QuickBooks.")
    return remote


def _push_credit_application(payment):
    if find_link('payment', erp_id=payment.get('id')):
        return None
    credit = find_link('credit', erp_id=payment.get('credit_note_id'))
    invoice_id = None
    amount = money(payment.get('amount'))
    for allocation in payment.get('allocations') or []:
        if allocation.get('target_type') == 'invoice':
            invoice_id = allocation.get('target_id')
            amount = money(allocation.get('amount'))
    invoice = find_link('invoice', erp_id=invoice_id) if invoice_id else None
    customer_link = find_link('customer', erp_id=payment.get('customer_id'))
    if not (credit and invoice and customer_link):
        log('error', f"Credit application {payment.get('id')} was not sent because QuickBooks is missing the invoice or credit memo.")
        return None
    payload = {
        'CustomerRef': {'value': customer_link['qbo_id']},
        'TotalAmt': 0,
        'TxnDate': payment.get('date') or _today(),
        'Line': [
            {'Amount': amount, 'LinkedTxn': [{'TxnId': invoice['qbo_id'], 'TxnType': 'Invoice'}]},
            {'Amount': amount, 'LinkedTxn': [{'TxnId': credit['qbo_id'], 'TxnType': 'CreditMemo'}]},
        ],
    }
    try:
        data = _post_entity('payment', payload)
    except QuickBooksError as exc:
        log('error', f"Credit application {payment.get('id')}: {exc}")
        return None
    remote = data.get('Payment') or {}
    if remote.get('Id'):
        upsert_link('payment', payment.get('id'), remote.get('Id'), remote.get('SyncToken'), 'erp')
    return remote


def void_payment(payment):
    if not connected() or not payment:
        return None
    link = find_link('payment', erp_id=payment.get('id'))
    if not link or link.get('origin') == 'qbo':
        return None
    try:
        realm = load_tokens()['realm_id']
        api_post(f"/v3/company/{realm}/payment?operation=void", {'Id': link['qbo_id'], 'SyncToken': link['sync_token']})
        log('info', f"Voided QuickBooks payment {link['qbo_id']}.")
    except QuickBooksError as exc:
        log('error', f'Void payment {payment.get("id")}: {exc}')
    return None


def verify_webhook(payload, signature):
    secret = (os.environ.get('QBO_WEBHOOK_VERIFIER') or '').strip()
    if not secret or not signature:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, signature.strip())


def handle_webhook(payload):
    body = json.loads(payload.decode() or '{}')
    realm = (load_tokens().get('realm_id') or '')
    for note in body.get('eventNotifications') or []:
        if realm and str(note.get('realmId')) != str(realm):
            log('error', 'Ignored a QuickBooks webhook for a different company.')
            continue
        for entity in ((note.get('dataChangeEvent') or {}).get('entities') or []):
            _handle_event(entity.get('name'), str(entity.get('id') or ''), entity.get('operation') or 'Create')
    return {'ok': True}


def _handle_event(name, qbo_id, operation):
    if name not in {'Customer', 'Invoice', 'Payment', 'CreditMemo'} or not qbo_id:
        return
    if operation in {'Void', 'Delete'}:
        _void_from_qbo(name, qbo_id)
        return
    realm = load_tokens()['realm_id']
    data = api_get(f"/v3/company/{realm}/{_entity_path(name)}/{qbo_id}")
    row = None
    for value in data.values():
        if isinstance(value, dict) and str(value.get('Id') or '') == qbo_id:
            row = value
            break
    if not row:
        log('error', f'QuickBooks did not return {name} {qbo_id}.')
        return
    if name == 'Customer':
        _ingest_customer_change(row)
    elif name == 'Invoice':
        _ingest_invoice(row)
    elif name == 'CreditMemo':
        _ingest_credit(row)
    elif name == 'Payment':
        _ingest_payment(row)


def _ingest_customer_change(row):
    link = find_link('customer', qbo_id=row.get('Id'))
    if link:
        _remember_token('customer', row)
        if link.get('origin') == 'erp':
            return
        customers = load_customers()
        customer = customers.get(link['erp_id'])
        if not customer:
            return
        customer['name'] = row.get('DisplayName') or customer.get('name')
        email = ((row.get('PrimaryEmailAddr') or {}).get('Address')) or customer.get('email')
        customer['email'] = email
        customer['phone'] = ((row.get('PrimaryPhone') or {}).get('FreeFormNumber')) or customer.get('phone')
        customers[link['erp_id']] = customer
        save_customers(customers)
        return
    from app import new_customer_id
    customers = load_customers()
    customer_id = new_customer_id(row.get('DisplayName') or 'customer', customers)
    customers[customer_id] = _customer_from_qbo(row, customer_id)
    customers[customer_id]['qbo_id'] = str(row.get('Id'))
    save_customers(customers)
    upsert_link('customer', customer_id, row.get('Id'), row.get('SyncToken'), 'qbo')
    log('info', f"Added {row.get('DisplayName')} from QuickBooks.")


def _void_from_qbo(name, qbo_id):
    kind = {'Invoice': 'invoice', 'CreditMemo': 'credit', 'Payment': 'payment', 'Customer': 'customer'}.get(name)
    link = find_link(kind, qbo_id=qbo_id) if kind else None
    if not link:
        log('info', f'QuickBooks {name} {qbo_id} was voided. It is not in the ERP.')
        return
    if kind in {'invoice', 'credit'}:
        invoices = load_invoices()
        for row in invoices:
            if str(row.get('id')) == str(link['erp_id']) and not row.get('voided'):
                row['voided'] = True
                row['void_reason'] = 'Voided in QuickBooks'
                row['qbo_origin'] = True
        save_invoices(invoices)
    elif kind == 'payment':
        payments = load_payments()
        for row in payments:
            if str(row.get('id')) == str(link['erp_id']) and not row.get('voided'):
                row['voided'] = True
                row['void_reason'] = 'Voided in QuickBooks'
                row['qbo_origin'] = True
        save_payments(payments)
    log('info', f'Voided the ERP copy of QuickBooks {name} {qbo_id}.')


def poll_cdc():
    if not connected():
        return {'skipped': True}
    settings = load_settings()
    since = settings.get('last_cdc') or (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
    realm = load_tokens()['realm_id']
    data = api_get(
        f"/v3/company/{realm}/cdc?entities=Customer,Invoice,Payment,CreditMemo&changedSince="
        + urllib.parse.quote(since)
    )
    count = 0
    for batch in data.get('CDCResponse') or []:
        for response in batch.get('QueryResponse') or []:
            for row in response.get('Customer') or []:
                _ingest_customer_change(row)
                count += 1
            for row in response.get('Invoice') or []:
                _ingest_invoice(row)
                count += 1
            for row in response.get('CreditMemo') or []:
                _ingest_credit(row)
                count += 1
            for row in response.get('Payment') or []:
                _ingest_payment(row)
                count += 1
    settings = load_settings()
    settings['last_cdc'] = _now()
    save_settings(settings)
    log('info', f'QuickBooks change poll read {count} records.')
    return {'count': count}


def maybe_poll():
    if os.environ.get('APP_ENV') == 'test' or not connected():
        return
    settings = load_settings()
    last = settings.get('last_cdc_epoch') or 0
    if time.time() - float(last) < CDC_INTERVAL:
        return
    try:
        poll_cdc()
    except QuickBooksError as exc:
        log('error', f'QuickBooks change poll failed: {exc}')
    finally:
        settings = load_settings()
        settings['last_cdc_epoch'] = time.time()
        save_settings(settings)

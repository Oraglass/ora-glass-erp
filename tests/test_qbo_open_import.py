"""Open QuickBooks invoices are copied in. The import never writes to QuickBooks."""

import json
import urllib.parse

import quickbooks
from billing import invoice_balance, money
from quickbooks import CANADA_REALM, US_REALM
from storage import (
    append_invoice, load_customers, load_invoices, load_payments, load_users, save_customers, save_users,
)
from tests.conftest import login, write_user
from tests.test_quickbooks import _configure, _save_connection


class OpenInvoiceQBO:
    def __init__(self):
        self.calls = []
        self.customers = {
            US_REALM: {
                '2': {
                    'Id': '2', 'SyncToken': '1',
                    'DisplayName': 'Open Windows Inc',
                    'CompanyName': 'Open Windows Inc',
                    'CurrencyRef': {'value': 'USD'},
                },
            },
        }
        self.invoices = {
            US_REALM: [
                _invoice('501', '5001', 500000, 500000, '2026-03-01', '2026-03-31'),
                _invoice('502', '5002', 4000, 1000, '2026-04-02', '2026-05-02'),
                _invoice('503', '5003', 200, 0, '2026-01-01', '2026-01-31'),
                _invoice('504', '5004', 50, 50, '2026-02-01', '2026-03-01'),
                _invoice('77', '5005', 80, 80, '2026-02-02', '2026-03-02'),
            ],
            CANADA_REALM: [
                _invoice('9', 'CA-9', 2635.02, 2635.02, '2026-05-01', '2026-06-01', 'CAD'),
            ],
        }

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split('/') if part]
        realm = parts[parts.index('company') + 1] if 'company' in parts else ''
        if method != 'GET':
            return 500, {}, b'{"Fault":{"Error":[{"Message":"writes are not allowed"}]}}'
        if parsed.path.rstrip('/').endswith('/query'):
            statement = urllib.parse.parse_qs(parsed.query).get('query', [''])[0]
            if 'from Customer' in statement:
                row = self.customers.get(realm, {}).get('2') if "Id = '2'" in statement else None
                payload = {'Customer': [row] if row else []}
            elif 'from Invoice' in statement:
                payload = {'Invoice': self.invoices.get(realm, [])}
            else:
                payload = {}
            return 200, {}, json.dumps({'QueryResponse': payload}).encode()
        if '/customer/' in parsed.path:
            row = self.customers.get(realm, {}).get(parts[-1])
            if not row:
                return 404, {}, b'{}'
            return 200, {}, json.dumps({'Customer': row}).encode()
        return 404, {}, b'{}'


def _invoice(qbo_id, number, total, balance, day, due, currency='USD'):
    return {
        'Id': qbo_id,
        'SyncToken': '0',
        'DocNumber': number,
        'CustomerRef': {'value': '2'},
        'TotalAmt': total,
        'Balance': balance,
        'CurrencyRef': {'value': currency},
        'TxnDate': day,
        'DueDate': due,
        'Line': [{'Description': 'IGU', 'Amount': total}],
    }


def _open_windows(limit=12345):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'ap@openwindows.example', 'customer', customer_id='open-windows',
        customer_name='Open Windows Inc',
    )
    customers = load_customers()
    customers['open-windows'].update({
        'credit_limit': limit,
        'credit_currency': 'cad',
        'credit_approved': True,
        'country': 'Canada',
    })
    save_customers(customers)


def test_open_invoice_import_copies_balances_and_does_not_write_to_quickbooks(client, monkeypatch):
    _configure(monkeypatch)
    _open_windows()
    assert login(client, 'admin@oraglass.us').status_code == 302
    settings = client.get('/settings/quickbooks')
    assert b'Import open invoices from QuickBooks' in settings.data
    kept = append_invoice({
        'number': '5004', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
        'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'currency': 'usd',
        'total': 50, 'balance': 0, 'opening_applied': 50, 'invoice_date': '2026-02-01',
        'voided': False, 'items': [{'name': 'Glass', 'pcs': 1, 'total': 50}],
    })
    linked_local = append_invoice({
        'number': 'OLD-77', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
        'customer_id': 'open-windows', 'company': 'Open Windows Inc', 'currency': 'usd',
        'total': 80, 'balance': 80, 'qbo_id': '77', 'invoice_date': '2026-02-02',
        'voided': False, 'items': [{'name': 'Glass', 'pcs': 1, 'total': 80}],
    })
    fake = OpenInvoiceQBO()
    quickbooks.TRANSPORT = fake
    try:
        _save_connection('us', US_REALM, 'Ora Glass Inc')
        _save_connection('ca', CANADA_REALM, 'Ora Glass (Canada) Inc')
        with quickbooks.connection_scope('us'):
            quickbooks.upsert_link('invoice', linked_local['id'], '77', '0', 'qbo', realm_id=US_REALM)
        linked = client.post('/api/quickbooks/link-customer', json={
            'connection': 'us', 'qbo_id': '2', 'customer_id': 'open-windows',
        })
        assert linked.status_code == 200, linked.get_json()
        before = len(fake.calls)
        imported = client.post('/api/financial/import-open-invoices', json={'customer_id': 'open-windows'})
        assert imported.status_code == 200, imported.get_json()
        result = imported.get_json()
        assert result['invoices'] == 2
        assert result['skipped'] == 2
        fresh_calls = fake.calls[before:]
        assert fresh_calls
        assert all(method == 'GET' for method, _url, _body in fresh_calls)
        assert all(CANADA_REALM not in url for _method, url, _body in fresh_calls)
        assert all(US_REALM in url for _method, url, _body in fresh_calls)
        assert not any(urllib.parse.urlparse(url).path.rstrip('/').endswith('/invoice') for _method, url, _body in fresh_calls)
        stored = [row for row in load_invoices() if row.get('customer_id') == 'open-windows']
        by_number = {row.get('number'): row for row in stored}
        assert set(by_number) == {'5004', 'OLD-77', '5001', '5002'}
        assert by_number['5004']['id'] == kept['id']
        opened = by_number['5001']
        assert opened['qbo_id'] == '501'
        assert opened['invoice_date'] == '2026-03-01'
        assert opened['due_date'] == '2026-03-31'
        assert opened['total'] == 500000
        assert opened['currency'] == 'usd'
        assert opened['imported'] is True
        assert opened['legacy'] is True
        assert opened['qbo_sync'] == 'synced'
        assert opened['qbo_origin'] is True
        assert invoice_balance(opened, load_payments()) == 500000
        partial = by_number['5002']
        assert partial['total'] == 4000
        assert invoice_balance(partial, load_payments()) == 1000
        assert partial['status'] == 'partial'
        customer = load_customers()['open-windows']
        assert customer['credit_limit'] == 12345
        credit = client.get('/api/credit?customer_id=open-windows').get_json()
        books = {row['currency']: row for row in credit['by_currency']}
        assert books['usd']['open_receivables'] == money(500000 + 1000 + 80)
        assert credit['credit_limit'] == 12345
        calls_after = len(fake.calls)
        assert quickbooks.push_document(opened) is None
        assert len(fake.calls) == calls_after
        again = client.post('/api/quickbooks/import/open-invoices', json={'connection': 'us', 'customer_id': 'open-windows'})
        assert again.status_code == 200, again.get_json()
        assert again.get_json()['result']['invoices'] == 0
        assert again.get_json()['result']['skipped'] == 4
        assert len([row for row in load_invoices() if row.get('customer_id') == 'open-windows']) == 4
        client.post('/logout')
        write_user('lee@oraglass.us', 'employee')
        users = load_users()
        users['lee@oraglass.us']['modules'] = ['dashboard', 'financial']
        save_users(users)
        assert login(client, 'lee@oraglass.us').status_code == 302
        page = client.get('/financial')
        assert b'Import open invoices from QuickBooks' not in page.data
        refused = client.post('/api/financial/import-open-invoices', json={'customer_id': 'open-windows'})
        assert refused.status_code == 403
    finally:
        quickbooks.TRANSPORT = None

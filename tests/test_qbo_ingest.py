"""QuickBooks documents are recorded in the ERP, including later edits."""

import base64
import hashlib
import hmac
import json

import quickbooks
from billing import invoice_balance
from storage import append_invoice, load_invoices, load_payments, save_customers
from tests.conftest import login, write_user
from tests.test_quickbooks import FakeQBO, _configure, _connect


def _signature(payload):
    return base64.b64encode(hmac.new(b'verifier-token', payload, hashlib.sha256).digest()).decode()


def _post_webhook(client, payload):
    raw = json.dumps(payload).encode()
    return client.post(
        '/quickbooks/webhook',
        data=raw,
        headers={'intuit-signature': _signature(raw), 'Content-Type': 'application/json'},
    )


def test_webhook_upsert_uses_the_doc_number_and_keeps_lines(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    fake.invoices[0]['Line'] = [
        {'Amount': 100, 'Description': 'Lite A', 'DetailType': 'SalesItemLineDetail',
         'SalesItemLineDetail': {'Qty': 2, 'UnitPrice': 50}},
        {'Amount': 40, 'Description': 'Lite B', 'DetailType': 'SalesItemLineDetail',
         'SalesItemLineDetail': {'Qty': 1, 'UnitPrice': 40}},
    ]
    fake.invoices[0]['TotalAmt'] = 140
    fake.invoices[0]['Balance'] = 140
    try:
        _connect(client, fake)
        save_customers({'open-windows': {'id': 'open-windows', 'name': 'Open Windows', 'tier': 'T2', 'status': 'active'}})
        quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'qbo', realm_id='123456')
        created = _post_webhook(client, {'eventNotifications': [{
            'realmId': '123456',
            'dataChangeEvent': {'entities': [{'name': 'Invoice', 'id': '90', 'operation': 'Create'}]},
        }]})
        assert created.status_code == 200
        invoice = next(row for row in load_invoices() if row.get('qbo_origin'))
        assert invoice['number'] == 'QBO-100'
        assert not str(invoice['number']).startswith('INV-')
        assert invoice['qbo_id'] == '90'
        assert invoice['qbo_realm_id'] == '123456'
        assert invoice['source'] == 'quickbooks'
        assert invoice['issuer'] == 'us'
        assert invoice['currency'] == 'usd'
        assert invoice['due_date'] == '2026-02-14'
        assert [item['name'] for item in invoice['items']] == ['Lite A', 'Lite B']
        assert invoice_balance(invoice, load_payments()) == 140
        fake.invoices[0]['TotalAmt'] = 150
        fake.invoices[0]['Balance'] = 150
        fake.invoices[0]['SyncToken'] = '1'
        fake.invoices[0]['Line'][1]['Amount'] = 50
        updated = _post_webhook(client, {'eventNotifications': [{
            'realmId': '123456',
            'dataChangeEvent': {'entities': [{'name': 'Invoice', 'id': '90', 'operation': 'Update'}]},
        }]})
        assert updated.status_code == 200
        rows = [row for row in load_invoices() if row.get('qbo_id') == '90']
        assert len(rows) == 1
        assert rows[0]['total'] == 150
        page = client.get('/settings/quickbooks').get_data(as_text=True)
        assert 'Last webhook:' in page
        assert 'Last change poll:' in page
        assert 'Pending QBO documents' in page
        assert quickbooks.start_cdc_loop() is None
    finally:
        quickbooks.TRANSPORT = None


def test_unlinked_customer_waits_and_is_ingested_when_linked(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        save_customers({'open-windows': {'id': 'open-windows', 'name': 'Open Windows', 'tier': 'T2', 'status': 'active'}})
        created = _post_webhook(client, {'eventNotifications': [{
            'realmId': '123456',
            'dataChangeEvent': {'entities': [{'name': 'Invoice', 'id': '90', 'operation': 'Create'}]},
        }]})
        assert created.status_code == 200
        assert load_invoices() == []
        pending = quickbooks.pending_view()
        assert len(pending) == 1
        assert pending[0]['doc_number'] == 'QBO-100'
        assert pending[0]['customer_qbo_id'] == '2'
        linked = quickbooks.link_customer('2', 'open-windows')
        assert linked['customer_id'] == 'open-windows'
        stored = load_invoices()
        assert len(stored) == 1
        assert stored[0]['number'] == 'QBO-100'
        assert quickbooks.pending_view() == []
    finally:
        quickbooks.TRANSPORT = None


def test_cdc_void_and_payment_allocation_match_the_balance(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    fake.invoices[0]['TotalAmt'] = 100
    fake.invoices[0]['Balance'] = 40
    fake.invoices[0]['Line'] = [{'Amount': 100, 'Description': 'Glass', 'DetailType': 'SalesItemLineDetail'}]
    try:
        _connect(client, fake)
        save_customers({'open-windows': {'id': 'open-windows', 'name': 'Open Windows', 'tier': 'T2', 'status': 'active'}})
        quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'qbo', realm_id='123456')
        assert quickbooks._ingest_invoice(fake.invoices[0]) is True
        fake.cdc = [{'Payment': [{
            'Id': '70', 'SyncToken': '0', 'CustomerRef': {'value': '2'},
            'TotalAmt': 60, 'TxnDate': '2026-11-01', 'CurrencyRef': {'value': 'USD'},
            'Line': [{'Amount': 60, 'LinkedTxn': [{'TxnType': 'Invoice', 'TxnId': '90'}]}],
        }]}]
        assert quickbooks.poll_cdc()['count'] == 1
        invoice = next(row for row in load_invoices() if row.get('qbo_id') == '90')
        payment = load_payments()[0]
        assert payment['allocations'][0]['amount'] == 60
        assert invoice_balance(invoice, load_payments()) == 40
        assert not any(row.get('reference') == 'QuickBooks history' for row in load_payments())
        fake.cdc = [{'Invoice': [{'Id': '90', 'status': 'Deleted'}]}]
        quickbooks.poll_cdc()
        voided = next(row for row in load_invoices() if row.get('qbo_id') == '90')
        assert voided['voided'] is True
    finally:
        quickbooks.TRANSPORT = None


def test_erp_invoice_is_not_rewritten_and_doc_numbers_stay_in_one_realm(client, monkeypatch):
    _configure(monkeypatch)
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    fake = FakeQBO()
    try:
        _connect(client, fake)
        save_customers({'open-windows': {'id': 'open-windows', 'name': 'Open Windows', 'tier': 'T2', 'status': 'active'}})
        quickbooks.upsert_link('customer', 'open-windows', '2', '0', 'qbo', realm_id='123456')
        append_invoice({
            'number': 'QBO-100', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'source': 'erp', 'customer_id': 'open-windows', 'currency': 'usd',
            'total': 10, 'balance': 10, 'voided': False, 'invoice_date': '2026-01-01',
            'items': [{'name': 'Keep me', 'pcs': 1, 'total': 10}],
            'qbo_realm_id': '123456',
        })
        assert quickbooks._ingest_invoice(fake.invoices[0]) is False
        kept = next(row for row in load_invoices() if row.get('number') == 'QBO-100')
        assert kept['items'][0]['name'] == 'Keep me'
        assert kept['qbo_id'] == '90'
        canada = {
            'number': 'QBO-100', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'ca',
            'qbo_realm_id': '123146408064299', 'qbo_id': '8', 'customer_id': 'open-windows',
            'currency': 'cad', 'total': 5, 'voided': False, 'items': [],
        }
        assert quickbooks._doc_in_this_realm(canada, 'QBO-100', '123456') is False
        assert quickbooks._doc_in_this_realm(kept, 'QBO-100', '123456') is True
    finally:
        quickbooks.TRANSPORT = None

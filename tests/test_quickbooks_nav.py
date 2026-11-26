"""QuickBooks sits in the nav for admins, and sync errors open that company's card."""

import re

import quickbooks
from storage import save_invoices, save_payments
from tests.conftest import login, write_user


CANADA_ERROR = (
    'HighQ has no QuickBooks tax code for British Columbia. '
    'Set the tax codes on the Canada QuickBooks card. '
    'The invoice was kept in the ERP and was not posted.'
)
US_ERROR = (
    'QuickBooks is not connected for Ora Glass Inc (realm 9341456057091137). '
    'Reconnect that company, then use Send to QuickBooks.'
)


def _nav(page):
    match = re.search(r'<nav class="module-nav"[\s\S]*?</nav>', page.get_data(as_text=True))
    assert match, page.get_data(as_text=True)[:400]
    return re.findall(r'<a [^>]*href="([^"]*)"[^>]*>([^<]*)</a>', match.group(0))


def test_only_an_admin_sees_quickbooks_after_financials(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('seller@oraglass.us', 'sales')
    write_user('lee@harbor.example', 'customer', customer_id='harbor', customer_name='Harbor Glass')
    assert login(client, 'admin@oraglass.us').status_code == 302
    labels = [label for _href, label in _nav(client.get('/invoices'))]
    assert labels[labels.index('Users') + 1] == 'QuickBooks'
    assert 'Production settings' in labels
    assert 'Shipments' in labels
    assert 'Racks' in labels
    hrefs = [href for href, label in _nav(client.get('/invoices'))]
    assert hrefs[hrefs.index('/users') + 1] == '/settings/quickbooks'
    client.post('/logout')

    assert login(client, 'seller@oraglass.us').status_code == 302
    sales = _nav(client.get('/invoices'))
    assert '/settings/quickbooks' not in [href for href, _label in sales]
    assert 'QuickBooks' not in [label for _href, label in sales]
    denied = client.get('/settings/quickbooks')
    assert denied.status_code == 302
    assert '/settings/quickbooks' not in denied.headers['Location']
    client.post('/logout')

    assert login(client, 'lee@harbor.example').status_code == 302
    customer = _nav(client.get('/invoices'))
    assert 'QuickBooks' not in [label for _href, label in customer]


def test_invoice_and_payment_errors_open_the_named_quickbooks_card(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_invoices([{
        'id': 1, 'number': 'CA-INV-0001', 'issuer': 'ca', 'currency': 'cad', 'kind': 'customer',
        'customer_id': 'highq', 'company': 'HighQ Developments Inc', 'total': 10, 'balance': 10,
        'qbo_sync': 'error', 'qbo_error': CANADA_ERROR, 'qbo_warning': CANADA_ERROR,
        'items': [{'name': 'Glass', 'pcs': 1, 'total': 10}],
    }])
    save_payments([{
        'id': 7, 'customer_id': 'open-windows', 'company': 'Open Windows Inc',
        'date': '2026-11-23', 'amount': 20987.25, 'currency': 'usd', 'method': 'wire',
        'voided': False, 'allocations': [], 'qbo_sync': 'not_connected', 'qbo_error': US_ERROR,
    }])
    invoice = client.get('/api/invoices/1').get_json()
    assert invoice['qbo_settings_url'] == '/settings/quickbooks?company=ca#ca'
    payment = client.get('/api/payments').get_json()
    row = next(item for item in payment if item['id'] == 7)
    assert row['qbo_settings_url'] == '/settings/quickbooks?company=us#us'
    assert quickbooks.settings_url(
        'The Ora Glass Inc card is connected to the Canada QuickBooks company. Reconnect it to realm 9341456057091137.'
    ) == '/settings/quickbooks?company=ca#ca'
    page = client.get('/invoices').get_data(as_text=True)
    assert 'qbo_settings_url' in page

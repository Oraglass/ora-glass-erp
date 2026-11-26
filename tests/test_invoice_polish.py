"""Invoice PDF polish: addresses, shipment numbers, money, and glass versus services."""

import documents
import quickbooks
from storage import load_customers, save_customers, save_invoices
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def test_typed_commas_are_dropped_when_an_address_is_printed_or_saved(client):
    assert documents.normalize_address_part('8610 Glenlyon Pkwy,,') == '8610 Glenlyon Pkwy'
    assert documents.normalize_address_part('8610 Glenlyon Pkwy, ,') == '8610 Glenlyon Pkwy'
    assert documents.normalize_address_part(',,') == ''
    assert documents.normalize_address_part('  , ,  ') == ''
    assert documents.normalize_address_part('Vancouver, BC') == 'Vancouver, BC'
    lines = __import__('app')._address_lines({
        'address': '8610 Glenlyon Pkwy,,',
        'city': ' ',
        'state': '',
        'zip': '',
        'country': 'Canada',
    })
    assert lines == ['8610 Glenlyon Pkwy', 'Canada']
    assert ', ,' not in ' '.join(lines)
    party = documents.invoice_party({'company': 'HighQ', 'customer_address': ['8610 Glenlyon Pkwy,,', '  , ,  ']})
    assert [row['text'] for row in party if row['key'] == 'address'] == ['8610 Glenlyon Pkwy']

    save_customers({'highq': {
        'id': 'highq', 'name': 'HighQ Developments Inc', 'status': 'active',
        'address': '8610 Glenlyon Pkwy,,', 'city': '', 'state': '', 'zip': '',
        'country': 'Canada',
    }})
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_invoices([{
        'id': 1, 'number': 'CA-INV-0001', 'kind': 'customer', 'issuer': 'ca', 'currency': 'cad',
        'customer_id': 'highq', 'company': 'HighQ Developments Inc',
        'invoice_date': '2026-11-20', 'igu_subtotal': 10, 'total': 10, 'balance': 10,
        'items': [{'name': 'Clear 4mm', 'pcs': 1, 'total': 10, 'item_type': 'igu'}],
    }])
    shown = client.get('/api/invoices/1').get_json()
    assert [row['text'] for row in shown['party'] if row['key'] == 'address'] == [
        '8610 Glenlyon Pkwy', 'Canada',
    ]
    assert load_customers()['highq']['address'] == '8610 Glenlyon Pkwy,,'
    saved = client.put('/api/customers/highq', json={'address': '8610 Glenlyon Pkwy,,', 'city': 'Burnaby'})
    assert saved.status_code == 200, saved.get_json()
    stored = load_customers()['highq']
    assert stored['address'] == '8610 Glenlyon Pkwy'
    assert stored['city'] == 'Burnaby'
    text = pdf_text(client.get('/api/invoices/1/pdf').data)
    assert b'8610 Glenlyon Pkwy' in text
    assert b', ,' not in text
    assert b',,' not in text


def test_services_are_separate_from_the_igu_subtotal_and_from_quickbooks(client, monkeypatch):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_invoices([{
        'id': 1, 'number': 'CA-INV-0001', 'kind': 'customer', 'issuer': 'ca', 'currency': 'cad',
        'company': 'HighQ Developments Inc', 'invoice_date': '2026-11-20', 'due_date': '2026-12-20',
        'project': 'Lobby',
        'igu_subtotal': 2716.95, 'total': 2852.80, 'balance': 2800.00, 'applied': 52.80,
        'seller_gst_number': '729631119 RT0001',
        'tax_lines': [{'name': 'GST', 'rate': '5', 'amount': 135.85}],
        'items': [
            {'name': 'Solarban 6mm tempered insulated unit with a long description that has to wrap',
             'pcs': 4, 'total': 2516.95},
            {'kind': 'irregular_surcharge', 'name': 'Irregular shape surcharge', 'total': 200},
        ],
        'extra_charges': [{'label': 'Crane', 'amount': 50}],
    }])
    body = client.get('/api/invoices/1').get_json()
    assert [row['item_type'] for row in body['igu_lines']] == ['igu']
    assert [row['name'] for row in body['service_lines']] == ['Irregular shape surcharge', 'Crane']
    assert body['service_lines'][0]['display_qty'] == 1
    assert body['service_lines'][1]['display_qty'] == 1
    labels = [row['label'] for row in body['summary']]
    assert labels == [
        'IGU subtotal', 'Services subtotal', 'Subtotal', 'GST 5%',
        'Grand total', 'Payments applied', 'Balance due',
    ]
    amounts = {row['label']: row['amount'] for row in body['summary']}
    assert amounts['IGU subtotal'] == 2516.95
    assert amounts['Services subtotal'] == 250
    assert amounts['Subtotal'] == 2766.95
    assert amounts['Grand total'] == 2852.80
    page = client.get('/invoices/1').data
    assert b'id="invoice-services"' in page
    assert b'id="invoice-totals"' in page
    text = pdf_text(client.get('/api/invoices/1/pdf').data)
    assert b'Services' in text
    assert b'IGU subtotal' in text
    assert b'Services subtotal' in text
    assert b'Subtotal' in text
    assert b'GST 5%' in text
    assert b'Grand total' in text
    assert b'Balance due' in text
    assert b'$2,516.95' in text
    assert b'$250.00' in text or b'$200.00' in text
    assert b'Ora Glass Inc Terms And Conditions of Sale apply.' in text
    assert b'Rack ' not in text

    monkeypatch.setattr(quickbooks, '_item_id', lambda name: '1')
    lines = quickbooks._document_lines(body)
    names = [
        line['SalesItemLineDetail']['ItemRef']['name']
        for line in lines if line.get('DetailType') == 'SalesItemLineDetail'
    ]
    assert names[0] == 'Insulated Glass Units'
    assert 'Irregular shape surcharge' in names
    assert 'Crane' in names
    assert names.count('Insulated Glass Units') == 1
    amounts = [line['Amount'] for line in lines if line.get('DetailType') == 'SalesItemLineDetail']
    assert 2516.95 in amounts
    assert 200 in amounts
    assert 50 in amounts

    glass_only = documents.invoice_summary({
        'items': [{'name': 'Glass', 'pcs': 1, 'total': 10, 'item_type': 'igu'}],
        'total': 10, 'balance': 10, 'applied': 0,
    })
    assert 'Services subtotal' not in [row['label'] for row in glass_only]

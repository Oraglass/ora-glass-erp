"""Canada invoice tax sits in the totals as a name and rate. Registration numbers stay in the header and the customer block."""

import documents
from storage import save_invoices
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def test_a_zero_percent_discount_is_left_off_the_totals():
    """Discount (0.00%) $-0.00 and the matching IGU total are not printed."""
    blank = {
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'igu_subtotal': 100,
        'subtotal': 100,
        'igu_total': 100,
        'total': 100,
        'balance': 100,
        'applied': 0,
    }
    labels = [row['label'] for row in documents.invoice_summary(blank)]
    assert labels == ['IGU subtotal', 'Grand total', 'Payments applied', 'Balance due']
    assert 'Discount (0.00%)' not in labels
    assert 'IGU total' not in labels
    credit = dict(blank, doc_type='credit_note')
    credit_labels = [row['label'] for row in documents.invoice_summary(credit)]
    assert 'Discount (0.00%)' not in credit_labels
    assert 'IGU total' not in credit_labels
    assert 'Unapplied credit' in credit_labels
    priced = dict(blank, discount={'type': 'percent', 'value': 5}, discount_amount=5, igu_total=95, total=95, balance=95)
    priced_labels = [row['label'] for row in documents.invoice_summary(priced)]
    assert priced_labels[:3] == ['IGU subtotal', 'Discount (5.00%)', 'IGU total']


def test_tax_line_labels_are_the_name_and_rate_only():
    invoice = {
        'seller_gst_number': '729631119 RT0001',
        'pst_number': 'PST-100',
        'qst_number': 'QST-200',
    }
    assert documents.tax_line_label({'name': 'GST', 'rate': '5'}, invoice) == 'GST 5%'
    assert documents.tax_line_label({'name': 'HST', 'rate': '13'}, invoice) == 'HST 13%'
    assert documents.tax_line_label({'name': 'PST', 'rate': '7'}, invoice) == 'PST 7%'
    assert documents.tax_line_label({'name': 'RST', 'rate': '7'}, invoice) == 'RST 7%'
    assert documents.tax_line_label({'name': 'QST', 'rate': '9.975'}, invoice) == 'QST 9.975%'
    assert documents.tax_line_label({'name': 'PST', 'rate': '7'}, {}) == 'PST 7%'
    assert 'GST#' not in documents.tax_line_label({'name': 'GST', 'rate': '5'}, invoice)
    assert 'PST#' not in documents.tax_line_label({'name': 'PST', 'rate': '7'}, invoice)


def test_customer_block_order_is_shared_by_every_invoice_type(client):
    from storage import save_customers
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_customers({
        'highq': {
            'id': 'highq', 'name': 'HighQ Developments Inc', 'status': 'active',
            'address': '100 Main St', 'city': 'Vancouver', 'province': 'BC', 'zip': 'V6B 1A1',
            'country': 'Canada', 'gst_number': '111222333 RT0001', 'pst_number': 'PST-100',
        },
        'ora-glass-canada': {
            'id': 'ora-glass-canada', 'name': 'Ora Glass (Canada) Inc', 'status': 'active',
            'address': '200 Bill To Rd', 'city': 'Surrey', 'province': 'BC', 'zip': 'V3T 0A1',
            'country': 'Canada',
        },
        'harbor': {
            'id': 'harbor', 'name': 'Harbor Glass', 'status': 'active',
            'address': '9 Dock St', 'city': 'Seattle', 'us_state': 'WA', 'zip': '98101',
            'country': 'United States',
        },
    })
    common = {
        'invoice_date': '2026-11-20', 'project': 'Lobby', 'order_id': 4, 'quote_id': 9,
        'igu_subtotal': 100, 'subtotal': 100, 'igu_total': 100, 'discount_amount': 0,
        'total': 100, 'balance': 100, 'items': [{'name': 'Glass', 'pcs': 1, 'total': 100}],
    }
    save_invoices([
        dict(common, id=1, number='CA-INV-0001', kind='customer', issuer='ca', currency='cad',
             customer_id='highq', company='HighQ Developments Inc', ship_province='BC',
             pst_number='PST-100', seller_gst_number='729631119 RT0001',
             tax_lines=[{'name': 'GST', 'rate': '5', 'amount': 5}]),
        dict(common, id=2, number='INV-0054', kind='intercompany', issuer='us', currency='usd',
             customer_id='ora-glass-canada', company='Ora Glass (Canada) Inc',
             seller_gst_number='729631119 RT0001'),
        dict(common, id=3, number='INV-US', kind='customer', issuer='us', currency='usd',
             customer_id='harbor', company='Harbor Glass'),
    ])
    canada = client.get('/api/invoices/1').get_json()
    assert [row['key'] for row in canada['party']] == [
        'customer', 'address', 'address', 'address', 'gst', 'pst', 'project', 'order',
    ]
    assert [row['text'] for row in canada['party'] if row['key'] == 'address'] == [
        '100 Main St', 'Vancouver, BC V6B 1A1', 'Canada',
    ]
    assert next(row['text'] for row in canada['party'] if row['key'] == 'gst') == 'GST# 111222333 RT0001'
    assert any(row['label'] == 'GST 5%' for row in canada['summary'])
    assert all('GST#' not in row['label'] and 'PST#' not in row['label'] for row in canada['summary'])
    intercompany = client.get('/api/invoices/2').get_json()
    assert [row['key'] for row in intercompany['party']] == [
        'customer', 'address', 'address', 'address', 'gst', 'project', 'order',
    ]
    assert next(row['text'] for row in intercompany['party'] if row['key'] == 'gst') == 'GST# 729631119 RT0001'
    assert intercompany['customs']['hts']
    united = client.get('/api/invoices/3').get_json()
    assert [row['key'] for row in united['party']] == ['customer', 'address', 'address', 'project', 'order']
    text = pdf_text(client.get('/api/invoices/1/pdf').data)
    customer = text.find(b'Customer:')
    street = text.find(b'100 Main St')
    gst = text.find(b'GST# 111222333 RT0001')
    pst = text.find(b'PST:')
    project = text.find(b'Project:')
    order = text.find(b'Order:')
    subtotal = text.find(b'IGU subtotal')
    assert -1 not in (customer, street, gst, pst, project, order, subtotal)
    assert customer < street < gst < pst < project < order < subtotal
    assert b'Discount' not in text
    assert b'IGU total' not in text
    canada_pdf = pdf_text(client.get('/api/invoices/1/pdf').data)
    assert b'Ora Glass \\(Canada\\) Inc.' in canada_pdf
    assert b'GST# 729631119 RT0001' in canada_pdf
    assert b'Leatherwood' not in canada_pdf
    assert b'Lynden' not in canada_pdf
    assert b'100 Main St' in canada_pdf
    us_text = pdf_text(client.get('/api/invoices/3/pdf').data)
    assert b'9 Dock St' in us_text
    assert b'Leatherwood' in us_text
    assert b'GST#' not in us_text
    assert b'PST:' not in us_text


def test_canada_invoice_moves_gst_into_the_totals_and_keeps_pst_in_the_header(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    save_invoices([{
        'id': 1,
        'number': 'CA-INV-0001',
        'kind': 'customer',
        'issuer': 'ca',
        'currency': 'cad',
        'company': 'HighQ Developments Inc',
        'customer_id': 'highq',
        'project': 'Lobby',
        'order_id': 4,
        'quote_id': 9,
        'invoice_date': '2026-11-20',
        'due_date': '2026-12-20',
        'ship_province': 'BC',
        'pst_number': 'PST-100',
        'seller_gst_number': '729631119 RT0001',
        'igu_subtotal': 2517.00,
        'subtotal': 2517.00,
        'igu_total': 2517.00,
        'discount': {'type': 'percent', 'value': 0},
        'discount_amount': 0,
        'total': 2819.04,
        'balance': 2819.04,
        'tax_lines': [
            {'name': 'GST', 'rate': '5', 'amount': 125.85},
            {'name': 'PST', 'rate': '7', 'amount': 176.19},
        ],
        'items': [{'name': 'Glass', 'pcs': 1, 'total': 2517.00}],
    }])
    body = client.get('/api/invoices/1').get_json()
    labels = [row['label'] for row in body['summary']]
    assert labels == [
        'IGU subtotal',
        'GST 5%',
        'PST 7%',
        'Grand total',
        'Payments applied',
        'Balance due',
    ]
    assert [row['amount'] for row in body['summary'] if row['label'].startswith('GST')] == [125.85]
    discounted = dict(body)
    discounted['discount'] = {'type': 'percent', 'value': 5}
    discounted['discount_amount'] = 10
    discounted['igu_total'] = 90
    shown = documents.invoice_summary(discounted)
    assert [row['label'] for row in shown if row['label'].startswith('Discount') or row['label'] == 'IGU total'] == [
        'Discount (5.00%)', 'IGU total',
    ]
    text = pdf_text(client.get('/api/invoices/1/pdf').data)
    pst = text.find(b'PST:')
    subtotal = text.find(b'IGU subtotal')
    gst = text.find(b'GST 5%')
    provincial = text.find(b'PST 7%')
    grand = text.find(b'Grand total')
    assert -1 not in (pst, subtotal, gst, provincial, grand)
    assert pst < subtotal < gst < provincial < grand
    assert b'PST-100' in text[:subtotal]
    assert b'GST# 729631119 RT0001' in text[:subtotal]
    assert b'GST#' not in text[subtotal:]
    assert b'PST#' not in text
    assert b'PST-100' not in text[subtotal:]
    assert b'Discount' not in text
    assert b'IGU total' not in text

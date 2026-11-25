"""Canada invoice tax sits in the totals, with the PST number left in the header."""

import documents
from storage import save_invoices
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def test_tax_line_labels_include_the_registration_for_each_canada_tax():
    invoice = {
        'seller_gst_number': '729631119 RT0001',
        'pst_number': 'PST-100',
        'qst_number': 'QST-200',
    }
    assert documents.tax_line_label({'name': 'GST', 'rate': '5'}, invoice) == 'GST 5% (GST# 729631119 RT0001)'
    assert documents.tax_line_label({'name': 'HST', 'rate': '13'}, invoice) == 'HST 13% (GST# 729631119 RT0001)'
    assert documents.tax_line_label({'name': 'PST', 'rate': '7'}, invoice) == 'PST 7% (PST# PST-100)'
    assert documents.tax_line_label({'name': 'RST', 'rate': '7'}, invoice) == 'RST 7% (PST# PST-100)'
    assert documents.tax_line_label({'name': 'QST', 'rate': '9.975'}, invoice) == 'QST 9.975% (QST# QST-200)'
    assert documents.tax_line_label({'name': 'PST', 'rate': '7'}, {}) == 'PST 7%'


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
        'Discount (fixed)',
        'IGU total',
        'GST 5% (GST# 729631119 RT0001)',
        'PST 7% (PST# PST-100)',
        'Grand total',
        'Payments applied',
        'Balance due',
    ]
    assert [row['amount'] for row in body['summary'] if row['label'].startswith('GST')] == [125.85]
    text = pdf_text(client.get('/api/invoices/1/pdf').data)
    pst = text.find(b'PST:')
    igu = text.find(b'IGU total')
    gst = text.find(b'GST 5% \\(GST# 729631119 RT0001\\)')
    provincial = text.find(b'PST 7% \\(PST# PST-100\\)')
    grand = text.find(b'Grand total')
    assert -1 not in (pst, igu, gst, provincial, grand)
    assert pst < igu < gst < provincial < grand
    assert b'PST-100' in text[:igu]
    assert text.find(b'GST 5%') == gst

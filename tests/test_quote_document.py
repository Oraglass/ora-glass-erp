"""The per-size quote and the order confirmation use the production layout."""

import documents
from production import makeup_from_spec
from storage import load_orders
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def test_build_label_puts_overall_millimetres_and_inches_first():
    spec = {
        'glass1': 'Solarban60 VT 4mm', 'temper1': 'yes',
        'spacer1': 'Superspacer 24mm / sqf', 'sealant1': 'Polysulfide',
        'glass2': 'Clear Glass 4mm', 'temper2': 'yes',
    }
    assert documents.build_label(spec) == (
        '32 (1 1/4") - 4mmSolarban60 VT Tempered+24mmSuperSpacer with Argon(Polysulfide)'
        '+4mmClear Glass Tempered'
    )
    assert documents.quote_form_fraction(28) == '1 1/8'
    assert documents.quote_form_fraction(42) == '1 5/8'
    assert documents.quote_form_fraction(47) == '1 7/8'
    text = documents.line_description({'spec': spec})
    assert text.startswith('32 (1 1/4") - 4mmSolarban60 VT Tempered /')
    assert '<br/>+' in text
    surcharge = documents.describe_items([
        {'name': 'Short', 'spec': spec},
        {'kind': 'irregular_surcharge', 'name': 'Irregular shape surcharge – Short'},
    ])
    assert surcharge[0]['name'] == documents.build_label(spec)
    assert surcharge[1]['name'] == f"Irregular shape surcharge – {documents.build_label(spec)}"


def test_inch_fractions_and_real_area_weight():
    assert documents.inch_fraction(49.25) == '49 1/4'
    assert documents.inch_fraction(77 + 23 / 32) == '77 23/32'
    assert documents.inch_fraction(24) == '24'
    wide, high = documents.dimension_lines(49.25, 12, 'in')
    assert '49 1/4' in wide
    assert '1250.95 mm' in wide
    assert '304.8 mm' in high
    triple = {
        'pcs': 1, 'real_sqf': 151.33, 'sqf': 200,
        'spec': {'glass1': '4mm Clear', 'glass2': '4mm Clear', 'glass3': '4mm Clear'},
    }
    assert abs(documents.line_weight(triple, 0.512) - (151.33 * 12 * 0.512)) < 0.001
    small = {
        'pcs': 3, 'sqf': 6, 'real_sqf': 2,
        'spec': {'glass1': 'Clear Glass 4mm', 'width': 24, 'height': 12, 'unit': 'in'},
    }
    assert documents.charged_sqf(small) == 18
    assert abs(documents.line_weight(small, 0.512) - (6 * 4 * 0.512)) < 0.001


def test_description_puts_the_production_id_on_its_own_components(monkeypatch):
    monkeypatch.setattr(documents, 'lookup_product', lambda spec, name='': {'product_id': '48'})
    spec = {
        'glass1': 'Solarban60 VT 4mm', 'temper1': 'yes',
        'spacer1': '18mm Super Spacer', 'sealant1': 'Polysulfide',
        'glass2': 'SolarCoat 4mm', 'temper2': 'yes',
    }
    text = documents.line_description({'spec': spec})
    assert text.startswith('26 (1") - 48 - ')
    assert '<br/>+' in text
    assert ' /' in text
    assert makeup_from_spec(spec)


def test_size_pdf_matches_the_production_layout_and_orders_use_it(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    created = client.post('/api/customers', json={
        'name': 'Harbor Glass', 'tier': 'T2', 'address': '10 Dock St',
        'city': 'Everett', 'state': 'WA', 'zip': '98201',
        'payment_terms': 'Net 30', 'country': 'United States',
        'minimum_sqf_per_piece': 6,
        'credit_limit': 100000, 'credit_approved': True,
    })
    assert created.status_code == 200, created.get_json()
    customer_id = created.get_json()['id']
    saved = client.post('/api/quotes', json={
        'items': [{
            'name': 'Small lite',
            'spec': {'glass1': 'Clear Glass 4mm', 'temper1': 'n/a', 'width': 24, 'height': 12, 'unit': 'in'},
            'sqf': 6, 'real_sqf': 2, 'minimum_sqf': 6,
            'price_sqf': 10, 'price_pc': 60, 'pcs': 3, 'total': 180,
            'floor': 'A',
        }],
        'currency': 'usd', 'project': 'Raising Cane', 'type': 'size', 'tier': 'T2',
        'customer_id': customer_id, 'notes': 'COD',
    })
    assert saved.status_code == 200, saved.get_json()
    quote_id = saved.get_json()['id']
    pdf = client.post('/api/export-quote', json={'id': quote_id})
    text = pdf_text(pdf.data)
    assert b'Quotation' in text
    assert f'Quotation:'.encode() in text or b'Quotation:' in text
    assert str(quote_id).encode() in text
    assert b'Harbor Glass' in text
    assert b'10 Dock St' in text
    assert b'Raising Cane' in text
    assert b'Remarks:' in text
    assert b'COD' in text
    assert b'Ora Glass Inc.' in text
    assert b'Canada' not in text
    assert b'GST#' not in text
    assert b'729631119' not in text
    from pathlib import Path
    Path('/opt/cursor/artifacts').mkdir(parents=True, exist_ok=True)
    Path('/opt/cursor/artifacts/usd-quote.pdf').write_bytes(pdf.data)
    assert b'QTY' in text
    assert b'WIDTH' in text
    assert b'HEIGHT' in text
    assert b'FLOOR' in text
    assert b'ITEM DESCRIPTION' in text
    assert b'US$/SQF' in text
    assert b'US$/PC' in text
    assert b'24' in text
    assert b'609.6 mm' in text
    assert b'12' in text
    assert b'304.8 mm' in text
    assert b'18.00' in text
    assert b'Total Pcs: 3' in text
    assert b'Total SQ/FT' in text
    assert b'Total Weight:' in text
    assert b'Appx.' in text
    assert b'Sub-Total:' in text
    assert b'US$ 180.00' in text
    cad = pdf_text(client.post('/api/export-quote', json={
        'items': [{
            'name': 'Wide', 'sqf': 10, 'price_sqf': 2, 'price_pc': 20, 'pcs': 1, 'total': 20,
        }],
        'currency': 'cad', 'project': 'North', 'type': 'size',
    }).data)
    assert b'C$/SQF' in cad
    assert b'C$ 20.00' in cad
    assert b'Sub-Total:' in cad
    assert b'- AS APPLICABLE' in cad
    assert b'Discount:' not in cad
    assert b'Discount:' not in text
    discounted = pdf_text(client.post('/api/export-quote', json={
        'items': [{
            'name': 'Wide', 'sqf': 10, 'price_sqf': 2, 'price_pc': 20, 'pcs': 1, 'total': 20,
        }],
        'currency': 'cad', 'project': 'Discounted', 'type': 'size',
        'additional_discount_percent': 10,
    }).data)
    assert b'Discount:' in discounted
    assert b'C$ 2.00' in discounted
    assert b'C$ 18.00' in discounted
    assert b'- AS APPLICABLE' in text
    assert b'Accept Signature:' in text
    assert b'Ora Glass Inc Terms And Conditions of Sale apply.' in text
    assert b'Quote By:' in text
    assert b'admin@oraglass.us' in text
    assert b'Net:' in text
    assert b'Net 30' in text
    assert b'Add-on' not in text
    assert b'price_addon' not in text
    assert b'Lynden, WA 98264,USA' in text
    # 3 pieces x 2 real sq ft x 4 mm x 0.512 lb.
    assert b'12.29' in text

    ordered = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': customer_id})
    assert ordered.status_code == 200, ordered.get_json()
    order_id = ordered.get_json()['order_id']
    confirmation = client.get(f'/api/orders/{order_id}/confirmation.pdf')
    assert confirmation.status_code == 200
    assert confirmation.mimetype == 'application/pdf'
    order_text = pdf_text(confirmation.data)
    assert b'Order Confirmation' in order_text
    assert b'Sub-Total:' in order_text
    assert b'Discount:' not in order_text
    assert b'- AS APPLICABLE' in order_text
    assert f'Order: {order_id}'.encode() in order_text or str(order_id).encode() in order_text
    assert b'Quotation' not in order_text
    assert b'18.00' in order_text
    assert b'Add-on' not in order_text
    page = client.get(f'/orders/{order_id}')
    assert b'confirmation.pdf' in page.data
    assert b'Print / Download PDF' in page.data
    assert load_orders()
    formula = client.get('/pricing?tab=formula')
    assert b'glass_lb_per_sqf_mm' in formula.data
    assert b'0.512' in formula.data


def test_charges_are_not_counted_as_pieces_and_the_id_column_is_blank():
    items = [
        {
            'name': 'Lite A', 'pcs': 4, 'sqf': 6, 'real_sqf': 6,
            'price_sqf': 1, 'price_pc': 6, 'total': 24, 'floor': 'W1', 'line_id': 'g1',
            'spec': {'glass1': 'Clear Glass 4mm', 'width': 24, 'height': 36, 'unit': 'in'},
        },
        {
            'kind': 'irregular_surcharge', 'name': 'Irregular shape surcharge – Lite A',
            'pcs': 4, 'price_pc': 50, 'total': 200,
        },
        {
            'name': 'Lite B', 'pcs': 10, 'sqf': 6, 'real_sqf': 6,
            'price_sqf': 1, 'price_pc': 6, 'total': 60, 'line_id': 'g2',
            'spec': {'glass1': 'Clear Glass 4mm', 'width': 20, 'height': 30, 'unit': 'in'},
        },
        {
            'kind': 'dwg_creation', 'name': 'DWG drawing creation',
            'pcs': 1, 'price_pc': 75, 'total': 75, 'drawing_for': 'g1',
        },
    ]
    payload = documents.build_pdf({
        'kind': 'order', 'id': 1, 'type': 'size', 'currency': 'usd', 'project': 'Order 1',
        'company': 'HighQ Developments Inc', 'date': '2026-11-18',
        'items': items, 'extra_charges': [{'label': 'Delivery', 'amount': 40}],
        'weight_factor': 0.512,
    })
    text = pdf_text(payload)
    assert b'Total Pcs: 14' in text
    assert b'Total SQ/FT : 84.00' in text
    assert b'(ID)' not in text
    assert b'\\(1\\)' not in text
    assert b'\\(4\\)' not in text
    assert b'Irregular shape surcharge +USD $200.00' in text
    assert b'DWG drawing creation +USD $75.00' in text
    assert b'Delivery' in text
    assert text.find(b'DWG drawing creation') < text.find(b'(2)')
    from pathlib import Path
    Path('/opt/cursor/artifacts').mkdir(parents=True, exist_ok=True)
    Path('/opt/cursor/artifacts/order-1-pieces.pdf').write_bytes(payload)


def test_table_header_repeats_when_the_quote_breaks_onto_another_page(client):
    write_user('admin@oraglass.us', 'admin')
    login(client, 'admin@oraglass.us')
    items = []
    for index in range(28):
        items.append({
            'name': f'Lite {index}',
            'spec': {
                'glass1': 'Clear Glass 4mm', 'temper1': 'yes',
                'spacer1': '12mm Super Spacer', 'sealant1': 'Polysulfide',
                'glass2': 'Clear Glass 4mm', 'temper2': 'n/a',
                'width': 49.25, 'height': 77.71875, 'unit': 'in',
            },
            'sqf': 26.4, 'real_sqf': 26.4, 'price_sqf': 8.5, 'price_pc': 224.4,
            'pcs': 2, 'total': 448.8, 'floor': 'A',
        })
    text = pdf_text(client.post('/api/export-quote', json={
        'items': items, 'currency': 'usd', 'project': 'Long', 'type': 'size',
    }).data)
    assert text.count(b'ITEM DESCRIPTION') >= 2
    assert b'49 1/4' in text
    assert b'77 23/32' in text
    assert b'US$/SQF' in text

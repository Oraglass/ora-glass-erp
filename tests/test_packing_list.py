"""Packing-list text is matched to order lines before a shipment is saved."""

import io

from reportlab.pdfgen import canvas

import packing
import production
from storage import load_invoices
from tests.test_shipments import _order, _ship


SAMPLE = """
Sales Delivery: XF260729003  Date: 7/29/2026
Job Name: 17.0055.VLD-Vlad Active Doors
Total Pcs : 3
Total SQ/FT : 80.61
RackCode 4001 L2100*920*2200
QTY WIDTH HEIGHT FLOOR # ITEM DESCRIPTION
2
53 15/16 96 11/32
W52 / 2
28 - 6mmSolarban60 VT Tempered +16mmSuperSpacer with
Argon(Polysulfide)+6mmSolarCoat Tempered
1370 mm 2447 mm
QTY WIDTH HEIGHT FLOOR # ITEM DESCRIPTION
1
40 1/2 30
D19 / 1
24 - 4mmClear Glass+16mmSuperSpacer with Argon(Polysulfide)+4mmClear Glass
1029 mm 762 mm
"""


def _pdf(text):
    buffer = io.BytesIO()
    sheet = canvas.Canvas(buffer)
    y = 760
    for line in text.splitlines():
        sheet.drawString(36, y, line)
        y -= 14
    sheet.save()
    return buffer.getvalue()


def test_parser_reads_the_packing_list_layout():
    parsed = packing.parse_packing_lines(SAMPLE.splitlines())
    assert parsed['number'] == 'XF260729003'
    assert parsed['date'] == '2026-07-29'
    assert parsed['job'] == '17.0055.VLD-Vlad Active Doors'
    assert parsed['rack'] == 'RackCode 4001 L2100*920*2200'
    assert parsed['total_pcs'] == 3
    assert parsed['parsed_pcs'] == 3
    assert parsed['warnings'] == []
    assert len(parsed['rows']) == 2
    first = parsed['rows'][0]
    assert first['qty'] == 2
    assert first['floor'] == 'W52 / 2'
    assert first['width_mm'] == 1370
    assert first['height_mm'] == 2447
    assert first['description'] == (
        '28 - 6mmSolarban60 VT Tempered +16mmSuperSpacer with '
        'Argon(Polysulfide)+6mmSolarCoat Tempered'
    )
    from_pdf = packing.parse_packing_pdf(_pdf(SAMPLE))
    assert from_pdf['number'] == 'XF260729003'
    assert from_pdf['rows'][0]['qty'] == 2
    assert from_pdf['rows'][0]['floor'] == 'W52 / 2'


def test_match_uses_floor_size_and_product_and_preview_does_not_save(client):
    order_id = _order(client, pcs=4)
    from storage import load_orders, save_orders
    orders = load_orders()
    order = next(row for row in orders if row['id'] == order_id)
    width_mm, height_mm, _width_in, _height_in = production.dimensions_mm_in(53 + 15 / 16, 96 + 11 / 32, 'in')
    order['items'][0]['spec'] = {
        'glass1': 'Solarban60 VT 6mm', 'temper1': 'yes',
        'spacer1': 'Superspacer 16mm / sqf', 'sealant1': 'Polysulfide',
        'glass2': 'SolarCoat 6mm', 'temper2': 'yes',
        'width': width_mm, 'height': height_mm, 'unit': 'mm',
    }
    order['items'][0]['mark'] = 'W52 / 2'
    order['items'][0]['pcs'] = 4
    order['items'].append({
        'name': 'Other',
        'pcs': 2,
        'sqf': 10,
        'price_sqf': 5,
        'price_pc': 50,
        'total': 100,
        'mark': 'D19 / 1',
        'spec': {'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'width': 1000, 'height': 1000, 'unit': 'mm'},
    })
    save_orders(orders)
    fresh = next(row for row in load_orders() if row['id'] == order_id)
    parsed = packing.parse_packing_lines(SAMPLE.splitlines())
    review = packing.match_packing(fresh, parsed)
    assert review['number'] == 'XF260729003'
    assert review['date'] == '2026-07-29'
    assert len(review['matched']) == 1
    assert review['matched'][0]['qty'] == 2
    assert review['matched'][0]['bo_after'] == 2
    assert review['lines'] == [{'index': 0, 'qty': 2}]
    assert len(review['unmatched']) == 1
    assert review['unmatched'][0]['floor'] == 'D19 / 1'
    swapped = dict(parsed['rows'][0], width_mm=parsed['rows'][0]['height_mm'], height_mm=parsed['rows'][0]['width_mm'])
    swapped_review = packing.match_packing(fresh, {'rows': [swapped], 'warnings': [], 'number': 'X', 'date': '', 'rack': ''})
    assert swapped_review['lines'] == [{'index': 0, 'qty': 2}]
    strict = dict(fresh)
    strict['items'] = [dict(fresh['items'][0], mark='W52 / 9')]
    assert packing.match_packing(strict, parsed)['lines'] == []

    preview = client.post(
        f'/api/orders/{order_id}/shipments/preview',
        data={'packing_list': (io.BytesIO(_pdf(SAMPLE)), 'packing.pdf')},
        content_type='multipart/form-data',
    )
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()['lines'] == [{'index': 0, 'qty': 2}]
    assert load_invoices() == []
    assert next(row for row in load_orders() if row['id'] == order_id).get('shipments') in (None, [])
    saved = _ship(client, order_id, 2, 'XF260729003')
    assert saved.status_code == 200, saved.get_json()
    open_rows = client.get('/api/shipments/open').get_json()
    listed = next(row for row in open_rows if row['id'] == order_id)
    bo = next(line for line in listed['lines'] if line['floor_code'] == 'W52 / 2')
    assert bo['bo_qty'] == 2

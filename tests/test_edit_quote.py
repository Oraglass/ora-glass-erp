"""A draft quote can be edited in place. The quote number stays, and the order uses the new lines."""

import io

from quoting import reprice_size_line
from storage import load_orders, load_quotes, load_tiers, save_quotes, save_tiers
from tests.conftest import login, write_user
from tests.test_irregular_shape import SPEC, _line


def _draft(client, items, project='Lobby', tier='T2'):
    saved = client.post('/api/quotes', json={
        'items': items, 'currency': 'usd', 'project': project, 'type': 'size', 'tier': tier,
    })
    assert saved.status_code == 200, saved.get_json()
    quotes = load_quotes()
    quote = next(row for row in quotes if row['id'] == saved.get_json()['id'])
    quote['status'] = 'draft'
    quote['items'][0]['floor'] = 'W12'
    quote['items'][0]['remarks'] = 'Lobby lite'
    quote['items'][0]['line_id'] = 'line-1'
    quote['items'][0]['source_filename'] = 'glass-order.pdf'
    quote['items'][0]['line_number'] = 1
    save_quotes(quotes)
    return quote


def test_editing_a_draft_keeps_the_quote_number_and_reprices_at_its_tier(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('sales@oraglass.us', 'sales')
    write_user('view@oraglass.us', 'viewer')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    original = _draft(client, [_line(pcs=2, irregular=False)])
    quote_id = original['id']
    old_total = next(row['total'] for row in original['items'] if row.get('spec'))
    tiers = load_tiers()
    tiers['T2']['glass_markup'] = tiers['T2']['glass_markup'] + 0.25
    save_tiers(tiers)
    changed = dict(SPEC)
    changed['glass1'] = 'Clear Glass 6mm'
    edited = client.post('/api/quotes', json={
        'id': quote_id,
        'items': [{
            'name': 'Clear Glass 6mm - 24 x 36 in',
            'pcs': 3,
            'spec': changed,
            'floor': 'W12',
            'remarks': 'Lobby lite',
            'line_id': 'line-1',
            'source_filename': 'glass-order.pdf',
            'line_number': 1,
            'irregular_shape': False,
        }],
        'currency': 'usd',
        'project': 'Lobby revised',
        'job_address': '10 Dock St',
        'type': 'size',
        'tier': 'T5',
    })
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()['id'] == quote_id
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    assert stored['status'] == 'draft'
    assert stored['project'] == 'Lobby revised'
    assert stored['job_address'] == '10 Dock St'
    assert stored['tier'] == 'T2'
    assert stored['edited_by'] == 'admin@oraglass.us'
    assert stored['edited_at']
    assert stored['revision'] == 1
    assert stored['revisions'][0]['user'] == 'admin@oraglass.us'
    assert stored['revisions'][0]['kind'] == 'edit'
    assert stored['revisions'][0]['number'] == 1
    glass = next(row for row in stored['items'] if row.get('spec'))
    assert glass['pcs'] == 3
    assert glass['spec']['glass1'] == 'Clear Glass 6mm'
    assert glass['floor'] == 'W12'
    assert glass['remarks'] == 'Lobby lite'
    assert glass['line_id'] == 'line-1'
    assert glass['source_filename'] == 'glass-order.pdf'
    expected = reprice_size_line(changed, 3, 'T2', 'usd', __import__('app').PRICING, load_tiers())
    assert glass['total'] == expected['total']
    assert glass['total'] != old_total
    listed = client.get('/api/quotes').get_json()
    row = next(item for item in listed if item['id'] == quote_id)
    assert row['can_edit'] is True
    assert row['edited_by'] == 'admin@oraglass.us'
    page = client.get('/quote-history')
    assert b'Edit quote' in page.data
    calculator = client.get(f'/quotes?edit={quote_id}')
    assert b'Save quote' in calculator.data
    assert b'Editing quote #' in calculator.data
    assert b'Duplicate line' in calculator.data
    assert b'class="igu-name"' in calculator.data
    assert b'class="line-actions"' in calculator.data
    assert b'Update line' in calculator.data
    assert b'openSizeIndex === i' in calculator.data
    assert b'aria-label="Quantity"' in calculator.data

    created = client.post(f'/api/quotes/{quote_id}/order', json={'customer_id': 'harbor-glass'})
    assert created.status_code == 200, created.get_json()
    order = next(item for item in load_orders() if item['id'] == created.get_json()['order_id'])
    ordered = next(item for item in order['items'] if item.get('spec'))
    assert ordered['pcs'] == 3
    assert ordered['spec']['glass1'] == 'Clear Glass 6mm'
    blocked = client.post('/api/quotes', json={
        'id': quote_id, 'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'Too late', 'type': 'size', 'tier': 'T2',
    })
    assert blocked.status_code == 400
    assert next(row for row in load_quotes() if row['id'] == quote_id)['project'] == 'Lobby revised'

    client.post('/logout')
    assert login(client, 'admin@oraglass.us').status_code == 302
    draft = _draft(client, [_line(pcs=1, irregular=False)], project='Still draft')
    client.post('/logout')
    assert login(client, 'view@oraglass.us').status_code == 302
    denied = client.post('/api/quotes', json={
        'id': draft['id'], 'items': [_line(pcs=4, irregular=False)],
        'currency': 'usd', 'project': 'Viewer edit', 'type': 'size', 'tier': 'T2',
    })
    assert denied.status_code == 400
    visible = client.get('/api/quotes').get_json()
    assert next(item for item in visible if item['id'] == draft['id'])['can_edit'] is False

    client.post('/logout')
    assert login(client, 'sales@oraglass.us').status_code == 302
    sales = client.post('/api/quotes', json={
        'id': draft['id'], 'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'Sales edit', 'type': 'size', 'tier': 'T2',
    })
    assert sales.status_code == 200, sales.get_json()
    assert sales.get_json()['id'] == draft['id']
    assert next(row for row in load_quotes() if row['id'] == draft['id'])['edited_by'] == 'sales@oraglass.us'

    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    assert client.post('/api/quotes', json={
        'id': draft['id'], 'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'Not mine', 'type': 'size', 'tier': 'T2',
    }).status_code == 400

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    own = client.post('/api/quotes', json={
        'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'Own', 'type': 'size', 'tier': 'T2',
    })
    own_id = own.get_json()['id']
    quotes = load_quotes()
    mine = next(row for row in quotes if row['id'] == own_id)
    mine['status'] = 'draft'
    save_quotes(quotes)
    customer_edit = client.post('/api/quotes', json={
        'id': own_id, 'items': [_line(pcs=2, irregular=False)],
        'currency': 'usd', 'project': 'Own edited', 'type': 'size', 'tier': 'T2',
    })
    assert customer_edit.status_code == 200, customer_edit.get_json()
    assert customer_edit.get_json()['id'] == own_id
    assert next(row for row in load_quotes() if row['id'] == own_id)['edited_by'] == 'boss@harbor.example'
    voided = load_quotes()
    row = next(item for item in voided if item['id'] == own_id)
    row['status'] = 'voided'
    save_quotes(voided)
    assert client.post('/api/quotes', json={
        'id': own_id, 'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'After void', 'type': 'size', 'tier': 'T2',
    }).status_code == 400


def test_a_duplicated_line_is_priced_on_its_own_and_needs_its_own_drawing(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    original = _draft(client, [_line(pcs=2, irregular=True)])
    quote_id = original['id']
    wider = dict(SPEC)
    wider['width'] = 30
    wider['height'] = 20
    saved = client.post('/api/quotes', json={
        'id': quote_id,
        'items': [
            {
                'name': 'Clear Glass 4mm - 24 x 36 in',
                'pcs': 2,
                'spec': SPEC,
                'floor': 'W12',
                'remarks': 'Lobby lite',
                'line_id': 'line-1',
                'line_number': 1,
                'irregular_shape': True,
                'source_filename': 'glass-order.pdf',
            },
            {
                'name': 'Clear Glass 4mm - 30 x 20 in',
                'pcs': 1,
                'spec': wider,
                'floor': 'W13',
                'remarks': 'Lobby lite',
                'line_number': 2,
                'irregular_shape': True,
                'source_filename': 'glass-order.pdf',
            },
        ],
        'currency': 'usd',
        'project': 'Lobby',
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['id'] == quote_id
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    glass = [row for row in stored['items'] if row.get('spec')]
    assert [row['line_number'] for row in glass] == [1, 2]
    assert glass[0]['line_id'] == 'line-1'
    assert glass[1]['line_id'] and glass[1]['line_id'] != 'line-1'
    assert glass[1]['floor'] == 'W13'
    assert glass[1]['remarks'] == 'Lobby lite'
    assert glass[1]['spec']['glass1'] == 'Clear Glass 4mm'
    assert glass[1]['spec']['width'] == 30
    pricing = __import__('app').PRICING
    tiers = load_tiers()
    assert glass[0]['total'] == reprice_size_line(SPEC, 2, 'T2', 'usd', pricing, tiers)['total']
    assert glass[1]['total'] == reprice_size_line(wider, 1, 'T2', 'usd', pricing, tiers)['total']
    assert glass[0]['total'] != glass[1]['total']
    page = client.get('/quotes')
    assert b'duplicateSizeItem' in page.data
    assert b'duplicateSqfItem' in page.data
    assert b'aria-label="Width"' in page.data
    missing = client.post(f'/api/quotes/{quote_id}/order', data={
        'customer_id': 'harbor-glass',
        'drawing_line-1': (io.BytesIO(b'AC1015'), 'lobby.dwg'),
    }, content_type='multipart/form-data')
    assert missing.status_code == 400
    body = missing.get_json()
    assert body['needs_drawings'] is True
    assert [row['line_id'] for row in body['lines']] == [glass[1]['line_id']]
    created = client.post(f'/api/quotes/{quote_id}/order', data={
        'customer_id': 'harbor-glass',
        'drawing_line-1': (io.BytesIO(b'AC1015'), 'lobby.dwg'),
        f"drawing_{glass[1]['line_id']}": (io.BytesIO(b'other'), 'copy.pdf'),
    }, content_type='multipart/form-data')
    assert created.status_code == 200, created.get_json()
    order = next(row for row in load_orders() if row['id'] == created.get_json()['order_id'])
    drawings = {row['line_id']: row for row in order['drawings']}
    assert set(drawings) == {'line-1', glass[1]['line_id']}
    assert drawings['line-1']['filename'] == 'lobby.dwg'
    assert drawings[glass[1]['line_id']]['filename'] == 'copy.pdf'


def test_changing_a_line_to_silicone_rebuilds_the_name_and_the_price(client):
    from quoting import suggest_sealant
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    sealants = [item['name'] for item in __import__('app').SEALANTS]
    spacers = [item['name'] for item in __import__('app').SPACERS]
    spacer = next(name for name in spacers if 'super' in name.lower() and '16' in name)
    choice = suggest_sealant(spacer, sealants)
    poly = choice['selected']
    silicone = next(name for name in choice['options'] if 'silicone' in name.lower())
    assert 'poly' in poly.lower()
    base = dict(SPEC)
    base.update({
        'glass1': 'Clear Glass 6mm', 'temper1': 'yes',
        'spacer1': spacer, 'sealant1': poly,
        'glass2': 'Clear Glass 6mm', 'temper2': 'yes',
    })
    original = _draft(client, [_line(pcs=1, irregular=False)])
    quote_id = original['id']
    saved = client.post('/api/quotes', json={
        'id': quote_id,
        'items': [{
            'name': 'old name', 'pcs': 1, 'spec': base, 'floor': 'W12', 'remarks': 'Seal',
            'line_number': 1, 'irregular_shape': False,
        }],
        'currency': 'usd', 'project': 'Sealant', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    stored_poly = next(row for row in load_quotes() if row['id'] == quote_id)
    poly_line = next(row for row in stored_poly['items'] if row.get('spec'))
    assert poly_line['spec']['sealant1'] == poly
    assert 'Polysulfide' in poly_line['name']
    assert 'Silicone' not in poly_line['name']
    switched = dict(base)
    switched['sealant1'] = silicone
    edited = client.post('/api/quotes', json={
        'id': quote_id,
        'items': [{
            'name': 'old name', 'pcs': 1, 'spec': switched, 'floor': 'W12', 'remarks': 'Seal',
            'line_number': 1, 'irregular_shape': False,
        }],
        'currency': 'usd', 'project': 'Sealant', 'type': 'size', 'tier': 'T5',
    })
    assert edited.status_code == 200, edited.get_json()
    stored_silicone = next(row for row in load_quotes() if row['id'] == quote_id)
    line = next(row for row in stored_silicone['items'] if row.get('spec'))
    assert line['spec']['sealant1'] == silicone
    assert 'Silicone' in line['name']
    assert 'Polysulfide' not in line['name']
    assert stored_silicone['tier'] == 'T2'
    expected = reprice_size_line(switched, 1, 'T2', 'usd', __import__('app').PRICING, load_tiers())
    assert line['total'] == expected['total']
    assert line['total'] != poly_line['total']


def _save_spec(client, quote_id, spec):
    saved = client.post('/api/quotes', json={
        'id': quote_id,
        'items': [{
            'name': 'stale name with Sealant - 14mm Polyurethane',
            'pcs': 1, 'spec': spec, 'floor': 'W12', 'remarks': 'Gap',
            'line_number': 3, 'irregular_shape': False,
        }],
        'currency': 'usd', 'project': 'Gap', 'type': 'size', 'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    stored = next(row for row in load_quotes() if row['id'] == quote_id)
    return next(row for row in stored['items'] if row.get('spec'))


def test_clearing_a_lite_or_spacer_clears_that_gap_and_reprices(client):
    """Lite 3 n/a makes a double. A spacer set to n/a drops its sealant. Choosing it again auto-selects."""
    from production import normalize_igu_spec
    from quoting import suggest_sealant
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    sealants = [item['name'] for item in __import__('app').SEALANTS]
    spacers = [item['name'] for item in __import__('app').SPACERS]
    spacer = next(name for name in spacers if 'super' in name.lower() and '14' in name)
    poly = suggest_sealant(spacer, sealants)['selected']
    assert 'poly' in poly.lower()
    triple = {
        'glass1': 'Clear Glass 6mm', 'temper1': 'n/a',
        'spacer1': spacer, 'sealant1': poly,
        'glass2': 'Clear Glass 6mm', 'temper2': 'n/a',
        'spacer2': spacer, 'sealant2': poly,
        'glass3': 'Clear Glass 4mm', 'temper3': 'n/a',
        'width': 30, 'height': 20, 'unit': 'in',
    }
    quote_id = _draft(client, [_line(pcs=1, irregular=False)])['id']
    pricing = __import__('app').PRICING
    tiers = load_tiers()
    triple_total = reprice_size_line(triple, 1, 'T2', 'usd', pricing, tiers)['total']

    stale_lite = dict(triple)
    stale_lite['glass3'] = 'n/a'
    line = _save_spec(client, quote_id, stale_lite)
    assert line['spec']['glass3'] == ''
    assert line['spec']['spacer2'] == ''
    assert line['spec']['sealant2'] == ''
    assert line['spec']['sealant1'] == poly
    assert line['name'].count('+') == 2
    assert line['name'].count('14mm') == 1
    assert 'Sealant' not in line['name']
    double_total = reprice_size_line(normalize_igu_spec(stale_lite), 1, 'T2', 'usd', pricing, tiers)['total']
    assert line['total'] == double_total
    assert line['total'] != triple_total

    stale_spacer = dict(triple)
    stale_spacer['spacer2'] = 'n/a'
    line = _save_spec(client, quote_id, stale_spacer)
    assert line['spec']['glass3'] == 'Clear Glass 4mm'
    assert line['spec']['spacer2'] == ''
    assert line['spec']['sealant2'] == ''
    assert line['name'].count('14mm') == 1
    assert line['name'].endswith('+4mmClear Glass')
    gap_total = reprice_size_line(normalize_igu_spec(stale_spacer), 1, 'T2', 'usd', pricing, tiers)['total']
    assert line['total'] == gap_total
    assert line['total'] != triple_total
    assert line['total'] != double_total

    single = dict(triple)
    single['glass2'] = ''
    line = _save_spec(client, quote_id, single)
    assert line['spec']['glass2'] == ''
    assert line['spec']['glass3'] == ''
    assert line['spec']['spacer1'] == ''
    assert line['spec']['sealant1'] == ''
    assert line['spec']['spacer2'] == ''
    assert line['spec']['sealant2'] == ''
    assert '+' not in line['name']
    assert '14mm' not in line['name']
    single_total = reprice_size_line(normalize_igu_spec(single), 1, 'T2', 'usd', pricing, tiers)['total']
    assert line['total'] == single_total
    assert line['total'] != double_total

    restored = dict(triple)
    restored['sealant2'] = ''
    line = _save_spec(client, quote_id, restored)
    assert line['spec']['spacer2'] == spacer
    assert line['spec']['sealant2'] == poly
    assert line['name'].count('+') == 4
    assert line['name'].count('14mm') == 2
    assert line['total'] == triple_total

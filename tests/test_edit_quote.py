"""A draft quote can be edited in place. The quote number stays, and the order uses the new lines."""

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

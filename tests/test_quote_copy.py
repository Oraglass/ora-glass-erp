"""Copying a quote makes a new draft at current prices and leaves the original alone."""

from datetime import date

from quoting import reprice_size_line
from storage import load_quotes, load_tiers, save_pricing_settings, save_quotes, save_tiers
from tests.conftest import login, write_user
from tests.test_irregular_shape import SPEC, _line


def _save(client, items, currency='usd', project='Lobby'):
    saved = client.post('/api/quotes', json={
        'items': items,
        'currency': currency,
        'project': project,
        'type': 'size',
        'tier': 'T2',
    })
    assert saved.status_code == 200, saved.get_json()
    return next(row for row in load_quotes() if row['id'] == saved.get_json()['id'])


def test_copy_is_a_new_draft_at_current_prices_and_the_original_stays(client):
    write_user('admin@oraglass.us', 'admin')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass', customer_name='Harbor Glass',
        customer_tier='T2', customer_subrole='management',
    )
    write_user(
        'other@maple.example', 'customer', customer_id='maple', customer_name='Maple Glass',
        customer_tier='T2', customer_subrole='management',
    )
    assert login(client, 'admin@oraglass.us').status_code == 302
    original = _save(client, [_line(pcs=2)], project='Lobby')
    quotes = load_quotes()
    original = next(row for row in quotes if row['id'] == original['id'])
    original['notes'] = 'Call before noon'
    original['extra_charges'] = [{'label': 'Delivery', 'amount': 25}]
    original['order_id'] = 9
    original['items'].append({
        'kind': 'dwg_creation', 'name': 'DWG drawing creation', 'pcs': 1, 'total': 50,
        'line_id': 'abc', 'drawing_for': 'abc',
    })
    save_quotes(quotes)
    original_total = next(row['total'] for row in original['items'] if row.get('kind') == 'irregular_surcharge')
    save_pricing_settings({'irregular_shape_surcharge': 80})
    tiers = load_tiers()
    tiers['T2']['glass_markup'] = tiers['T2']['glass_markup'] + 0.25
    save_tiers(tiers)

    copied = client.post(f"/api/quotes/{original['id']}/copy", json={})
    assert copied.status_code == 200, copied.get_json()
    body = copied.get_json()
    assert body['copy_note'] == f"Copied from quote #{original['id']}"
    assert body['id'] != original['id']
    fresh = next(row for row in load_quotes() if row['id'] == body['id'])
    untouched = next(row for row in load_quotes() if row['id'] == original['id'])
    assert fresh['status'] == 'draft'
    assert fresh['date'] == date.today().isoformat()
    assert fresh.get('order_id') is None
    assert fresh['customer_id'] is None
    assert fresh['company'] == untouched['company']
    assert fresh['currency'] == 'usd'
    assert fresh['project'] == 'Lobby'
    assert fresh['copy_note'] == f"Copied from quote #{original['id']}"
    assert 'Call before noon' in fresh['notes']
    assert fresh['extra_charges'] == [{'label': 'Delivery', 'amount': 25}]
    assert not any(row.get('kind') == 'dwg_creation' for row in fresh['items'])
    assert untouched['order_id'] == 9
    assert next(row['total'] for row in untouched['items'] if row.get('kind') == 'irregular_surcharge') == original_total
    surcharge = next(row for row in fresh['items'] if row.get('kind') == 'irregular_surcharge')
    assert surcharge['unit_price'] == 80
    glass = next(row for row in fresh['items'] if row.get('spec'))
    expected = reprice_size_line(SPEC, 2, 'T2', 'usd', __import__('app').PRICING, load_tiers())
    assert glass['total'] == expected['total']
    assert glass['total'] != next(row['total'] for row in untouched['items'] if row.get('spec'))
    assert client.get(f"/api/quotes/{fresh['id']}").status_code == 200
    history = client.get('/quote-history')
    assert b'Copy quote' in history.data
    assert b'Details' in history.data

    client.post('/logout')
    assert login(client, 'other@maple.example').status_code == 302
    assert client.post(f"/api/quotes/{original['id']}/copy", json={}).status_code == 404

    client.post('/logout')
    assert login(client, 'boss@harbor.example').status_code == 302
    own = client.post('/api/quotes', json={
        'items': [_line(pcs=1, irregular=False)],
        'currency': 'usd', 'project': 'Own', 'type': 'size', 'tier': 'T2',
    })
    assert own.status_code == 200, own.get_json()
    own_id = own.get_json()['id']
    customer_copy = client.post(f'/api/quotes/{own_id}/copy', json={})
    assert customer_copy.status_code == 200, customer_copy.get_json()
    stored = next(row for row in load_quotes() if row['id'] == customer_copy.get_json()['id'])
    assert stored['customer_id'] == 'harbor-glass'
    assert stored['company'] == 'Harbor Glass'
    assert stored['status'] == 'draft'
    assert client.post(f"/api/quotes/{original['id']}/copy", json={}).status_code == 404

    edited = client.post('/api/quotes', json={
        'id': stored['id'],
        'items': [_line(pcs=3, irregular=False)],
        'currency': 'usd',
        'project': 'Own revised',
        'type': 'size',
        'tier': 'T2',
    })
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()['id'] == stored['id']
    revised = next(row for row in load_quotes() if row['id'] == stored['id'])
    assert revised['project'] == 'Own revised'
    assert revised['items'][0]['pcs'] == 3
    assert revised['status'] == 'draft'
    assert sum(1 for row in load_quotes() if row.get('copied_from') == own_id) == 1
    blocked = client.post('/api/quotes', json={
        'id': own_id,
        'items': [_line(pcs=4, irregular=False)],
        'currency': 'usd', 'project': 'Overwrite', 'type': 'size', 'tier': 'T2',
    })
    assert blocked.status_code == 400
    assert next(row for row in load_quotes() if row['id'] == own_id)['project'] == 'Own'

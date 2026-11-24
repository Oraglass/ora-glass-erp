"""Quote history shows the same quote number the PDF prints."""

from storage import append_quote, load_quotes, save_quotes
from tests.conftest import login, write_user
from tests.test_quote_export import pdf_text


def test_missing_quote_numbers_are_backfilled_from_the_id_and_stay_put(client):
    write_user('admin@oraglass.us', 'admin')
    save_quotes([
        {'id': 4, 'user': 'admin@oraglass.us', 'project': 'Lobby glass', 'date': '2026-03-01',
         'items': [{'name': 'Clear', 'total': 10}], 'currency': 'usd', 'type': 'sqf', 'status': 'draft'},
        {'user': 'admin@oraglass.us', 'project': 'No number yet', 'date': '2026-03-02',
         'items': [{'name': 'Clear', 'total': 12}], 'currency': 'usd', 'type': 'sqf', 'status': 'draft'},
    ])
    first = load_quotes()
    assert first[0]['number'] == 4
    assert first[1]['id'] == 5
    assert first[1]['number'] == 5
    second = load_quotes()
    assert [(row['id'], row['number']) for row in second] == [(4, 4), (5, 5)]
    created = append_quote({
        'user': 'admin@oraglass.us', 'project': 'Added later', 'date': '2026-03-03',
        'items': [], 'currency': 'usd', 'type': 'sqf', 'status': 'draft',
    })
    assert created['id'] == 6
    assert created['number'] == 6

    assert login(client, 'admin@oraglass.us').status_code == 302
    page = client.get('/quote-history')
    html = page.data
    assert html.find(b'<th>Quote #</th>') < html.find(b'<th>Date</th>')
    assert b'Project name or quote #' in html
    rows = client.get('/api/quotes').get_json()
    by_id = {row['id']: row for row in rows}
    assert by_id[4]['number'] == 4
    assert by_id[5]['number'] == 5
    by_number = client.get('/api/quotes?project=5').get_json()
    assert [row['id'] for row in by_number] == [5]
    by_name = client.get('/api/quotes?project=lobby').get_json()
    assert [row['id'] for row in by_name] == [4]
    pdf = pdf_text(client.post('/api/export-quote', json={'id': 4}).data)
    assert b'Quotation:' in pdf
    assert b'4' in pdf.split(b'Quotation:')[1][:40]

"""Financial overview is for accounts with financial access, and a customer sees only their company."""

from storage import load_users, save_invoices, save_users
from tests.conftest import login, write_user


def _nav(page):
    html = page.get_data(as_text=True)
    if 'aria-label="Modules"' not in html:
        return ''
    return html.split('aria-label="Modules"', 1)[1].split('</nav>', 1)[0]


def _modules(email, modules):
    users = load_users()
    users[email]['modules'] = modules
    save_users(users)


def test_staff_overview_starts_empty_and_a_customer_is_locked_to_their_company(client):
    write_user('admin@oraglass.us', 'admin')
    write_user('finance@oraglass.us', 'employee')
    write_user('shop@oraglass.us', 'employee')
    write_user(
        'boss@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass',
    )
    write_user(
        'clerk@harbor.example', 'customer', customer_id='harbor-glass',
        customer_name='Harbor Glass',
    )
    write_user('ap@other.example', 'customer', customer_id='other-co', customer_name='Other Co')
    _modules('finance@oraglass.us', ['dashboard', 'financial'])
    _modules('shop@oraglass.us', ['dashboard', 'orders'])
    _modules('boss@harbor.example', ['dashboard', 'financial', 'invoices'])
    _modules('clerk@harbor.example', ['dashboard', 'quotes', 'invoices'])
    save_invoices([
        {
            'id': 1, 'number': 'INV-HARBOR', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'harbor-glass', 'company': 'Harbor Glass', 'currency': 'usd',
            'total': 40, 'balance': 40, 'invoice_date': '2026-11-02', 'voided': False,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 40}],
        },
        {
            'id': 2, 'number': 'INV-OTHER', 'doc_type': 'invoice', 'kind': 'customer', 'issuer': 'us',
            'customer_id': 'other-co', 'company': 'Other Co', 'currency': 'usd',
            'total': 90, 'balance': 90, 'invoice_date': '2026-11-03', 'voided': False,
            'items': [{'name': 'Glass', 'pcs': 1, 'total': 90}],
        },
    ])

    assert login(client, 'admin@oraglass.us').status_code == 302
    overview = client.get('/financial')
    assert overview.status_code == 200
    page = overview.get_data(as_text=True)
    assert 'id="fin-customer"' in page
    assert 'Choose a customer' in page
    assert 'id="fin-empty"' in page
    assert 'id="fin-body" hidden' in page
    assert 'load(rows[0].id)' not in page
    assert '>Financials<' in _nav(overview)
    assert '>Invoices<' not in _nav(overview)
    assert client.get('/api/financial').status_code == 400
    chosen = client.get('/api/financial?customer_id=other-co').get_json()
    assert chosen['credit']['customer_id'] == 'other-co'
    assert [row['number'] for row in chosen['invoices']] == ['INV-OTHER']
    client.post('/logout')

    assert login(client, 'finance@oraglass.us').status_code == 302
    staff = client.get('/financial')
    assert staff.status_code == 200
    assert 'id="fin-customer"' in staff.get_data(as_text=True)
    assert '>Financials<' in _nav(staff)
    client.post('/logout')

    assert login(client, 'shop@oraglass.us').status_code == 302
    home = client.get('/dashboard')
    assert '>Financials<' not in _nav(home)
    assert b'href="/financial"' not in home.data
    assert client.get('/financial').status_code != 200
    assert client.get('/api/financial?customer_id=harbor-glass').status_code == 403
    client.post('/logout')

    assert login(client, 'boss@harbor.example').status_code == 302
    own_page = client.get('/financial')
    assert own_page.status_code == 200
    own_html = own_page.get_data(as_text=True)
    assert 'id="fin-customer"' not in own_html
    assert 'Harbor Glass' in own_html
    assert '>Financials<' in _nav(own_page)
    assert '>Invoices<' not in _nav(own_page)
    assert '>Invoices<' in own_html
    assert '>Record payment<' in own_html
    own = client.get('/api/financial?customer_id=other-co')
    assert own.status_code == 403
    listed = client.get('/api/financial').get_json()
    assert listed['credit']['customer_id'] == 'harbor-glass'
    assert [row['number'] for row in listed['invoices']] == ['INV-HARBOR']
    client.post('/logout')

    assert login(client, 'clerk@harbor.example').status_code == 302
    clerk_home = client.get('/dashboard')
    assert '>Financials<' not in _nav(clerk_home)
    assert b'href="/financial"' not in clerk_home.data
    assert '>Invoices<' in _nav(clerk_home)
    assert client.get('/financial').status_code != 200
    assert client.get('/api/financial').status_code == 403
    assert client.get('/api/financial?customer_id=harbor-glass').status_code == 403
    assert client.get('/invoices').status_code == 200

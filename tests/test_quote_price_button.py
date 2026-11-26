"""Both quote calculators price a CAD company and a USD company."""

from storage import load_customers, save_customers
from tests.conftest import login, write_user

GLASS = {'glass1': 'Clear Glass 6mm', 'temper1': 'n/a', 'tier': 'T2'}
SIZE = {
    'glass1': 'Clear Glass 6mm', 'temper1': 'n/a',
    'glass2': '', 'glass3': '',
    'temper2': 'n/a', 'temper3': 'n/a',
    'spacer1': '', 'spacer2': '', 'sealant1': '', 'sealant2': '',
    'tier': 'T2', 'fx_rate': 1,
}


def _company(email, customer_id, name, country, province=''):
    write_user(email, 'customer', customer_id=customer_id, customer_name=name, customer_subrole='management')
    customers = load_customers()
    customers[customer_id].update({'country': country, 'province': province, 'name': name})
    save_customers(customers)


def _price(client, payload):
    response = client.post('/api/calculate', json=payload)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body['price_sqf'] > 0
    assert 'fx_rate' not in body
    return body['price_sqf']


def test_cad_and_usd_customers_get_a_price_on_both_calculators(client):
    _company('cad@maple.example', 'maple-glass', 'Maple Glass', 'Canada', 'BC')
    _company('usd@harbor.example', 'harbor-glass', 'Harbor Glass', 'United States', 'WA')
    login(client, 'cad@maple.example')
    cad_page = client.get('/quotes')
    assert b'const lockedCurrency = "cad"' in cad_page.data
    assert b'selectedCurrency = lockedCurrency' in cad_page.data
    assert b'currency !== lockedCurrency' in cad_page.data
    cad_sqf = _price(client, GLASS)
    cad_size = _price(client, SIZE)
    assert cad_sqf == cad_size
    client.post('/logout')

    login(client, 'usd@harbor.example')
    usd_page = client.get('/quotes')
    assert b'const lockedCurrency = "usd"' in usd_page.data
    assert b'selectedCurrency = lockedCurrency' in usd_page.data
    usd_sqf = _price(client, GLASS)
    usd_size = _price(client, SIZE)
    assert usd_sqf == usd_size
    assert cad_sqf > usd_sqf

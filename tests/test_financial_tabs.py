"""Financials holds Overview, Invoices, and Record payment. An invoice opens over the list."""

from tests.conftest import login, write_user


def _nav(page):
    html = page.get_data(as_text=True)
    start = html.split('aria-label="Modules"', 1)[1]
    return start.split('</nav>', 1)[0]


def test_invoices_leave_the_top_nav_and_the_financial_tabs_split_the_work(client):
    write_user('admin@oraglass.us', 'admin')
    assert login(client, 'admin@oraglass.us').status_code == 302
    overview = client.get('/financial')
    assert overview.status_code == 200
    nav = _nav(overview)
    assert '>Invoices<' not in nav
    assert '>Financials<' in nav
    assert 'href="/financial"' in nav
    body = overview.get_data(as_text=True)
    assert 'aria-label="Financial"' in body
    assert '>Overview<' in body
    assert 'href="/invoices"' in body
    assert 'href="/financial/payments"' in body
    assert 'class="active"' in body
    invoices = client.get('/invoices')
    text = invoices.get_data(as_text=True)
    assert 'id="invoice-panel"' in text
    assert 'id="invoice-list"' in text
    assert 'Create invoice' in text
    assert 'Create credit note' in text
    assert 'id="invoice-modal"' in text
    assert 'aria-label="Close"' in text
    assert 'id="invoice-detail"' in text
    assert text.index('id="invoice-modal"') < text.index('id="invoice-detail"')
    assert 'data-invoice=' in text
    assert "event.key === 'Escape'" in text
    assert 'id="payment-screen" hidden' in text or 'id="payment-screen" hidden' in text.replace('  ', ' ')
    payments = client.get('/financial/payments')
    pay = payments.get_data(as_text=True)
    assert 'Record payment' in pay
    assert 'id="payment-form"' in pay
    assert 'Send unsynced payments to QuickBooks' in pay
    assert 'id="open-invoice-list"' in pay
    assert 'Order prepayment' in pay
    assert 'id="alloc-order"' in pay
    assert 'id="invoice-panel" hidden' in pay
    detail = client.get('/invoices/12')
    assert detail.status_code == 200
    assert 'const FOCUS = 12' in detail.get_data(as_text=True)
    client.post('/logout')
    write_user('pat@oraglass.us', 'sales')
    assert login(client, 'pat@oraglass.us').status_code == 302
    sales = client.get('/invoices')
    sales_nav = _nav(sales)
    assert '>Financials<' not in sales_nav
    assert 'href="/financial"' not in sales_nav
    assert '>Invoices<' in sales_nav
    sales_body = sales.get_data(as_text=True)
    assert '>Overview<' not in sales_body
    assert '>Invoices<' in sales_body
    assert client.get('/financial').status_code != 200

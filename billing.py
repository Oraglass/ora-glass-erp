"""Order totals, credit, and payment allocation. No Flask imports."""

import re

TERM_DAYS = {
    'Due on receipt': 0,
    'Net 15': 15,
    'Net 30': 30,
    'Net 45': 45,
    'Net 60': 60,
}
PAYMENT_METHODS = ('cheque', 'e-transfer', 'wire', 'card')
CURRENCIES = ('usd', 'cad')
STATUS_LABELS = {
    'approved': 'Approved',
    'sent': 'Sent to production',
    'made': 'Made',
    'delivered': 'Delivered',
}
NEXT_STATUS = {
    'approved': 'sent',
    'sent': 'made',
    'made': 'delivered',
}
# Orders in these states use up credit until an invoice takes over.
OPEN_ORDER_STATUSES = ('sent', 'made', 'delivered')


def money(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def payment_term_days(customer):
    customer = customer or {}
    label = customer.get('payment_terms') or 'Net 30'
    if label == 'Custom':
        try:
            days = int(customer.get('payment_terms_days') or 0)
        except (TypeError, ValueError):
            return 30
        return max(0, min(days, 365))
    if label in TERM_DAYS:
        return TERM_DAYS[label]
    if label in {'COD', 'Prepaid'}:
        return 0
    match = re.fullmatch(r'Net\s+(\d+)', str(label))
    if match:
        return max(0, min(int(match.group(1)), 365))
    return 30


def apply_payment_terms(customer, data):
    """Store payment terms. Custom needs a day count from 0 to 365."""
    label = str((data or {}).get('payment_terms') or 'Net 30').replace('\x00', '').strip()[:40]
    if label in {'COD', 'Prepaid'}:
        label = 'Due on receipt'
    if label in TERM_DAYS:
        customer['payment_terms'] = label
        customer['payment_terms_days'] = TERM_DAYS[label]
        return None
    if label != 'Custom':
        return 'Unknown payment terms.'
    raw = (data or {}).get('payment_terms_days')
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 'Custom payment terms need a number of days.'
    if days < 0 or days > 365:
        return 'Payment terms must be between 0 and 365 days.'
    customer['payment_terms'] = 'Custom'
    customer['payment_terms_days'] = days
    return None


def apply_credit_limit(customer, data):
    raw_limit = (data or {}).get('credit_limit', customer.get('credit_limit', 0))
    try:
        limit = money(raw_limit or 0)
    except (TypeError, ValueError):
        return 'Credit limit must be a number.'
    if limit < 0:
        return 'Credit limit cannot be negative.'
    currency = str((data or {}).get('credit_currency') or customer.get('credit_currency') or 'usd').strip().lower()[:8]
    if currency not in CURRENCIES:
        return 'Credit currency must be USD or CAD.'
    customer['credit_limit'] = limit
    customer['credit_currency'] = currency
    return None


def order_status(order):
    value = str((order or {}).get('status') or 'approved').lower()
    return value if value in STATUS_LABELS else 'approved'


def discount_amount(subtotal, discount):
    kind = (discount or {}).get('type') or 'percent'
    value = money((discount or {}).get('value') or 0)
    if kind == 'percent':
        return round(subtotal * value / 100.0, 2)
    return value


def price_summary(items, discount, charges):
    subtotal = round(sum(money(item.get('total')) for item in items or []), 2)
    kind = (discount or {}).get('type') or 'percent'
    value = money((discount or {}).get('value') or 0)
    if kind not in {'percent', 'amount'}:
        return None, 'Discount must be a percent or a fixed amount.'
    if value < 0:
        return None, 'Discount cannot be negative.'
    if kind == 'percent' and value > 100:
        return None, 'Discount percent cannot exceed 100.'
    amount = discount_amount(subtotal, {'type': kind, 'value': value})
    if amount - subtotal > 0.001:
        return None, 'Discount cannot exceed the IGU subtotal.'
    igu_total = round(subtotal - amount, 2)
    extras = round(sum(money(charge.get('amount')) for charge in charges or []), 2)
    return {
        'igu_subtotal': subtotal,
        'discount': {'type': kind, 'value': value},
        'discount_amount': amount,
        'igu_total': igu_total,
        'extra_charges': list(charges or []),
        'extras_total': extras,
        'total': round(igu_total + extras, 2),
    }, None


def order_currency(order):
    return str((order or {}).get('currency') or 'usd').lower()


def payment_is_open(payment):
    return bool(payment) and not payment.get('voided')


def allocation_amount(payment, target_type=None, target_id=None):
    if not payment_is_open(payment):
        return 0.0
    total = 0.0
    for row in payment.get('allocations') or []:
        if row.get('voided'):
            continue
        if target_type and row.get('target_type') != target_type:
            continue
        if target_id is not None and row.get('target_id') != target_id:
            continue
        total += money(row.get('amount'))
    return round(total, 2)


def unallocated_amount(payment):
    if not payment_is_open(payment):
        return 0.0
    return round(money(payment.get('amount')) - allocation_amount(payment), 2)


def customer_payments(payments, customer_id, currency=None):
    rows = []
    for payment in payments or []:
        if payment.get('customer_id') != customer_id:
            continue
        if currency and str(payment.get('currency') or 'usd').lower() != currency:
            continue
        rows.append(payment)
    return rows


def allocated_to_target(payments, customer_id, currency, target_type, target_id):
    total = 0.0
    for payment in customer_payments(payments, customer_id, currency):
        total += allocation_amount(payment, target_type, target_id)
    return round(total, 2)


def unallocated_credit(payments, customer_id, currency):
    total = 0.0
    for payment in customer_payments(payments, customer_id, currency):
        total += unallocated_amount(payment)
    return round(total, 2)


def invoice_applied(invoice, payments):
    return allocated_to_target(
        payments,
        invoice.get('customer_id'),
        str(invoice.get('currency') or 'usd').lower(),
        'invoice',
        invoice.get('id'),
    )


def document_type(row):
    value = str((row or {}).get('doc_type') or 'invoice')
    return value if value in {'invoice', 'credit_note'} else 'invoice'


def invoice_balance(invoice, payments):
    if (invoice or {}).get('voided') or document_type(invoice) == 'credit_note':
        return 0.0
    return round(money(invoice.get('total')) - invoice_applied(invoice, payments), 2)


def invoice_status(invoice, payments):
    if (invoice or {}).get('voided'):
        return 'void'
    applied = invoice_applied(invoice, payments)
    balance = round(money(invoice.get('total')) - applied, 2)
    if balance <= 0.009:
        return 'paid'
    if applied > 0.009:
        return 'partial'
    return 'unpaid'


def invoice_status_label(status):
    return {
        'paid': 'Paid',
        'partial': 'Partially paid',
        'unpaid': 'Unpaid',
        'void': 'Void',
        'open': 'Unapplied',
        'applied': 'Applied',
    }.get(status, 'Unpaid')


def credit_note_applied(note):
    total = 0.0
    for row in (note or {}).get('applications') or []:
        if row.get('voided'):
            continue
        total += money(row.get('amount'))
    return round(total, 2)


def unapplied_credits(invoices, customer_id, currency):
    """Credit notes not yet applied to an invoice. They increase available credit."""
    total = 0.0
    for note in invoices or []:
        if document_type(note) != 'credit_note' or note.get('voided'):
            continue
        if note.get('customer_id') != customer_id:
            continue
        if str(note.get('currency') or 'usd').lower() != currency:
            continue
        remaining = round(money(note.get('total')) - credit_note_applied(note), 2)
        if remaining > 0:
            total += remaining
    return round(total, 2)


def open_receivables(customer_id, currency, orders, invoices, payments, exclude_order_id=None):
    """Unpaid invoice balances plus uncovered in-production orders, in one currency."""
    total = 0.0
    invoiced_orders = set()
    for invoice in invoices or []:
        if document_type(invoice) != 'invoice' or invoice.get('voided'):
            continue
        if invoice.get('customer_id') != customer_id:
            continue
        if str(invoice.get('currency') or 'usd').lower() != currency:
            continue
        if invoice.get('order_id') and invoice.get('source') != 'manual':
            invoiced_orders.add(invoice.get('order_id'))
        balance = invoice_balance(invoice, payments)
        if balance > 0:
            total += balance
    for order in orders or []:
        if order.get('customer_id') != customer_id:
            continue
        if order.get('id') == exclude_order_id:
            continue
        if order_currency(order) != currency:
            continue
        if order.get('invoice_id') or order.get('id') in invoiced_orders:
            continue
        if order_status(order) not in OPEN_ORDER_STATUSES:
            continue
        covered = allocated_to_target(payments, customer_id, currency, 'order', order.get('id'))
        uncovered = round(money(order.get('total')) - covered, 2)
        if uncovered > 0:
            total += uncovered
    return round(total, 2)


def available_credit(customer, currency, orders, invoices, payments, exclude_order_id=None):
    customer = customer or {}
    limit_currency = str(customer.get('credit_currency') or 'usd').lower()
    limit = money(customer.get('credit_limit') or 0) if limit_currency == currency else 0.0
    receivables = open_receivables(
        customer.get('id'), currency, orders, invoices, payments, exclude_order_id=exclude_order_id,
    )
    credits = unapplied_credits(invoices, customer.get('id'), currency)
    return round(limit - receivables + credits, 2), limit, receivables


def coverage(order, customer, orders, invoices, payments):
    """How an order total is covered by credit and prepayments allocated to it."""
    currency = order_currency(order)
    total = money(order.get('total'))
    customer_id = order.get('customer_id')
    available, limit, receivables = available_credit(
        customer, currency, orders, invoices, payments, exclude_order_id=order.get('id'),
    )
    prepay = allocated_to_target(payments, customer_id, currency, 'order', order.get('id'))
    # Once the prepayment has moved onto the invoice, still show it as cover for a delivered order.
    if order.get('invoice_id'):
        prepay = round(prepay + allocated_to_target(
            payments, customer_id, currency, 'invoice', order.get('invoice_id'),
        ), 2)
    covered_by_prepayment = round(min(total, max(prepay, 0)), 2)
    rest = round(total - covered_by_prepayment, 2)
    covered_by_credit = round(min(rest, max(available, 0)), 2)
    shortfall = round(rest - covered_by_credit, 2)
    if shortfall < 0:
        shortfall = 0.0
    unallocated = unallocated_credit(payments, customer_id, currency)
    return {
        'currency': currency,
        'order_total': total,
        'credit_limit': limit,
        'credit_currency': str((customer or {}).get('credit_currency') or 'usd').lower(),
        'open_receivables': receivables,
        'available_credit': available,
        'unallocated_payments': unallocated,
        'prepayments': prepay,
        'covered_by_credit': covered_by_credit,
        'covered_by_prepayment': covered_by_prepayment,
        'shortfall': shortfall,
        'can_send': shortfall <= 0.009,
    }


def conversion_allowed(total, customer, currency, orders, invoices, payments):
    """Management may convert only when credit plus unallocated payments covers the new total."""
    available, limit, receivables = available_credit(customer, currency, orders, invoices, payments)
    unallocated = unallocated_credit(payments, (customer or {}).get('id'), currency)
    room = round(available + unallocated, 2)
    return {
        'allowed': money(total) <= room + 0.009,
        'order_total': money(total),
        'available_credit': available,
        'unallocated_payments': unallocated,
        'credit_limit': limit,
        'open_receivables': receivables,
        'currency': currency,
        'room': room,
    }


def credit_message(check):
    currency = str(check.get('currency') or 'usd').upper()
    return (
        f"This order total of {currency} ${check['order_total']:.2f} exceeds available credit of "
        f"{currency} ${check['room']:.2f} "
        f"(credit limit ${check['credit_limit']:.2f}, open receivables ${check['open_receivables']:.2f}, "
        f"unallocated payments ${check['unallocated_payments']:.2f})."
    )


def line_snapshot(item):
    spec = item.get('spec') if isinstance(item.get('spec'), dict) else {}
    width = spec.get('width')
    height = spec.get('height')
    return {
        'name': item.get('name') or '',
        'pcs': item.get('pcs'),
        'sqf': money(item.get('sqf')),
        'price_sqf': money(item.get('price_sqf')),
        'total': money(item.get('total')),
        'width': width,
        'height': height,
        'unit': spec.get('unit') or '',
        'glass1': spec.get('glass1') or '',
    }


def diff_changes(before, after):
    changes = []
    if (before.get('project') or '') != (after.get('project') or ''):
        changes.append({'field': 'project', 'old': before.get('project') or '', 'new': after.get('project') or ''})
    if (before.get('notes') or '') != (after.get('notes') or ''):
        changes.append({'field': 'notes', 'old': before.get('notes') or '', 'new': after.get('notes') or ''})
    old_items = before.get('items') or []
    new_items = after.get('items') or []
    shared = min(len(old_items), len(new_items))
    for index in range(shared):
        old_snap = line_snapshot(old_items[index])
        new_snap = line_snapshot(new_items[index])
        if old_snap != new_snap:
            changes.append({'field': 'line', 'action': 'changed', 'old': old_snap, 'new': new_snap})
    for item in old_items[shared:]:
        changes.append({'field': 'line', 'action': 'removed', 'old': line_snapshot(item)})
    for item in new_items[shared:]:
        changes.append({'field': 'line', 'action': 'added', 'new': line_snapshot(item)})
    old_discount = before.get('discount') or {'type': 'percent', 'value': 0}
    new_discount = after.get('discount') or {'type': 'percent', 'value': 0}
    old_discount_view = {'type': old_discount.get('type') or 'percent', 'value': money(old_discount.get('value'))}
    new_discount_view = {'type': new_discount.get('type') or 'percent', 'value': money(new_discount.get('value'))}
    if old_discount_view != new_discount_view:
        changes.append({'field': 'discount', 'old': old_discount_view, 'new': new_discount_view})
    if money(before.get('discount_amount')) != money(after.get('discount_amount')) and old_discount_view == new_discount_view:
        changes.append({
            'field': 'discount_amount',
            'old': money(before.get('discount_amount')),
            'new': money(after.get('discount_amount')),
        })
    old_charges = before.get('extra_charges') or []
    new_charges = after.get('extra_charges') or []
    shared_charges = min(len(old_charges), len(new_charges))
    for index in range(shared_charges):
        old_charge = {'label': old_charges[index].get('label') or '', 'amount': money(old_charges[index].get('amount'))}
        new_charge = {'label': new_charges[index].get('label') or '', 'amount': money(new_charges[index].get('amount'))}
        if old_charge != new_charge:
            changes.append({'field': 'extra_charge', 'action': 'changed', 'old': old_charge, 'new': new_charge})
    for charge in old_charges[shared_charges:]:
        changes.append({
            'field': 'extra_charge',
            'action': 'removed',
            'old': {'label': charge.get('label') or '', 'amount': money(charge.get('amount'))},
        })
    for charge in new_charges[shared_charges:]:
        changes.append({
            'field': 'extra_charge',
            'action': 'added',
            'new': {'label': charge.get('label') or '', 'amount': money(charge.get('amount'))},
        })
    if order_status(before) != order_status(after):
        changes.append({
            'field': 'status',
            'old': STATUS_LABELS[order_status(before)],
            'new': STATUS_LABELS[order_status(after)],
        })
    if money(before.get('total')) != money(after.get('total')):
        changes.append({'field': 'total', 'old': money(before.get('total')), 'new': money(after.get('total'))})
    return changes

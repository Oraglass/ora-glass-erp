"""First-run records for Open Windows. No passwords are stored here."""

import logging

from security import attach_invite
from storage import load_customers, load_users, save_customers, save_users

logger = logging.getLogger('ora_glass.seed')

OPEN_WINDOWS_ID = 'open-windows'

# Company default is T2. Alex, Kiu, Vlad, and support each have a per-user T5 override.
SEED_REVISION = 3
SEED_CUSTOMER = {
    'id': OPEN_WINDOWS_ID,
    'name': 'Open Windows',
    'domain': 'open-windows.ca',
    'address': '',
    'city': '',
    'state': '',
    'zip': '',
    'phone': '',
    'email': '',
    'credit_approved': False,
    'payment_terms': 'Net 30',
    'tier': 'T2',
    'status': 'active',
    'seed_revision': SEED_REVISION,
}

SEED_USERS = [
    {
        'email': 'alex@open-windows.ca',
        'name': 'Alex',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': 'T5',
    },
    {
        'email': 'kiu@open-windows.ca',
        'name': 'Kiu',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': 'T5',
    },
    {
        'email': 'vlad@open-windows.ca',
        'name': 'Vlad',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': 'T5',
    },
    {
        'email': 'support@open-windows.ca',
        'name': 'Open Windows Support',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': 'T5',
    },
]


def new_user(email, name, role, customer_id=None, tier_override=None, password_hash=None, must_change_password=False):
    return {
        'email': email,
        'name': name,
        'role': role,
        'customer_id': customer_id or None,
        'tier_override': tier_override or None,
        'password_hash': password_hash,
        'must_change_password': bool(must_change_password),
        'active': True,
        'invite_token_hash': None,
        'invite_expires': None,
    }


def apply_seed(issue_links=False):
    """Create missing Open Windows records. Never assigns a password.

    When issue_links is true, users who still have no password get a new one-time
    token. Raw tokens are returned to the caller and are not written to disk.
    """
    customers = load_customers()
    customers_changed = False
    if OPEN_WINDOWS_ID not in customers:
        customers[OPEN_WINDOWS_ID] = dict(SEED_CUSTOMER)
        customers_changed = True
        logger.info('Seeded customer Open Windows')
    for customer in customers.values():
        if customer.get('tier') == 'EU':
            customer['tier'] = 'T5'
            customers_changed = True
    open_windows = customers.get(OPEN_WINDOWS_ID)
    migrate_seed_tiers = bool(open_windows) and int(open_windows.get('seed_revision') or 0) < SEED_REVISION
    if migrate_seed_tiers:
        open_windows['tier'] = 'T2'
        open_windows['seed_revision'] = SEED_REVISION
        customers_changed = True
        logger.info('Set Open Windows company tier to T2')
    if customers_changed:
        save_customers(customers)

    users = load_users()
    tokens = {}
    changed = False
    old_alex = 'alex@oraglass.us'
    new_alex = 'alex@open-windows.ca'
    if old_alex in users and new_alex not in users:
        moved = users.pop(old_alex)
        moved['email'] = new_alex
        users[new_alex] = moved
        changed = True
        logger.info('Renamed seeded user %s to %s', old_alex, new_alex)
    elif old_alex in users and not users[old_alex].get('password_hash'):
        del users[old_alex]
        changed = True
    for user in users.values():
        if user.get('tier_override') == 'EU':
            user['tier_override'] = 'T5'
            changed = True
    if migrate_seed_tiers:
        for spec in SEED_USERS:
            existing = users.get(spec['email'])
            if not existing:
                continue
            existing['tier_override'] = spec['tier_override']
            existing['customer_id'] = OPEN_WINDOWS_ID
            existing['role'] = 'customer'
            changed = True
    for spec in SEED_USERS:
        email = spec['email']
        if email not in users:
            users[email] = new_user(
                email=email,
                name=spec['name'],
                role=spec['role'],
                customer_id=spec['customer_id'],
                tier_override=spec['tier_override'],
            )
            changed = True
            logger.info('Seeded user %s without a password', email)
        if issue_links and not users[email].get('password_hash'):
            tokens[email] = attach_invite(users[email])
            changed = True
    if changed:
        save_users(users)
    return tokens

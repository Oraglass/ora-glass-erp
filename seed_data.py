"""First-run records for Open Windows. No passwords are stored here."""

import logging

from security import attach_invite
from storage import load_customers, load_users, save_customers, save_users

logger = logging.getLogger('ora_glass.seed')

OPEN_WINDOWS_ID = 'open-windows'

# Company default tier is End User. alex@oraglass.us is seeded as a customer user of
# Open Windows with a T2 override. The mailbox is on oraglass.us, so the owner should
# confirm whether Alex should instead be an Ora Glass staff account.
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
    'tier': 'EU',
    'status': 'active',
}

SEED_USERS = [
    {
        'email': 'alex@oraglass.us',
        'name': 'Alex',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': 'T2',
    },
    {
        'email': 'kiu@open-windows.ca',
        'name': 'Kiu',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': None,
    },
    {
        'email': 'vlad@open-windows.ca',
        'name': 'Vlad',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': None,
    },
    {
        'email': 'support@open-windows.ca',
        'name': 'Open Windows Support',
        'role': 'customer',
        'customer_id': OPEN_WINDOWS_ID,
        'tier_override': None,
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
    if OPEN_WINDOWS_ID not in customers:
        customers[OPEN_WINDOWS_ID] = dict(SEED_CUSTOMER)
        save_customers(customers)
        logger.info('Seeded customer Open Windows')

    users = load_users()
    tokens = {}
    changed = False
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

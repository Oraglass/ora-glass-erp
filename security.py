"""Password hashing, invite tokens, and login rate limiting."""

import hashlib
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from hmac import compare_digest

from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger('ora_glass.security')

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
INVITE_DAYS = 7
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_dummy_hash = None
_dummy_lock = threading.Lock()


def is_development():
    """True for local dev, tests, and the Flask CLI. Production must opt in via APP_ENV."""
    env = os.environ.get('APP_ENV', os.environ.get('FLASK_ENV', '')).strip().lower()
    if env in {'production', 'prod'}:
        return False
    if env in {'development', 'dev', 'test', 'testing'}:
        return True
    if os.environ.get('RENDER', '').strip().lower() in {'true', '1', 'yes'}:
        return False
    argv0 = os.path.basename(sys.argv[0]) if sys.argv else ''
    return argv0 in {'app.py', 'flask'}


def resolve_secret_key():
    """Return the session signing key. Production has no hardcoded fallback."""
    key = os.environ.get('SECRET_KEY', '').strip()
    if key:
        if len(key) < 32 and not is_development():
            raise RuntimeError('SECRET_KEY must be at least 32 characters')
        if len(key) < 32:
            logger.warning('SECRET_KEY is shorter than 32 characters')
        return key
    if is_development():
        logger.warning('SECRET_KEY is not set; using an ephemeral development key')
        return secrets.token_hex(32)
    raise RuntimeError('SECRET_KEY environment variable is required')


def session_cookie_secure():
    explicit = os.environ.get('SESSION_COOKIE_SECURE', '').strip().lower()
    if explicit in {'1', 'true', 'yes'}:
        return True
    if explicit in {'0', 'false', 'no'}:
        return False
    return not is_development()


def trust_proxy():
    explicit = os.environ.get('TRUST_PROXY', '').strip().lower()
    if explicit in {'1', 'true', 'yes'}:
        return True
    if explicit in {'0', 'false', 'no'}:
        return False
    return not is_development()


def normalize_email(value):
    return str(value or '').strip().lower()


def valid_email(value):
    email = normalize_email(value)
    return bool(email) and len(email) <= 254 and EMAIL_RE.fullmatch(email) is not None


def _dummy_password_hash():
    global _dummy_hash
    if _dummy_hash is None:
        with _dummy_lock:
            if _dummy_hash is None:
                _dummy_hash = generate_password_hash('not-a-real-password')
    return _dummy_hash


def hash_password(password):
    if not isinstance(password, str) or not password:
        raise ValueError('Password is required')
    return generate_password_hash(password)


def verify_password(password_hash, password):
    """Check a salted hash. Missing and legacy unsalted digests never succeed."""
    candidate = password if isinstance(password, str) else ''
    if not password_hash or not isinstance(password_hash, str):
        check_password_hash(_dummy_password_hash(), candidate or 'invalid')
        return False
    try:
        return bool(check_password_hash(password_hash, candidate))
    except (TypeError, ValueError):
        check_password_hash(_dummy_password_hash(), candidate or 'invalid')
        return False


def password_is_strong(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return False, f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, 'Password is too long.'
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        return False, 'Password must include a letter and a number.'
    blocked = {
        'admin' + '123',
        'sales' + '123',
        'prod' + '123',
        'customer' + '123',
        'password' + '123',
        'changeme' + '123',
    }
    if password.lower() in blocked:
        return False, 'That password is not allowed.'
    return True, ''


def new_invite_token():
    return secrets.token_urlsafe(32)


def hash_invite_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def attach_invite(user, days=INVITE_DAYS):
    """Store only a hash of a new one-time token. Returns the raw token."""
    token = new_invite_token()
    user['invite_token_hash'] = hash_invite_token(token)
    user['invite_expires'] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return token


def invite_matches(user, token, now=None):
    stored = (user or {}).get('invite_token_hash') or ''
    if not token or not stored:
        return False
    digest = hash_invite_token(token)
    if len(stored) != len(digest) or not compare_digest(stored, digest):
        return False
    expires_raw = user.get('invite_expires')
    if not expires_raw:
        return False
    try:
        expires = datetime.fromisoformat(expires_raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return expires >= current


class LoginRateLimiter:
    """In-process failure window. Use a single gunicorn worker so attempts are shared."""

    def __init__(self, max_email=5, max_ip=30, window_seconds=15 * 60):
        self.max_email = max_email
        self.max_ip = max_ip
        self.window_seconds = window_seconds
        self._events = {'email': {}, 'ip': {}}
        self._lock = threading.Lock()

    def _prune(self, bucket, key, now):
        kept = [stamp for stamp in bucket.get(key, []) if now - stamp < self.window_seconds]
        if kept:
            bucket[key] = kept
        else:
            bucket.pop(key, None)
        return kept

    def _cap(self, bucket):
        if len(bucket) > 2000:
            bucket.clear()

    def is_limited(self, email, ip):
        now = time.monotonic()
        with self._lock:
            email_hits = self._prune(self._events['email'], email or '', now)
            ip_hits = self._prune(self._events['ip'], ip or '', now)
            return len(email_hits) >= self.max_email or len(ip_hits) >= self.max_ip

    def record_failure(self, email, ip):
        now = time.monotonic()
        with self._lock:
            self._events['email'].setdefault(email or '', []).append(now)
            self._events['ip'].setdefault(ip or '', []).append(now)
            self._cap(self._events['email'])
            self._cap(self._events['ip'])

    def clear(self, email, ip):
        with self._lock:
            self._events['email'].pop(email or '', None)
            self._events['ip'].pop(ip or '', None)

    def reset(self):
        with self._lock:
            self._events = {'email': {}, 'ip': {}}


login_limiter = LoginRateLimiter()

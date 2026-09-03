"""Users, sessions and single-use tokens. All database access for accounts.

Security choices worth knowing:
  * Passwords use Werkzeug's scrypt default (memory-hard, ships with Flask, so
    no extra dependency).
  * Session and email tokens are random 256-bit values; only their SHA-256 is
    stored, so a database dump yields nothing usable.
  * Token consumption is a single conditional UPDATE, so it cannot race.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from . import config, db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True)
class User:
    id: int
    email: str
    email_verified: bool
    name: str | None
    has_password: bool
    access_until: datetime | None
    is_admin: bool
    status: str

    @property
    def has_access(self) -> bool:
        """Admins bypass the paywall; everyone else needs unexpired access."""
        if self.status != "active":
            return False
        if self.is_admin:
            return True
        return self.access_until is not None and self.access_until > utcnow()


_USER_FIELDS = ("id", "email", "email_verified", "name", "password_hash IS NOT NULL",
                "access_until", "is_admin", "status")
_USER_COLS = ", ".join(_USER_FIELDS)
# Qualified form, for queries that join another table with an `id` column.
_USER_COLS_U = ", ".join(f"u.{f}" for f in _USER_FIELDS)


def _row_to_user(r) -> User:
    return User(id=r[0], email=r[1], email_verified=r[2], name=r[3],
                has_password=r[4], access_until=r[5], is_admin=r[6], status=r[7])


# ── Users ─────────────────────────────────────────────────────────────────── #

def get_user(user_id: int) -> User | None:
    with db.connection() as conn:
        r = conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id = %s", (user_id,)).fetchone()
    return _row_to_user(r) if r else None


def get_user_by_email(email: str) -> User | None:
    with db.connection() as conn:
        r = conn.execute(f"SELECT {_USER_COLS} FROM users WHERE lower(email) = lower(%s)",
                         (email,)).fetchone()
    return _row_to_user(r) if r else None


def create_user(email: str, password: str | None = None, name: str | None = None,
                email_verified: bool = False) -> User:
    pw_hash = generate_password_hash(password) if password else None
    with db.connection() as conn:
        r = conn.execute(
            "INSERT INTO users (email, password_hash, name, email_verified)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (email.strip(), pw_hash, name, email_verified)).fetchone()
    return get_user(r[0])


def verify_password(email: str, password: str) -> User | None:
    """Returns the user on a correct password, else None. An account with no
    password (OAuth-only) can never match."""
    with db.connection() as conn:
        r = conn.execute("SELECT id, password_hash FROM users WHERE lower(email) = lower(%s)",
                         (email,)).fetchone()
    if not r or not r[1]:
        # Hash anyway so a missing account isn't distinguishable by timing.
        generate_password_hash(password)
        return None
    if not check_password_hash(r[1], password):
        return None
    return get_user(r[0])


def set_password(user_id: int, password: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                     (generate_password_hash(password), user_id))


def clear_password(user_id: int) -> None:
    """Drop a password so only OAuth (or a reset, which proves mailbox control)
    can sign in. Used against account pre-hijacking — see oauth.py."""
    with db.connection() as conn:
        conn.execute("UPDATE users SET password_hash = NULL WHERE id = %s", (user_id,))


def mark_verified(user_id: int) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE users SET email_verified = true WHERE id = %s", (user_id,))


def touch_login(user_id: int) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))


def grant_access(user_id: int, days: int, cap_days: int = 365) -> datetime | None:
    """Extend access. Stacks onto unexpired access rather than resetting it, so
    paying early never costs the customer days.

    `cap_days` guards the payment path: a webhook retry loop must not be able to
    grant years. Admin grants pass a larger cap deliberately, because a
    comped account is a decision someone made, not an accident.
    """
    with db.connection() as conn:
        r = conn.execute(
            "UPDATE users SET access_until = LEAST("
            "  GREATEST(COALESCE(access_until, now()), now()) + make_interval(days => %s),"
            "  now() + make_interval(days => %s))"
            " WHERE id = %s RETURNING access_until",
            (days, cap_days, user_id)).fetchone()
    return r[0] if r else None


# ── Sessions ──────────────────────────────────────────────────────────────── #

def create_session(user_id: int, ip: str | None = None, user_agent: str | None = None
                   ) -> tuple[str, str]:
    """Returns (session_token, csrf_token). Only hashes are persisted."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(days=config.SESSION_DAYS)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at, ip, user_agent)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (_hash(token), user_id, csrf, expires, ip, (user_agent or "")[:500]))
    return token, csrf


def lookup_session(token: str) -> tuple[User, str] | None:
    """Returns (user, csrf_token) for a live session, else None."""
    if not token:
        return None
    with db.connection() as conn:
        r = conn.execute(
            f"SELECT s.csrf_token, {_USER_COLS_U}"
            " FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = %s AND s.expires_at > now()",
            (_hash(token),)).fetchone()
    if not r:
        return None
    return _row_to_user(r[1:]), r[0]


def touch_session(token: str) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = now() WHERE token_hash = %s",
                     (_hash(token),))


def revoke_session(token: str) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def revoke_all_sessions(user_id: int) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def purge_expired_sessions() -> int:
    with db.connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        return cur.rowcount


# ── Single-use tokens ─────────────────────────────────────────────────────── #

def issue_token(user_id: int, purpose: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, purpose, expires_at)"
            " VALUES (%s, %s, %s, %s)",
            (_hash(token), user_id, purpose, utcnow() + ttl))
    return token


def consume_token(token: str, purpose: str) -> int | None:
    """Atomically spend a token. Returns the user_id, or None if it is unknown,
    expired, or already used. The conditional UPDATE means two concurrent
    requests cannot both succeed."""
    if not token:
        return None
    with db.connection() as conn:
        r = conn.execute(
            "UPDATE auth_tokens SET used_at = now()"
            " WHERE token_hash = %s AND purpose = %s"
            "   AND used_at IS NULL AND expires_at > now()"
            " RETURNING user_id",
            (_hash(token), purpose)).fetchone()
    return r[0] if r else None


def invalidate_tokens(user_id: int, purpose: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "UPDATE auth_tokens SET used_at = now()"
            " WHERE user_id = %s AND purpose = %s AND used_at IS NULL",
            (user_id, purpose))


# ── OAuth identities ──────────────────────────────────────────────────────── #

def get_user_by_oauth(provider: str, subject: str) -> User | None:
    with db.connection() as conn:
        r = conn.execute(
            f"SELECT {_USER_COLS_U}"
            " FROM oauth_identities o JOIN users u ON u.id = o.user_id"
            " WHERE o.provider = %s AND o.subject = %s",
            (provider, subject)).fetchone()
    return _row_to_user(r) if r else None


def link_oauth(user_id: int, provider: str, subject: str, email: str | None) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO oauth_identities (user_id, provider, subject, email_at_link)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (provider, subject) DO NOTHING",
            (user_id, provider, subject, email))

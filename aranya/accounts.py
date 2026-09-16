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


token_hash = _hash


def device_label(user_agent: str | None) -> str:
    """'Chrome on Android' — enough for a person to recognise their own
    device, without a user-agent parsing dependency. Order matters: Edge and
    Opera announce themselves as Chrome too, and Chrome as Safari."""
    ua = user_agent or ""
    browser = "Browser"
    for needle, name in (("Edg/", "Edge"), ("OPR/", "Opera"),
                         ("SamsungBrowser", "Samsung Internet"),
                         ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
                         ("CriOS", "Chrome"), ("Chrome/", "Chrome"),
                         ("Safari/", "Safari")):
        if needle in ua:
            browser = name
            break
    system = None
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"),
                         ("Android", "Android"), ("Windows", "Windows"),
                         ("Mac OS X", "Mac"), ("CrOS", "ChromeOS"),
                         ("Linux", "Linux")):
        if needle in ua:
            system = name
            break
    return f"{browser} on {system}" if system else browser


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


# The one definition of "add days to access": stacks onto unexpired access
# rather than resetting it, so paying early never costs the customer days, and
# never runs further ahead than the cap. Parameters: (days, cap_days).
EXTEND_ACCESS_SQL = (
    "LEAST(GREATEST(COALESCE(access_until, now()), now()) + make_interval(days => %s),"
    " now() + make_interval(days => %s))")


def grant_access(user_id: int, days: int,
                 cap_days: int = config.PAID_ACCESS_CAP_DAYS) -> datetime | None:
    """Extend access.

    `cap_days` guards the payment path: a webhook retry loop must not be able to
    grant years. Admin grants pass a larger cap deliberately, because a
    comped account is a decision someone made, not an accident.
    """
    with db.connection() as conn:
        r = conn.execute(
            f"UPDATE users SET access_until = {EXTEND_ACCESS_SQL}"
            " WHERE id = %s RETURNING access_until",
            (days, cap_days, user_id)).fetchone()
    return r[0] if r else None


def admin_emails() -> list[str]:
    """Where operational notices go: every active admin account."""
    with db.connection() as conn:
        rows = conn.execute("SELECT email FROM users WHERE is_admin AND status = 'active'"
                            " ORDER BY id").fetchall()
    return [r[0] for r in rows]


# ── Sessions ──────────────────────────────────────────────────────────────── #

def create_session(user_id: int, ip: str | None = None, user_agent: str | None = None,
                   max_sessions: int | None = None) -> tuple[str, str, list[bytes]]:
    """Returns (session_token, csrf_token, ended_hashes). Only hashes are persisted.

    With `max_sessions`, the newest sign-in always succeeds and the least
    recently used sessions beyond the limit are ended in the same transaction
    ("last device wins"). Refusing the new sign-in instead would lock a
    customer out of their own account whenever they forgot to sign out of a
    borrowed laptop, and would be no harder to share around.

    The user row is locked first, so two sign-ins racing each other can't both
    count the same survivors and leave three sessions standing.
    """
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(days=config.SESSION_DAYS)
    ua = (user_agent or "")[:500]
    ended: list[bytes] = []
    with db.connection() as conn:
        if max_sessions:
            conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
            rows = conn.execute(
                "SELECT token_hash, user_agent FROM sessions"
                " WHERE user_id = %s AND expires_at > now()"
                " ORDER BY last_seen_at DESC, created_at DESC",
                (user_id,)).fetchall()
            by = device_label(ua)
            for th, old_ua in rows[max(0, max_sessions - 1):]:
                th = bytes(th)
                conn.execute("DELETE FROM sessions WHERE token_hash = %s", (th,))
                conn.execute(
                    "INSERT INTO session_revocations"
                    " (token_hash, user_id, reason, ended_device, by_device, by_ip)"
                    " VALUES (%s, %s, 'device_limit', %s, %s, %s)",
                    (th, user_id, device_label(old_ua), by, ip))
                ended.append(th)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at, ip, user_agent)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (_hash(token), user_id, csrf, expires, ip, ua))
    return token, csrf, ended


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


def touch_sessions(seen: dict[bytes, float]) -> None:
    """Write buffered last-seen times (token_hash -> unix time) in one query.
    Never moves a time backwards, so a late flush can't undo a newer one."""
    if not seen:
        return
    hashes = list(seen.keys())
    times = [seen[h] for h in hashes]
    with db.connection() as conn:
        conn.execute(
            "UPDATE sessions s SET last_seen_at = to_timestamp(v.t)"
            " FROM unnest(%s::bytea[], %s::float8[]) AS v(h, t)"
            " WHERE s.token_hash = v.h AND s.last_seen_at < to_timestamp(v.t)",
            (hashes, times))


def list_sessions(user_id: int) -> list[dict]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT token_hash, created_at, last_seen_at, user_agent FROM sessions"
            " WHERE user_id = %s AND expires_at > now()"
            " ORDER BY last_seen_at DESC, created_at DESC", (user_id,)).fetchall()
    return [{"hash": bytes(r[0]), "created": r[1], "last_seen": r[2],
             "device": device_label(r[3])} for r in rows]


def revoke_session(token: str) -> None:
    """A voluntary sign-out. Not recorded: the device ended its own session."""
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def revoke_sessions(user_id: int, reason: str, only: list[bytes] | None = None,
                    keep: bytes | None = None, by_device: str | None = None,
                    by_ip: str | None = None) -> list[bytes]:
    """End some or all of a user's sessions on their behalf, recording why, so
    the device that was signed out can be told. Returns the hashes ended.

    `only` restricts to those sessions; `keep` spares one (the device doing
    the signing-out)."""
    with db.connection() as conn:
        rows = conn.execute(
            "DELETE FROM sessions WHERE user_id = %s"
            "   AND (%s::bytea[] IS NULL OR token_hash = ANY(%s::bytea[]))"
            "   AND (%s::bytea IS NULL OR token_hash <> %s::bytea)"
            " RETURNING token_hash, user_agent",
            (user_id, only, only, keep, keep)).fetchall()
        for th, ua in rows:
            conn.execute(
                "INSERT INTO session_revocations"
                " (token_hash, user_id, reason, ended_device, by_device, by_ip)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (bytes(th), user_id, reason, device_label(ua), by_device, by_ip))
    return [bytes(r[0]) for r in rows]


def revoke_all_sessions(user_id: int, reason: str = "password_reset") -> list[bytes]:
    return revoke_sessions(user_id, reason)


def find_revocation(th: bytes) -> dict | None:
    """Why this (now missing) session was ended, if it was ended for the user."""
    with db.connection() as conn:
        r = conn.execute(
            "SELECT reason, revoked_at, by_device FROM session_revocations"
            " WHERE token_hash = %s ORDER BY revoked_at DESC LIMIT 1", (th,)).fetchone()
    if not r:
        return None
    return {"reason": r[0], "at": r[1].isoformat(), "by": r[2]}


def purge_old_revocations(days: int = 90) -> int:
    with db.connection() as conn:
        cur = conn.execute("DELETE FROM session_revocations"
                           " WHERE revoked_at < now() - make_interval(days => %s)", (days,))
        return cur.rowcount


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

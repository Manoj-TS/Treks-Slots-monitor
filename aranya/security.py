"""Request-level auth: session resolution, CSRF, and the access decorators.

One before_request hook resolves the session once and puts it on `g`; the
decorators then only inspect `g` and never touch the database. Session lookups
are cached in-process briefly so a burst of requests from one page load costs a
single query.
"""

import hmac
import threading
import time
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from . import accounts, config, storage

# token -> (user, csrf_token, cached_at). Short TTL: long enough to absorb a
# page load's worth of requests, short enough that a revocation takes effect
# almost immediately. Logout evicts its own entry outright.
_SESSION_CACHE: dict[str, tuple] = {}
_CACHE_TTL = 60.0
_CACHE_MAX = 2000

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT_PREFIXES = ("/webhooks/",)


def _cache_get(token: str):
    hit = _SESSION_CACHE.get(token)
    if not hit:
        return None
    user, csrf, cached_at = hit
    if time.time() - cached_at > _CACHE_TTL:
        _SESSION_CACHE.pop(token, None)
        return None
    return user, csrf


def _cache_put(token: str, user, csrf: str) -> None:
    if len(_SESSION_CACHE) > _CACHE_MAX:
        _SESSION_CACHE.clear()
    _SESSION_CACHE[token] = (user, csrf, time.time())


def evict(token: str) -> None:
    _SESSION_CACHE.pop(token, None)


def evict_user(user_id: int) -> None:
    """Drop every cached session for one user, so their next request re-reads
    the account (access granted or revoked). Does NOT sign anyone out — see
    revoke_tokens for that."""
    for tok in [t for t, (u, _, _) in _SESSION_CACHE.items() if u.id == user_id]:
        _SESSION_CACHE.pop(tok, None)


# ── Revocation, as seen by long-lived streams ─────────────────────────────── #
#
# Deleting a session row stops the *next* request. An SSE stream authenticated
# once, possibly an hour ago, and never makes another; without this it would
# keep serving the board to a device that was signed out. So every forced
# sign-out is also noted here, and the stream checks it on each wakeup: a dict
# lookup, no database (streams must never hold a pooled connection).
#
# In-process is enough because the app is one process (see the Dockerfile's
# note on waitress). Entries outlive the longest possible stream, then go.

_revoked_tokens: dict[bytes, tuple[str, float]] = {}   # hash -> (reason, at)
_revoked_lock = threading.Lock()
_REVOKED_TTL = config.MAX_STREAM_SECONDS + 300


def revoke_tokens(hashes, reason: str) -> None:
    """Note sessions just deleted from the database, by hash. Every path that
    ends a session for someone knows exactly which ones it ended, so this is
    precise: "sign out my other devices" leaves this device's stream alone."""
    now = time.time()
    hashes = set(hashes)
    if not hashes:
        return
    with _revoked_lock:
        for h in hashes:
            _revoked_tokens[h] = (reason, now)
    for tok in [t for t in list(_SESSION_CACHE) if accounts.token_hash(t) in hashes]:
        _SESSION_CACHE.pop(tok, None)


def stream_revoked(th: bytes) -> str | None:
    """Why the stream for this session must end, or None."""
    with _revoked_lock:
        hit = _revoked_tokens.get(th)
    return hit[0] if hit else None


def _prune_revoked() -> None:
    cutoff = time.time() - _REVOKED_TTL
    with _revoked_lock:
        for h in [h for h, (_, at) in _revoked_tokens.items() if at < cutoff]:
            del _revoked_tokens[h]


# ── Last seen ─────────────────────────────────────────────────────────────── #
#
# The device limit ends the *least recently used* session, so "used" has to be
# true. Writing on every request would be a database write per click; instead
# times are buffered here and flushed once a minute. Streams feed this too, so
# a board left open on a laptop counts as in use.

_seen: dict[bytes, float] = {}
_seen_lock = threading.Lock()


def note_seen(th: bytes) -> None:
    with _seen_lock:
        _seen[th] = time.time()


def flush_seen() -> None:
    with _seen_lock:
        batch = dict(_seen)
        _seen.clear()
    if not batch:
        return
    try:
        accounts.touch_sessions(batch)
    except Exception as e:
        print(f"[Auth] last-seen flush failed: {e}")
        with _seen_lock:
            for h, t in batch.items():
                _seen[h] = max(t, _seen.get(h, 0.0))


def session_maintenance_loop() -> None:
    last_purge = 0.0
    while True:
        time.sleep(config.SESSION_TOUCH_SECONDS)
        try:
            _prune_revoked()
            if not storage.db_ready():
                continue
            flush_seen()
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                accounts.purge_expired_sessions()
                accounts.purge_old_revocations()
        except Exception as e:
            print(f"[Auth] session maintenance: {e}")


_maint_started = False


def start_maintenance() -> None:
    global _maint_started
    if _maint_started:
        return
    _maint_started = True
    threading.Thread(target=session_maintenance_loop, daemon=True,
                     name="sessions").start()


# ── Per request ───────────────────────────────────────────────────────────── #

def load_session() -> None:
    """before_request: populate g.user / g.csrf_token. Never raises.

    When the cookie names a session that no longer exists, also sets
    g.signed_out (why, if it was ended for the user) and asks for the dead
    cookie to be cleared, so it isn't looked up again on every request."""
    g.user = None
    g.session_token = None
    g.csrf_token = None
    g.signed_out = None
    g.clear_session_cookie = False

    if not storage.db_ready():
        return

    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return
    th = accounts.token_hash(token)

    with _revoked_lock:
        hit = _revoked_tokens.get(th)
    if hit:
        _SESSION_CACHE.pop(token, None)
    else:
        cached = _cache_get(token)
        if cached:
            g.user, g.csrf_token = cached
            g.session_token = token
            note_seen(th)
            return

    try:
        found = accounts.lookup_session(token)
    except Exception as e:
        print(f"[Auth] session lookup failed: {e}")
        return
    if found:
        user, csrf = found
        _cache_put(token, user, csrf)
        g.user, g.csrf_token, g.session_token = user, csrf, token
        note_seen(th)
        return

    g.clear_session_cookie = True
    try:
        g.signed_out = accounts.find_revocation(th) or {"reason": "expired"}
    except Exception as e:
        print(f"[Auth] revocation lookup failed: {e}")
        g.signed_out = {"reason": "expired"}


def clear_dead_cookie(response):
    """after_request: drop a cookie whose session is gone."""
    if getattr(g, "clear_session_cookie", False):
        response.delete_cookie(config.SESSION_COOKIE, path="/")
        response.delete_cookie(config.CSRF_COOKIE, path="/")
    return response


def csrf_token() -> str:
    """The token a form or fetch() must echo back. Logged-in requests use the
    session's token; pre-login forms fall back to one in Flask's signed
    cookie, since no server session exists yet."""
    if getattr(g, "csrf_token", None):
        return g.csrf_token
    tok = session.get("_csrf")
    if not tok:
        import secrets
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def check_csrf():
    """before_request: reject unsafe methods without a matching token.
    Returns a response to short-circuit with, or None to continue."""
    if request.method in SAFE_METHODS:
        return None
    if any(request.path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
        return None

    # Reject an obviously cross-site request outright, whatever the token says.
    origin = request.headers.get("Origin")
    if origin and not origin.rstrip("/") == config.PUBLIC_BASE_URL:
        return _deny("Cross-origin request rejected.", 403)

    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or ""
    expected = g.csrf_token if getattr(g, "csrf_token", None) else session.get("_csrf", "")
    if not expected or not sent or not hmac.compare_digest(sent, expected):
        return _deny("Invalid or missing CSRF token.", 400)
    return None


def _wants_json() -> bool:
    return request.path.startswith("/api/") or \
        request.accept_mimetypes.best == "application/json"


def _deny(message: str, code: int, redirect_to: str | None = None):
    if _wants_json():
        return jsonify({"error": message}), code
    if redirect_to:
        return redirect(redirect_to)
    return message, code


# ── Decorators ────────────────────────────────────────────────────────────── #

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not getattr(g, "user", None):
            gone = getattr(g, "signed_out", None)
            if gone and gone.get("reason") != "expired":
                # Ended for them rather than by them: explain, don't just
                # drop them at a login form wondering what happened.
                if _wants_json():
                    return jsonify({"error": "You were signed out.",
                                    "signed_out": gone["reason"]}), 401
                session["signed_out"] = gone
                return redirect(url_for("auth.signed_out"))
            return _deny("Sign in required.", 401,
                         url_for("auth.login", next=request.path))
        return f(*a, **kw)
    return wrapper


def verified_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not g.user.email_verified:
            return _deny("Confirm your email address first.", 403,
                         url_for("auth.check_email"))
        return f(*a, **kw)
    return wrapper


def paid_required(f):
    """402 rather than 403, so the frontend can tell 'log in' from 'pay'."""
    @wraps(f)
    @verified_required
    def wrapper(*a, **kw):
        if not g.user.has_access:
            return _deny("This needs active access.", 402, url_for("billing.page"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    """404, not 403 — don't advertise that an admin surface exists."""
    @wraps(f)
    def wrapper(*a, **kw):
        if not getattr(g, "user", None) or not g.user.is_admin:
            return _deny("Not found.", 404)
        return f(*a, **kw)
    return wrapper

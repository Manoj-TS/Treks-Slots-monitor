"""Request-level auth: session resolution, CSRF, and the access decorators.

One before_request hook resolves the session once and puts it on `g`; the
decorators then only inspect `g` and never touch the database. Session lookups
are cached in-process briefly so a burst of requests from one page load costs a
single query.
"""

import hmac
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
    """Drop every cached session for one user (password change, admin disable)."""
    for tok in [t for t, (u, _, _) in _SESSION_CACHE.items() if u.id == user_id]:
        _SESSION_CACHE.pop(tok, None)


def load_session() -> None:
    """before_request: populate g.user / g.csrf_token. Never raises."""
    g.user = None
    g.session_token = None
    g.csrf_token = None

    if not storage.db_ready():
        return

    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return

    cached = _cache_get(token)
    if cached:
        g.user, g.csrf_token = cached
        g.session_token = token
        return

    try:
        found = accounts.lookup_session(token)
    except Exception as e:
        print(f"[Auth] session lookup failed: {e}")
        return
    if not found:
        return

    user, csrf = found
    _cache_put(token, user, csrf)
    g.user, g.csrf_token, g.session_token = user, csrf, token


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
            # Literal path, not url_for: the billing blueprint lands in a later
            # phase and url_for would raise BuildError until then.
            return _deny("This needs an active subscription.", 402, "/billing")
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

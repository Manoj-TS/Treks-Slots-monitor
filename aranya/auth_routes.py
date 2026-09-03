"""Signup, login, logout, email verification and password reset."""

import re
import time
from datetime import timedelta

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)

from . import accounts, config, mail, oauth, security, storage

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8

# email -> [failure timestamps]. In-process and resets on deploy, which is
# acceptable at this scale; nginx rate-limits /login as a second layer.
_failures: dict[str, list[float]] = {}
_LOCK_AFTER = 5
_LOCK_WINDOW = 900.0     # 15 minutes


def _locked_out(email: str) -> bool:
    hits = [t for t in _failures.get(email.lower(), []) if time.time() - t < _LOCK_WINDOW]
    _failures[email.lower()] = hits
    return len(hits) >= _LOCK_AFTER


def _record_failure(email: str) -> None:
    _failures.setdefault(email.lower(), []).append(time.time())


def _clear_failures(email: str) -> None:
    _failures.pop(email.lower(), None)


def _abs_url(path: str) -> str:
    """Always build emailed links from configured config, never request.host —
    host-header injection into a reset link is account takeover."""
    return f"{config.PUBLIC_BASE_URL}{path}"


def _start_session(user, response):
    token, csrf = accounts.create_session(
        user.id, ip=request.headers.get("X-Real-IP") or request.remote_addr,
        user_agent=request.headers.get("User-Agent"))
    accounts.touch_login(user.id)
    secure = config.PUBLIC_BASE_URL.startswith("https://")
    max_age = config.SESSION_DAYS * 24 * 3600
    # SameSite=Lax, not Strict: the Google OAuth redirect and the payment
    # return navigation must carry the cookie. Strict breaks both silently.
    response.set_cookie(config.SESSION_COOKIE, token, max_age=max_age,
                        httponly=True, secure=secure, samesite="Lax", path="/")
    # Readable by JS so fetch() can echo it back as X-CSRF-Token.
    response.set_cookie(config.CSRF_COOKIE, csrf, max_age=max_age,
                        httponly=False, secure=secure, samesite="Lax", path="/")
    return response


def _require_db():
    if not storage.db_ready():
        return render_template("auth/unavailable.html"), 503
    return None


# ── Signup ────────────────────────────────────────────────────────────────── #

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if down := _require_db():
        return down
    if g.user:
        return redirect(url_for("main.index"))
    if request.method == "GET":
        return render_template("auth/signup.html", csrf=security.csrf_token())

    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    name = (request.form.get("name") or "").strip() or None

    if not EMAIL_RE.match(email):
        flash("That doesn't look like an email address.", "error")
        return render_template("auth/signup.html", csrf=security.csrf_token(),
                               email=email), 400
    if len(password) < MIN_PASSWORD:
        flash(f"Password must be at least {MIN_PASSWORD} characters.", "error")
        return render_template("auth/signup.html", csrf=security.csrf_token(),
                               email=email), 400

    existing = accounts.get_user_by_email(email)
    if existing:
        # Don't reveal that the address is taken. Tell the address itself.
        mail.send_account_exists(email)
    else:
        user = accounts.create_user(email, password=password, name=name)
        token = accounts.issue_token(user.id, "verify_email",
                                     timedelta(hours=config.VERIFY_TOKEN_HOURS))
        mail.send_verification(email, _abs_url(url_for("auth.verify", token=token)))

    # Identical response either way.
    return redirect(url_for("auth.check_email"))


@bp.route("/check-email")
def check_email():
    return render_template("auth/check_email.html")


@bp.route("/verify")
def verify():
    if down := _require_db():
        return down
    user_id = accounts.consume_token(request.args.get("token", ""), "verify_email")
    if not user_id:
        flash("That confirmation link is invalid or has expired.", "error")
        return render_template("auth/check_email.html", expired=True), 400
    accounts.mark_verified(user_id)
    user = accounts.get_user(user_id)
    resp = redirect(url_for("main.index"))
    flash("Email confirmed. You're signed in.", "ok")
    return _start_session(user, resp)


# ── Login / logout ────────────────────────────────────────────────────────── #

@bp.route("/login", methods=["GET", "POST"])
def login():
    if down := _require_db():
        return down
    if g.user:
        return redirect(url_for("main.index"))
    nxt = request.args.get("next") or request.form.get("next") or ""
    if request.method == "GET":
        return render_template("auth/login.html", csrf=security.csrf_token(), next=nxt)

    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    if _locked_out(email):
        flash("Too many failed attempts. Try again in 15 minutes.", "error")
        return render_template("auth/login.html", csrf=security.csrf_token(),
                               email=email, next=nxt), 429

    user = accounts.verify_password(email, password)
    if not user:
        _record_failure(email)
        # Same message whether the account exists or the password was wrong.
        flash("Email or password is incorrect.", "error")
        return render_template("auth/login.html", csrf=security.csrf_token(),
                               email=email, next=nxt), 401

    if user.status != "active":
        flash("That account has been disabled. Contact support.", "error")
        return render_template("auth/login.html", csrf=security.csrf_token()), 403

    _clear_failures(email)
    if not user.email_verified:
        token = accounts.issue_token(user.id, "verify_email",
                                     timedelta(hours=config.VERIFY_TOKEN_HOURS))
        mail.send_verification(user.email, _abs_url(url_for("auth.verify", token=token)))
        return redirect(url_for("auth.check_email"))

    # Only redirect to internal paths — an open redirect here is a phishing gift.
    target = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.index")
    return _start_session(user, redirect(target))


@bp.route("/logout", methods=["POST"])
def logout():
    token = getattr(g, "session_token", None)
    if token:
        security.evict(token)
        try:
            accounts.revoke_session(token)
        except Exception as e:
            print(f"[Auth] logout: {e}")
    resp = redirect(url_for("main.index"))
    resp.delete_cookie(config.SESSION_COOKIE, path="/")
    resp.delete_cookie(config.CSRF_COOKIE, path="/")
    session.clear()
    return resp


# ── Password reset ────────────────────────────────────────────────────────── #

@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if down := _require_db():
        return down
    if request.method == "GET":
        return render_template("auth/forgot.html", csrf=security.csrf_token())

    email = (request.form.get("email") or "").strip()
    user = accounts.get_user_by_email(email) if EMAIL_RE.match(email) else None
    if user:
        accounts.invalidate_tokens(user.id, "reset_password")
        token = accounts.issue_token(user.id, "reset_password",
                                     timedelta(minutes=config.RESET_TOKEN_MINUTES))
        mail.send_password_reset(user.email, _abs_url(url_for("auth.reset", token=token)))
    # Identical response whether or not the account exists.
    return render_template("auth/forgot.html", sent=True)


@bp.route("/reset", methods=["GET", "POST"])
def reset():
    if down := _require_db():
        return down
    token = request.values.get("token", "")

    if request.method == "GET":
        # Don't spend the token just to render the form.
        return render_template("auth/reset.html", csrf=security.csrf_token(), token=token)

    password = request.form.get("password") or ""
    if len(password) < MIN_PASSWORD:
        flash(f"Password must be at least {MIN_PASSWORD} characters.", "error")
        return render_template("auth/reset.html", csrf=security.csrf_token(),
                               token=token), 400

    user_id = accounts.consume_token(token, "reset_password")
    if not user_id:
        flash("That reset link is invalid, expired, or already used.", "error")
        return render_template("auth/reset.html", expired=True), 400

    accounts.set_password(user_id, password)
    accounts.mark_verified(user_id)          # proves control of the mailbox
    accounts.revoke_all_sessions(user_id)    # log out everywhere
    security.evict_user(user_id)
    user = accounts.get_user(user_id)
    mail.send_password_changed(user.email)

    flash("Password updated. You're signed in.", "ok")
    return _start_session(user, redirect(url_for("main.index")))


# ── Google sign-in ────────────────────────────────────────────────────────── #

@bp.route("/auth/google")
def google_start():
    if down := _require_db():
        return down
    if not oauth.enabled():
        flash("Google sign-in isn't configured.", "error")
        return redirect(url_for("auth.login"))
    # Remember where to land afterwards; only internal paths.
    nxt = request.args.get("next") or ""
    session["oauth_next"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else ""
    redirect_uri = _abs_url(url_for("auth.google_callback"))
    return oauth.client().authorize_redirect(redirect_uri)


@bp.route("/auth/google/callback")
def google_callback():
    if down := _require_db():
        return down
    if not oauth.enabled():
        return redirect(url_for("auth.login"))

    try:
        # Verifies state, exchanges the code, and validates the ID token's
        # signature and claims. Raises on anything suspicious.
        token = oauth.client().authorize_access_token()
    except Exception as e:
        print(f"[Auth] google callback failed: {e.__class__.__name__}: {e}")
        flash("Google sign-in failed or was cancelled. Please try again.", "error")
        return redirect(url_for("auth.login"))

    claims = token.get("userinfo") or {}
    user, err = oauth.upsert_from_claims(claims)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.login"))

    if user.status != "active":
        flash("That account has been disabled. Contact support.", "error")
        return redirect(url_for("auth.login"))

    target = session.pop("oauth_next", "") or url_for("main.index")
    return _start_session(user, redirect(target))


# ── Account ───────────────────────────────────────────────────────────────── #

@bp.route("/account")
@security.login_required
def account():
    return render_template("auth/account.html", user=g.user, csrf=security.csrf_token())

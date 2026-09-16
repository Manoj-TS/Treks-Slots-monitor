"""Operator console: see accounts, grant or revoke access, set the sweep rate.

This is what makes the product sellable before Razorpay exists — take payment
however you like (UPI, bank transfer), then grant 30 days here. Every grant is
recorded in `payments` with a synthetic order id, so when real billing lands
the access model doesn't change and the history is already there.
"""

import json
import re
import uuid
from datetime import timedelta

from flask import (Blueprint, Response, flash, g, redirect, render_template, request,
                   url_for)

from . import accounts, config, db, health, mail, security, state, storage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

bp = Blueprint("admin", __name__)


def _list_users():
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT u.id, u.email, u.name, u.email_verified, u.is_admin, u.status,"
            "       u.access_until, u.created_at, u.last_login_at,"
            "       (SELECT count(*) FROM user_favourites f WHERE f.user_id = u.id),"
            "       (SELECT count(*) FROM sessions s"
            "         WHERE s.user_id = u.id AND s.expires_at > now()),"
            # Sign-ins that pushed another device out. A steady stream of these
            # is what one account passed around a team looks like.
            "       (SELECT count(*) FROM session_revocations r"
            "         WHERE r.user_id = u.id AND r.reason = 'device_limit'"
            "           AND r.revoked_at > now() - make_interval(days => %s))"
            " FROM users u ORDER BY u.created_at DESC",
            (config.SHARING_WINDOW_DAYS,)).fetchall()
    return [{"id": r[0], "email": r[1], "name": r[2], "verified": r[3], "is_admin": r[4],
             "status": r[5], "access_until": r[6], "created_at": r[7],
             "last_login": r[8], "favourites": r[9], "sessions": r[10],
             "pushed_out": r[11]} for r in rows]


def _record_manual_payment(user_id: int, days: int, before, after, note: str,
                           product: str = "access_manual") -> None:
    """Every grant lands in the ledger, including free ones — a comped account
    is recorded at zero so the history stays complete and auditable."""
    amount = 0 if product == "access_comp" else config.PRICE_RUPEES * 100
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, product, razorpay_order_id, amount_paise,"
            " status, days_granted, access_before, access_after, applied_at, notes)"
            " VALUES (%s, %s, %s, %s, 'paid', %s, %s, %s, now(), %s)",
            (user_id, product, f"manual_{uuid.uuid4().hex[:16]}", amount,
             days, before, after, json.dumps({"note": note, "source": "admin"})))


@bp.route("/admin")
@security.admin_required
def index():
    with state.lock:
        stats = dict(state.stats)
        cadence = state.settings["cadence"]
    return render_template("admin.html", users=_list_users(), stats=stats,
                           cadence=cadence, csrf=security.csrf_token(),
                           cadence_min=config.OPEN_INTERVAL_MIN,
                           cadence_max=config.OPEN_INTERVAL_MAX,
                           access_days=config.ACCESS_DAYS,
                           price=config.PRICE_RUPEES,
                           db_ready=storage.db_ready(),
                           health=health.snapshot(),
                           incidents=health.recent_events(8),
                           samples=health.recent_samples(8),
                           max_devices=config.MAX_DEVICES,
                           sharing_days=config.SHARING_WINDOW_DAYS,
                           sharing_flag=config.SHARING_FLAG_AT,
                           alerts_to_ok=config.mail_configured())


@bp.route("/admin/health/sample/<int:sample_id>")
@security.admin_required
def health_sample(sample_id):
    """A saved portal response, for working out what changed.

    It is someone else's HTML, so it is never rendered: plain text, no MIME
    sniffing, and a sandbox CSP in case a browser ignores both."""
    s = health.get_sample(sample_id)
    if not s:
        return "No such sample.", 404
    head = [
        f"Sample #{s['id']} — {s['kind']} ({s['outcome']})",
        f"Captured:  {s['captured'].isoformat()}",
        f"URL:       {s['url'] or '-'}",
        f"HTTP:      {s['status'] if s['status'] is not None else 'no response'}",
        f"Trek/date: {s['trek_id'] or '-'} / {s['date'] or '-'}",
        f"Note:      {s['note'] or '-'}",
        f"Body:      {s['bytes'] or 0} bytes"
        + (" (truncated)" if (s['bytes'] or 0) > len(s['body']) else ""),
        "Headers:",
        *[f"  {k}: {v}" for k, v in s["headers"].items()],
        "",
        "-" * 72,
        "",
    ]
    return Response("\n".join(head) + s["body"], mimetype="text/plain",
                    headers={"X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "sandbox",
                             "Cache-Control": "no-store"})


@bp.route("/admin/signout", methods=["POST"])
@security.admin_required
def signout_user():
    """End every session an account has — for a shared or compromised one."""
    try:
        user_id = int(request.form.get("user_id"))
    except (TypeError, ValueError):
        flash("Bad request.", "error")
        return redirect(url_for("admin.index"))
    if user_id == g.user.id:
        flash("Use your own account page to sign out your other devices.", "error")
        return redirect(url_for("admin.index"))
    ended = accounts.revoke_sessions(user_id, "admin", by_device="an administrator")
    security.revoke_tokens(ended, "admin")
    security.evict_user(user_id)
    flash(f"Signed out {len(ended)} session{'s' if len(ended) != 1 else ''}.", "ok")
    return redirect(url_for("admin.index"))


@bp.route("/admin/invite", methods=["POST"])
@security.admin_required
def invite():
    """Give access to an email address, whether or not it has an account yet.

    Existing account -> top it up and tell them.
    New address      -> create the account, grant the days, and email a link to
                        set a password. Consuming that link proves they control
                        the mailbox, so it also verifies the address (the reset
                        flow already does this).
    """
    email = (request.form.get("email") or "").strip()
    note = (request.form.get("note") or "").strip()
    if not EMAIL_RE.match(email):
        flash("That doesn't look like an email address.", "error")
        return redirect(url_for("admin.index"))
    try:
        days = max(1, min(3650, int(request.form.get("days") or config.ACCESS_DAYS)))
    except (TypeError, ValueError):
        flash("Days must be a number.", "error")
        return redirect(url_for("admin.index"))

    user = accounts.get_user_by_email(email)
    created = False
    if not user:
        user = accounts.create_user(email, password=None, name=None, email_verified=False)
        created = True

    before = user.access_until
    # Admin grants are a deliberate decision, so they aren't held to the
    # payment path's 365-day anti-runaway cap.
    after = accounts.grant_access(user.id, days, cap_days=3650)
    _record_manual_payment(user.id, days, before, after,
                           note or ("invited by admin" if created else "granted by admin"),
                           product="access_comp" if created else "access_manual")
    storage.reload_user(user.id)
    security.evict_user(user.id)
    state.mark_changed()

    until_txt = after.strftime("%d %b %Y") if after else "?"
    if created:
        token = accounts.issue_token(user.id, "reset_password",
                                     timedelta(minutes=config.RESET_TOKEN_MINUTES))
        link = f"{config.PUBLIC_BASE_URL}/reset?token={token}"
        sent = mail.send_invite(email, link, days)
        flash(f"Created {email} with {days} days (until {until_txt}). "
              + ("Invite emailed." if sent
                 else "COULD NOT EMAIL — send them this link yourself: " + link), "ok")
    else:
        mail.send_access_granted(email, days, until_txt)
        flash(f"Gave {email} {days} more days — access until {until_txt}.", "ok")
    return redirect(url_for("admin.index"))


@bp.route("/admin/grant", methods=["POST"])
@security.admin_required
def grant():
    try:
        user_id = int(request.form.get("user_id"))
        days = max(1, min(3650, int(request.form.get("days") or config.ACCESS_DAYS)))
    except (TypeError, ValueError):
        flash("Bad request.", "error")
        return redirect(url_for("admin.index"))

    user = accounts.get_user(user_id)
    if not user:
        flash("No such user.", "error")
        return redirect(url_for("admin.index"))

    before = user.access_until
    after = accounts.grant_access(user_id, days, cap_days=3650)
    _record_manual_payment(user_id, days, before, after,
                           request.form.get("note") or "")
    storage.reload_user(user_id)
    security.evict_user(user_id)
    state.mark_changed()
    flash(f"Granted {days} days to {user.email} — access until "
          f"{after.strftime('%d %b %Y') if after else '?'}.", "ok")
    return redirect(url_for("admin.index"))


@bp.route("/admin/revoke", methods=["POST"])
@security.admin_required
def revoke():
    try:
        user_id = int(request.form.get("user_id"))
    except (TypeError, ValueError):
        flash("Bad request.", "error")
        return redirect(url_for("admin.index"))
    with db.connection() as conn:
        conn.execute("UPDATE users SET access_until = NULL WHERE id = %s", (user_id,))
    storage.reload_user(user_id)
    security.evict_user(user_id)
    state.mark_changed()
    flash("Access revoked.", "ok")
    return redirect(url_for("admin.index"))


@bp.route("/admin/cadence", methods=["POST"])
@security.admin_required
def set_cadence():
    try:
        asked = int(request.form.get("cadence"))
    except (TypeError, ValueError):
        flash("Cadence must be a number.", "error")
        return redirect(url_for("admin.index"))

    seconds = storage.clamp_cadence(asked)
    storage.write_cadence(seconds)
    with state.lock:
        state.settings["cadence"] = seconds
    if seconds != asked:
        flash(f"Sweep interval set to {seconds}s — {asked}s is outside the "
              f"allowed {config.OPEN_INTERVAL_MIN}–{config.OPEN_INTERVAL_MAX}s "
              f"range. Polling the portal harder than this risks the block that "
              f"empties the board for everyone.", "ok")
    else:
        flash(f"Sweep interval set to {seconds}s.", "ok")
    return redirect(url_for("admin.index"))

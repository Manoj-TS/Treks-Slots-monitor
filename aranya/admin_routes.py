"""Operator console: see accounts, grant or revoke access, set the sweep rate.

This is what makes the product sellable before Razorpay exists — take payment
however you like (UPI, bank transfer), then grant 30 days here. Every grant is
recorded in `payments` with a synthetic order id, so when real billing lands
the access model doesn't change and the history is already there.
"""

import json
import uuid

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import accounts, config, db, security, state, storage

bp = Blueprint("admin", __name__)


def _list_users():
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT u.id, u.email, u.name, u.email_verified, u.is_admin, u.status,"
            "       u.access_until, u.created_at, u.last_login_at,"
            "       (SELECT count(*) FROM user_favourites f WHERE f.user_id = u.id)"
            " FROM users u ORDER BY u.created_at DESC").fetchall()
    return [{"id": r[0], "email": r[1], "name": r[2], "verified": r[3], "is_admin": r[4],
             "status": r[5], "access_until": r[6], "created_at": r[7],
             "last_login": r[8], "favourites": r[9]} for r in rows]


def _record_manual_payment(user_id: int, days: int, before, after, note: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO payments (user_id, product, razorpay_order_id, amount_paise,"
            " status, days_granted, access_before, access_after, applied_at, notes)"
            " VALUES (%s, 'access_manual', %s, %s, 'paid', %s, %s, %s, now(), %s)",
            (user_id, f"manual_{uuid.uuid4().hex[:16]}", config.PRICE_RUPEES * 100,
             days, before, after, json.dumps({"note": note, "source": "admin"})))


@bp.route("/admin")
@security.admin_required
def index():
    with state.lock:
        stats = dict(state.stats)
        cadence = state.settings["cadence"]
    return render_template("admin.html", users=_list_users(), stats=stats,
                           cadence=cadence, csrf=security.csrf_token(),
                           access_days=config.ACCESS_DAYS,
                           price=config.PRICE_RUPEES,
                           db_ready=storage.db_ready())


@bp.route("/admin/grant", methods=["POST"])
@security.admin_required
def grant():
    try:
        user_id = int(request.form.get("user_id"))
        days = max(1, min(400, int(request.form.get("days") or config.ACCESS_DAYS)))
    except (TypeError, ValueError):
        flash("Bad request.", "error")
        return redirect(url_for("admin.index"))

    user = accounts.get_user(user_id)
    if not user:
        flash("No such user.", "error")
        return redirect(url_for("admin.index"))

    before = user.access_until
    after = accounts.grant_access(user_id, days)
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
        seconds = max(20, min(900, int(request.form.get("cadence"))))
    except (TypeError, ValueError):
        flash("Cadence must be a number.", "error")
        return redirect(url_for("admin.index"))
    storage.write_cadence(seconds)
    with state.lock:
        state.settings["cadence"] = seconds
    flash(f"Sweep interval set to {seconds}s.", "ok")
    return redirect(url_for("admin.index"))

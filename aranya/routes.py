"""Board routes. Everything here is per-user and behind the paywall."""

import calendar as _cal
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from flask import (Blueprint, Response, g, jsonify, redirect, render_template,
                   request, url_for)

from . import board, config, portal, security, state, storage, sweeper, views

bp = Blueprint("main", __name__)


@bp.route("/healthz")
def healthz():
    """Public, unauthenticated, and cheap — the container healthcheck uses it.
    Deliberately NOT /api/meta: that is behind the paywall now, so probing it
    would return 402 and put the container into a restart loop."""
    ok = storage.db_ready() and state.stats.get("worker_alive", False)
    return (jsonify({"ok": ok, "db": storage.db_ready(),
                     "worker": state.stats.get("worker_alive", False),
                     "cycle": state.stats.get("cycle", 0)}), 200 if ok else 503)


@bp.route("/app")
@security.paid_required
def index():
    return render_template("dashboard.html", asset_version=config.ASSET_VERSION,
                           csrf=security.csrf_token())


@bp.route("/billing")
def billing_placeholder():
    """Stands in until real billing lands, so a signed-up user sees something
    coherent instead of a dead link."""
    return render_template("billing.html", user=getattr(g, "user", None),
                           support_email=config.SUPPORT_EMAIL,
                           csrf=security.csrf_token())


@bp.route("/api/meta")
@security.paid_required
def api_meta():
    with state.lock:
        return jsonify({"catalog_ready": state.registry["ready"],
                        "error": state.registry["error"] or state.stats["error"],
                        "treks": state.registry["treks"], "cycle": state.stats["cycle"],
                        "last_update": state.stats["last_update"]})


@bp.route("/api/state")
@security.paid_required
def api_state():
    payload = board.payload_for(g.user.id)
    if payload is None:
        return jsonify({"error": "No board for this account yet."}), 404
    return Response(payload, mimetype="application/json")


@bp.route("/api/stream")
@security.paid_required
def api_stream():
    uid = g.user.id
    deadline = time.time() + config.MAX_STREAM_SECONDS

    def gen():
        last, last_sent = None, 0.0
        yield "retry: 5000\n\n"
        while True:
            # Re-checked every wakeup against the in-memory view, so access
            # that lapses mid-stream is noticed within about a second. This is
            # a struct field read: no database, no pooled connection.
            view = views.get(uid)
            if view is None or not view.has_access:
                yield "event: expired\ndata: {}\n\n"
                return
            if time.time() > deadline:
                # Force a reconnect so @paid_required runs again, even if
                # everything else somehow failed to notice.
                yield "event: reconnect\ndata: {}\n\n"
                return
            payload = board.payload_for(uid)
            now = time.time()
            if payload and (payload != last or (now - last_sent) > 10):
                yield f"data: {payload}\n\n"
                last, last_sent = payload, now
            state.state_changed.wait(timeout=1.0)

    # No "Connection" header: it's hop-by-hop (PEP 3333) and waitress rejects it.
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/trek-calendar")
@security.paid_required
def api_trek_calendar():
    """On-demand full-month availability for one trek (any date, not just weekends)."""
    try:
        tid = int(request.args.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        y, m = (int(x) for x in month.split("-"))
        ndays = _cal.monthrange(y, m)[1]
    except Exception:
        return jsonify({"error": "Bad month (use YYYY-MM)"}), 400

    view = views.get(g.user.id)
    name, did = board.resolve_trek(tid, view)
    if did is None:
        return jsonify({"error": "Trek has no district — add it under Treks first."}), 400

    today = date.today()
    horizon = today + timedelta(days=(view.window_days if view else config.WINDOW_DAYS_DEFAULT) + 10)
    all_days = [date(y, m, d) for d in range(1, ndays + 1)]
    to_query = [d for d in all_days if today <= d <= horizon]

    cells = {}
    if to_query:
        sess = portal.new_session()
        csrf = portal.fetch_csrf(sess)
        if csrf:
            now = time.time()
            need = []
            with state.lock:
                cadence = state.settings["cadence"]
                for dt in to_query:
                    c = state.board_state.get(f"{tid}_{dt.isoformat()}")
                    fresh = False
                    if c and c.get("checked"):
                        try:
                            fresh = (now - datetime.fromisoformat(c["checked"]).timestamp()) < cadence
                        except Exception:
                            fresh = False
                    if fresh:
                        cells[dt.isoformat()] = c
                    else:
                        need.append(dt)

            def q(dt):
                # Same token bucket as the sweep, so browsing the calendar
                # cannot outrun the portal budget.
                sweeper.PORTAL_BUCKET.acquire()
                cell = portal.check_target(sess, csrf, {"trek_id": tid, "district_id": did,
                                                        "date": dt.isoformat()})
                cell.pop("_transport_ok", None)
                return dt.isoformat(), cell

            if need:
                with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
                    for iso, cell in ex.map(q, need):
                        cells[iso] = cell
                with state.lock:
                    for iso, cell in cells.items():
                        state.board_state[f"{tid}_{iso}"] = cell

    days = [{"iso": d.isoformat(), "day": d.day, "weekday": d.strftime("%a"),
             "past": d < today, "in_window": today <= d <= horizon,
             "cell": cells.get(d.isoformat())} for d in all_days]
    return jsonify({"trek": {"trek_id": tid, "name": name,
                             "district_name": config.district_name(did)},
                    "month": f"{y:04d}-{m:02d}", "days": days})


@bp.route("/api/catalog")
@security.paid_required
def api_catalog():
    with state.lock:
        discovered = list(state.registry["treks"])
        cfgs = list(state.trek_configs.values())
    have = {t["id"] for t in discovered}
    merged = list(discovered)
    for cfg in cfgs:
        if cfg["trek_id"] not in have:
            merged.append({"id": cfg["trek_id"], "name": cfg["name"],
                           "district_id": cfg.get("district_id"),
                           "district_name": config.district_name(cfg.get("district_id"))})
    merged.sort(key=lambda x: (x.get("district_name") or "", x["name"]))
    return jsonify({"ready": state.registry["ready"], "treks": merged})


def _resolve_for_add(tid: int):
    """Name + district for a trek the user is adding to their own board."""
    with state.lock:
        src = next((t for t in state.registry["treks"] if t["id"] == tid), None)
        cfg = state.trek_configs.get(str(tid))
    if src:
        return src["name"], src["district_id"], None
    if cfg:
        return cfg["name"], cfg.get("district_id"), None
    return None, None, f"Trek {tid} is not in the catalog."


@bp.route("/api/favourites", methods=["GET", "POST", "DELETE"])
@security.paid_required
def api_favourites():
    uid = g.user.id
    view = views.get_or_empty(uid)

    if request.method == "GET":
        return jsonify([{"trek_id": f.trek_id, "name": f.name,
                         "district_id": f.district_id,
                         "district_name": config.district_name(f.district_id)}
                        for f in view.favourites])

    body = request.get_json(silent=True) or {}
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400

    if request.method == "POST":
        if any(f.trek_id == tid for f in view.favourites):
            return jsonify({"ok": True})
        if len(view.favourites) >= config.MAX_FAVOURITES_PER_USER:
            return jsonify({"error": f"You can follow up to "
                                     f"{config.MAX_FAVOURITES_PER_USER} treks."}), 400
        name, did, err = _resolve_for_add(tid)
        if err:
            return jsonify({"error": err}), 400
        if did is None:
            return jsonify({"error": f"Trek {tid} has no district_id."}), 400
        storage.add_favourite(uid, tid, name, did)
    else:
        storage.remove_favourite(uid, tid)

    storage.reload_user(uid)
    state.mark_changed()
    return jsonify({"ok": True})


@bp.route("/api/favourites/reorder", methods=["POST"])
@security.paid_required
def api_favourites_reorder():
    uid = g.user.id
    order = (request.get_json(silent=True) or {}).get("order") or []
    mine = {f.trek_id for f in views.get_or_empty(uid).favourites}
    storage.reorder_favourites(uid, [t for t in order if t in mine])
    storage.reload_user(uid)
    state.mark_changed()
    return jsonify({"ok": True})


@bp.route("/api/watch", methods=["GET", "POST", "DELETE"])
@security.paid_required
def api_watch():
    uid = g.user.id
    view = views.get_or_empty(uid)

    if request.method == "GET":
        with state.lock:
            return jsonify([{"trek_id": w.trek_id, "name": w.name,
                             "district_id": w.district_id,
                             "district_name": config.district_name(w.district_id),
                             "date": w.date,
                             "cell": state.board_state.get(f"{w.trek_id}_{w.date}")}
                            for w in view.watch])

    body = request.get_json(silent=True) or {}
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    day = (body.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return jsonify({"error": "Date must be YYYY-MM-DD"}), 400

    if request.method == "POST":
        if len(view.watch) >= config.MAX_WATCH_PER_USER:
            return jsonify({"error": f"You can pin up to "
                                     f"{config.MAX_WATCH_PER_USER} dates."}), 400
        name, did = board.resolve_trek(tid, view)
        if did is None:
            return jsonify({"error": f"Trek {tid} is not in the catalog."}), 400
        storage.add_watch(uid, tid, name, did, day)
    else:
        storage.remove_watch(uid, tid, day)

    storage.reload_user(uid)
    state.mark_changed()
    return jsonify({"ok": True})


@bp.route("/api/settings", methods=["GET", "POST"])
@security.paid_required
def api_settings():
    uid = g.user.id
    view = views.get_or_empty(uid)

    if request.method == "GET":
        return jsonify({"window_days": view.window_days,
                        "cadence": state.settings["cadence"]})

    body = request.get_json(silent=True) or {}
    if "window_days" in body:
        try:
            storage.set_window_days(uid, max(1, min(60, int(body["window_days"]))))
        except (TypeError, ValueError):
            return jsonify({"error": "window_days must be a number"}), 400
    # `cadence` is intentionally not settable here — it is global, and one user
    # must not control how hard everyone polls the portal. See /admin.
    storage.reload_user(uid)
    state.mark_changed()
    view = views.get_or_empty(uid)
    return jsonify({"window_days": view.window_days, "cadence": state.settings["cadence"]})


# --- operator catalog: admin only, it is not a customer feature ---
@bp.route("/api/trek-configs", methods=["GET", "POST", "DELETE"])
@security.admin_required
def api_trek_configs():
    if request.method == "GET":
        with state.lock:
            return jsonify(list(state.trek_configs.values()))

    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        cfg, err = board.coerce_trek(body)
        if err:
            return jsonify({"error": err}), 400
        with state.lock:
            state.trek_configs[str(cfg["trek_id"])] = cfg
            cfgs = dict(state.trek_configs)
    else:
        tid = str(body.get("trek_id"))
        with state.lock:
            state.trek_configs.pop(tid, None)
            cfgs = dict(state.trek_configs)

    storage.write_trek_configs(cfgs)
    state.mark_changed()
    return jsonify({"ok": True})

"""All HTTP routes, as a Blueprint registered by create_app()."""

import calendar as _cal
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, jsonify, render_template, request

from . import board, config, portal, state, storage

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("dashboard.html", asset_version=config.ASSET_VERSION)


@bp.route("/api/meta")
def api_meta():
    with state.lock:
        return jsonify({"catalog_ready": state.registry["ready"],
                        "error": state.registry["error"] or state.stats["error"],
                        "treks": state.registry["treks"], "cycle": state.stats["cycle"],
                        "last_update": state.stats["last_update"]})


@bp.route("/api/state")
def api_state():
    return Response(board.current_payload(), mimetype="application/json")


@bp.route("/api/stream")
def api_stream():
    def gen():
        last, last_sent = None, 0.0
        while True:
            payload = board.current_payload()
            now = time.time()
            if payload != last or (now - last_sent) > 10:
                yield f"data: {payload}\n\n"
                last, last_sent = payload, now
            state.state_changed.wait(timeout=1.0)
    # No "Connection" header here: it's hop-by-hop (PEP 3333) and a spec-compliant
    # WSGI server (waitress) raises on it. Flask's dev server silently tolerated it.
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/board")
def api_board():
    return jsonify(board.build_state())


@bp.route("/api/trek-calendar")
def api_trek_calendar():
    """On-demand full-month availability for a single trek (any date, not just weekends)."""
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
    name, did = board._resolve_trek(tid)
    if did is None:
        return jsonify({"error": "Trek has no district — add it under Treks first."}), 400

    today = date.today()
    horizon = today + timedelta(days=state.settings["window_days"] + 10)
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
                for dt in to_query:
                    c = state.board_state.get(f"{tid}_{dt.isoformat()}")
                    fresh = False
                    if c and c.get("checked"):
                        try:
                            fresh = (now - datetime.fromisoformat(c["checked"]).timestamp()) < state.settings["cadence"]
                        except Exception:
                            fresh = False
                    if fresh:
                        cells[dt.isoformat()] = c
                    else:
                        need.append(dt)

            def q(dt):
                cell = portal.check_target(sess, csrf, {"trek_id": tid, "district_id": did, "date": dt.isoformat()})
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
    return jsonify({"trek": {"trek_id": tid, "name": name, "district_name": config.district_name(did)},
                    "month": f"{y:04d}-{m:02d}", "days": days})


# --- trek catalog for pickers (discovered + saved configs merged) ---
@bp.route("/api/catalog")
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


# --- favourites (the board rows) ---
@bp.route("/api/favourites", methods=["GET", "POST", "DELETE"])
def api_favourites():
    if request.method == "GET":
        with state.lock:
            return jsonify(list(state.favourites))
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        try:
            tid = int(body.get("trek_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trek id"}), 400
        with state.lock:
            if any(f["trek_id"] == tid for f in state.favourites):
                return jsonify({"ok": True})
            src = next((t for t in state.registry["treks"] if t["id"] == tid), None)
            cfg = state.trek_configs.get(str(tid))
            if src:
                name, did = src["name"], src["district_id"]
            elif cfg:
                name, did = cfg["name"], cfg.get("district_id")
            else:
                return jsonify({"error": f"Trek {tid} not known. Add it under Treks first."}), 400
            if did is None:
                return jsonify({"error": f"Trek {tid} has no district_id."}), 400
            state.favourites.append({"trek_id": tid, "name": name,
                               "district_id": did, "district_name": config.district_name(did)})
            storage.save_favourites()
        state.mark_changed()
        return jsonify({"ok": True})
    # DELETE
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    with state.lock:
        state.favourites = [f for f in state.favourites if f["trek_id"] != tid]
        storage.save_favourites()
    state.mark_changed()
    return jsonify({"ok": True})


@bp.route("/api/favourites/reorder", methods=["POST"])
def api_favourites_reorder():
    order = (request.get_json(silent=True) or {}).get("order") or []
    with state.lock:
        by_id = {f["trek_id"]: f for f in state.favourites}
        newlist = [by_id[t] for t in order if t in by_id]
        for f in state.favourites:                       # keep any not named in order
            if f["trek_id"] not in order:
                newlist.append(f)
        state.favourites = newlist
        storage.save_favourites()
    state.mark_changed()
    return jsonify({"ok": True})


# --- custom watch (rest of the dates) ---
@bp.route("/api/watch", methods=["GET", "POST", "DELETE"])
def api_watch():
    if request.method == "GET":
        with state.lock:
            return jsonify(board._watch_public())
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        try:
            tid = int(body.get("trek_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trek id"}), 400
        d = (body.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return jsonify({"error": "Date must be YYYY-MM-DD"}), 400
        with state.lock:
            src = next((t for t in state.registry["treks"] if t["id"] == tid), None)
            cfg = state.trek_configs.get(str(tid))
            fav = next((f for f in state.favourites if f["trek_id"] == tid), None)
            if fav:
                name, did = fav["name"], fav["district_id"]
            elif src:
                name, did = src["name"], src["district_id"]
            elif cfg:
                name, did = cfg["name"], cfg.get("district_id")
            else:
                return jsonify({"error": f"Trek {tid} not known."}), 400
            if did is None:
                return jsonify({"error": f"Trek {tid} has no district_id."}), 400
            if not any(w["trek_id"] == tid and w["date"] == d for w in state.custom_watch):
                state.custom_watch.append({"trek_id": tid, "name": name, "district_id": did,
                                     "district_name": config.district_name(did), "date": d})
                storage.save_watch()
        state.mark_changed()
        return jsonify({"ok": True})
    # DELETE
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    d = (body.get("date") or "").strip()
    with state.lock:
        state.custom_watch = [w for w in state.custom_watch
                                     if not (w["trek_id"] == tid and w["date"] == d)]
        storage.save_watch()
    state.mark_changed()
    return jsonify({"ok": True})


# --- trek configs (catalog source with district_id) ---
@bp.route("/api/trek-configs", methods=["GET", "POST", "DELETE"])
def api_trek_configs():
    if request.method == "GET":
        return jsonify(list(state.trek_configs.values()))
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        cfg, err = board._coerce_trek(body)
        if err:
            return jsonify({"error": err}), 400
        with state.lock:
            state.trek_configs[str(cfg["trek_id"])] = cfg
            storage.save_treks()
        state.mark_changed()
        return jsonify({"ok": True})
    tid = str(body.get("trek_id"))
    with state.lock:
        state.trek_configs.pop(tid, None)
        storage.save_treks()
    state.mark_changed()
    return jsonify({"ok": True})


# --- settings (window size + cadence) ---
@bp.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        with state.lock:
            return jsonify(dict(state.settings))
    body = request.get_json(silent=True) or {}
    with state.lock:
        if "window_days" in body:
            try:
                state.settings["window_days"] = max(1, min(60, int(body["window_days"])))
            except (TypeError, ValueError):
                pass
        if "cadence" in body:
            try:
                state.settings["cadence"] = max(5, min(600, int(body["cadence"])))
            except (TypeError, ValueError):
                pass
        storage.save_settings()
        out = dict(state.settings)
    state.mark_changed()
    return jsonify(out)

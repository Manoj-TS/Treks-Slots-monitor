#!/usr/bin/env python3
"""
ARANYA — TREK SLOT BOARD
========================
Unofficial. Shows live trek slot availability for your favourite treks across
every weekend (Sat/Sun) in the next 30 days — the portal's booking window.
No booking, no login; availability display only.

  Board       — favourite treks × the weekends in the rolling window (today…+30d).
                Each cell: open n/N (with capacity bar) · sold out · unreleased.
  Calendar    — any trek, month by month; tap a day to pin that trek+date.
  Favourites  — pick which treks appear on the board (seeded from trek_configs).
  Settings    — appearance (theme / accent / typeface / text size) and poll cadence.

Customization: filter (district / status), search, sort, group, density, column
visibility, window size (7/14/30 days), theme, accent, typeface and text size —
all persisted per browser.

    pip install -r requirements.txt
    python monitor.py   ->   http://localhost:5020
"""

import calendar as _cal
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request

# ── Config ────────────────────────────────────────────────────────────────── #

BASE = "https://aranyavihaara.karnataka.gov.in"
WORKERS = 8
BOARD_CYCLE_DEFAULT = 40           # seconds between sweeps (display, not a race)
WINDOW_DAYS_DEFAULT = 30           # portal opens bookings up to 30 days ahead
SESSION_RESET_AFTER = 4

FAVOURITES_FILE = "favourites.json"
WATCHLIST_FILE = "watchlist.json"
TREKS_FILE = "trek_configs.json"
SETTINGS_FILE = "dashboard_settings.json"

# Where to serve. Override with environment variables, e.g.  PORT=8080  HOST=0.0.0.0
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5020"))

DISTRICT_NAMES = {
    4: "Kalaburagi", 11: "Chikkaballapura", 15: "Shivamogga", 16: "Udupi",
    17: "Chikkamagaluru", 19: "Kolar", 21: "Bengaluru Gramantara",
    24: "Dakshina Kannada", 25: "Kodagu", 28: "Chamarajanagara", 29: "Ramanagara"
}

# Seeded trek_id -> config. Also seeds the initial favourites list.
DEFAULT_TREKS = {
    "112": {"trek_id": 112, "name": "Kudremukha", "district_id": 17, "timeslot_mapping_id": 190, "timeslot_id": 44},
    "114": {"trek_id": 114, "name": "Gangadikal", "district_id": 17, "timeslot_mapping_id": 188, "timeslot_id": 45},
    "110": {"trek_id": 110, "name": "Kurinjal",   "district_id": 17, "timeslot_mapping_id": 184, "timeslot_id": 45},
    "84":  {"trek_id": 84,  "name": "Bandaje",     "district_id": 17, "timeslot_mapping_id": 145, "timeslot_id": 44},
    "113": {"trek_id": 113, "name": "Netravathi", "district_id": 24, "timeslot_mapping_id": 187, "timeslot_id": 45},
}


def district_name(did):
    try:
        return DISTRICT_NAMES.get(int(did), f"Zone {did}")
    except (TypeError, ValueError):
        return "-"

# ── Shared state ──────────────────────────────────────────────────────────── #

registry = {"treks": [], "ready": False, "error": None}
trek_configs = {}
favourites = []          # [{trek_id, name, district_id, district_name}]
custom_watch = []        # [{trek_id, name, district_id, district_name, date}]
board_state = {}         # "{trek_id}_{YYYY-MM-DD}" -> cell dict
settings = {"window_days": WINDOW_DAYS_DEFAULT, "cadence": BOARD_CYCLE_DEFAULT}
stats = {"cycle": 0, "last_update": None, "error": None, "worker_alive": False}

lock = threading.Lock()
state_changed = threading.Event()

# Snapshot cache. state_changed is set/cleared back-to-back, so each SSE viewer
# wakes about once a second; without this cache every viewer would rebuild and
# re-serialize the whole board under `lock` every second — O(viewers) work per
# second. Instead we serialize once per actual change and share the string.
_snapshot_lock = threading.Lock()
_state_version = 0
_snapshot_version = -1
_snapshot_payload = None


def mark_changed():
    global _state_version
    with _snapshot_lock:
        _state_version += 1
    state_changed.set()
    state_changed.clear()

# ── Persistence ───────────────────────────────────────────────────────────── #

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Storage] load {path}: {e}")
    return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Storage] save {path}: {e}")


def save_favourites():
    _save_json(FAVOURITES_FILE, favourites)


def save_watch():
    _save_json(WATCHLIST_FILE, custom_watch)


def save_treks():
    _save_json(TREKS_FILE, trek_configs)


def save_settings():
    _save_json(SETTINGS_FILE, settings)


def _fav_from_cfg(cfg):
    did = cfg.get("district_id")
    return {"trek_id": int(cfg["trek_id"]), "name": cfg.get("name") or f"Trek {cfg['trek_id']}",
            "district_id": did, "district_name": district_name(did)}


def load_all_from_disk():
    global trek_configs, favourites, custom_watch, settings
    saved = _load_json(TREKS_FILE, None)
    trek_configs = dict(DEFAULT_TREKS) if saved is None else saved
    if saved is None:
        save_treks()

    fav = _load_json(FAVOURITES_FILE, None)
    if fav is None:
        # Seed favourites from the configured treks so the board is populated on first launch.
        favourites = [_fav_from_cfg(c) for c in trek_configs.values()]
        save_favourites()
    else:
        favourites = fav

    watch = _load_json(WATCHLIST_FILE, [])
    # The old monitor stored an events map here; accept only clean watch entries.
    custom_watch = watch if isinstance(watch, list) else []

    st = _load_json(SETTINGS_FILE, {})
    settings["window_days"] = int(st.get("window_days", WINDOW_DAYS_DEFAULT))
    settings["cadence"] = int(st.get("cadence", BOARD_CYCLE_DEFAULT))

# ── Trek config coercion (Treks/Favourites picker source) ─────────────────── #

def _coerce_trek(d):
    try:
        tid = int(d.get("id") or d.get("trek_id"))
    except (TypeError, ValueError):
        return None, "Missing numeric trek id."
    try:
        district = int(d.get("district_id"))
    except (TypeError, ValueError):
        return None, "Missing district_id (needed to query availability)."
    return {
        "trek_id": tid, "name": d.get("name") or f"Trek {tid}",
        "district_id": district,
        "timeslot_mapping_id": d.get("timeslot_mapping_id"),
        "timeslot_id": d.get("timeslot_id"),
    }, None

# ── Weekend window helpers ────────────────────────────────────────────────── #

def window_weekends(days=None):
    """All Saturdays/Sundays from today through today+days (inclusive)."""
    if days is None:
        days = settings["window_days"]
    today = date.today()
    out = []
    for i in range(days + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:      # 5 = Saturday, 6 = Sunday
            out.append(d)
    return out


def weekend_columns(days=None):
    cols = []
    for d in window_weekends(days):
        cols.append({
            "iso": d.isoformat(),
            "day": d.day,
            "weekday": d.strftime("%a"),
            "month": d.strftime("%b"),
            "group": d.isocalendar()[1],           # Sat & Sun of one weekend share an ISO week
        })
    return cols

# ── Monitor HTTP (availability only) ──────────────────────────────────────── #

def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_csrf(session):
    try:
        r = session.get(f"{BASE}/login", timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("input", {"name": "_token"}) or soup.find("meta", {"name": "_token"})
        if tag:
            return tag.get("value") or tag.get("content")
    except Exception as e:
        print(f"[csrf] {e}")
    return None


def fetch_treks_for_district(session, csrf, district_id):
    try:
        r = session.post(f"{BASE}/get-treks", data={"_token": csrf, "district_id": str(district_id)},
                         timeout=8, headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def fetch_availability(session, csrf, district_id, trek_id, date_ddmmyyyy):
    try:
        r = session.post(f"{BASE}/availability", data={
            "_token": csrf, "district": str(district_id),
            "trek": str(trek_id), "check_in": date_ddmmyyyy,
        }, timeout=12)
        if r.status_code == 200:
            return r.text, True
        if r.status_code in (419, 401, 403):
            return None, False
        return None, True
    except Exception as e:
        print(f"[avail] {trek_id} @ {date_ddmmyyyy}: {e}")
        return None, False


MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def parse_displayed_date(html):
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(id="dateDisplay")
    if not el:
        return None, soup
    txt = el.get_text(" ", strip=True)
    m = re.search(r"(\d{1,2})\w*\s+([A-Za-z]+)\s+(\d{4})", txt)
    if not m:
        return None, soup
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = MONTHS.get(month_name.capitalize())
    if not month:
        return None, soup
    try:
        return datetime(year, month, day).date(), soup
    except ValueError:
        return None, soup


def parse_slots(soup):
    slots = []
    for card in soup.select(".slot_card"):
        name_el = card.select_one(".slot_text")
        avail_el = card.select_one(".available_text")
        name = name_el.get_text(" ", strip=True) if name_el else "?"
        avail_text = avail_el.get_text(" ", strip=True) if avail_el else ""
        m = re.search(r"(\d+)\s*/\s*(\d+)", avail_text)
        if m:
            slots.append({"name": re.sub(r"\s+", " ", name).strip(),
                          "available": int(m.group(1)), "capacity": int(m.group(2))})
    return slots


def check_target(session, csrf, tgt):
    """tgt = {trek_id, district_id, date(YYYY-MM-DD)}. Returns a cell dict."""
    d_obj = datetime.strptime(tgt["date"], "%Y-%m-%d")
    cell = {"released": False, "available": 0, "capacity": 0, "slots": [],
            "checked": datetime.now().isoformat(), "_transport_ok": True}
    html, ok = fetch_availability(session, csrf, tgt["district_id"], tgt["trek_id"],
                                  d_obj.strftime("%d-%m-%Y"))
    cell["_transport_ok"] = ok
    if not html:
        return cell
    shown_date, soup = parse_displayed_date(html)
    slots = parse_slots(soup)
    if shown_date == d_obj.date() and slots:
        cell["released"] = True
        cell["slots"] = slots
        cell["available"] = sum(s["available"] for s in slots)
        cell["capacity"] = sum(s["capacity"] for s in slots)
    return cell

# ── Discovery (best-effort; the board does not depend on it) ──────────────── #

def discover_all_treks(session, csrf):
    treks = []
    for did in range(1, 36):
        for t in fetch_treks_for_district(session, csrf, did):
            if t.get("id") and t.get("is_active", 1) == 1:
                tdid = int(t.get("district_id", did))
                treks.append({"id": int(t["id"]), "name": t.get("name") or f"Trek {t['id']}",
                              "district_id": tdid, "district_name": district_name(tdid)})
    seen, unique = set(), []
    for t in treks:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)
    unique.sort(key=lambda x: (x["district_name"], x["name"]))
    return unique


def discovery_loop():
    session = new_session()
    while not registry["ready"]:
        csrf = fetch_csrf(session)
        if csrf:
            items = discover_all_treks(session, csrf)
            if items:
                with lock:
                    registry["treks"] = items
                    registry["ready"] = True
                    registry["error"] = None
                print(f"[Discovery] Mapped {len(items)} treks.")
                mark_changed()
                return
        time.sleep(5)

# ── Board polling worker ──────────────────────────────────────────────────── #

def board_targets():
    targets = {}
    today = date.today()
    with lock:
        favs = list(favourites)
        watches = list(custom_watch)
        weekends = window_weekends()
    for f in favs:
        did = f.get("district_id")
        if did is None:
            continue
        for d in weekends:
            key = f"{f['trek_id']}_{d.isoformat()}"
            targets[key] = {"trek_id": f["trek_id"], "district_id": did, "date": d.isoformat()}
    for w in watches:
        did = w.get("district_id")
        if did is None:
            continue
        try:
            d = datetime.strptime(w["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if d < today:
            continue
        key = f"{w['trek_id']}_{w['date']}"
        targets[key] = {"trek_id": w["trek_id"], "district_id": did, "date": w["date"]}
    return targets


def worker_loop():
    session = new_session()
    csrf = None
    bad_cycles = 0
    cycle = stats["cycle"]

    while True:
        try:
            with lock:
                stats["worker_alive"] = True

            targets = board_targets()
            if not targets:
                with lock:
                    stats["error"] = "No favourites yet. Add treks under the Favourites tab."
                mark_changed()
                time.sleep(3)
                continue

            if not csrf:
                csrf = fetch_csrf(session)
                if not csrf:
                    bad_cycles += 1
                    with lock:
                        stats["error"] = "Portal connection failed — retrying…"
                    mark_changed()
                    if bad_cycles >= SESSION_RESET_AFTER:
                        session = new_session()
                        bad_cycles = 0
                    time.sleep(min(3 * (bad_cycles or 1), 15))
                    continue

            keys = list(targets.keys())
            tgts = [targets[k] for k in keys]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                results = list(ex.map(lambda t: check_target(session, csrf, t), tgts))

            if results and all(not r.get("_transport_ok") for r in results):
                csrf = None
                bad_cycles += 1
                if bad_cycles >= SESSION_RESET_AFTER:
                    session = new_session()
                    bad_cycles = 0
                with lock:
                    stats["error"] = "Portal not responding — refreshing session…"
                mark_changed()
                time.sleep(min(3 * bad_cycles, 15))
                continue

            bad_cycles = 0
            cycle += 1
            with lock:
                for k, cell in zip(keys, results):
                    cell.pop("_transport_ok", None)
                    board_state[k] = cell
                stats["cycle"] = cycle
                stats["last_update"] = datetime.now().isoformat()
                stats["error"] = None
                cadence = settings["cadence"]
            mark_changed()
            time.sleep(max(5, cadence))
        except Exception as e:
            print(f"[Worker Error] {e}")
            with lock:
                stats["error"] = str(e)
            mark_changed()
            time.sleep(4)


def supervised_worker():
    while True:
        t = threading.Thread(target=worker_loop, daemon=True)
        t.start()
        t.join()
        with lock:
            stats["worker_alive"] = False
            stats["error"] = "Worker crashed — restarting…"
        mark_changed()
        print("[Supervisor] Worker died. Restarting in 3s…")
        time.sleep(3)

# ── State assembly ────────────────────────────────────────────────────────── #

def _build_board():
    cols = weekend_columns()
    isos = [c["iso"] for c in cols]
    rows = []
    for f in favourites:
        cells = {}
        for iso in isos:
            c = board_state.get(f"{f['trek_id']}_{iso}")
            if c:
                cells[iso] = c
        rows.append({"trek_id": f["trek_id"], "name": f["name"],
                     "district_id": f.get("district_id"),
                     "district_name": f.get("district_name"), "cells": cells})
    return cols, rows


def _watch_public():
    out = []
    for w in custom_watch:
        cell = board_state.get(f"{w['trek_id']}_{w['date']}")
        out.append({**w, "cell": cell})
    return out


def build_state():
    with lock:
        cols, rows = _build_board()
        today = date.today()
        end = today + timedelta(days=settings["window_days"])
        base = {
            "ready": True,
            "catalog_ready": registry["ready"],
            "error": stats["error"] or registry["error"],
            "cycle": stats["cycle"], "last_update": stats["last_update"],
            "window_days": settings["window_days"],
            "cadence": settings["cadence"],
            "window_start": today.isoformat(),
            "window_end": end.isoformat(),
            "weekends": cols,
            "rows": rows,
            "favourites": list(favourites),
            "watch": _watch_public(),
        }
    return base


def current_payload():
    """Serialized state, rebuilt only when something actually changed.

    Tagging the cache with the version read *before* building means a change that
    lands mid-build simply leaves the cache stale, so the next caller rebuilds —
    never serves newer data under an older tag.
    """
    global _snapshot_version, _snapshot_payload
    with _snapshot_lock:
        version = _state_version
        if _snapshot_version == version and _snapshot_payload is not None:
            return _snapshot_payload
    payload = json.dumps(build_state(), sort_keys=True)
    with _snapshot_lock:
        if version >= _snapshot_version:
            _snapshot_version, _snapshot_payload = version, payload
    return payload

# ── Flask app ─────────────────────────────────────────────────────────────── #

app = Flask(__name__)

# Cache-busts static/css and static/js so a deploy doesn't serve a stale
# cached asset. GIT_SHA is exported by start.sh; "dev" outside that context.
ASSET_VERSION = os.environ.get("GIT_SHA", "dev")


@app.route("/")
def index():
    return render_template("dashboard.html", asset_version=ASSET_VERSION)


@app.route("/api/meta")
def api_meta():
    with lock:
        return jsonify({"catalog_ready": registry["ready"],
                        "error": registry["error"] or stats["error"],
                        "treks": registry["treks"], "cycle": stats["cycle"],
                        "last_update": stats["last_update"]})


@app.route("/api/state")
def api_state():
    return Response(current_payload(), mimetype="application/json")


@app.route("/api/stream")
def api_stream():
    def gen():
        last, last_sent = None, 0.0
        while True:
            payload = current_payload()
            now = time.time()
            if payload != last or (now - last_sent) > 10:
                yield f"data: {payload}\n\n"
                last, last_sent = payload, now
            state_changed.wait(timeout=1.0)
    # No "Connection" header here: it's hop-by-hop (PEP 3333) and a spec-compliant
    # WSGI server (waitress) raises on it. Flask's dev server silently tolerated it.
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/board")
def api_board():
    return jsonify(build_state())


def _resolve_trek(tid):
    with lock:
        fav = next((f for f in favourites if f["trek_id"] == tid), None)
        src = next((t for t in registry["treks"] if t["id"] == tid), None)
        cfg = trek_configs.get(str(tid))
    if fav:
        return fav["name"], fav["district_id"]
    if src:
        return src["name"], src["district_id"]
    if cfg:
        return cfg["name"], cfg.get("district_id")
    return None, None


@app.route("/api/trek-calendar")
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
    name, did = _resolve_trek(tid)
    if did is None:
        return jsonify({"error": "Trek has no district — add it under Treks first."}), 400

    today = date.today()
    horizon = today + timedelta(days=settings["window_days"] + 10)
    all_days = [date(y, m, d) for d in range(1, ndays + 1)]
    to_query = [d for d in all_days if today <= d <= horizon]

    cells = {}
    if to_query:
        sess = new_session()
        csrf = fetch_csrf(sess)
        if csrf:
            now = time.time()
            need = []
            with lock:
                for dt in to_query:
                    c = board_state.get(f"{tid}_{dt.isoformat()}")
                    fresh = False
                    if c and c.get("checked"):
                        try:
                            fresh = (now - datetime.fromisoformat(c["checked"]).timestamp()) < settings["cadence"]
                        except Exception:
                            fresh = False
                    if fresh:
                        cells[dt.isoformat()] = c
                    else:
                        need.append(dt)

            def q(dt):
                cell = check_target(sess, csrf, {"trek_id": tid, "district_id": did, "date": dt.isoformat()})
                cell.pop("_transport_ok", None)
                return dt.isoformat(), cell

            if need:
                with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                    for iso, cell in ex.map(q, need):
                        cells[iso] = cell
                with lock:
                    for iso, cell in cells.items():
                        board_state[f"{tid}_{iso}"] = cell

    days = [{"iso": d.isoformat(), "day": d.day, "weekday": d.strftime("%a"),
             "past": d < today, "in_window": today <= d <= horizon,
             "cell": cells.get(d.isoformat())} for d in all_days]
    return jsonify({"trek": {"trek_id": tid, "name": name, "district_name": district_name(did)},
                    "month": f"{y:04d}-{m:02d}", "days": days})


# --- trek catalog for pickers (discovered + saved configs merged) ---
@app.route("/api/catalog")
def api_catalog():
    with lock:
        discovered = list(registry["treks"])
        cfgs = list(trek_configs.values())
    have = {t["id"] for t in discovered}
    merged = list(discovered)
    for cfg in cfgs:
        if cfg["trek_id"] not in have:
            merged.append({"id": cfg["trek_id"], "name": cfg["name"],
                           "district_id": cfg.get("district_id"),
                           "district_name": district_name(cfg.get("district_id"))})
    merged.sort(key=lambda x: (x.get("district_name") or "", x["name"]))
    return jsonify({"ready": registry["ready"], "treks": merged})


# --- favourites (the board rows) ---
@app.route("/api/favourites", methods=["GET", "POST", "DELETE"])
def api_favourites():
    if request.method == "GET":
        with lock:
            return jsonify(list(favourites))
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        try:
            tid = int(body.get("trek_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trek id"}), 400
        with lock:
            if any(f["trek_id"] == tid for f in favourites):
                return jsonify({"ok": True})
            src = next((t for t in registry["treks"] if t["id"] == tid), None)
            cfg = trek_configs.get(str(tid))
            if src:
                name, did = src["name"], src["district_id"]
            elif cfg:
                name, did = cfg["name"], cfg.get("district_id")
            else:
                return jsonify({"error": f"Trek {tid} not known. Add it under Treks first."}), 400
            if did is None:
                return jsonify({"error": f"Trek {tid} has no district_id."}), 400
            favourites.append({"trek_id": tid, "name": name,
                               "district_id": did, "district_name": district_name(did)})
            save_favourites()
        mark_changed()
        return jsonify({"ok": True})
    # DELETE
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    with lock:
        globals()["favourites"] = [f for f in favourites if f["trek_id"] != tid]
        save_favourites()
    mark_changed()
    return jsonify({"ok": True})


@app.route("/api/favourites/reorder", methods=["POST"])
def api_favourites_reorder():
    order = (request.get_json(silent=True) or {}).get("order") or []
    with lock:
        by_id = {f["trek_id"]: f for f in favourites}
        newlist = [by_id[t] for t in order if t in by_id]
        for f in favourites:                       # keep any not named in order
            if f["trek_id"] not in order:
                newlist.append(f)
        globals()["favourites"] = newlist
        save_favourites()
    mark_changed()
    return jsonify({"ok": True})


# --- custom watch (rest of the dates) ---
@app.route("/api/watch", methods=["GET", "POST", "DELETE"])
def api_watch():
    if request.method == "GET":
        with lock:
            return jsonify(_watch_public())
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        try:
            tid = int(body.get("trek_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid trek id"}), 400
        d = (body.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return jsonify({"error": "Date must be YYYY-MM-DD"}), 400
        with lock:
            src = next((t for t in registry["treks"] if t["id"] == tid), None)
            cfg = trek_configs.get(str(tid))
            fav = next((f for f in favourites if f["trek_id"] == tid), None)
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
            if not any(w["trek_id"] == tid and w["date"] == d for w in custom_watch):
                custom_watch.append({"trek_id": tid, "name": name, "district_id": did,
                                     "district_name": district_name(did), "date": d})
                save_watch()
        mark_changed()
        return jsonify({"ok": True})
    # DELETE
    try:
        tid = int(body.get("trek_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid trek id"}), 400
    d = (body.get("date") or "").strip()
    with lock:
        globals()["custom_watch"] = [w for w in custom_watch
                                     if not (w["trek_id"] == tid and w["date"] == d)]
        save_watch()
    mark_changed()
    return jsonify({"ok": True})


# --- trek configs (catalog source with district_id) ---
@app.route("/api/trek-configs", methods=["GET", "POST", "DELETE"])
def api_trek_configs():
    if request.method == "GET":
        return jsonify(list(trek_configs.values()))
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        cfg, err = _coerce_trek(body)
        if err:
            return jsonify({"error": err}), 400
        with lock:
            trek_configs[str(cfg["trek_id"])] = cfg
            save_treks()
        mark_changed()
        return jsonify({"ok": True})
    tid = str(body.get("trek_id"))
    with lock:
        trek_configs.pop(tid, None)
        save_treks()
    mark_changed()
    return jsonify({"ok": True})


# --- settings (window size + cadence) ---
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        with lock:
            return jsonify(dict(settings))
    body = request.get_json(silent=True) or {}
    with lock:
        if "window_days" in body:
            try:
                settings["window_days"] = max(1, min(60, int(body["window_days"])))
            except (TypeError, ValueError):
                pass
        if "cadence" in body:
            try:
                settings["cadence"] = max(5, min(600, int(body["cadence"])))
            except (TypeError, ValueError):
                pass
        save_settings()
        out = dict(settings)
    mark_changed()
    return jsonify(out)


_started = False


def start_background():
    """Load data + launch the discovery/polling threads. Safe to call once."""
    global _started
    if _started:
        return
    _started = True
    load_all_from_disk()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=supervised_worker, daemon=True).start()


# Start workers on import too, so a production WSGI server (e.g. waitress) also runs them.
start_background()

if __name__ == "__main__":
    print("=" * 52)
    print("  ARANYA - TREK SLOT BOARD")
    print("  Weekends / Calendar / Favourites / Settings")
    print("  Unofficial. Availability display only.")
    print(f"  Open: http://localhost:{PORT}")
    print("=" * 52)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)

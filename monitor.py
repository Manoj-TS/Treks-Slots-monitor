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
from flask import Flask, Response, jsonify, request

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


@app.route("/")
def index():
    return DASHBOARD_UI


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


# ── Dashboard UI ──────────────────────────────────────────────────────────── #

DASHBOARD_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aranya</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Outfit:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:calc(15px * var(--fs-scale))}
:root{
  --bg:#070a12; --bg2:#0a0f1a; --surface:#0e1320; --surface2:#111827;
  --border:#1d293f; --border-hi:#2c3e5d;
  --text:#cfd6e4; --text-dim:#62738d; --text-bright:#f1f5f9;
  --green:#3ba776; --green-dim:#14351f; --red:#ef4444; --red-dim:#3a1620;
  --amber:#f59e0b; --accent:#3b82f6; --accent-2:#2563eb;
  --radius:14px; --cell:16px;
  /* status semantics: sold out recedes, open is the signal */
  --sold:var(--text-dim); --warn:#d99a3c; --danger:#d85f5f;
  --font:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --font-mono:'IBM Plex Mono',ui-monospace,'Cascadia Mono',monospace;
  --fs-scale:1;
}
:root[data-density="compact"]{--cell:9px}
html[data-light="1"]{
  --bg:#f4f6fb; --bg2:#eef1f8; --surface:#ffffff; --surface2:#f7f9fd;
  --border:#dbe2ef; --border-hi:#c3cee0;
  --text:#2b3550; --text-dim:#7484a0; --text-bright:#0d1526;
  --green:#15803d; --green-dim:#dcfce7; --red:#dc2626; --red-dim:#fee2e2;
  --warn:#b45309; --danger:#b91c1c;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;overflow-x:hidden;
  font-variant-numeric:tabular-nums}
a{color:inherit}
.mono{font-family:var(--font-mono)}
/* thin, theme-aware scrollbars */
*{scrollbar-width:thin;scrollbar-color:var(--border-hi) transparent}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border-hi);border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:var(--accent)}
::-webkit-scrollbar-corner{background:transparent}

/* header */
.header{position:sticky;top:0;z-index:60;padding:14px 26px;border-bottom:1px solid var(--border);
  background:color-mix(in srgb,var(--bg2) 88%,transparent);backdrop-filter:blur(10px);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;font-size:1.1rem;
  background:linear-gradient(140deg,var(--accent),var(--accent-2));box-shadow:0 4px 18px color-mix(in srgb,var(--accent) 45%,transparent)}
.brand h1{font-size:1.06rem;font-weight:600;color:var(--text-bright);letter-spacing:.01em;line-height:1.15}
.brand .sub{font-size:.68rem;color:var(--text-dim);letter-spacing:.1em;text-transform:uppercase}
.hgroup{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.window-pill{display:flex;align-items:center;gap:10px;font-size:.8rem}
.window-pill .rng{color:var(--text-bright);font-weight:500}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:9px;overflow:hidden}
.seg button{background:transparent;border:none;color:var(--text-dim);padding:6px 11px;font:inherit;font-size:.76rem;font-weight:500;cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}
.status{display:flex;align-items:center;gap:8px;font-size:.78rem}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
.dot.err{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.dot.dead{background:var(--red);box-shadow:0 0 10px var(--red)}
.flash{transition:opacity .4s;opacity:0;width:7px;height:7px;border-radius:50%;background:var(--accent)}
.flash.on{opacity:1}

/* tabs */
.tabs{display:flex;gap:2px;padding:0 26px;border-bottom:1px solid var(--border);background:var(--bg2);flex-wrap:wrap;position:sticky;top:63px;z-index:55}
.tab-btn{background:transparent;border:none;color:var(--text-dim);padding:13px 16px;cursor:pointer;font:inherit;font-size:.88rem;font-weight:500;border-bottom:2px solid transparent}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{color:var(--text-bright);border-bottom-color:var(--accent)}

.wrap{max-width:1280px;margin:22px auto;padding:0 22px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px}
.card+.card{margin-top:16px}
.ptitle{font-size:1rem;font-weight:600;color:var(--text-bright);margin-bottom:4px}
.psub{font-size:.78rem;color:var(--text-dim);margin-bottom:14px}
label.fld{display:block;font-size:.74rem;font-weight:500;color:var(--text-dim);margin-bottom:5px;letter-spacing:.01em}
select,input[type=text],input[type=date],input[type=number]{width:100%;background:var(--bg);color:var(--text-bright);border:1px solid var(--border-hi);padding:9px;border-radius:8px;font:inherit;font-size:.86rem;outline:none}
select:focus,input:focus{border-color:var(--accent)}
input[type=date]::-webkit-calendar-picker-indicator{filter:invert(.7);cursor:pointer}
.btn{background:var(--accent);color:#fff;border:none;padding:9px 15px;border-radius:8px;font:inherit;font-weight:500;cursor:pointer}
.btn:hover{background:var(--accent-2)}
.btn-sm{background:transparent;border:1px solid var(--border-hi);color:var(--text);padding:6px 11px;border-radius:7px;cursor:pointer;font:inherit;font-size:.78rem}
.btn-sm:hover{border-color:var(--accent);color:var(--text-bright)}
.btn-danger{color:var(--red);border-color:color-mix(in srgb,var(--red) 40%,transparent)}
.msg{font-size:.8rem;color:var(--amber);margin-top:8px;min-height:15px}

/* summary strip (compact KPIs) */
.summary{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;margin-bottom:14px}
.summary .s{display:inline-flex;align-items:baseline;gap:5px;padding:6px 11px;background:var(--surface);
  border:1px solid var(--border);border-radius:20px;font-size:.78rem;color:var(--text-dim);white-space:nowrap}
.summary .s b{font-family:var(--font-mono);font-size:.92rem;font-weight:500;color:var(--text-bright)}
.summary .s.good{border-color:color-mix(in srgb,var(--green) 45%,var(--border))}
.summary .s.good b{color:var(--green)}

/* toolbar */
.toolbar{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:14px}
.toolbar .tb{display:flex;flex-direction:column;gap:4px}
.toolbar .grow{flex:1;min-width:170px}
.toolbar select,.toolbar input{padding:7px 9px;font-size:.8rem}

/* board matrix */
.board-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}
table.board{border-collapse:separate;border-spacing:0;width:100%;min-width:640px}
.grp-head th{position:sticky;top:0;z-index:3;background:var(--surface2);border-bottom:1px solid var(--border)}
.col-head th{position:sticky;top:0;z-index:2;background:var(--surface2);border-bottom:1px solid var(--border)}
.grp-head th.gh{text-align:center;font-size:.66rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.08em;padding:8px 6px 4px}
.col-head th.ch{text-align:center;padding:2px 8px 8px;min-width:74px}
.ch .dw{font-size:.66rem;color:var(--text-dim);text-transform:uppercase}
.ch .dd{font-size:1rem;font-weight:600;color:var(--text-bright)}
.corner{position:sticky;left:0;top:0;z-index:5;background:var(--surface2);border-bottom:1px solid var(--border);border-right:1px solid var(--border);
  text-align:left;padding:10px 14px;font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.06em;min-width:210px}
.grp-head th.corner{z-index:6}
.trek-cell{position:sticky;left:0;z-index:1;background:var(--surface);border-right:1px solid var(--border);border-bottom:1px solid var(--border);
  padding:10px 14px;min-width:210px}
.trek-name{font-weight:500;color:var(--text-bright);font-size:.92rem}
.trek-dist{font-size:.68rem;color:var(--text-dim)}
tr.tr-row:hover .trek-cell,tr.tr-row:hover td.cell{background:color-mix(in srgb,var(--accent) 7%,var(--surface))}
td.cell{border-bottom:1px solid var(--border);border-left:1px solid var(--border);padding:var(--cell) 8px;text-align:center;vertical-align:middle}
td.cell.wkend-start{border-left:2px solid var(--border-hi)}
/* open cells get a tint so the eye lands on them; sold out stays flat */
td.cell.is-open{background:color-mix(in srgb,var(--green) 7%,transparent)}
.pill{display:inline-flex;flex-direction:column;align-items:center;gap:3px;min-width:52px}
.pill .st{font-size:.7rem;font-weight:500;letter-spacing:.01em}
.pill .nn{font-family:var(--font-mono);font-size:.82rem;font-weight:500}
.bar{width:46px;height:4px;border-radius:3px;background:var(--border);overflow:hidden}
.bar>i{display:block;height:100%;background:var(--green)}
.bar>i.mid{background:var(--warn)}
.bar>i.lo{background:var(--danger)}
.st-open .st{color:var(--green)} .st-open .nn{color:var(--green)}
.st-sold .st{color:var(--sold)} .st-sold .nn{color:var(--sold)}
.st-unrel .st{color:var(--text-dim)}
.st-pend .st{color:var(--text-dim)}
.st-past{opacity:.35}
.grp-row td{background:var(--bg2);border-bottom:1px solid var(--border);border-top:1px solid var(--border);
  padding:7px 14px;font-size:.74rem;font-weight:500;color:var(--text-dim);letter-spacing:.01em;position:sticky;left:0}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:.72rem;color:var(--text-dim)}
.legend b{font-weight:500}
.legend .lg{display:inline-flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;border-radius:3px;display:inline-block}

/* generic list rows */
.lrow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 14px;border:1px solid var(--border);border-radius:10px;background:var(--surface2)}
.lrow+.lrow{margin-top:8px}
.lrow .nm{font-weight:500;color:var(--text-bright);font-size:.9rem}
.lrow .mt{font-size:.7rem;color:var(--text-dim)}
.tag{display:inline-block;font-size:.68rem;padding:2px 8px;border-radius:10px;font-family:var(--font-mono);margin-left:6px}
.tag-open{background:var(--green-dim);color:var(--green)}
.tag-sold{background:color-mix(in srgb,var(--text-dim) 14%,transparent);color:var(--sold)}
.tag-unrel{background:color-mix(in srgb,var(--text-dim) 12%,transparent);color:var(--text-dim);border:1px dashed var(--border-hi)}
.empty{color:var(--text-dim);padding:40px 0;text-align:center}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:130px}
.swatches{display:flex;gap:8px;flex-wrap:wrap}
.swatch{width:30px;height:30px;border-radius:8px;border:2px solid transparent;cursor:pointer}
.swatch.on{border-color:var(--text-bright)}

/* pinned chips */
.pinned-strip{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.ps-lbl{font-size:.74rem;color:var(--text-dim);letter-spacing:.01em;font-weight:500}
.pchip{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border-radius:20px;background:var(--surface);border:1px solid var(--border);font-size:.76rem;color:var(--text)}
.pchip b{font-family:var(--font-mono);font-weight:500;color:var(--text-bright)}
.pchip.open{border-color:color-mix(in srgb,var(--green) 45%,var(--border))}
.pchip.open b{color:var(--green)}
.pchip.sold{border-color:var(--border)}
.pchip.sold b{color:var(--sold)}
.pchip .x{cursor:pointer;color:var(--text-dim);font-weight:500;padding-left:2px}
.pchip .x:hover{color:var(--red)}

/* calendar */
.cal-top{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;margin-bottom:14px}
.cal-nav{display:flex;align-items:center;gap:8px}
.cal-nav .mlabel{font-weight:600;color:var(--text-bright);min-width:132px;text-align:center;font-size:.95rem}
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}
.cal-dow{text-align:center;font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;padding:2px 0}
.cal-day{min-height:76px;border:1px solid var(--border);border-radius:9px;padding:8px 8px 7px;background:var(--surface2);
  display:flex;flex-direction:column;gap:3px;cursor:pointer;position:relative;transition:border-color .15s}
.cal-day:hover:not(.blank):not(.past){border-color:var(--accent)}
.cal-day.blank{background:transparent;border:none;cursor:default}
.cal-day.past{opacity:.35;cursor:default}
.cal-day .dn{font-size:1.05rem;font-weight:600;color:var(--text-bright);line-height:1.1}
.cal-day .cs{font-size:.82rem;font-weight:500;font-family:var(--font-mono);color:var(--sold);margin-top:auto}
.cal-day.c-open{border-color:color-mix(in srgb,var(--green) 55%,var(--border));background:color-mix(in srgb,var(--green) 9%,var(--surface2))}
.cal-day.c-open .cs{color:var(--green)}
.cal-day.c-sold{border-color:var(--border)}
.cal-day.c-sold .cs{color:var(--sold)}
.cal-day.pinned{outline:2px solid var(--accent);outline-offset:-1px}
.cal-pin{position:absolute;top:4px;right:6px;font-size:.72rem;line-height:1}
.cal-hint{font-size:.74rem;color:var(--text-dim);margin-top:12px}

/* footer disclaimer */
.disclaimer{margin:26px 0 8px;padding-top:14px;border-top:1px solid var(--border);
  font-size:.72rem;line-height:1.6;color:var(--text-dim);max-width:70ch}
.disclaimer a{color:var(--text);text-decoration:underline;text-underline-offset:2px}
.disclaimer a:hover{color:var(--accent)}

/* ---------- responsive / mobile ---------- */
@media(max-width:820px){
  .header{padding:12px 16px}
  .wrap{padding:0 14px}
  .tabs{top:0}
  .header{position:static}
}
@media(max-width:600px){
  .header{padding:11px 14px;gap:10px}
  .brand .sub{display:none}
  .brand h1{font-size:.92rem}
  .logo{width:30px;height:30px;font-size:1rem}
  .hgroup{gap:10px 14px;width:100%;justify-content:space-between}
  .window-pill{gap:8px}
  .window-pill .rng{font-size:.7rem}
  .status{font-size:.72rem;gap:6px}
  .tabs{padding:0 6px}
  .tab-btn{padding:11px 11px;font-size:.84rem}
  .wrap{padding:0 10px;margin:14px auto}
  .card{padding:14px}
  .card+.card{margin-top:12px}
  :root{--cell:11px}
  :root[data-density="compact"]{--cell:7px}
  .summary{gap:6px}
  .summary .s{font-size:.74rem;padding:5px 10px}
  .summary .s b{font-size:.86rem}
  .toolbar{gap:8px}
  .toolbar .grow{flex-basis:100%;min-width:0}
  .toolbar .tb{flex:1;min-width:calc(50% - 5px)}
  .corner,.trek-cell{min-width:124px;padding:9px 10px}
  .grp-head th.corner{min-width:124px}
  .trek-name{font-size:.84rem}
  .trek-dist{font-size:.64rem}
  .col-head th.ch{min-width:50px;padding:2px 5px 8px}
  .ch .dd{font-size:.9rem}
  .pill{min-width:44px}
  .pill .nn{font-size:.76rem}
  .bar{width:38px}
  .lrow{padding:10px 12px}
  .cal-grid{gap:5px}
  .cal-day{min-height:62px;padding:6px 5px 5px;border-radius:7px}
  .cal-day .dn{font-size:.95rem}
  .cal-day .cs{font-size:.75rem}
  .cal-nav .mlabel{min-width:104px;font-size:.86rem}
  .cal-top .tb{flex-basis:100%}
}
</style>
</head>
<body>

<div class="header">
  <div class="brand">
    <div class="logo">&#9968;</div>
    <div><h1>Aranya</h1><div class="sub">Unofficial slot board</div></div>
  </div>
  <div class="hgroup">
    <div class="window-pill">
      <span class="mono rng" id="rangeTxt">—</span>
      <span class="seg" id="winSeg">
        <button data-d="7">7d</button><button data-d="14">14d</button><button data-d="30" class="on">30d</button>
      </span>
    </div>
    <div class="status">
      <span class="dot" id="dot"></span><span id="statusTxt">Connecting…</span>
      <span class="flash" id="flash"></span>
      <span class="mono" style="color:var(--text-dim)" id="updTxt">—</span>
    </div>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="board" onclick="switchTab('board')">Weekends</button>
  <button class="tab-btn" data-tab="calendar" onclick="switchTab('calendar')">Calendar</button>
  <button class="tab-btn" data-tab="favourites" onclick="switchTab('favourites')">Favourites</button>
  <button class="tab-btn" data-tab="settings" onclick="switchTab('settings')">Settings</button>
</div>

<div class="wrap">
  <div class="tab-sec" id="tab-board"><div class="empty">Loading board…</div></div>
  <div class="tab-sec" id="tab-calendar" style="display:none"></div>
  <div class="tab-sec" id="tab-favourites" style="display:none"></div>
  <div class="tab-sec" id="tab-settings" style="display:none"></div>
  <div class="disclaimer">
    Unofficial. Not affiliated with or endorsed by the Karnataka Forest Department.
    Availability is read live from the official portal at
    <a href="https://aranyavihaara.karnataka.gov.in" rel="noopener noreferrer" target="_blank">aranyavihaara.karnataka.gov.in</a>
    — all bookings must be made there.
  </div>
</div>

<script>
/* ---------- utils ---------- */
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c];});}
function timeAgo(iso){if(!iso)return'never';var s=Math.round((Date.now()-new Date(iso).getTime())/1000);return s<5?'just now':s<60?(s+'s ago'):s<3600?(Math.floor(s/60)+'m ago'):(Math.floor(s/3600)+'h ago');}
function fmtDate(iso){var d=new Date(iso+'T00:00:00');return d.toLocaleDateString('en-US',{day:'numeric',month:'short'});}
function todayIso(){return new Date().toISOString().slice(0,10);}
function optHtml(val,opts){return opts.map(function(o){return '<option value="'+o[0]+'"'+(val===o[0]?' selected':'')+'>'+o[1]+'</option>';}).join('');}

/* ---------- prefs (persisted) ---------- */
var PREF_KEYS={fltTrek:'all',fltDate:'all',sort:'fav',group:'none',density:'comfortable',theme:'midnight',accent:'#3b82f6',light:'0',font:'inter',fontScale:'1'};
var prefs=Object.assign({},PREF_KEYS);
try{var saved=JSON.parse(localStorage.getItem('aranya_prefs')||'{}');Object.assign(prefs,saved);}catch(e){}
function savePrefs(){localStorage.setItem('aranya_prefs',JSON.stringify(prefs));}

var FONTS={
  inter:"'Inter',system-ui,-apple-system,'Segoe UI',sans-serif",
  outfit:"'Outfit',system-ui,-apple-system,'Segoe UI',sans-serif",
  system:"system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
};
var FONT_LABELS=[['inter','Inter'],['outfit','Outfit'],['system','System']];
var SCALES=[['0.93','S'],['1','M'],['1.08','L'],['1.18','XL']];

var THEMES={
  midnight:{'--bg':'#070a12','--bg2':'#0a0f1a','--surface':'#0e1320','--surface2':'#111827','--border':'#1d293f','--border-hi':'#2c3e5d','--text':'#cfd6e4','--text-dim':'#62738d','--text-bright':'#f1f5f9'},
  slate:{'--bg':'#0b0d10','--bg2':'#101317','--surface':'#15191f','--surface2':'#1b2028','--border':'#262d38','--border-hi':'#38414f','--text':'#d0d6de','--text-dim':'#7c8794','--text-bright':'#f4f6f8'},
  forest:{'--bg':'#060d0a','--bg2':'#0a140f','--surface':'#0d1a13','--surface2':'#112418','--border':'#193626','--border-hi':'#245038','--text':'#c8e0d2','--text-dim':'#6d8a7c','--text-bright':'#effaf3'}
};
function shade(hex,p){var n=parseInt(hex.slice(1),16),r=(n>>16)&255,g=(n>>8)&255,b=n&255;r=Math.max(0,Math.min(255,Math.round(r*(1-p))));g=Math.max(0,Math.min(255,Math.round(g*(1-p))));b=Math.max(0,Math.min(255,Math.round(b*(1-p))));return '#'+((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1);}
function applyTheme(){
  var root=document.documentElement;
  document.documentElement.setAttribute('data-light',prefs.light);
  var pal=THEMES[prefs.theme]||THEMES.midnight;
  if(prefs.light!=='1'){for(var k in pal)root.style.setProperty(k,pal[k]);}
  else{['--bg','--bg2','--surface','--surface2','--border','--border-hi','--text','--text-dim','--text-bright'].forEach(function(k){root.style.removeProperty(k);});}
  root.style.setProperty('--accent',prefs.accent);
  root.style.setProperty('--accent-2',shade(prefs.accent,0.18));
  root.style.setProperty('--font',FONTS[prefs.font]||FONTS.inter);
  root.style.setProperty('--fs-scale',prefs.fontScale);
  root.setAttribute('data-density',prefs.density);
}
applyTheme();

/* ---------- global state ---------- */
var latest={rows:[],weekends:[],favourites:[],watch:[],window_days:30};
var catalog=[];
var activeTab='board';

function switchTab(name){
  activeTab=name;
  ['board','calendar','favourites','settings'].forEach(function(n){document.getElementById('tab-'+n).style.display=(n===name?'':'none');});
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.toggle('active',b.dataset.tab===name);});
  if(name==='board')renderBoard();
  else if(name==='calendar')renderCalendar();
  else if(name==='favourites')renderFavourites();
  else if(name==='settings')renderSettings();
}

/* ---------- SSE ---------- */
function render(state){
  latest=state;
  var dot=document.getElementById('dot'),txt=document.getElementById('statusTxt');
  if(state.error){dot.className='dot err';txt.textContent=state.error;}
  else{dot.className='dot';txt.textContent='Live · sweep '+state.cycle;}
  document.getElementById('updTxt').textContent='updated '+timeAgo(state.last_update);
  document.getElementById('rangeTxt').textContent=fmtDate(state.window_start)+' – '+fmtDate(state.window_end)+' · next '+state.window_days+'d';
  document.querySelectorAll('#winSeg button').forEach(function(b){b.classList.toggle('on',+b.dataset.d===state.window_days);});
  var f=document.getElementById('flash');f.classList.add('on');setTimeout(function(){f.classList.remove('on');},350);
  if(activeTab==='board')renderBoard();
}

/* ---------- BOARD ---------- */
function cellStatus(iso,cell){
  if(iso<todayIso())return{cls:'st-past',st:'—',nn:'',bar:0};
  if(!cell)return{cls:'st-pend',st:'· · ·',nn:'',bar:0};
  if(!cell.released)return{cls:'st-unrel',st:'unreleased',nn:'',bar:0};
  if(cell.available>0)return{cls:'st-open',st:'open',nn:cell.available+'/'+cell.capacity,bar:cell.capacity?cell.available/cell.capacity:0};
  return{cls:'st-sold',st:'sold out',nn:'0/'+cell.capacity,bar:0};
}
function rowMatches(row){
  if(prefs.fltTrek!=='all'){
    var exists=(latest.favourites||[]).some(function(f){return String(f.trek_id)===String(prefs.fltTrek);});
    if(exists&&String(row.trek_id)!==String(prefs.fltTrek))return false;
  }
  return true;
}
function visibleCols(){
  var wk=latest.weekends||[];
  if(prefs.fltDate!=='all'&&wk.some(function(c){return c.iso===prefs.fltDate;}))
    return wk.filter(function(c){return c.iso===prefs.fltDate;});
  return wk;
}
function visibleIsos(){return visibleCols().map(function(c){return c.iso;});}
function rowOpenTotal(row){var isos=visibleIsos(),t=0;isos.forEach(function(iso){var c=row.cells[iso];if(c&&c.released)t+=c.available;});return t;}
function sortedRows(rows){
  var r=rows.slice();
  if(prefs.sort==='name')r.sort(function(a,b){return a.name.localeCompare(b.name);});
  else if(prefs.sort==='district')r.sort(function(a,b){return (a.district_name||'').localeCompare(b.district_name||'')||a.name.localeCompare(b.name);});
  else if(prefs.sort==='open')r.sort(function(a,b){return rowOpenTotal(b)-rowOpenTotal(a);});
  return r;
}
function toolbarHtml(){
  var trekOpts='<option value="all">All treks</option>'+(latest.favourites||[]).map(function(f){
    return '<option value="'+f.trek_id+'"'+(String(prefs.fltTrek)===String(f.trek_id)?' selected':'')+'>'+esc(f.name)+'</option>';}).join('');
  var dateOpts='<option value="all">All weekends</option>'+(latest.weekends||[]).map(function(c){
    return '<option value="'+c.iso+'"'+(prefs.fltDate===c.iso?' selected':'')+'>'+c.weekday+', '+c.month+' '+c.day+'</option>';}).join('');
  return '<div class="toolbar">'
    +'<div class="tb grow"><label class="fld">Trek</label><select onchange="onPref(\'fltTrek\',this.value)">'+trekOpts+'</select></div>'
    +'<div class="tb grow"><label class="fld">Date</label><select onchange="onPref(\'fltDate\',this.value)">'+dateOpts+'</select></div>'
    +'</div>';
}
function onPref(k,v){prefs[k]=v;savePrefs();if(k==='density')applyTheme();renderBoard();}

function kpiHtml(){
  var isos=visibleIsos();
  var totalOpen=0,treksOpen=0;
  (latest.rows||[]).forEach(function(r){var ro=0;isos.forEach(function(iso){var c=r.cells[iso];if(c&&c.released)ro+=c.available;});totalOpen+=ro;if(ro>0)treksOpen++;});
  return '<div class="summary">'
    +'<span class="s"><b>'+(latest.favourites||[]).length+'</b> treks</span>'
    +'<span class="s"><b>'+isos.length+'</b> weekend dates</span>'
    +'<span class="s '+(totalOpen>0?'good':'')+'"><b>'+totalOpen+'</b> open slots</span>'
    +'<span class="s '+(treksOpen>0?'good':'')+'"><b>'+treksOpen+'</b> with openings</span>'
    +'</div>';
}
function legendHtml(){
  return '<div class="legend">'
    +'<span class="lg"><span class="sw" style="background:var(--green)"></span> Open (available/capacity)</span>'
    +'<span class="lg"><span class="sw" style="background:var(--red)"></span> Sold out</span>'
    +'<span class="lg"><span class="sw" style="background:var(--border-hi)"></span> Unreleased (opens later)</span>'
    +'<span class="lg"><span class="sw" style="background:var(--surface2)"></span> Waiting for first sweep</span>'
    +'</div>';
}
function renderBoard(){
  var host=document.getElementById('tab-board');
  var cols=visibleCols();
  var rows=(latest.rows||[]).filter(rowMatches);
  var head=kpiHtml()+pinnedStripHtml()+toolbarHtml();
  if(!(latest.favourites||[]).length){host.innerHTML=head+'<div class="card"><div class="empty">No favourite treks yet. Add some under the <b>Favourites</b> tab — they appear here across the next '+latest.window_days+' days of weekends.</div></div>';return;}
  if(!cols.length){host.innerHTML=head+'<div class="card"><div class="empty">No weekends in the selected window.</div></div>';return;}
  if(!rows.length){host.innerHTML=head+'<div class="card"><div class="empty">No treks match the current filters.</div></div>';return;}

  // group headers (weekend blocks)
  var groupSpans=[];var lastG=null;
  cols.forEach(function(c){if(c.group!==lastG){groupSpans.push({g:c.group,label:c.month+' '+c.day,count:1,firstIdx:c.iso});lastG=c.group;}else{groupSpans[groupSpans.length-1].count++;}});
  var firstOfGroup={};groupSpans.forEach(function(g){firstOfGroup[g.firstIdx]=1;});

  var gh='<tr class="grp-head"><th class="corner">Trek</th>';
  groupSpans.forEach(function(g){gh+='<th class="gh" colspan="'+g.count+'">Weekend · '+esc(g.label)+'</th>';});
  gh+='</tr>';
  var chh='<tr class="col-head"><th class="corner" style="top:auto"></th>';
  cols.forEach(function(c){chh+='<th class="ch"><div class="dw">'+c.weekday+'</div><div class="dd">'+c.day+'</div></th>';});
  chh+='</tr>';

  rows=sortedRows(rows);
  var body='';
  function emitRow(r){
    var tds='';
    cols.forEach(function(c){
      var s=cellStatus(c.iso,r.cells[c.iso]);
      var cls='cell '+s.cls+(firstOfGroup[c.iso]?' wkend-start':'')+(s.cls==='st-open'?' is-open':'');
      // bar colour grades with how full the slot is: green > 40% > amber > 15% > red
      var lvl=s.bar<0.15?' lo':(s.bar<0.40?' mid':'');
      var inner='<div class="pill '+s.cls+'"><span class="st">'+esc(s.st)+'</span>'+(s.nn?'<span class="nn">'+esc(s.nn)+'</span>':'')+(s.cls==='st-open'?'<span class="bar"><i class="'+lvl.trim()+'" style="width:'+Math.round(s.bar*100)+'%"></i></span>':'')+'</div>';
      tds+='<td class="'+cls+'">'+inner+'</td>';
    });
    return '<tr class="tr-row"><td class="trek-cell"><div class="trek-name">'+esc(r.name)+'</div><div class="trek-dist">'+esc(r.district_name||'')+'</div></td>'+tds+'</tr>';
  }
  if(prefs.group==='district'){
    var byd={};rows.forEach(function(r){(byd[r.district_name||'—']=byd[r.district_name||'—']||[]).push(r);});
    Object.keys(byd).sort().forEach(function(d){
      body+='<tr class="grp-row"><td colspan="'+(cols.length+1)+'">'+esc(d)+'</td></tr>';
      byd[d].forEach(function(r){body+=emitRow(r);});
    });
  }else{rows.forEach(function(r){body+=emitRow(r);});}

  host.innerHTML=head
    +'<div class="board-scroll"><table class="board"><thead>'+gh+chh+'</thead><tbody>'+body+'</tbody></table></div>'
    +legendHtml();
}

/* ---------- FAVOURITES ---------- */
function loadCatalog(cb){
  if(catalog.length){cb&&cb();return;}
  fetch('/api/catalog').then(function(r){return r.json();}).then(function(d){catalog=d.treks||[];cb&&cb();});
}
function renderFavourites(){
  var host=document.getElementById('tab-favourites');
  loadCatalog(function(){
    var favIds={};(latest.favourites||[]).forEach(function(f){favIds[f.trek_id]=1;});
    var g={};catalog.filter(function(t){return !favIds[t.id];}).forEach(function(t){var k=t.district_name||'Other';(g[k]=g[k]||[]).push(t);});
    var opts='<option value="" disabled selected>Choose a trek to feature…</option>';
    Object.keys(g).sort().forEach(function(dist){opts+='<optgroup label="'+esc(dist)+'">';g[dist].forEach(function(t){opts+='<option value="'+t.id+'">'+esc(t.name)+' (#'+t.id+')</option>';});opts+='</optgroup>';});
    var add='<div class="card"><div class="ptitle">Featured treks</div><div class="psub">These are the rows on the board. Reorder to control display order.</div>'
      +'<div class="row"><div style="flex:3"><label class="fld">Add a trek</label><select id="favSel">'+opts+'</select></div>'
      +'<div style="flex:0"><button class="btn" onclick="addFav()">Add to board</button></div></div>'
      +'<div class="msg" id="favMsg"></div></div>';
    var favs=latest.favourites||[];
    var list=favs.map(function(f,i){
      return '<div class="lrow"><div><span class="nm">'+esc(f.name)+'</span> <span class="mt">#'+f.trek_id+' · '+esc(f.district_name||'')+'</span></div>'
        +'<div style="display:flex;gap:6px">'
        +'<button class="btn-sm" '+(i===0?'disabled':'')+' onclick="moveFav('+f.trek_id+',-1)">↑</button>'
        +'<button class="btn-sm" '+(i===favs.length-1?'disabled':'')+' onclick="moveFav('+f.trek_id+',1)">↓</button>'
        +'<button class="btn-sm btn-danger" onclick="delFav('+f.trek_id+')">Remove</button></div></div>';
    }).join('');
    if(!favs.length)list='<div class="empty">No featured treks yet.</div>';
    host.innerHTML=add+'<div class="card">'+list+'</div>';
  });
}
function addFav(){var v=document.getElementById('favSel').value;if(!v)return;
  fetch('/api/favourites',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trek_id:+v})})
  .then(function(r){return r.json();}).then(function(d){if(d.error){document.getElementById('favMsg').textContent=d.error;return;}setTimeout(renderFavourites,300);});}
function delFav(id){fetch('/api/favourites',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({trek_id:id})}).then(function(){setTimeout(renderFavourites,300);});}
function moveFav(id,dir){
  var ids=(latest.favourites||[]).map(function(f){return f.trek_id;});
  var i=ids.indexOf(id),j=i+dir;if(i<0||j<0||j>=ids.length)return;
  ids.splice(j,0,ids.splice(i,1)[0]);
  fetch('/api/favourites/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:ids})}).then(function(){setTimeout(renderFavourites,250);});
}

/* ---------- pinned (stick a trek+date) ---------- */
function fmtDayFull(iso){var d=new Date(iso+'T00:00:00');return d.toLocaleDateString('en-US',{weekday:'short',day:'numeric',month:'short'});}
function pinnedSet(){var s={};(latest.watch||[]).forEach(function(w){s[w.trek_id+'_'+w.date]=1;});return s;}
function pinnedStripHtml(){
  var w=(latest.watch||[]).slice().sort(function(a,b){return a.date.localeCompare(b.date);});
  if(!w.length)return '';
  var chips=w.map(function(x){
    var c=x.cell,cls='pchip',v='soon';
    if(c){if(!c.released)v='soon';else if(c.available>0){cls+=' open';v=c.available+'/'+c.capacity;}else{cls+=' sold';v='sold';}}
    else v='…';
    return '<span class="'+cls+'">'+esc(x.name)+' · '+fmtDayFull(x.date)+' <b>'+v+'</b>'
      +'<span class="x" title="Unpin" onclick="togglePin('+x.trek_id+',\''+x.date+'\',1)">✕</span></span>';
  }).join('');
  return '<div class="pinned-strip"><span class="ps-lbl">📌 Pinned</span>'+chips+'</div>';
}
function togglePin(tid,iso,isPinned){
  fetch('/api/watch',{method:isPinned?'DELETE':'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({trek_id:tid,date:iso})})
    .then(function(){setTimeout(function(){if(activeTab==='calendar')renderCalendar();else renderBoard();},300);});
}

/* ---------- CALENDAR (any trek, any date) ---------- */
var calState={trek:null,ym:null};
function renderCalendar(){
  var host=document.getElementById('tab-calendar');
  loadCatalog(function(){
    if(calState.trek==null){var f=(latest.favourites||[])[0];calState.trek=f?f.trek_id:((catalog[0]||{}).id||null);}
    if(!calState.ym){var d=new Date();calState.ym={y:d.getFullYear(),m:d.getMonth()+1};}
    if(calState.trek==null){host.innerHTML='<div class="card"><div class="empty">No treks available yet — mapping the portal…</div></div>';return;}
    var g={};catalog.forEach(function(t){var k=t.district_name||'Other';(g[k]=g[k]||[]).push(t);});
    var opts='';Object.keys(g).sort().forEach(function(dist){opts+='<optgroup label="'+esc(dist)+'">';
      g[dist].forEach(function(t){opts+='<option value="'+t.id+'"'+(String(calState.trek)===String(t.id)?' selected':'')+'>'+esc(t.name)+' (#'+t.id+')</option>';});opts+='</optgroup>';});
    var mlabel=new Date(calState.ym.y,calState.ym.m-1,1).toLocaleDateString('en-US',{month:'long',year:'numeric'});
    host.innerHTML='<div class="card">'+pinnedStripHtml()
      +'<div class="cal-top"><div class="tb grow" style="max-width:340px"><label class="fld">Trek</label><select onchange="calPick(this.value)">'+opts+'</select></div>'
      +'<div class="cal-nav"><button class="btn-sm" onclick="calMonth(-1)">‹</button><span class="mlabel">'+esc(mlabel)+'</span><button class="btn-sm" onclick="calMonth(1)">›</button></div></div>'
      +'<div id="calBody"><div class="empty">Checking availability…</div></div>'
      +'<div class="cal-hint">Tap any day to pin that trek + date. Pinned combos stay live at the top here and on the Weekends board.</div></div>';
    var mm=calState.ym.y+'-'+String(calState.ym.m).padStart(2,'0');
    fetch('/api/trek-calendar?trek_id='+calState.trek+'&month='+mm)
      .then(function(r){return r.json();}).then(renderCalBody)
      .catch(function(){var b=document.getElementById('calBody');if(b)b.innerHTML='<div class="empty">Could not load availability.</div>';});
  });
}
function renderCalBody(d){
  var body=document.getElementById('calBody');if(!body)return;
  if(d.error){body.innerHTML='<div class="empty">'+esc(d.error)+'</div>';return;}
  var pins=pinnedSet();
  var dow=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var html='<div class="cal-grid">'+dow.map(function(x){return '<div class="cal-dow">'+x+'</div>';}).join('');
  var firstDow=new Date(d.days[0].iso+'T00:00:00').getDay();
  for(var i=0;i<firstDow;i++)html+='<div class="cal-day blank"></div>';
  d.days.forEach(function(day){
    var c=day.cell,cls='cal-day',cs='<span class="cs">—</span>';
    if(day.past){cls+=' past';cs='';}
    else if(!c){cs='<span class="cs">…</span>';}
    else if(!c.released){cs='<span class="cs">soon</span>';}
    else if(c.available>0){cls+=' c-open';cs='<span class="cs">'+c.available+'/'+c.capacity+'</span>';}
    else{cls+=' c-sold';cs='<span class="cs">sold</span>';}
    var key=d.trek.trek_id+'_'+day.iso,isPin=!!pins[key];
    if(isPin)cls+=' pinned';
    var click=day.past?'':' onclick="togglePin('+d.trek.trek_id+',\''+day.iso+'\','+(isPin?1:0)+')"';
    html+='<div class="'+cls+'"'+click+'>'+(isPin?'<span class="cal-pin">📌</span>':'')+'<span class="dn">'+day.day+'</span>'+cs+'</div>';
  });
  html+='</div>';
  body.innerHTML=html;
}
function calPick(v){calState.trek=+v;renderCalendar();}
function calMonth(dir){var m=calState.ym.m+dir,y=calState.ym.y;if(m<1){m=12;y--;}if(m>12){m=1;y++;}calState.ym={y:y,m:m};renderCalendar();}

/* ---------- SETTINGS ---------- */
function renderSettings(){
  var host=document.getElementById('tab-settings');
  var themeSw=Object.keys(THEMES).map(function(t){return '<div class="swatch'+(prefs.theme===t?' on':'')+'" title="'+t+'" style="background:linear-gradient(135deg,'+THEMES[t]['--surface']+','+THEMES[t]['--bg']+')" onclick="setTheme(\''+t+'\')"></div>';}).join('');
  var accents=['#3b82f6','#22c55e','#f59e0b','#ef4444','#a855f7','#06b6d4','#ec4899'];
  var accSw=accents.map(function(a){return '<div class="swatch'+(prefs.accent===a?' on':'')+'" style="background:'+a+'" onclick="setAccent(\''+a+'\')"></div>';}).join('');
  // each typeface button previews itself, so the choice is visible before committing
  var fontSeg=FONT_LABELS.map(function(f){
    return '<button class="'+(prefs.font===f[0]?'on':'')+'" style="font-family:'+FONTS[f[0]]+'" onclick="setFont(\''+f[0]+'\')">'+f[1]+'</button>';
  }).join('');
  var scaleSeg=SCALES.map(function(s){
    return '<button class="'+(prefs.fontScale===s[0]?'on':'')+'" onclick="setFontScale(\''+s[0]+'\')">'+s[1]+'</button>';
  }).join('');
  host.innerHTML=
    '<div class="card"><div class="ptitle">Appearance</div><div class="psub">Saved in this browser.</div>'
    +'<label class="fld">Dark palette</label><div class="swatches" style="margin-bottom:14px">'+themeSw+'</div>'
    +'<label class="fld">Accent</label><div class="swatches" style="margin-bottom:14px">'+accSw
      +'<input type="color" value="'+prefs.accent+'" onchange="setAccent(this.value)" style="width:34px;height:30px;padding:0;border:none;background:none;cursor:pointer"></div>'
    +'<label class="fld">Mode</label><div class="seg" style="margin-bottom:14px"><button class="'+(prefs.light!=='1'?'on':'')+'" onclick="setLight(\'0\')">Dark</button><button class="'+(prefs.light==='1'?'on':'')+'" onclick="setLight(\'1\')">Light</button></div>'
    +'<label class="fld">Typeface</label><div class="seg" style="margin-bottom:14px">'+fontSeg+'</div>'
    +'<label class="fld">Text size</label><div class="seg">'+scaleSeg+'</div>'
    +'</div>'
    +'<div class="card"><div class="ptitle">Board layout</div><div class="psub">How treks are ordered, compared and spaced. Saved in this browser.</div>'
    +'<div class="row">'
    +'<div><label class="fld">Order / compare treks by</label><select onchange="onPref(\'sort\',this.value)">'+optHtml(prefs.sort,[['fav','Favourite order'],['name','Name (A–Z)'],['district','District'],['open','Most availability first']])+'</select></div>'
    +'<div><label class="fld">Group</label><select onchange="onPref(\'group\',this.value)">'+optHtml(prefs.group,[['none','No grouping'],['district','By district']])+'</select></div>'
    +'<div><label class="fld">Row density</label><select onchange="onPref(\'density\',this.value)">'+optHtml(prefs.density,[['comfortable','Comfortable'],['compact','Compact']])+'</select></div>'
    +'</div></div>'
    +'<div class="card"><div class="ptitle">Data</div><div class="psub">Applies to everyone viewing this dashboard.</div>'
    +'<div class="row"><div><label class="fld">Window (days ahead)</label><input type="number" id="setWin" min="1" max="60" value="'+latest.window_days+'"></div>'
    +'<div><label class="fld">Sweep interval (seconds)</label><input type="number" id="setCad" min="5" max="600" value="'+(latest.cadence||40)+'"></div>'
    +'<div style="flex:0"><button class="btn" onclick="saveServerSettings()">Apply</button></div></div>'
    +'<div class="msg" id="setMsg" style="color:var(--green)"></div></div>';
}
function setTheme(t){prefs.theme=t;savePrefs();applyTheme();renderSettings();}
function setAccent(a){prefs.accent=a;savePrefs();applyTheme();renderSettings();}
function setLight(v){prefs.light=v;savePrefs();applyTheme();renderSettings();}
function setFont(f){prefs.font=f;savePrefs();applyTheme();renderSettings();}
function setFontScale(s){prefs.fontScale=s;savePrefs();applyTheme();renderSettings();}
function saveServerSettings(){
  var win=+document.getElementById('setWin').value,cad=+document.getElementById('setCad').value;
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({window_days:win,cadence:cad})})
  .then(function(r){return r.json();}).then(function(){document.getElementById('setMsg').textContent='Saved. Board updates on the next sweep.';});
}

/* ---------- window segmented control ---------- */
document.getElementById('winSeg').addEventListener('click',function(e){
  var b=e.target.closest('button');if(!b)return;
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({window_days:+b.dataset.d})});
});

/* ---------- live link ---------- */
var lastEventAt=0;
function connectStream(){var es=new EventSource('/api/stream');
  es.onmessage=function(ev){lastEventAt=Date.now();try{render(JSON.parse(ev.data));}catch(e){}};
  es.onerror=function(){document.getElementById('dot').className='dot dead';};}
function pollFallback(){if(Date.now()-lastEventAt>20000){fetch('/api/state').then(function(r){return r.json();}).then(render).catch(function(){var d=document.getElementById('dot');d.className='dot dead';document.getElementById('statusTxt').textContent='Server unreachable';});}}
connectStream();setInterval(pollFallback,5000);
fetch('/api/state').then(function(r){return r.json();}).then(render);
</script>
</body>
</html>
"""

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

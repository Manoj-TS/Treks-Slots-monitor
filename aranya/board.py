"""Assembling one user's board out of the shared cell cache, plus the
per-user serialized-payload cache that feeds SSE."""

import json
import threading
from datetime import date, timedelta

from . import config, state, views

# ── Weekend window helpers ────────────────────────────────────────────────── #

def window_weekends(days: int):
    """All Saturdays/Sundays from today through today+days (inclusive).

    Note every window starts at today, so window_weekends(7) is a strict prefix
    of window_weekends(30). That is what lets one sweep at the widest window
    serve every user, each slicing the columns they asked for.
    """
    today = date.today()
    out = []
    for i in range(days + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:      # 5 = Saturday, 6 = Sunday
            out.append(d)
    return out


def weekend_columns(days: int):
    cols = []
    for d in window_weekends(days):
        cols.append({
            "iso": d.isoformat(),
            "day": d.day,
            "weekday": d.strftime("%a"),
            "month": d.strftime("%b"),
            "group": d.isocalendar()[1],           # Sat & Sun share an ISO week
        })
    return cols

# ── Trek config coercion ──────────────────────────────────────────────────── #

def coerce_trek(d):
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


def resolve_trek(tid: int, view=None):
    """Name + district for a trek, preferring the user's own copy, then the
    discovered catalog, then the operator's configured catalog."""
    if view:
        for f in view.favourites:
            if f.trek_id == tid:
                return f.name, f.district_id
    with state.lock:
        src = next((t for t in state.registry["treks"] if t["id"] == tid), None)
        cfg = state.trek_configs.get(str(tid))
    if src:
        return src["name"], src["district_id"]
    if cfg:
        return cfg["name"], cfg.get("district_id")
    return None, None

# ── Per-user state assembly ───────────────────────────────────────────────── #

def build_state_for(view) -> dict:
    cols = weekend_columns(view.window_days)
    isos = [c["iso"] for c in cols]
    today = date.today()

    with state.lock:
        rows = []
        for f in view.favourites:
            cells = {}
            for iso in isos:
                c = state.board_state.get(f"{f.trek_id}_{iso}")
                if c:
                    cells[iso] = c
            rows.append({"trek_id": f.trek_id, "name": f.name,
                         "district_id": f.district_id,
                         "district_name": config.district_name(f.district_id),
                         "cells": cells})
        watch = []
        for w in view.watch:
            watch.append({"trek_id": w.trek_id, "name": w.name,
                          "district_id": w.district_id,
                          "district_name": config.district_name(w.district_id),
                          "date": w.date,
                          "cell": state.board_state.get(f"{w.trek_id}_{w.date}")})
        # `error` is a global fault (portal down); `hint` is about this user.
        error = state.stats["error"] or state.registry["error"]
        stats_snapshot = {"cycle": state.stats["cycle"],
                          "last_update": state.stats["last_update"]}
        catalog_ready = state.registry["ready"]

    hint = None
    if not view.favourites:
        hint = "Add treks under the Favourites tab to build your board."

    return {
        "ready": True,
        "catalog_ready": catalog_ready,
        "error": error,
        "hint": hint,
        "cycle": stats_snapshot["cycle"],
        "last_update": stats_snapshot["last_update"],
        "window_days": view.window_days,
        "cadence": state.settings["cadence"],
        "window_start": today.isoformat(),
        "window_end": (today + timedelta(days=view.window_days)).isoformat(),
        "weekends": cols,
        "rows": rows,
        "favourites": [{"trek_id": f.trek_id, "name": f.name,
                        "district_id": f.district_id,
                        "district_name": config.district_name(f.district_id)}
                       for f in view.favourites],
        "watch": watch,
    }


# Per-user payload cache: user_id -> (shared_version, user_version, payload, last_read).
# Between sweeps every SSE wakeup is a cache hit, so this stays O(1) per viewer
# per second rather than re-serializing the board for everyone every second.
_cache: dict[int, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_IDLE_SECONDS = 300
_CACHE_MAX = 500


def payload_for(user_id: int) -> str | None:
    """Serialized board for one user, rebuilt only when the shared sweep or
    that user's own view actually changed. None if the user has no view."""
    import time
    view = views.get(user_id)
    if view is None:
        return None

    with state._snapshot_lock:
        shared_version = state._state_version

    with _cache_lock:
        hit = _cache.get(user_id)
        if hit and hit[0] == shared_version and hit[1] == view.version:
            _cache[user_id] = (hit[0], hit[1], hit[2], time.time())
            return hit[2]

    payload = json.dumps(build_state_for(view), sort_keys=True)

    with _cache_lock:
        _cache[user_id] = (shared_version, view.version, payload, time.time())
        if len(_cache) > _CACHE_MAX:
            cutoff = time.time() - _CACHE_IDLE_SECONDS
            for uid in [u for u, v in _cache.items() if v[3] < cutoff]:
                _cache.pop(uid, None)
    return payload

"""Assembling the board/state payload from `state`, plus the serialized-payload cache."""

import json
from datetime import date, timedelta

from . import config, state

# ── Weekend window helpers ────────────────────────────────────────────────── #

def window_weekends(days=None):
    """All Saturdays/Sundays from today through today+days (inclusive)."""
    if days is None:
        days = state.settings["window_days"]
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


def _resolve_trek(tid):
    with state.lock:
        fav = next((f for f in state.favourites if f["trek_id"] == tid), None)
        src = next((t for t in state.registry["treks"] if t["id"] == tid), None)
        cfg = state.trek_configs.get(str(tid))
    if fav:
        return fav["name"], fav["district_id"]
    if src:
        return src["name"], src["district_id"]
    if cfg:
        return cfg["name"], cfg.get("district_id")
    return None, None

# ── State assembly ────────────────────────────────────────────────────────── #

def _build_board():
    cols = weekend_columns()
    isos = [c["iso"] for c in cols]
    rows = []
    for f in state.favourites:
        cells = {}
        for iso in isos:
            c = state.board_state.get(f"{f['trek_id']}_{iso}")
            if c:
                cells[iso] = c
        rows.append({"trek_id": f["trek_id"], "name": f["name"],
                     "district_id": f.get("district_id"),
                     "district_name": f.get("district_name"), "cells": cells})
    return cols, rows


def _watch_public():
    out = []
    for w in state.custom_watch:
        cell = state.board_state.get(f"{w['trek_id']}_{w['date']}")
        out.append({**w, "cell": cell})
    return out


def build_state():
    with state.lock:
        cols, rows = _build_board()
        today = date.today()
        end = today + timedelta(days=state.settings["window_days"])
        base = {
            "ready": True,
            "catalog_ready": state.registry["ready"],
            "error": state.stats["error"] or state.registry["error"],
            "cycle": state.stats["cycle"], "last_update": state.stats["last_update"],
            "window_days": state.settings["window_days"],
            "cadence": state.settings["cadence"],
            "window_start": today.isoformat(),
            "window_end": end.isoformat(),
            "weekends": cols,
            "rows": rows,
            "favourites": list(state.favourites),
            "watch": _watch_public(),
        }
    return base


# Snapshot cache — see the comment on state._state_version. Kept here, not in
# state.py, because it depends on build_state() and state.py must not import
# board.py (board.py already imports state.py; the reverse would be circular).
_snapshot_version = -1
_snapshot_payload = None


def current_payload():
    """Serialized state, rebuilt only when something actually changed.

    Tagging the cache with the version read *before* building means a change that
    lands mid-build simply leaves the cache stale, so the next caller rebuilds —
    never serves newer data under an older tag.
    """
    global _snapshot_version, _snapshot_payload
    with state._snapshot_lock:
        version = state._state_version
        if _snapshot_version == version and _snapshot_payload is not None:
            return _snapshot_payload
    payload = json.dumps(build_state(), sort_keys=True)
    with state._snapshot_lock:
        if version >= _snapshot_version:
            _snapshot_version, _snapshot_payload = version, payload
    return payload

"""Shared in-process state. Single-tenant today (one board, shared by every
viewer) — see the project plan for the multi-tenant refactor this feeds into.

Other modules must access these via `state.<name>`, never `from .state import
favourites` — the latter binds a local reference that goes stale the moment
this module reassigns the name (e.g. on a DELETE that rebuilds the list).
"""

import threading

from . import config

registry = {"treks": [], "ready": False, "error": None}
trek_configs = {}        # global operator catalog, not per-user
# "{trek_id}_{YYYY-MM-DD}" -> cell dict. Deliberately global and shared: a cell
# is a fact about the world, identical for every user, so one sweep serves all.
board_state = {}
# Only genuinely global settings live here. window_days is per-user (see
# views.UserView); cadence is global and admin-only, because one user must not
# be able to set the poll rate against the government portal for everyone.
settings = {"cadence": config.BOARD_CYCLE_DEFAULT}
stats = {"cycle": 0, "last_update": None, "error": None, "worker_alive": False,
         "targets": 0, "skipped": 0}

lock = threading.Lock()
state_changed = threading.Event()

# Version counter for the payload cache in board.py. state_changed is set/cleared
# back-to-back, so each SSE viewer wakes about once a second; without a cache tagged
# by this version, every viewer would rebuild and re-serialize the whole board under
# `lock` every second — O(viewers) work per second.
_snapshot_lock = threading.Lock()
_state_version = 0


def mark_changed():
    global _state_version
    with _snapshot_lock:
        _state_version += 1
    state_changed.set()
    state_changed.clear()

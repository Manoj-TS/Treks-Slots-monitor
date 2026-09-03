"""Shared in-process state. Single-tenant today (one board, shared by every
viewer) — see the project plan for the multi-tenant refactor this feeds into.

Other modules must access these via `state.<name>`, never `from .state import
favourites` — the latter binds a local reference that goes stale the moment
this module reassigns the name (e.g. on a DELETE that rebuilds the list).
"""

import threading

from . import config

registry = {"treks": [], "ready": False, "error": None}
trek_configs = {}
favourites = []          # [{trek_id, name, district_id, district_name}]
custom_watch = []        # [{trek_id, name, district_id, district_name, date}]
board_state = {}         # "{trek_id}_{YYYY-MM-DD}" -> cell dict
settings = {"window_days": config.WINDOW_DAYS_DEFAULT, "cadence": config.BOARD_CYCLE_DEFAULT}
stats = {"cycle": 0, "last_update": None, "error": None, "worker_alive": False}

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

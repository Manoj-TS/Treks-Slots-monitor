"""The polling worker.

Fetches one cell at a time at a steady rate rather than bursting, and decides
what to fetch next from a per-cell due time rather than sweeping everything on
a fixed cycle.

Two reasons for the shape:

* A burst of 8 concurrent connections every few seconds looks far more like
  something worth rate-limiting than a steady trickle does. Rate limiters and
  WAFs trigger on burstiness, not on daily totals, and losing this IP means
  every paying customer sees an empty board.
* Most cells hold answers that cannot have changed. A sold-out date is terminal
  (this portal never cancels or re-releases tickets) and an unreleased date
  stays unreleased until the next daily release. Re-asking those every cycle
  was the bulk of the traffic, and it was worst in peak season when most cells
  are sold out.

The saving is spent where it matters: *open* cells — the only ones customers
act on — are still refreshed on the operator-set cadence.
"""

import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

from . import board, config, portal, ratelimit, state, storage, views

PORTAL_BUCKET = ratelimit.TokenBucket(config.SWEEP_RPS, burst=10)

# key -> monotonic timestamp when this cell should next be fetched.
_due: dict[str, float] = {}
# Cells a user explicitly asked to refresh. Drained before the due map, so a
# "Refresh now" click is served ahead of routine work.
_priority: deque[str] = deque()
# user_id -> monotonic timestamp of their last accepted force refresh.
_last_force: dict[int, float] = {}
_sched_lock = threading.Lock()

_last_rollover: date | None = None


# ── Scheduling ────────────────────────────────────────────────────────────── #

def _interval_for(cell) -> float:
    """How long this cell's answer stays trustworthy."""
    if not cell:
        return 0.0                                   # never fetched: do it now
    if not cell.get("released"):
        return config.UNRELEASED_INTERVAL            # opens on the daily cycle
    if cell.get("available", 0) <= 0:
        return config.SOLD_OUT_INTERVAL              # terminal; poll for corrections
    with state.lock:
        return max(20, int(state.settings["cadence"]))   # open: the live number


def reschedule(key: str, cell) -> None:
    with _sched_lock:
        _due[key] = time.monotonic() + _interval_for(cell)


def sync_schedule(targets: dict) -> None:
    """Add newly-watched cells (due immediately) and forget dropped ones."""
    with _sched_lock:
        for key in targets:
            if key not in _due:
                _due[key] = 0.0                      # never seen: highest priority
        for key in [k for k in _due if k not in targets]:
            _due.pop(key, None)


def flush_unreleased() -> int:
    """Mark every unreleased cell due now.

    Called when the calendar date changes. That is the moment the portal's
    daily release happens *and* the moment the rolling window shifts, so one
    trigger covers both — no release hour has to be hardcoded.
    """
    n = 0
    with state.lock:
        unreleased = [k for k, c in state.board_state.items()
                      if c and not c.get("released")]
    with _sched_lock:
        for key in unreleased:
            if key in _due:
                _due[key] = 0.0
                n += 1
    return n


def next_cell(targets: dict) -> tuple[str | None, float]:
    """The cell to fetch now, and how long to wait if none is due.

    A linear scan: at the MAX_TARGETS ceiling of 1500 that is a few thousand
    comparisons a second, which is nothing, and it avoids keeping a heap in
    sync with a set that changes underneath it.
    """
    now = time.monotonic()
    with _sched_lock:
        while _priority:
            key = _priority.popleft()
            if key in targets:
                return key, 0.0
        best, best_due = None, None
        for key, due in _due.items():
            if key not in targets:
                continue
            if best_due is None or due < best_due:
                best, best_due = key, due
        if best is None:
            return None, 1.0
        if best_due <= now:
            return best, 0.0
        return None, min(best_due - now, 5.0)


def due_count(targets: dict) -> int:
    now = time.monotonic()
    with _sched_lock:
        return sum(1 for k, d in _due.items() if k in targets and d <= now)


def behind_seconds(targets: dict) -> float:
    """How overdue the most overdue cell is — surfaces demand exceeding SWEEP_RPS."""
    now = time.monotonic()
    with _sched_lock:
        overdue = [now - d for k, d in _due.items() if k in targets and d < now]
    return max(overdue) if overdue else 0.0


def next_due_seconds(keys) -> float | None:
    """Seconds until the soonest scheduled check among these cells.

    Returned as a *duration*, never a raw `_due` value: those are
    time.monotonic() floats — process-relative, with no epoch — so they are
    meaningless to a browser. A duration also sidesteps client clock skew.
    """
    now = time.monotonic()
    with _sched_lock:
        upcoming = [_due[k] for k in keys if k in _due]
    if not upcoming:
        return None
    return max(0.0, min(upcoming) - now)


# ── Force refresh ─────────────────────────────────────────────────────────── #

def request_refresh(user_id: int, keys: list[str]) -> tuple[int, float]:
    """Queue a user's own cells for immediate re-check.

    Returns (cells_queued, seconds_until_next_allowed). This can only reorder
    work the sweeper was already going to do — it never adds a target, and it
    draws from the same token bucket — so it cannot increase portal load.
    """
    now = time.monotonic()
    with _sched_lock:
        last = _last_force.get(user_id)
        if last is not None and now - last < config.FORCE_REFRESH_COOLDOWN:
            return 0, config.FORCE_REFRESH_COOLDOWN - (now - last)
        _last_force[user_id] = now
        queued = 0
        already = set(_priority)
        for key in keys[:config.MAX_FORCE_CELLS]:
            if key in _due and key not in already:
                _priority.append(key)
                queued += 1
    return queued, float(config.FORCE_REFRESH_COOLDOWN)


def cooldown_remaining(user_id: int) -> float:
    with _sched_lock:
        last = _last_force.get(user_id)
    if last is None:
        return 0.0
    return max(0.0, config.FORCE_REFRESH_COOLDOWN - (time.monotonic() - last))


# ── Target set ────────────────────────────────────────────────────────────── #

def board_targets() -> dict:
    """Union of every paying user's (favourites x their weekend window), plus
    their pinned dates. Keyed '{trek_id}_{iso}' with no user identity in the
    key, so N users watching the same trek cost exactly one fetch."""
    targets = {}
    today = date.today()
    active = views.active()
    if not active:
        return targets

    widest = max(v.window_days for v in active)
    all_weekends = board.window_weekends(widest)

    for v in active:
        # Every window starts today, so a narrower one is a prefix.
        cutoff = today + timedelta(days=v.window_days)
        for f in v.favourites:
            if f.district_id is None:
                continue
            for d in all_weekends:
                if d > cutoff:
                    break
                key = f"{f.trek_id}_{d.isoformat()}"
                targets[key] = {"trek_id": f.trek_id, "district_id": f.district_id,
                                "date": d.isoformat()}
        for w in v.watch:
            if w.district_id is None:
                continue
            try:
                d = datetime.strptime(w.date, "%Y-%m-%d").date()
            except Exception:
                continue
            if d < today:
                continue
            targets[f"{w.trek_id}_{w.date}"] = {
                "trek_id": w.trek_id, "district_id": w.district_id, "date": w.date}

    if len(targets) > config.MAX_TARGETS:
        print(f"[Sweeper] {len(targets)} targets exceeds MAX_TARGETS="
              f"{config.MAX_TARGETS}; truncating. Raise SWEEP_RPS or the limit.")
        targets = dict(list(targets.items())[:config.MAX_TARGETS])
    return targets


def user_cell_keys(view) -> list[str]:
    """The cells on one user's board — what a force refresh acts on."""
    if view is None:
        return []
    today = date.today()
    keys = []
    for d in board.window_weekends(view.window_days):
        for f in view.favourites:
            if f.district_id is not None:
                keys.append(f"{f.trek_id}_{d.isoformat()}")
    for w in view.watch:
        try:
            if datetime.strptime(w.date, "%Y-%m-%d").date() >= today:
                keys.append(f"{w.trek_id}_{w.date}")
        except Exception:
            continue
    return keys


def gc_board_state(active_keys: set, today: date) -> int:
    """Drop past dates and cells nobody watches any more. Without this the
    cache grows without limit as the window rolls forward across a season."""
    dropped = 0
    with state.lock:
        for key in list(state.board_state.keys()):
            iso = key.split("_", 1)[-1]
            if iso < today.isoformat() or key not in active_keys:
                state.board_state.pop(key, None)
                dropped += 1
    return dropped


def _significant(old, new) -> bool:
    """Did this fetch actually change what a viewer would see?

    Most fetches confirm "still sold out" and change nothing but the timestamp.
    Republishing those would invalidate every viewer's payload cache for no
    visible reason, which on a drip means several needless re-serialisations a
    second — strictly worse than the burst design it replaces.
    """
    if old is None or new is None:
        return True
    return (old.get("released") != new.get("released")
            or old.get("available") != new.get("available")
            or old.get("capacity") != new.get("capacity"))


# ── The loop ──────────────────────────────────────────────────────────────── #

def worker_loop():
    global _last_rollover
    session = portal.new_session()
    csrf = None
    bad = 0
    cycle = state.stats["cycle"]
    last_reload = 0.0
    last_publish = 0.0
    last_heartbeat = 0.0
    last_stats = 0.0
    dirty = False
    targets: dict = {}
    fetched_since_pass = 0
    _last_rollover = date.today()

    while True:
        try:
            with state.lock:
                state.stats["worker_alive"] = True

            now_wall = time.time()

            # Who wants what. Also the moment new dates enter the window.
            if now_wall - last_reload > config.VIEW_RELOAD_SECONDS:
                storage.reload_views()
                targets = board_targets()
                sync_schedule(targets)
                gc_board_state(set(targets.keys()), date.today())
                last_reload = now_wall
                with state.lock:
                    state.stats["targets"] = len(targets)

            # The portal releases on a daily cycle, so the date changing is
            # when unreleased answers stop being trustworthy.
            today = date.today()
            if today != _last_rollover:
                _last_rollover = today
                targets = board_targets()
                sync_schedule(targets)
                n = flush_unreleased()
                gc_board_state(set(targets.keys()), today)
                print(f"[Sweeper] Date rolled over to {today}; "
                      f"re-checking {n} unreleased cells.")

            if not targets:
                targets = board_targets()
                sync_schedule(targets)
                if not targets:
                    with state.lock:
                        state.stats["error"] = None
                        state.stats["targets"] = 0
                    time.sleep(5)
                    continue

            if not csrf:
                csrf = portal.fetch_csrf(session)
                if not csrf:
                    bad += 1
                    with state.lock:
                        state.stats["error"] = "Portal connection failed — retrying…"
                    state.mark_changed()
                    if bad >= config.SESSION_RESET_AFTER:
                        session = portal.new_session()
                        bad = 0
                    time.sleep(min(3 * (bad or 1), 15))
                    continue

            key, wait = next_cell(targets)
            if key is None:
                # Nothing due. Publish anything pending, then idle briefly.
                if dirty and time.time() - last_publish >= config.PUBLISH_MIN_INTERVAL:
                    state.mark_changed()
                    dirty, last_publish = False, time.time()
                time.sleep(min(wait, 2.0))
                continue

            # One request in flight, paced by the shared bucket. The calendar
            # endpoint draws from the same budget.
            PORTAL_BUCKET.acquire()
            cell = portal.check_target(session, csrf, targets[key])
            ok = cell.pop("_transport_ok", True)

            if not ok:
                bad += 1
                csrf = None
                if bad >= config.SESSION_RESET_AFTER:
                    session = portal.new_session()
                    bad = 0
                with state.lock:
                    state.stats["error"] = "Portal not responding — refreshing session…"
                state.mark_changed()
                # Retry this cell soon rather than losing its turn.
                with _sched_lock:
                    _due[key] = time.monotonic() + 5
                time.sleep(min(3 * (bad or 1), 15))
                continue

            bad = 0
            with state.lock:
                changed = _significant(state.board_state.get(key), cell)
                state.board_state[key] = cell
                state.stats["error"] = None
                if changed:
                    # "last_update" means the last time the BOARD CHANGED, not
                    # the last time we fetched anything. On a drip the latter
                    # advances ~3x/second, so "updated just now" would be
                    # permanent and would tell a customer nothing.
                    state.stats["last_update"] = datetime.now().isoformat()
                    state.stats["content_version"] += 1
            reschedule(key, cell)
            dirty = dirty or changed

            fetched_since_pass += 1
            if fetched_since_pass >= max(1, len(targets)):
                cycle += 1
                fetched_since_pass = 0
                with state.lock:
                    state.stats["cycle"] = cycle

            now = time.time()
            # These are two full scans of _due under the scheduler lock. Doing
            # them per fetch (~3x/s) was wasted work — recompute a few times a
            # minute instead; they only feed a display.
            if now - last_stats >= 5.0:
                last_stats = now
                with state.lock:
                    state.stats["skipped"] = len(targets) - due_count(targets)
                    state.stats["behind"] = round(behind_seconds(targets))
            if dirty and now - last_publish >= config.PUBLISH_MIN_INTERVAL:
                state.mark_changed()
                dirty, last_publish, last_heartbeat = False, now, now
            elif now - last_heartbeat >= config.PUBLISH_HEARTBEAT:
                # Nothing changed, but the client's countdown needs a fresh
                # next_check or it would run to zero and sit there on a quiet
                # board. content_version is deliberately NOT bumped, so the UI
                # doesn't pulse as though something happened.
                state.mark_changed()
                last_heartbeat = now

        except Exception as e:
            print(f"[Worker Error] {e}")
            with state.lock:
                state.stats["error"] = str(e)
            state.mark_changed()
            time.sleep(4)


def supervised_worker():
    while True:
        t = threading.Thread(target=worker_loop, daemon=True)
        t.start()
        t.join()
        with state.lock:
            state.stats["worker_alive"] = False
            state.stats["error"] = "Worker crashed — restarting…"
        state.mark_changed()
        print("[Supervisor] Worker died. Restarting in 3s…")
        time.sleep(3)

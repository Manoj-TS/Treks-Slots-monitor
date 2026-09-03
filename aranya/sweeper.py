"""The polling worker: decides what to check across all paying users, sweeps
it under a global rate limit, and writes results into the shared cell cache."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import board, config, portal, ratelimit, state, storage, views

PORTAL_BUCKET = ratelimit.TokenBucket(config.SWEEP_RPS, burst=10)

# A released cell with zero seats is terminal: this portal never cancels or
# re-releases tickets, so it cannot go back up. Re-checking it every cycle is
# the single biggest waste of portal budget, and it is worst in peak season
# when most cells are sold out. Check it occasionally anyway, to catch a
# correction on the portal's side.
SOLD_OUT_EVERY = 10

_sweep_counter = 0


def _is_terminal(cell) -> bool:
    return bool(cell) and cell.get("released") and cell.get("available", 0) == 0


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


def due_targets(targets: dict, sweep_no: int) -> tuple[dict, int]:
    """Drop terminal (sold-out) cells except every Nth sweep."""
    if sweep_no % SOLD_OUT_EVERY == 0:
        return targets, 0
    due, skipped = {}, 0
    with state.lock:
        for key, tgt in targets.items():
            if _is_terminal(state.board_state.get(key)):
                skipped += 1
            else:
                due[key] = tgt
    return due, skipped


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


def _fetch(session, csrf, tgt):
    # Rate-limited at the point of the outbound call, so both the sweep and
    # on-demand calendar lookups draw from the same budget.
    PORTAL_BUCKET.acquire()
    return portal.check_target(session, csrf, tgt)


def worker_loop():
    global _sweep_counter
    session = portal.new_session()
    csrf = None
    bad_cycles = 0
    cycle = state.stats["cycle"]
    last_reload = 0.0

    while True:
        try:
            with state.lock:
                state.stats["worker_alive"] = True

            # Refresh who wants what, before deciding what to sweep.
            if time.time() - last_reload > config.VIEW_RELOAD_SECONDS:
                storage.reload_views()
                last_reload = time.time()

            targets = board_targets()
            if not targets:
                with state.lock:
                    state.stats["error"] = None
                    state.stats["targets"] = 0
                state.mark_changed()
                time.sleep(5)
                continue

            if not csrf:
                csrf = portal.fetch_csrf(session)
                if not csrf:
                    bad_cycles += 1
                    with state.lock:
                        state.stats["error"] = "Portal connection failed — retrying…"
                    state.mark_changed()
                    if bad_cycles >= config.SESSION_RESET_AFTER:
                        session = portal.new_session()
                        bad_cycles = 0
                    time.sleep(min(3 * (bad_cycles or 1), 15))
                    continue

            _sweep_counter += 1
            due, skipped = due_targets(targets, _sweep_counter)
            keys = list(due.keys())
            tgts = [due[k] for k in keys]

            results = []
            if tgts:
                with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
                    results = list(ex.map(lambda t: _fetch(session, csrf, t), tgts))

            if results and all(not r.get("_transport_ok") for r in results):
                csrf = None
                bad_cycles += 1
                if bad_cycles >= config.SESSION_RESET_AFTER:
                    session = portal.new_session()
                    bad_cycles = 0
                with state.lock:
                    state.stats["error"] = "Portal not responding — refreshing session…"
                state.mark_changed()
                time.sleep(min(3 * bad_cycles, 15))
                continue

            bad_cycles = 0
            cycle += 1
            with state.lock:
                for k, cell in zip(keys, results):
                    cell.pop("_transport_ok", None)
                    state.board_state[k] = cell
                state.stats["cycle"] = cycle
                state.stats["last_update"] = datetime.now().isoformat()
                state.stats["error"] = None
                state.stats["targets"] = len(targets)
                state.stats["skipped"] = skipped
                cadence = state.settings["cadence"]

            gc_board_state(set(targets.keys()), date.today())
            state.mark_changed()
            time.sleep(max(5, cadence))
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

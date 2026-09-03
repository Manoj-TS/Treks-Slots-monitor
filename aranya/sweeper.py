"""Board polling worker: decides what to check, sweeps it, writes results into state.board_state."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from . import board, config, portal, state


def board_targets():
    targets = {}
    today = date.today()
    with state.lock:
        favs = list(state.favourites)
        watches = list(state.custom_watch)
        weekends = board.window_weekends()
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
    session = portal.new_session()
    csrf = None
    bad_cycles = 0
    cycle = state.stats["cycle"]

    while True:
        try:
            with state.lock:
                state.stats["worker_alive"] = True

            targets = board_targets()
            if not targets:
                with state.lock:
                    state.stats["error"] = "No favourites yet. Add treks under the Favourites tab."
                state.mark_changed()
                time.sleep(3)
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

            keys = list(targets.keys())
            tgts = [targets[k] for k in keys]
            with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
                results = list(ex.map(lambda t: portal.check_target(session, csrf, t), tgts))

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
                cadence = state.settings["cadence"]
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

"""Best-effort trek catalog discovery. The board does not depend on this finishing."""

import time

from . import config, portal, state


def discover_all_treks(session, csrf):
    treks = []
    for did in range(1, 36):
        for t in portal.fetch_treks_for_district(session, csrf, did):
            if t.get("id") and t.get("is_active", 1) == 1:
                tdid = int(t.get("district_id", did))
                treks.append({"id": int(t["id"]), "name": t.get("name") or f"Trek {t['id']}",
                              "district_id": tdid, "district_name": config.district_name(tdid)})
    seen, unique = set(), []
    for t in treks:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)
    unique.sort(key=lambda x: (x["district_name"], x["name"]))
    return unique


def discovery_loop():
    session = portal.new_session()
    while not state.registry["ready"]:
        csrf = portal.fetch_csrf(session)
        if csrf:
            items = discover_all_treks(session, csrf)
            if items:
                with state.lock:
                    state.registry["treks"] = items
                    state.registry["ready"] = True
                    state.registry["error"] = None
                print(f"[Discovery] Mapped {len(items)} treks.")
                state.mark_changed()
                return
        time.sleep(5)

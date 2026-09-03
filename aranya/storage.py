"""Persistence for per-user board preferences and the global trek catalog.

Postgres is the source of truth. There is no JSON fallback any more: the flat
files could only ever represent one user, so they cannot express a multi-tenant
board. Resilience against a database blip comes instead from the in-memory view
registry (see views.py), which keeps the sweep and every connected viewer
working from the last known state until Postgres returns.

The legacy JSON files are still read once, to import them into an empty
database, and are otherwise left untouched on disk as a rollback artifact.
"""

import json
import os

from . import config, db, state, views

_db_ready = False


def db_ready() -> bool:
    return _db_ready


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Storage] load {path}: {e}")
    return default


# ── Catalog + global settings ─────────────────────────────────────────────── #

def read_trek_configs() -> dict:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT trek_id, name, district_id, timeslot_mapping_id, timeslot_id"
            " FROM trek_configs ORDER BY trek_id").fetchall()
    return {str(r[0]): {"trek_id": r[0], "name": r[1], "district_id": r[2],
                        "timeslot_mapping_id": r[3], "timeslot_id": r[4]} for r in rows}


def write_trek_configs(cfgs: dict) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM trek_configs")
        for cfg in cfgs.values():
            conn.execute(
                "INSERT INTO trek_configs"
                " (trek_id, name, district_id, timeslot_mapping_id, timeslot_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (cfg["trek_id"], cfg["name"], cfg.get("district_id"),
                 cfg.get("timeslot_mapping_id"), cfg.get("timeslot_id")))


def read_cadence() -> int:
    with db.connection() as conn:
        r = conn.execute("SELECT value FROM app_settings WHERE key = 'cadence'").fetchone()
    return int(r[0]) if r else config.BOARD_CYCLE_DEFAULT


def write_cadence(seconds: int) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('cadence', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (json.dumps(int(seconds)),))


# ── Per-user reads ────────────────────────────────────────────────────────── #

def read_view_rows() -> list[dict]:
    """Every active user's view, in three queries regardless of user count."""
    with db.connection() as conn:
        users = conn.execute(
            "SELECT u.id, u.is_admin, u.access_until, COALESCE(s.window_days, %s)"
            " FROM users u LEFT JOIN user_settings s ON s.user_id = u.id"
            " WHERE u.status = 'active'", (config.WINDOW_DAYS_DEFAULT,)).fetchall()
        favs = conn.execute(
            "SELECT user_id, trek_id, name, district_id FROM user_favourites"
            " ORDER BY user_id, position, trek_id").fetchall()
        watches = conn.execute(
            "SELECT user_id, trek_id, name, district_id, watch_date FROM user_watch"
            " ORDER BY user_id, watch_date, trek_id").fetchall()

    by_user_favs: dict[int, list] = {}
    for uid, tid, name, did in favs:
        by_user_favs.setdefault(uid, []).append(
            {"trek_id": tid, "name": name, "district_id": did})
    by_user_watch: dict[int, list] = {}
    for uid, tid, name, did, d in watches:
        by_user_watch.setdefault(uid, []).append(
            {"trek_id": tid, "name": name, "district_id": did, "date": d.isoformat()})

    return [{"user_id": uid, "is_admin": is_admin, "access_until": until,
             "window_days": wd,
             "favourites": by_user_favs.get(uid, []),
             "watch": by_user_watch.get(uid, [])}
            for uid, is_admin, until, wd in users]


def read_view_row(user_id: int) -> dict | None:
    with db.connection() as conn:
        u = conn.execute(
            "SELECT u.id, u.is_admin, u.access_until, COALESCE(s.window_days, %s)"
            " FROM users u LEFT JOIN user_settings s ON s.user_id = u.id"
            " WHERE u.id = %s AND u.status = 'active'",
            (config.WINDOW_DAYS_DEFAULT, user_id)).fetchone()
        if not u:
            return None
        favs = conn.execute(
            "SELECT trek_id, name, district_id FROM user_favourites"
            " WHERE user_id = %s ORDER BY position, trek_id", (user_id,)).fetchall()
        watches = conn.execute(
            "SELECT trek_id, name, district_id, watch_date FROM user_watch"
            " WHERE user_id = %s ORDER BY watch_date, trek_id", (user_id,)).fetchall()
    return {"user_id": u[0], "is_admin": u[1], "access_until": u[2], "window_days": u[3],
            "favourites": [{"trek_id": r[0], "name": r[1], "district_id": r[2]} for r in favs],
            "watch": [{"trek_id": r[0], "name": r[1], "district_id": r[2],
                       "date": r[3].isoformat()} for r in watches]}


# ── Per-user writes ───────────────────────────────────────────────────────── #

def add_favourite(user_id: int, trek_id: int, name: str, district_id: int) -> None:
    with db.connection() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(max(position) + 1, 0) FROM user_favourites WHERE user_id = %s",
            (user_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO user_favourites (user_id, trek_id, name, district_id, position)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, trek_id) DO NOTHING",
            (user_id, trek_id, name, district_id, nxt))


def remove_favourite(user_id: int, trek_id: int) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM user_favourites WHERE user_id = %s AND trek_id = %s",
                     (user_id, trek_id))


def reorder_favourites(user_id: int, order: list[int]) -> None:
    with db.connection() as conn:
        for pos, tid in enumerate(order):
            conn.execute(
                "UPDATE user_favourites SET position = %s WHERE user_id = %s AND trek_id = %s",
                (pos, user_id, tid))


def count_favourites(user_id: int) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) FROM user_favourites WHERE user_id = %s",
                            (user_id,)).fetchone()[0]


def add_watch(user_id: int, trek_id: int, name: str, district_id: int, day: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO user_watch (user_id, trek_id, name, district_id, watch_date)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (user_id, trek_id, watch_date) DO NOTHING",
            (user_id, trek_id, name, district_id, day))


def remove_watch(user_id: int, trek_id: int, day: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "DELETE FROM user_watch WHERE user_id = %s AND trek_id = %s AND watch_date = %s",
            (user_id, trek_id, day))


def count_watch(user_id: int) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) FROM user_watch WHERE user_id = %s",
                            (user_id,)).fetchone()[0]


def set_window_days(user_id: int, days: int) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, window_days) VALUES (%s, %s)"
            " ON CONFLICT (user_id) DO UPDATE SET window_days = EXCLUDED.window_days,"
            " updated_at = now()", (user_id, days))


# ── Startup ───────────────────────────────────────────────────────────────── #

def _ensure_owner():
    with db.connection() as conn:
        if conn.execute("SELECT 1 FROM users WHERE id = %s",
                        (config.OWNER_USER_ID,)).fetchone():
            return
        # access_until stays NULL: is_admin bypasses the paywall, and a literal
        # 'infinity' timestamptz cannot be loaded into a Python datetime.
        conn.execute(
            "INSERT INTO users (id, email, email_verified, name, is_admin)"
            " VALUES (%s, %s, true, 'Owner', true) ON CONFLICT (id) DO NOTHING",
            (config.OWNER_USER_ID, config.OWNER_EMAIL))
        conn.execute("SELECT setval('users_id_seq', GREATEST((SELECT max(id) FROM users), 1))")
        print(f"[Storage] Seeded owner account (id={config.OWNER_USER_ID}).")


def _import_legacy_if_empty():
    """First run against an empty database: adopt the old single-user JSON
    files (or the built-in defaults) as the owner's board."""
    with db.connection() as conn:
        have_cfgs = conn.execute("SELECT 1 FROM trek_configs LIMIT 1").fetchone()
        have_favs = conn.execute("SELECT 1 FROM user_favourites LIMIT 1").fetchone()

    if not have_cfgs:
        legacy = _load_json(config.TREKS_FILE, None)
        write_trek_configs(legacy if legacy else dict(config.DEFAULT_TREKS))
        print("[Storage] Seeded trek catalog.")

    if not have_favs:
        cfgs = read_trek_configs()
        legacy = _load_json(config.FAVOURITES_FILE, None)
        rows = legacy if legacy is not None else [
            {"trek_id": int(c["trek_id"]), "name": c["name"], "district_id": c["district_id"]}
            for c in cfgs.values()]
        for r in rows:
            if r.get("district_id") is None:
                continue
            add_favourite(config.OWNER_USER_ID, int(r["trek_id"]), r["name"], r["district_id"])
        watch = _load_json(config.WATCHLIST_FILE, [])
        for w in watch if isinstance(watch, list) else []:
            if w.get("district_id") is not None:
                add_watch(config.OWNER_USER_ID, int(w["trek_id"]), w["name"],
                          w["district_id"], w["date"])
        st = _load_json(config.SETTINGS_FILE, {})
        set_window_days(config.OWNER_USER_ID,
                        int(st.get("window_days", config.WINDOW_DAYS_DEFAULT)))
        if "cadence" in st:
            write_cadence(int(st["cadence"]))
        print(f"[Storage] Imported legacy board for the owner ({len(rows)} favourites).")


def init_storage() -> bool:
    """Connect, migrate, seed. Never raises: a database problem must not stop
    the process from starting — reload_views() retries in the background."""
    global _db_ready
    if not config.DATABASE_URL:
        print("[Storage] DATABASE_URL not set — the board will stay empty.")
        _db_ready = False
        return False
    try:
        db.init()
        db.migrate()
        _ensure_owner()
        _import_legacy_if_empty()
        _db_ready = True
        print("[Storage] Postgres ready.")
    except Exception as e:
        print(f"[Storage] Postgres unavailable ({e.__class__.__name__}: {e}). "
              f"Retrying in the background.")
        _db_ready = False
    return _db_ready


def reload_views() -> bool:
    """Refresh the whole view registry plus global state. Returns success."""
    global _db_ready
    try:
        if not _db_ready:
            db.init()
            db.migrate()
            _ensure_owner()
            _import_legacy_if_empty()
            _db_ready = True
            print("[Storage] Postgres reconnected.")
        rows = read_view_rows()
        cfgs = read_trek_configs()
        cadence = read_cadence()
    except Exception as e:
        print(f"[Storage] view reload failed ({e.__class__.__name__}: {e})")
        _db_ready = False
        return False

    views.replace_all(rows)
    with state.lock:
        state.trek_configs = cfgs
        state.settings["cadence"] = cadence
    return True


def reload_user(user_id: int) -> None:
    """Refresh one user's view after they change something."""
    try:
        row = read_view_row(user_id)
    except Exception as e:
        print(f"[Storage] reload user {user_id}: {e}")
        return
    if row:
        views.put(row)

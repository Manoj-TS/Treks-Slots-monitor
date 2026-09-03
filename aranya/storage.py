"""Persistence for favourites / watch / trek configs / settings.

Postgres is the source of truth. The legacy JSON files are kept as a fallback
so that a database outage cannot take the board down, and as the rollback path
for this migration — a `git revert` of the Postgres work finds the files still
on disk and current.

Everything is keyed by user_id even though there is exactly one user today
(config.OWNER_USER_ID). That is deliberate: when accounts arrive, the change is
"stop passing the constant", not a rewrite of every call site.
"""

import json
import os

from . import config, db, state

_db_ready = False


def db_ready() -> bool:
    return _db_ready


# ── JSON fallback ─────────────────────────────────────────────────────────── #

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


# ── Row <-> dict mapping ──────────────────────────────────────────────────── #
# district_name is derived, never stored: it would otherwise drift from
# district_id, and the JSON files already carry a redundant copy.

def _fav_row(trek_id, name, district_id):
    return {"trek_id": trek_id, "name": name, "district_id": district_id,
            "district_name": config.district_name(district_id)}


def _watch_row(trek_id, name, district_id, watch_date):
    return {"trek_id": trek_id, "name": name, "district_id": district_id,
            "district_name": config.district_name(district_id),
            "date": watch_date.isoformat() if hasattr(watch_date, "isoformat") else str(watch_date)}


def _fav_from_cfg(cfg):
    did = cfg.get("district_id")
    return _fav_from_parts(int(cfg["trek_id"]), cfg.get("name") or f"Trek {cfg['trek_id']}", did)


def _fav_from_parts(trek_id, name, district_id):
    return _fav_row(trek_id, name, district_id)


# ── Postgres reads ────────────────────────────────────────────────────────── #

def _db_read_favourites(user_id):
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT trek_id, name, district_id FROM user_favourites"
            " WHERE user_id = %s ORDER BY position, trek_id", (user_id,)).fetchall()
    return [_fav_row(r[0], r[1], r[2]) for r in rows]


def _db_read_watch(user_id):
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT trek_id, name, district_id, watch_date FROM user_watch"
            " WHERE user_id = %s ORDER BY watch_date, trek_id", (user_id,)).fetchall()
    return [_watch_row(r[0], r[1], r[2], r[3]) for r in rows]


def _db_read_trek_configs():
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT trek_id, name, district_id, timeslot_mapping_id, timeslot_id"
            " FROM trek_configs ORDER BY trek_id").fetchall()
    return {str(r[0]): {"trek_id": r[0], "name": r[1], "district_id": r[2],
                        "timeslot_mapping_id": r[3], "timeslot_id": r[4]} for r in rows}


def _db_read_settings(user_id):
    with db.connection() as conn:
        row = conn.execute("SELECT window_days FROM user_settings WHERE user_id = %s",
                           (user_id,)).fetchone()
        cad = conn.execute("SELECT value FROM app_settings WHERE key = 'cadence'").fetchone()
    window_days = row[0] if row else config.WINDOW_DAYS_DEFAULT
    cadence = int(cad[0]) if cad else config.BOARD_CYCLE_DEFAULT
    return window_days, cadence


# ── Postgres writes ───────────────────────────────────────────────────────── #
# Replace-the-whole-collection semantics, matching how the JSON files behave
# today: routes mutate the in-memory list, then ask us to persist it. At this
# size that is simpler and less error-prone than diffing, and it keeps the
# route handlers unchanged.

def _db_write_favourites(user_id, favs):
    with db.connection() as conn:
        conn.execute("DELETE FROM user_favourites WHERE user_id = %s", (user_id,))
        for pos, f in enumerate(favs):
            conn.execute(
                "INSERT INTO user_favourites (user_id, trek_id, name, district_id, position)"
                " VALUES (%s, %s, %s, %s, %s)",
                (user_id, f["trek_id"], f["name"], f["district_id"], pos))


def _db_write_watch(user_id, watches):
    with db.connection() as conn:
        conn.execute("DELETE FROM user_watch WHERE user_id = %s", (user_id,))
        for w in watches:
            conn.execute(
                "INSERT INTO user_watch (user_id, trek_id, name, district_id, watch_date)"
                " VALUES (%s, %s, %s, %s, %s)",
                (user_id, w["trek_id"], w["name"], w["district_id"], w["date"]))


def _db_write_trek_configs(cfgs):
    with db.connection() as conn:
        conn.execute("DELETE FROM trek_configs")
        for cfg in cfgs.values():
            conn.execute(
                "INSERT INTO trek_configs"
                " (trek_id, name, district_id, timeslot_mapping_id, timeslot_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (cfg["trek_id"], cfg["name"], cfg.get("district_id"),
                 cfg.get("timeslot_mapping_id"), cfg.get("timeslot_id")))


def _db_write_settings(user_id, window_days, cadence):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, window_days) VALUES (%s, %s)"
            " ON CONFLICT (user_id) DO UPDATE SET window_days = EXCLUDED.window_days,"
            " updated_at = now()", (user_id, window_days))
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('cadence', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (json.dumps(cadence),))


def _ensure_owner():
    """Seed the single owner account this app runs as today."""
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = %s", (config.OWNER_USER_ID,)).fetchone()
        if row:
            return
        # access_until stays NULL: is_admin already bypasses the paywall, and a
        # literal 'infinity' timestamptz is valid in Postgres but cannot be
        # loaded into a Python datetime (max year 9999) — it raises DataError
        # on every read of this row.
        conn.execute(
            "INSERT INTO users (id, email, email_verified, name, is_admin)"
            " VALUES (%s, %s, true, 'Owner', true)"
            " ON CONFLICT (id) DO NOTHING",
            (config.OWNER_USER_ID, config.OWNER_EMAIL))
        # bigserial keeps its own counter; nudge it past the explicit id we just used.
        conn.execute("SELECT setval('users_id_seq', GREATEST((SELECT max(id) FROM users), 1))")
        print(f"[Storage] Seeded owner account (id={config.OWNER_USER_ID}).")


# ── Public API ────────────────────────────────────────────────────────────── #

def init_storage() -> bool:
    """Connect + migrate. Returns True if Postgres is usable, False to run on
    the JSON files. Never raises: a database problem must not take the board
    down, it is only a display of public data."""
    global _db_ready
    if not config.DATABASE_URL:
        print("[Storage] DATABASE_URL not set — using JSON files.")
        _db_ready = False
        return False
    try:
        db.init()
        db.migrate()
        _ensure_owner()
        _db_ready = True
        print("[Storage] Postgres ready.")
    except Exception as e:
        print(f"[Storage] Postgres unavailable ({e.__class__.__name__}: {e}) — "
              f"falling back to JSON files.")
        _db_ready = False
    return _db_ready


def save_favourites(user_id=config.OWNER_USER_ID):
    if _db_ready:
        try:
            _db_write_favourites(user_id, state.favourites)
            return
        except Exception as e:
            print(f"[Storage] save favourites: {e}")
    _save_json(config.FAVOURITES_FILE, state.favourites)


def save_watch(user_id=config.OWNER_USER_ID):
    if _db_ready:
        try:
            _db_write_watch(user_id, state.custom_watch)
            return
        except Exception as e:
            print(f"[Storage] save watch: {e}")
    _save_json(config.WATCHLIST_FILE, state.custom_watch)


def save_treks(user_id=config.OWNER_USER_ID):
    if _db_ready:
        try:
            _db_write_trek_configs(state.trek_configs)
            return
        except Exception as e:
            print(f"[Storage] save trek configs: {e}")
    _save_json(config.TREKS_FILE, state.trek_configs)


def save_settings(user_id=config.OWNER_USER_ID):
    if _db_ready:
        try:
            _db_write_settings(user_id, state.settings["window_days"], state.settings["cadence"])
            return
        except Exception as e:
            print(f"[Storage] save settings: {e}")
    _save_json(config.SETTINGS_FILE, state.settings)


def load_all(user_id=config.OWNER_USER_ID):
    """Populate in-memory state. Falls back to the JSON files when Postgres is
    unavailable, and imports them into an empty database on first run."""
    if not _db_ready:
        _load_from_json()
        return

    try:
        cfgs = _db_read_trek_configs()
        favs = _db_read_favourites(user_id)
        watches = _db_read_watch(user_id)
        window_days, cadence = _db_read_settings(user_id)
    except Exception as e:
        print(f"[Storage] read failed ({e}) — falling back to JSON files.")
        _load_from_json()
        return

    # First run against an empty database: adopt whatever the JSON files hold
    # (or the built-in defaults), then write it back so Postgres owns it.
    imported = False
    if not cfgs:
        legacy = _load_json(config.TREKS_FILE, None)
        cfgs = legacy if legacy else dict(config.DEFAULT_TREKS)
        imported = True
    if not favs:
        legacy = _load_json(config.FAVOURITES_FILE, None)
        favs = (legacy if legacy is not None
                else [_fav_from_cfg(c) for c in cfgs.values()])
        imported = True
    if not watches:
        legacy = _load_json(config.WATCHLIST_FILE, [])
        watches = legacy if isinstance(legacy, list) else []
        if watches:
            imported = True

    state.trek_configs = cfgs
    state.favourites = favs
    state.custom_watch = watches
    state.settings["window_days"] = int(window_days)
    state.settings["cadence"] = int(cadence)

    if imported:
        legacy_settings = _load_json(config.SETTINGS_FILE, {})
        state.settings["window_days"] = int(legacy_settings.get("window_days", window_days))
        state.settings["cadence"] = int(legacy_settings.get("cadence", cadence))
        print(f"[Storage] Importing legacy data into Postgres "
              f"({len(favs)} favourites, {len(watches)} pinned, {len(cfgs)} treks).")
        save_treks(user_id)
        save_favourites(user_id)
        save_watch(user_id)
        save_settings(user_id)


def _load_from_json():
    saved = _load_json(config.TREKS_FILE, None)
    state.trek_configs = dict(config.DEFAULT_TREKS) if saved is None else saved
    if saved is None:
        _save_json(config.TREKS_FILE, state.trek_configs)

    fav = _load_json(config.FAVOURITES_FILE, None)
    if fav is None:
        # Seed favourites from the configured treks so the board is populated on first launch.
        state.favourites = [_fav_from_cfg(c) for c in state.trek_configs.values()]
        _save_json(config.FAVOURITES_FILE, state.favourites)
    else:
        state.favourites = fav

    watch = _load_json(config.WATCHLIST_FILE, [])
    # The old monitor stored an events map here; accept only clean watch entries.
    state.custom_watch = watch if isinstance(watch, list) else []

    st = _load_json(config.SETTINGS_FILE, {})
    state.settings["window_days"] = int(st.get("window_days", config.WINDOW_DAYS_DEFAULT))
    state.settings["cadence"] = int(st.get("cadence", config.BOARD_CYCLE_DEFAULT))

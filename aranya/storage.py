"""Flat-JSON persistence for the four data files. Superseded by Postgres in a
later phase of the project plan; kept as-is for now (verbatim move)."""

import json
import os

from . import config, state


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


def save_favourites():
    _save_json(config.FAVOURITES_FILE, state.favourites)


def save_watch():
    _save_json(config.WATCHLIST_FILE, state.custom_watch)


def save_treks():
    _save_json(config.TREKS_FILE, state.trek_configs)


def save_settings():
    _save_json(config.SETTINGS_FILE, state.settings)


def _fav_from_cfg(cfg):
    did = cfg.get("district_id")
    return {"trek_id": int(cfg["trek_id"]), "name": cfg.get("name") or f"Trek {cfg['trek_id']}",
            "district_id": did, "district_name": config.district_name(did)}


def load_all_from_disk():
    saved = _load_json(config.TREKS_FILE, None)
    state.trek_configs = dict(config.DEFAULT_TREKS) if saved is None else saved
    if saved is None:
        save_treks()

    fav = _load_json(config.FAVOURITES_FILE, None)
    if fav is None:
        # Seed favourites from the configured treks so the board is populated on first launch.
        state.favourites = [_fav_from_cfg(c) for c in state.trek_configs.values()]
        save_favourites()
    else:
        state.favourites = fav

    watch = _load_json(config.WATCHLIST_FILE, [])
    # The old monitor stored an events map here; accept only clean watch entries.
    state.custom_watch = watch if isinstance(watch, list) else []

    st = _load_json(config.SETTINGS_FILE, {})
    state.settings["window_days"] = int(st.get("window_days", config.WINDOW_DAYS_DEFAULT))
    state.settings["cadence"] = int(st.get("cadence", config.BOARD_CYCLE_DEFAULT))

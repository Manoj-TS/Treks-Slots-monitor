"""Constants and env-derived config. No internal imports — everything else depends on this."""

import os

BASE = "https://aranyavihaara.karnataka.gov.in"
WORKERS = 8
BOARD_CYCLE_DEFAULT = 40           # seconds between sweeps (display, not a race)
WINDOW_DAYS_DEFAULT = 30           # portal opens bookings up to 30 days ahead
SESSION_RESET_AFTER = 4

FAVOURITES_FILE = "favourites.json"
WATCHLIST_FILE = "watchlist.json"
TREKS_FILE = "trek_configs.json"
SETTINGS_FILE = "dashboard_settings.json"

# Where to serve. Override with environment variables, e.g.  PORT=8080  HOST=0.0.0.0
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5020"))

# Cache-busts static/css and static/js so a deploy doesn't serve a stale cached
# asset. GIT_SHA is exported by start.sh; "dev" outside that context.
ASSET_VERSION = os.environ.get("GIT_SHA", "dev")

# Postgres. Unset means "run on the legacy JSON files" — see storage.init_storage.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Everything is stored per user_id already, but there is exactly one account
# today. When accounts land, these call sites take g.user.id instead.
OWNER_USER_ID = 1
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@aranyavihaara.org")

DISTRICT_NAMES = {
    4: "Kalaburagi", 11: "Chikkaballapura", 15: "Shivamogga", 16: "Udupi",
    17: "Chikkamagaluru", 19: "Kolar", 21: "Bengaluru Gramantara",
    24: "Dakshina Kannada", 25: "Kodagu", 28: "Chamarajanagara", 29: "Ramanagara"
}

# Seeded trek_id -> config. Also seeds the initial favourites list.
DEFAULT_TREKS = {
    "112": {"trek_id": 112, "name": "Kudremukha", "district_id": 17, "timeslot_mapping_id": 190, "timeslot_id": 44},
    "114": {"trek_id": 114, "name": "Gangadikal", "district_id": 17, "timeslot_mapping_id": 188, "timeslot_id": 45},
    "110": {"trek_id": 110, "name": "Kurinjal",   "district_id": 17, "timeslot_mapping_id": 184, "timeslot_id": 45},
    "84":  {"trek_id": 84,  "name": "Bandaje",     "district_id": 17, "timeslot_mapping_id": 145, "timeslot_id": 44},
    "113": {"trek_id": 113, "name": "Netravathi", "district_id": 24, "timeslot_mapping_id": 187, "timeslot_id": 45},
}


def district_name(did):
    try:
        return DISTRICT_NAMES.get(int(did), f"Zone {did}")
    except (TypeError, ValueError):
        return "-"

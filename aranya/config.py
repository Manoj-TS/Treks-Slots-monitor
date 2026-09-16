"""Constants and env-derived config. No internal imports — everything else depends on this."""

import os

BASE = "https://aranyavihaara.karnataka.gov.in"
WORKERS = 8
BOARD_CYCLE_DEFAULT = 120          # seconds between sweeps (display, not a race)
WINDOW_DAYS_DEFAULT = 30           # portal opens bookings up to 30 days ahead
SESSION_RESET_AFTER = 4

# ── Portal load control ───────────────────────────────────────────────────── #
# The sweep is a union across paying users, so without a ceiling the request
# rate against a .gov.in host would grow with the customer base. This keeps it
# a constant: more customers make a full sweep take longer, not hit harder.
SWEEP_RPS = float(os.environ.get("SWEEP_RPS", "3.0"))
MAX_TARGETS = int(os.environ.get("MAX_TARGETS", "1500"))
VIEW_RELOAD_SECONDS = int(os.environ.get("VIEW_RELOAD_SECONDS", "60"))

# ── Per-cell refresh schedule ─────────────────────────────────────────────── #
# How stale each kind of cell is allowed to get. Only "open" cells hold data
# that genuinely moves; the other two are re-checked slowly purely to catch a
# correction on the portal's side, not because they are expected to change.
#
# OPEN_INTERVAL is not here: it comes from the operator-set `cadence` in
# app_settings, so it stays adjustable from /admin without a deploy.
SOLD_OUT_INTERVAL = int(os.environ.get("SOLD_OUT_INTERVAL", "1800"))
UNRELEASED_INTERVAL = int(os.environ.get("UNRELEASED_INTERVAL", "1800"))

# Floor under the operator-set cadence, enforced on write (storage.write_cadence)
# and again on read (sweeper._interval_for), so no stored value, form post or
# legacy import can poll open cells faster than this.
#
# This is deliberately not a matter of taste. A tight interval is the one knob
# that can get this IP blocked by the portal, and a block means every paying
# customer sees an empty board — worse than data that is two minutes old. Two
# minutes is already far tighter than the portal's own release cycle. Lowering
# it is possible but has to be a conscious act with a deploy behind it, not a
# number left in a form field.
OPEN_INTERVAL_MIN = int(os.environ.get("OPEN_INTERVAL_MIN", "120"))
OPEN_INTERVAL_MAX = int(os.environ.get("OPEN_INTERVAL_MAX", "900"))

# Republish to connected viewers at most this often, and at least this often.
# The floor stops a drip from invalidating every viewer's payload cache several
# times a second; the heartbeat keeps "updated Xs ago" honest.
PUBLISH_MIN_INTERVAL = float(os.environ.get("PUBLISH_MIN_INTERVAL", "2.0"))
PUBLISH_HEARTBEAT = float(os.environ.get("PUBLISH_HEARTBEAT", "15.0"))

# User-triggered "Refresh now". This is a path from a browser button to a
# .gov.in portal, so it carries its own limits on top of the shared bucket.
FORCE_REFRESH_COOLDOWN = int(os.environ.get("FORCE_REFRESH_COOLDOWN", "120"))
MAX_FORCE_CELLS = int(os.environ.get("MAX_FORCE_CELLS", "120"))

# ── Portal health ─────────────────────────────────────────────────────────── #
# A scraper fails quietly: a renamed CSS class makes every date look
# unreleased, and the board shows a calm, wrong answer. These thresholds decide
# when the pattern of responses means "something is broken", as opposed to one
# slow request.
HEALTH_WINDOW_SECONDS = 900           # rolling window the ratios are taken over
HEALTH_MIN_SAMPLES = 10               # no ratio verdicts on fewer responses
HEALTH_FAIL_RATIO = 0.5               # share of failed/unparsed responses
HEALTH_FAIL_STREAK = 5                # consecutive failures, however few overall
HEALTH_BLOCKED_COUNT = 3              # refusals (403/429/WAF page) in the window
HEALTH_REGRESSION_TREKS = 3           # treks whose released dates went "unreleased"
HEALTH_ALERT_AFTER = int(os.environ.get("HEALTH_ALERT_AFTER", "300"))
HEALTH_CLEAR_AFTER = 180              # quiet this long before calling it over
HEALTH_REALERT_SECONDS = 6 * 3600     # reminder while a problem is still open
HEALTH_SAMPLE_BYTES = 200_000
HEALTH_SAMPLES_KEEP = 30
HEALTH_SAMPLE_EVERY = 600             # at most one saved page per kind per 10 min
# Extra recipient for health alerts, on top of every admin account.
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")

# Per-user ceilings, enforced at write time.
MAX_FAVOURITES_PER_USER = int(os.environ.get("MAX_FAVOURITES_PER_USER", "25"))
MAX_WATCH_PER_USER = int(os.environ.get("MAX_WATCH_PER_USER", "50"))

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

# ── Accounts / sessions ───────────────────────────────────────────────────── #

# Absolute base for links in emails. NEVER build these from request.host —
# host-header injection into a password-reset link is account takeover.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5020").rstrip("/")

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")

SESSION_COOKIE = "av_session"
CSRF_COOKIE = "av_csrf"
SESSION_DAYS = 30

# Concurrent signed-in devices per customer account. Signing in on one more
# signs out the least recently used. Admins are exempt. One ₹99 subscription is
# one organiser's board, not a team's — but a phone and a laptop is normal use.
MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "2"))
# How often in-memory "last seen" times are written to the sessions table.
SESSION_TOUCH_SECONDS = 60
# Forced sign-outs from the device limit within this many days that mark an
# account as probably shared, in /admin.
SHARING_WINDOW_DAYS = 30
SHARING_FLAG_AT = int(os.environ.get("SHARING_FLAG_AT", "6"))
VERIFY_TOKEN_HOURS = 24
RESET_TOKEN_MINUTES = 60

# Force an SSE reconnect (and therefore a fresh paywall check) at least this
# often. Kept under nginx's proxy_read_timeout of 3600s.
MAX_STREAM_SECONDS = int(os.environ.get("MAX_STREAM_SECONDS", "3300"))

ACCESS_DAYS = int(os.environ.get("ACCESS_DAYS", "30"))
PRICE_RUPEES = int(os.environ.get("PRICE_RUPEES", "99"))
# Paid access can't run further ahead than this. The checkout refuses a
# purchase that would cross it, so money is never taken for days that can't be
# granted.
PAID_ACCESS_CAP_DAYS = 365

# ── Razorpay ──────────────────────────────────────────────────────────────── #
# All three, or online payment stays off and /billing shows the email-us page —
# which also makes unsetting them the kill switch.
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
MAX_ORDERS_PER_HOUR = 10

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# ── Outbound mail (Zoho, India data centre) ───────────────────────────────── #

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.zoho.in")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@aranyavihaara.org")
MAIL_FROM_NAME = "Aranya"

# ── Business identity ─────────────────────────────────────────────────────── #
# Razorpay's onboarding review requires a contactable phone number and a postal
# address published on the site. Set these in .env before applying — the contact
# page shows a visible placeholder until you do.
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "")
BUSINESS_PHONE = os.environ.get("BUSINESS_PHONE", "")
BUSINESS_ADDRESS = os.environ.get("BUSINESS_ADDRESS", "")


def mail_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def billing_configured() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET)


def billing_mode() -> str:
    if not billing_configured():
        return "off"
    return "test" if RAZORPAY_KEY_ID.startswith("rzp_test_") else "live"


def auth_configured() -> bool:
    """Accounts require both a database and a signing key."""
    return bool(DATABASE_URL and SECRET_KEY)

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

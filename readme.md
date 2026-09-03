a live web dashboard that watches weekend (Sat/Sun) seat availability across
  your favourite treks for the portal's rolling booking window.


> No credentials are needed for the monitor — it only reads public availability

---

## Requirements

- Python 3.9+
- Dependencies in `requirements.txt` (Flask, Requests, BeautifulSoup4)

## Setup

```bash
# clone, then:
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## Slot Monitor (`aranya/` package, served via `wsgi.py`)

A Flask app (`aranya/`, entry point `wsgi.py`). On startup it discovers every
active trek across the configured districts (via the portal's `/get-treks`
endpoint) and seeds your favourites list from the default treks in
`trek_configs.json`.

```bash
python wsgi.py
```

Then open **http://localhost:5020** (override with the `PORT`/`HOST` env vars).

### What it does

- **Board** — favourite treks × every weekend (Sat/Sun) in the rolling window
  (today … +30 days, the portal's booking horizon). Each cell shows
  `OPEN n/N` (with a capacity bar), `SOLD OUT`, or `UNRELEASED`.
- **Favourites** — pick which treks appear on the board (seeded from
  `trek_configs.json`).
- **Monitor** — watch any specific trek + date outside the weekend columns
  ("rest of dates").
- **Settings** — appearance (theme / accent / light) and poll cadence.
- Display only — no login, no booking, no sound alarm. Reads the portal's own
  `/availability` page per date and shows the real seat counts exactly as the
  site reports them; a date shows **UNRELEASED** when the portal won't serve it
  yet (detected by comparing the date the page echoes back against the date
  requested).
- Filter (district / status), search, sort, group, density, column visibility,
  window size (7/14/30 days), theme + accent — all persisted per browser.

### Configuration

Edit the constants in `aranya/config.py`:

| Constant              | Purpose                                            |
| ---------------------- | -------------------------------------------------- |
| `DISTRICT_NAMES`       | district id → name; drives trek discovery           |
| `DEFAULT_TREKS`        | seeded trek_id → config (also seeds favourites)     |
| `WINDOW_DAYS_DEFAULT`  | rolling booking window in days (default 30)         |
| `BOARD_CYCLE_DEFAULT`  | seconds between sweeps (raise to ease load)         |
| `WORKERS`              | concurrent date fetches per sweep                   |

Runtime data (favourites, watchlist, trek configs, settings) is persisted to
`favourites.json`, `watchlist.json`, `trek_configs.json`, and
`dashboard_settings.json` next to the script — editable from the UI, or by hand.
None of these files need to exist beforehand; the app creates them (seeded with
defaults) on first run.

> If the government server starts throttling, increase `BOARD_CYCLE_DEFAULT` or
> lower `WORKERS`.

---

## Deploying

The app runs in a single Docker container (waitress-served Flask) bound to
`127.0.0.1:5020`. It expects a **host-level nginx** already serving other sites
on the box to reverse-proxy to it and terminate TLS, with host `certbot`
managing the certificate — so it coexists with other projects on a shared VPS
rather than competing for ports 80/443.

With DNS for your domain already pointing at the VPS:

```bash
git clone https://github.com/Manoj-TS/Treks-Slots-monitor.git aranyavihaara
cd aranyavihaara
cp .env.example .env && nano .env   # set DOMAIN and EMAIL
sudo ./start.sh
```

`start.sh` builds and starts the container, waits until it answers, installs an
nginx vhost (`nginx/aranyavihaara.org.conf`) plus rate-limit zones, then runs
`certbot --nginx` the same way the other sites on the box are set up. It's
idempotent and is also the update command:

```bash
git pull && sudo ./start.sh
```

It never overwrites the vhost once certbot has edited it, and it verifies at the
end that SSE actually streams and that state writes are landing.

App state (`favourites.json`, `watchlist.json`, etc.) lives in `./data/` on the
host, outside git, so it survives rebuilds and a `git pull` never conflicts with
it. See the comments in `start.sh`, `Dockerfile`, and
`nginx/aranyavihaara.org.conf` for the reasoning behind each piece — notably why
the WSGI server must stay single-process, and which proxy settings SSE depends
on.

---

## Notes

- These tools talk to a government portal. Use them responsibly — avoid hammering

- Endpoints and page structure can change without notice; if parsing breaks, check
  the portal's current HTML/JSON against the parsing logic.

## Disclaimer

Personal, educational use. Not affiliated with or endorsed by the Karnataka Forest
Department. You are responsible for complying with the portal's terms of use.

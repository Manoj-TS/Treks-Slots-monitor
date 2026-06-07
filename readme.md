a live web dashboard that watches seat availability for any
  trek over the next 15 days and sounds an alarm when a Saturday slot opens.


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

## Slot Monitor (`monitor.py`)

A self-contained Flask app. On startup it discovers every active trek across the
configured districts (via the portal's `/get-treks` endpoint) and lets you pick
one from a dropdown.

```bash
python monitor.py
```

Then open **http://localhost:5000**.

### What it does

- Watches **tomorrow → +15 days** (16 date cards, e.g. 8th–23rd).
- Reads the portal's own `/availability` page per date and shows the real seat
  counts (`available / capacity`) exactly as the site reports them.
- Marks a date **NOT RELEASED** when the portal won't serve it yet — detected by
  comparing the date the page echoes back against the date requested. It flips to
  live counts automatically the moment that date opens.
- Highlights **Saturdays**; if any Saturday slot has seats, the card pulses and an
  audible alarm + banner fire. A **SILENCE** button stops the sound.
- Refreshes the full grid every ~2 seconds.

### Configuration

Edit the constants at the top of `monitor.py`:

| Constant      | Purpose                                              |
| ------------- | ---------------------------------------------------- |
| `DISTRICTS`   | district id → name; drives trek discovery & dropdown |
| `DAYS_AHEAD`  | number of dates to watch (default 16)                |
| `CYCLE_SLEEP` | seconds between full sweeps (raise to ease load)     |
| `WORKERS`     | concurrent date fetches per sweep                    |

> Tip: click anywhere on the page once to unlock browser audio (autoplay policy).
> If the government server starts throttling, increase `CYCLE_SLEEP` or lower
> `WORKERS`.

---


## Notes

- These tools talk to a government portal. Use them responsibly — avoid hammering

- Endpoints and page structure can change without notice; if parsing breaks, check
  the portal's current HTML/JSON against the parsing logic.

## Disclaimer

Personal, educational use. Not affiliated with or endorsed by the Karnataka Forest
Department. You are responsible for complying with the portal's terms of use.
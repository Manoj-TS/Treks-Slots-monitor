"""HTTP client for the government portal (availability only — no login, no booking).

Every availability response is classified, not just parsed, because the two
page shapes the portal returns look nothing alike and "I couldn't read this"
must never be recorded as "not released yet":

  released    the availability page: #dateDisplay plus .slot_card blocks
  unreleased  a date that isn't open is bounced to the portal's home page —
              byte-identical to /login, recognisable by its #loginForm
  parse_fail  a 200 that is neither shape: the portal changed its markup
  blocked     403/429, or a WAF "request rejected" page served as a 200
  session     419/401: the CSRF token or portal session expired (routine)
  http_error  any other status
  network     timeout, DNS, connection reset

Only the first two are answers. Everything else leaves the board's existing
cell alone and feeds aranya.health.
"""

import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from . import config

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

ANSWERS = ("released", "unreleased")

# Phrases from the refusal pages that front common government-site WAFs (F5,
# Akamai, Cloudflare, Imperva). Matched only against small or unrecognised
# pages, so a trek description that happens to say "blocked" can't trip it.
_WAF_MARKERS = ("the requested url was rejected", "your support id is",
                "access denied", "request rejected", "attention required",
                "cf-chl", "captcha", "incapsula", "you have been blocked",
                "too many requests")

# Deliberately no Set-Cookie: that is our scraper's live portal session.
_SAMPLE_HEADERS = ("server", "content-type", "content-length", "location",
                   "retry-after", "x-cache", "via")


def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _sample(r, url: str, note: str = "") -> dict:
    """What a human needs to diagnose a bad response, minus our session cookie."""
    return {"http_status": getattr(r, "status_code", None), "url": url, "note": note,
            "headers": {k: r.headers[k] for k in _SAMPLE_HEADERS
                        if r is not None and k in r.headers},
            "body": (r.text if r is not None else "")}


def _looks_blocked(text: str) -> bool:
    low = text[:20000].lower()
    return any(m in low for m in _WAF_MARKERS)


def _status_outcome(code: int) -> str:
    if code in (403, 429):
        return "blocked"
    if code in (401, 419):
        return "session"
    return "http_error"


def fetch_csrf_checked(session):
    """(token or None, outcome, sample or None). The token page is the first
    thing every sweep needs, so a change here stops everything — it is
    classified like any other response."""
    url = f"{config.BASE}/login"
    try:
        r = session.get(url, timeout=10)
    except Exception as e:
        print(f"[csrf] {e}")
        return None, "network", {"http_status": None, "url": url, "headers": {},
                                 "body": "", "note": f"{e.__class__.__name__}: {e}"}
    if r.status_code != 200:
        return None, _status_outcome(r.status_code), _sample(r, url, "token page")
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("input", {"name": "_token"}) or soup.find("meta", {"name": "_token"})
    token = tag and (tag.get("value") or tag.get("content"))
    if token:
        return token, "ok", None
    outcome = "blocked" if _looks_blocked(r.text) else "parse_fail"
    return None, outcome, _sample(r, url, "token page has no _token field")


def fetch_csrf(session):
    return fetch_csrf_checked(session)[0]


def fetch_treks_for_district(session, csrf, district_id):
    try:
        r = session.post(f"{config.BASE}/get-treks", data={"_token": csrf, "district_id": str(district_id)},
                         timeout=8, headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def fetch_availability(session, csrf, district_id, trek_id, date_ddmmyyyy):
    """(response or None, error note). None means the request itself failed."""
    try:
        r = session.post(f"{config.BASE}/availability", data={
            "_token": csrf, "district": str(district_id),
            "trek": str(trek_id), "check_in": date_ddmmyyyy,
        }, timeout=12)
        return r, ""
    except Exception as e:
        print(f"[avail] {trek_id} @ {date_ddmmyyyy}: {e}")
        return None, f"{e.__class__.__name__}: {e}"


def parse_displayed_date(html):
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find(id="dateDisplay")
    if not el:
        return None, soup
    txt = el.get_text(" ", strip=True)
    m = re.search(r"(\d{1,2})\w*\s+([A-Za-z]+)\s+(\d{4})", txt)
    if not m:
        return None, soup
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = MONTHS.get(month_name.capitalize())
    if not month:
        return None, soup
    try:
        return datetime(year, month, day).date(), soup
    except ValueError:
        return None, soup


def parse_slots(soup):
    slots = []
    for card in soup.select(".slot_card"):
        name_el = card.select_one(".slot_text")
        avail_el = card.select_one(".available_text")
        name = name_el.get_text(" ", strip=True) if name_el else "?"
        avail_text = avail_el.get_text(" ", strip=True) if avail_el else ""
        m = re.search(r"(\d+)\s*/\s*(\d+)", avail_text)
        if m:
            slots.append({"name": re.sub(r"\s+", " ", name).strip(),
                          "available": int(m.group(1)), "capacity": int(m.group(2))})
    return slots


def classify(status: int, html: str, want_date) -> tuple[str, list, str]:
    """(outcome, slots, note) for one availability response. Pure, so the
    rules can be tested against saved pages."""
    if status != 200:
        return _status_outcome(status), [], f"HTTP {status}"

    soup = BeautifulSoup(html, "html.parser")
    display = soup.find(id="dateDisplay")
    if display is None:
        if soup.find("form", id="loginForm") is not None:
            return "unreleased", [], ""
        if _looks_blocked(html):
            return "blocked", [], "refusal page"
        return "parse_fail", [], "neither the availability page nor the home page"

    shown, _ = parse_displayed_date(html)
    if shown is None:
        return "parse_fail", [], "date heading present but unreadable"
    if shown != want_date:
        # The portal showing some other date is its own way of saying "not
        # this one". Readable, so an answer.
        return "unreleased", [], ""
    cards = soup.select(".slot_card")
    if not cards:
        # The right date's availability page, with nothing we recognise as a
        # slot. That is what a renamed .slot_card looks like, and scoring it
        # "unreleased" is precisely the silent failure this module exists for.
        return "parse_fail", [], "availability page with no .slot_card blocks"
    slots = parse_slots(soup)
    if not slots:
        return "parse_fail", [], f"{len(cards)} slot card(s), no readable N/M counts"
    return "released", slots, ""


def check_target(session, csrf, tgt):
    """tgt = {trek_id, district_id, date(YYYY-MM-DD)}. Returns a cell dict.

    Private keys, removed by strip_private() before the cell is stored:
      _transport_ok  True only when the response was an answer
      _outcome       see the module docstring
      _sample        the response, when it was not an answer
    """
    d_obj = datetime.strptime(tgt["date"], "%Y-%m-%d")
    cell = {"released": False, "available": 0, "capacity": 0, "slots": [],
            "checked": datetime.now().isoformat()}
    r, err = fetch_availability(session, csrf, tgt["district_id"], tgt["trek_id"],
                                d_obj.strftime("%d-%m-%Y"))
    url = f"{config.BASE}/availability"
    if r is None:
        cell.update(_transport_ok=False, _outcome="network",
                    _sample={"http_status": None, "url": url, "headers": {},
                             "body": "", "note": err})
        return cell

    outcome, slots, note = classify(r.status_code, r.text, d_obj.date())
    cell["_outcome"] = outcome
    cell["_transport_ok"] = outcome in ANSWERS
    # Attached even to answers: an "unreleased" page for a date that was open
    # yesterday is the evidence health.py keeps for a regression.
    cell["_sample"] = _sample(r, url, note)
    if outcome == "released":
        cell["released"] = True
        cell["slots"] = slots
        cell["available"] = sum(s["available"] for s in slots)
        cell["capacity"] = sum(s["capacity"] for s in slots)
    return cell


def strip_private(cell: dict) -> tuple[bool, str, dict | None]:
    """Remove and return (transport_ok, outcome, sample) so the cell can be
    stored and served. Anything starting with "_" must never reach a browser."""
    ok = cell.pop("_transport_ok", True)
    outcome = cell.pop("_outcome", "released" if cell.get("released") else "unreleased")
    sample = cell.pop("_sample", None)
    return ok, outcome, sample

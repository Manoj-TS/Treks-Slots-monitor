"""HTTP client for the government portal (availability only — no login, no booking)."""

import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from . import config

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_csrf(session):
    try:
        r = session.get(f"{config.BASE}/login", timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("input", {"name": "_token"}) or soup.find("meta", {"name": "_token"})
        if tag:
            return tag.get("value") or tag.get("content")
    except Exception as e:
        print(f"[csrf] {e}")
    return None


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
    try:
        r = session.post(f"{config.BASE}/availability", data={
            "_token": csrf, "district": str(district_id),
            "trek": str(trek_id), "check_in": date_ddmmyyyy,
        }, timeout=12)
        if r.status_code == 200:
            return r.text, True
        if r.status_code in (419, 401, 403):
            return None, False
        return None, True
    except Exception as e:
        print(f"[avail] {trek_id} @ {date_ddmmyyyy}: {e}")
        return None, False


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


def check_target(session, csrf, tgt):
    """tgt = {trek_id, district_id, date(YYYY-MM-DD)}. Returns a cell dict."""
    d_obj = datetime.strptime(tgt["date"], "%Y-%m-%d")
    cell = {"released": False, "available": 0, "capacity": 0, "slots": [],
            "checked": datetime.now().isoformat(), "_transport_ok": True}
    html, ok = fetch_availability(session, csrf, tgt["district_id"], tgt["trek_id"],
                                  d_obj.strftime("%d-%m-%Y"))
    cell["_transport_ok"] = ok
    if not html:
        return cell
    shown_date, soup = parse_displayed_date(html)
    slots = parse_slots(soup)
    if shown_date == d_obj.date() and slots:
        cell["released"] = True
        cell["slots"] = slots
        cell["available"] = sum(s["available"] for s in slots)
        cell["capacity"] = sum(s["capacity"] for s in slots)
    return cell

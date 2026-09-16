"""Portal health: notice when the scraper stops seeing the real portal, tell an
admin, and keep the evidence.

A scraper rarely fails loudly. The portal renames a CSS class, every date reads
as "not released", and the board shows a calm, confident, wrong answer to every
paying customer. So this watches the *pattern* of responses rather than single
errors:

  unreachable  most requests time out, error, or lose their session
  blocked      the portal refuses us (403/429, or a WAF page)
  parse        responses arrive but match no page shape we know
  regression   dates we had seen open start reading as unreleased, on several
               treks at once — the signature of a markup change that still
               happens to look like a valid page

A problem has to persist for HEALTH_ALERT_AFTER before anyone is emailed or any
customer is shown a notice, so one slow minute on a government server doesn't
page anybody. When it clears, the same people get a "resolved" email.

What this cannot do is fix anything. It reports the symptom and saves the page
that caused it; working out which selector changed is a human job.

Detection is in memory and never needs the database. Only the record (events,
samples) and the recipient list do, and all of that is best-effort.
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import accounts, config, db, mail, state, storage
from .portal import ANSWERS

IST = timezone(timedelta(hours=5, minutes=30))
TRANSPORT = ("network", "http_error", "session")

# kind -> (title, what a customer is told, what the admin should check)
KINDS = {
    "unreachable": (
        "Portal not answering",
        "The Forest Department portal isn't answering our checks right now, so "
        "seat counts may be out of date. Confirm on the official site before "
        "promising anyone a seat.",
        "Open the portal in a browser. If it is down for everyone there is "
        "nothing to do: the sweeper keeps retrying with backoff. If it loads for "
        "you but not for the server, the server's IP may be blocked."),
    "blocked": (
        "Portal is refusing our requests",
        "The Forest Department portal isn't answering our checks right now, so "
        "seat counts may be out of date. Confirm on the official site before "
        "promising anyone a seat.",
        "The portal is rejecting this server. The sweeper has backed off. Look "
        "in the saved page for a WAF support ID. Do not raise the check rate; "
        "if it recurs, lower SWEEP_RPS or raise the open-slot interval."),
    "parse": (
        "Portal pages no longer parse",
        "The Forest Department portal has changed how it shows availability and "
        "we may be misreading it. Confirm seat counts on the official site "
        "before promising anyone a seat.",
        "The portal's HTML has probably changed. Compare the saved page with what "
        "portal.classify() expects: #dateDisplay, .slot_card, .available_text "
        "reading 'N/M', and #loginForm on the home page an unreleased date "
        "bounces to. While this lasts, cells are NOT overwritten: the board keeps "
        "the last good answer and its age keeps growing."),
    "regression": (
        "Open dates now read as unreleased",
        "The Forest Department portal has changed how it shows availability and "
        "we may be misreading it. Confirm seat counts on the official site "
        "before promising anyone a seat.",
        "Dates that were open are now bouncing to the portal's home page on "
        "several treks at once. Either the portal changed how released dates are "
        "served (a scraper problem), or the treks were closed (check KFD "
        "announcements). Unlike 'parse', these cells ARE being shown as "
        "unreleased, because a genuine closure should show."),
}

# Which problem a failed response is evidence for, when saving a sample.
_SAMPLE_KIND = {"parse_fail": "parse", "blocked": "blocked",
                "network": "unreachable", "http_error": "unreachable"}


@dataclass
class Problem:
    kind: str
    first_seen: float
    last_true: float
    detail: str
    alerted_at: float | None = None
    event_id: int | None = None
    sample_id: int | None = None

    @property
    def confirmed(self) -> bool:
        return self.last_true - self.first_seen >= config.HEALTH_ALERT_AFTER


_lock = threading.Lock()
_events: deque = deque(maxlen=5000)          # (t, outcome, trek_id)
_streak: deque = deque(maxlen=50)            # outcomes of the current failure run
_last_fail_at = 0.0
_last_answer_at: float | None = None
_released: dict[str, int] = {}               # cell key -> trek_id, last seen open
_regressions: deque = deque(maxlen=500)      # (t, trek_id, key)
_last_sample_at: dict[str, float] = {}
_latest_sample: dict[str, int] = {}          # kind -> sample id
_problems: dict[str, Problem] = {}
_last_eval = 0.0


def _reset_for_tests() -> None:
    global _last_fail_at, _last_answer_at, _last_eval
    with _lock:
        _events.clear(); _streak.clear(); _released.clear(); _regressions.clear()
        _last_sample_at.clear(); _latest_sample.clear(); _problems.clear()
        _last_fail_at, _last_answer_at, _last_eval = 0.0, None, 0.0


# ── Recording ─────────────────────────────────────────────────────────────── #

def _cell_date(key: str | None) -> date | None:
    if not key:
        return None
    try:
        return date.fromisoformat(key.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def record(outcome: str, trek_id: int | None = None, key: str | None = None,
           sample: dict | None = None, now: float | None = None) -> None:
    """One portal response. Cheap; called for every fetch."""
    global _last_fail_at, _last_answer_at
    now = time.time() if now is None else now
    cell_date = _cell_date(key)
    save_as = None

    with _lock:
        _events.append((now, outcome, trek_id))
        if outcome in ANSWERS:
            _streak.clear()
            _last_answer_at = now
        else:
            _streak.append(outcome)
            _last_fail_at = now

        if key is not None and outcome == "released":
            _released[key] = trek_id
        elif key is not None and outcome == "unreleased" and key in _released:
            _released.pop(key, None)
            # Dates within a day or two can legitimately close for booking, and
            # at midnight every "today" cell would otherwise look like a
            # regression. Only a date comfortably ahead is evidence.
            if cell_date and cell_date >= date.today() + timedelta(days=2):
                _regressions.append((now, trek_id, key))
                save_as = "regression"

        if save_as is None:
            save_as = _SAMPLE_KIND.get(outcome)
        if save_as and sample is not None:
            if now - _last_sample_at.get(save_as, 0.0) < config.HEALTH_SAMPLE_EVERY:
                save_as = None
            else:
                _last_sample_at[save_as] = now
        else:
            save_as = None

    if save_as:
        sid = _save_sample(save_as, outcome, sample, trek_id, cell_date)
        if sid:
            with _lock:
                _latest_sample[save_as] = sid


# ── Evaluation ────────────────────────────────────────────────────────────── #

def _ago(t: float | None, now: float) -> str:
    if t is None:
        return "never (since this process started)"
    s = int(now - t)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def _conditions(now: float, names: dict) -> dict[str, str]:
    """kind -> human detail, for every problem whose condition holds now.
    Caller holds _lock. `names` is trek_id -> name, gathered beforehand so this
    never takes state.lock while holding _lock."""
    since = now - config.HEALTH_WINDOW_SECONDS
    recent = [e for e in _events if e[0] >= since]
    total = len(recent)
    count = {}
    for _, outcome, _t in recent:
        count[outcome] = count.get(outcome, 0) + 1
    answers = sum(count.get(o, 0) for o in ANSWERS)
    transport = sum(count.get(o, 0) for o in TRANSPORT)
    parse = count.get("parse_fail", 0)
    blocked = count.get("blocked", 0)
    streak = len(_streak)
    streak_fresh = streak >= config.HEALTH_FAIL_STREAK and now - _last_fail_at < 300
    streak_transport = sum(1 for o in _streak if o in TRANSPORT)
    streak_parse = sum(1 for o in _streak if o == "parse_fail")
    window_min = config.HEALTH_WINDOW_SECONDS // 60
    last_good = _ago(_last_answer_at, now)

    out = {}
    if ((streak_fresh and streak_transport * 2 > streak)
            or (total >= config.HEALTH_MIN_SAMPLES
                and transport / total >= config.HEALTH_FAIL_RATIO)):
        out["unreachable"] = (
            f"{transport} of the last {total} requests ({window_min} min) failed "
            f"to get an answer; {streak} failures in a row. Last good answer "
            f"{last_good}.")

    if blocked >= config.HEALTH_BLOCKED_COUNT:
        out["blocked"] = (f"{blocked} refusals (403/429 or a block page) in the "
                          f"last {window_min} min. Last good answer {last_good}.")

    readable = answers + parse
    if ((readable >= config.HEALTH_MIN_SAMPLES
            and parse / readable >= config.HEALTH_FAIL_RATIO)
            or (streak_fresh and streak_parse * 2 > streak)):
        out["parse"] = (f"{parse} of the last {readable} pages the portal served "
                        f"({window_min} min) matched no known layout. Last "
                        f"readable page {last_good}.")

    regressed: dict[int, int] = {}
    for t, trek, _k in _regressions:
        if t >= since:
            regressed[trek] = regressed.get(trek, 0) + 1
    if regressed:
        still_open = set(_released.values())
        every_known = not (still_open - set(regressed))
        if (len(regressed) >= config.HEALTH_REGRESSION_TREKS
                or (len(regressed) >= 2 and every_known)):
            treks = ", ".join(
                f"{names[t]} (#{t})" if names.get(t) else f"#{t}"
                for t in sorted(regressed, key=str))
            out["regression"] = (
                f"{sum(regressed.values())} previously open dates on "
                f"{len(regressed)} treks now read as unreleased ({treks}).")
    return out


def _trek_names() -> dict:
    """trek_id -> name. Takes state.lock, so never call it holding _lock."""
    with state.lock:
        names = {int(c["trek_id"]): c["name"] for c in state.trek_configs.values()}
        names.update({t["id"]: t["name"] for t in state.registry["treks"]})
    return names


def evaluate(now: float | None = None, force: bool = False) -> None:
    """Advance every problem's lifecycle; send whatever email that implies.
    Called from the sweeper loop. Self-throttled."""
    global _last_eval
    now = time.time() if now is None else now
    alerts, recoveries, opened = [], [], []
    changed = False
    if not force and now - _last_eval < 10:
        return
    names = _trek_names()

    with _lock:
        _last_eval = now

        # Past dates can never be fetched again; don't let them linger.
        today_iso = date.today().isoformat()
        for k in [k for k in _released if k.split("_", 1)[-1] < today_iso]:
            _released.pop(k, None)

        conds = _conditions(now, names)
        for kind, detail in conds.items():
            p = _problems.get(kind)
            if p is None:
                p = _problems[kind] = Problem(kind, now, now, detail)
                was = False
            else:
                was = p.confirmed
                p.last_true, p.detail = now, detail
            if p.confirmed and not was:
                changed = True
                opened.append(p)

        for kind, p in list(_problems.items()):
            if kind in conds:
                continue
            if not p.confirmed:
                # Flickered on and off before it counted. Forget it.
                del _problems[kind]
            elif now - p.last_true >= config.HEALTH_CLEAR_AFTER:
                del _problems[kind]
                changed = True
                recoveries.append((p, now))

        for p in _problems.values():
            if p.confirmed and (p.alerted_at is None
                                or now - p.alerted_at >= config.HEALTH_REALERT_SECONDS):
                p.alerted_at = now
                p.sample_id = _latest_sample.get(p.kind)
                alerts.append((p.kind, p.detail, p.first_seen, p.sample_id))

        counts = _counts(now)

    for p in opened:
        p.event_id = _open_event(p)
    for kind, detail, first_seen, sample_id in alerts:
        p = _problems.get(kind)
        sent = _send_alert(kind, detail, first_seen, sample_id, counts, now)
        if sent and p is not None and p.event_id:
            _mark_alerted(p.event_id)
    for p, ended in recoveries:
        _close_event(p)
        if p.alerted_at is not None:
            _send_recovery(p, ended)
    if changed:
        # Board payloads carry the customer notice; make them rebuild.
        state.mark_changed()


def _counts(now: float) -> dict[str, int]:
    since = now - config.HEALTH_WINDOW_SECONDS
    out: dict[str, int] = {}
    for t, outcome, _ in _events:
        if t >= since:
            out[outcome] = out.get(outcome, 0) + 1
    return out


# ── Reading ───────────────────────────────────────────────────────────────── #

def customer_notice() -> str | None:
    """What a customer's board should say, if anything. Only confirmed
    problems: a blip must not make a paying customer doubt the board."""
    with _lock:
        live = [k for k, p in _problems.items() if p.confirmed]
    for kind in ("blocked", "unreachable", "parse", "regression"):
        if kind in live:
            return KINDS[kind][1]
    return None


def snapshot(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        counts = _counts(now)
        problems = [{
            "kind": p.kind, "title": KINDS[p.kind][0], "detail": p.detail,
            "advice": KINDS[p.kind][2], "confirmed": p.confirmed,
            "since": datetime.fromtimestamp(p.first_seen, IST),
            "alerted": p.alerted_at is not None,
            "sample_id": _latest_sample.get(p.kind),
        } for p in _problems.values()]
        total = sum(counts.values())
        streak = len(_streak)
        last_answer = _last_answer_at
    if any(p["confirmed"] for p in problems):
        status = "problem"
    elif problems:
        status = "watching"
    elif total == 0:
        status = "idle"
    else:
        status = "ok"
    return {"status": status, "problems": problems, "counts": counts,
            "total": total, "streak": streak,
            "last_answer": _ago(last_answer, now),
            "window_min": config.HEALTH_WINDOW_SECONDS // 60}


# ── Persistence (best-effort) ─────────────────────────────────────────────── #

def _save_sample(kind, outcome, sample, trek_id, cell_date) -> int | None:
    if not storage.db_ready():
        return None
    body = sample.get("body") or ""
    try:
        with db.connection() as conn:
            r = conn.execute(
                "INSERT INTO health_samples (kind, outcome, http_status, url, trek_id,"
                " cell_date, note, headers, body, body_bytes)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (kind, outcome, sample.get("http_status"), sample.get("url"), trek_id,
                 cell_date, sample.get("note") or None,
                 json.dumps(sample.get("headers") or {}),
                 body[:config.HEALTH_SAMPLE_BYTES], len(body))).fetchone()
            conn.execute(
                "DELETE FROM health_samples WHERE id NOT IN"
                " (SELECT id FROM health_samples ORDER BY captured_at DESC, id DESC"
                "  LIMIT %s)", (config.HEALTH_SAMPLES_KEEP,))
        return r[0]
    except Exception as e:
        print(f"[Health] could not save sample: {e}")
        return None


def _open_event(p: Problem) -> int | None:
    if not storage.db_ready():
        return None
    try:
        with db.connection() as conn:
            r = conn.execute(
                "INSERT INTO health_events (kind, started_at, detail)"
                " VALUES (%s, to_timestamp(%s), %s) RETURNING id",
                (p.kind, p.first_seen, p.detail)).fetchone()
        return r[0]
    except Exception as e:
        print(f"[Health] could not record event: {e}")
        return None


def _mark_alerted(event_id: int) -> None:
    try:
        with db.connection() as conn:
            conn.execute("UPDATE health_events SET alerted_at = now()"
                         " WHERE id = %s AND alerted_at IS NULL", (event_id,))
    except Exception as e:
        print(f"[Health] could not mark event alerted: {e}")


def _close_event(p: Problem) -> None:
    if not p.event_id:
        return
    try:
        with db.connection() as conn:
            conn.execute("UPDATE health_events SET ended_at = now(), detail = %s"
                         " WHERE id = %s", (p.detail, p.event_id))
    except Exception as e:
        print(f"[Health] could not close event: {e}")


def recent_events(limit: int = 10) -> list[dict]:
    if not storage.db_ready():
        return []
    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, started_at, alerted_at, ended_at, detail"
                " FROM health_events ORDER BY started_at DESC LIMIT %s",
                (limit,)).fetchall()
    except Exception as e:
        print(f"[Health] could not list events: {e}")
        return []
    return [{"id": r[0], "kind": r[1], "title": KINDS.get(r[1], (r[1],))[0],
             "started": r[2], "alerted": r[3], "ended": r[4], "detail": r[5]}
            for r in rows]


def recent_samples(limit: int = 10) -> list[dict]:
    if not storage.db_ready():
        return []
    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, outcome, http_status, trek_id, cell_date, note,"
                " body_bytes, captured_at FROM health_samples"
                " ORDER BY captured_at DESC, id DESC LIMIT %s", (limit,)).fetchall()
    except Exception as e:
        print(f"[Health] could not list samples: {e}")
        return []
    return [{"id": r[0], "kind": r[1], "outcome": r[2], "status": r[3],
             "trek_id": r[4], "date": r[5], "note": r[6], "bytes": r[7],
             "captured": r[8]} for r in rows]


def get_sample(sample_id: int) -> dict | None:
    with db.connection() as conn:
        r = conn.execute(
            "SELECT id, kind, outcome, http_status, url, trek_id, cell_date, note,"
            " headers, body, body_bytes, captured_at FROM health_samples WHERE id = %s",
            (sample_id,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "kind": r[1], "outcome": r[2], "status": r[3], "url": r[4],
            "trek_id": r[5], "date": r[6], "note": r[7], "headers": r[8] or {},
            "body": r[9] or "", "bytes": r[10], "captured": r[11]}


# ── Email ─────────────────────────────────────────────────────────────────── #

def _recipients() -> list[str]:
    out = []
    if storage.db_ready():
        try:
            out = accounts.admin_emails()
        except Exception as e:
            print(f"[Health] could not load admin emails: {e}")
    if config.ALERT_EMAIL:
        out.append(config.ALERT_EMAIL)
    seen, unique = set(), []
    for e in out:
        if e and e.lower() not in seen:
            seen.add(e.lower())
            unique.append(e)
    return unique


def _fmt(t: float) -> str:
    return datetime.fromtimestamp(t, IST).strftime("%d %b %Y, %H:%M IST")


def _send_alert(kind, detail, first_seen, sample_id, counts, now) -> bool:
    title, notice, advice = KINDS[kind]
    base = config.PUBLIC_BASE_URL
    mix = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    lines = [
        f"{title}",
        f"Since {_fmt(first_seen)} ({int((now - first_seen) // 60)} min so far).",
        "",
        detail,
        "",
        "What customers see on their board:",
        f"  {notice}",
        "",
        "What to check:",
        f"  {advice}",
        "",
        f"Portal responses, last {config.HEALTH_WINDOW_SECONDS // 60} min: {mix}",
    ]
    if sample_id:
        lines.append(f"Saved page: {base}/admin/health/sample/{sample_id}")
    lines += [
        f"Admin console: {base}/admin",
        "",
        "You'll get another email when this clears, and a reminder every "
        f"{config.HEALTH_REALERT_SECONDS // 3600} hours while it lasts.",
        "",
        "— Aranya health monitor",
    ]
    to = _recipients()
    if not to:
        print(f"[Health] ALERT with no recipients: {title} — {detail}")
        return False
    print(f"[Health] ALERT {kind}: {detail}")
    ok = False
    for addr in to:
        ok = mail.send(addr, f"[Aranya] Problem: {title}", "\n".join(lines)) or ok
    return ok


def _send_recovery(p: Problem, ended: float) -> None:
    title = KINDS[p.kind][0]
    lasted = max(1, int((p.last_true - p.first_seen) // 60))
    text = (f"Resolved: {title}\n\n"
            f"Started {_fmt(p.first_seen)}, last seen {_fmt(p.last_true)} "
            f"(about {lasted} min).\n\n"
            f"The portal is answering normally again and customers' boards no "
            f"longer show a warning. Seat counts refresh on the usual schedule.\n\n"
            f"Last detail: {p.detail}\n\n"
            f"Admin console: {config.PUBLIC_BASE_URL}/admin\n\n"
            f"— Aranya health monitor\n")
    print(f"[Health] RESOLVED {p.kind}")
    for addr in _recipients():
        mail.send(addr, f"[Aranya] Resolved: {title}", text)

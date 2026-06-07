#!/usr/bin/env python3
"""
KARNATAKA TREK SLOT MONITOR  (Aranya Vihaara)
=============================================
Monitors seat availability for one selected trek, tomorrow -> +15 days.
No login required.

  • Discovers every trek across the configured districts via /get-treks
    and fills a dropdown automatically.
  • Reads the public /availability page for each date and parses the real
    seat counts the portal itself shows (the clamped 0/300 values).
  • A date is "NOT RELEASED" when the portal serves back a different date
    than requested (detected via the page's own dateDisplay).
  • Alarms when any Saturday slot is open.

Usage:
    pip install flask requests beautifulsoup4
    python monitor.py
    -> open http://localhost:5000
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

# ── Config ────────────────────────────────────────────────────────────────── #

BASE        = "https://aranyavihaara.karnataka.gov.in"
DAYS_AHEAD  = 16          # tomorrow .. +15 days
CYCLE_SLEEP = 2.0         # seconds between full sweeps
WORKERS     = 6           # concurrent date fetches per sweep

# district_id -> display name. These drive trek discovery.
DISTRICTS = {
    4:  "Kalaburagi",
    15: "Shivamogga",
    16: "Udupi",
    17: "Chikkamagaluru",
    19: "Kolar",
    21: "Bengaluru Gramantara",
    24: "Dakshina Kannada",
    25: "Kodagu",
    28: "Chamarajanagara",
    29: "Ramanagara",
}

DEFAULT_TREK_ID = 113     # Nethravathi, if present

# ── Shared state ──────────────────────────────────────────────────────────── #

registry  = {"treks": [], "ready": False, "error": None}   # discovered treks
selection = {"trek_id": None}                              # trek being monitored
state     = {"dates": {}, "cycle": 0, "last_update": None,
             "error": None, "trek_name": None}
lock      = threading.Lock()

# ── HTTP ──────────────────────────────────────────────────────────────────── #

def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_csrf(session):
    """GET a public page, scrape the _token, establish the session cookie."""
    try:
        r = session.get(f"{BASE}/login", timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("input", {"name": "_token"}) or \
              soup.find("meta", {"name": "_token"})
        if tag:
            return tag.get("value") or tag.get("content")
    except Exception as e:
        print(f"[csrf] {e}")
    return None


def fetch_treks_for_district(session, csrf, district_id):
    """POST /get-treks -> list of trek dicts for one district."""
    try:
        r = session.post(f"{BASE}/get-treks", data={
            "_token": csrf,
            "district_id": str(district_id),
        }, timeout=12, headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[get-treks] district {district_id}: {e}")
    return []


def fetch_availability(session, csrf, district_id, trek_id, date_ddmmyyyy):
    """POST /availability for one date -> raw HTML (or None)."""
    try:
        r = session.post(f"{BASE}/availability", data={
            "_token": csrf,
            "district": str(district_id),
            "trek": str(trek_id),
            "check_in": date_ddmmyyyy,
        }, timeout=15)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"[avail] {trek_id} {date_ddmmyyyy}: {e}")
    return None


# ── Parsing ───────────────────────────────────────────────────────────────── #

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}


def parse_displayed_date(html):
    """Date the page reports (e.g. 'Monday, 22nd June 2026') -> date obj."""
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
    """Extract [{name, available, capacity}] from slot cards."""
    slots = []
    for card in soup.select(".slot_card"):
        name_el  = card.select_one(".slot_text")
        avail_el = card.select_one(".available_text")
        name = name_el.get_text(" ", strip=True) if name_el else "?"
        avail_text = avail_el.get_text(" ", strip=True) if avail_el else ""
        m = re.search(r"(\d+)\s*/\s*(\d+)", avail_text)
        if m:
            slots.append({
                "name": re.sub(r"\s+", " ", name).strip(),
                "available": int(m.group(1)),
                "capacity": int(m.group(2)),
            })
    return slots


def check_one(session, csrf, district_id, trek_id, d):
    """Fetch + parse one date for one trek -> dashboard record."""
    rec = {
        "date": d.strftime("%Y-%m-%d"),
        "display": d.strftime("%d %b %Y"),
        "day": d.strftime("%A"),
        "saturday": d.strftime("%A") == "Saturday",
        "released": False,
        "slots": [],
        "checked": datetime.now().isoformat(),
    }
    html = fetch_availability(session, csrf, district_id, trek_id,
                              d.strftime("%d-%m-%Y"))
    if not html:
        return rec

    shown_date, soup = parse_displayed_date(html)
    slots = parse_slots(soup)
    if shown_date == d.date() and slots:
        rec["released"] = True
        rec["slots"] = slots
    return rec


# ── Discovery ─────────────────────────────────────────────────────────────── #

def discover_treks(session, csrf):
    """Pull every active trek across the configured districts."""
    treks = []
    for did, dname in DISTRICTS.items():
        for t in fetch_treks_for_district(session, csrf, did):
            if t.get("id") and t.get("is_active", 1) == 1:
                tdid = t.get("district_id", did)
                treks.append({
                    "id": t["id"],
                    "name": t.get("name") or f"Trek {t['id']}",
                    "district_id": tdid,
                    "district_name": DISTRICTS.get(tdid, dname),
                })
        time.sleep(0.2)
    # de-dupe by trek id, then sort
    seen, unique = set(), []
    for t in treks:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)
    unique.sort(key=lambda x: (x["district_name"], x["name"]))
    return unique


# ── Background poller ─────────────────────────────────────────────────────── #

def poll_loop():
    session = new_session()

    # Phase 1: discover treks (retry until we have them)
    while not registry["ready"]:
        csrf = fetch_csrf(session)
        if csrf:
            treks = discover_treks(session, csrf)
            if treks:
                with lock:
                    registry["treks"] = treks
                    registry["ready"] = True
                    registry["error"] = None
                    if selection["trek_id"] is None:
                        ids = {t["id"] for t in treks}
                        selection["trek_id"] = (
                            DEFAULT_TREK_ID if DEFAULT_TREK_ID in ids
                            else treks[0]["id"])
                print(f"[discovery] {len(treks)} treks found")
                break
        with lock:
            registry["error"] = "Discovering treks…"
        time.sleep(3)

    # Phase 2: monitor the selected trek
    cycle = 0
    last_trek = None
    while True:
        try:
            csrf = fetch_csrf(session)
            if not csrf:
                with lock:
                    state["error"] = "Cannot reach portal — retrying…"
                time.sleep(3)
                continue

            with lock:
                tid = selection["trek_id"]
            trek = next((t for t in registry["treks"] if t["id"] == tid), None)
            if not trek:
                time.sleep(2)
                continue

            # On trek switch, wipe old grid so we don't show stale data
            if tid != last_trek:
                with lock:
                    state["dates"] = {}
                    state["trek_name"] = trek["name"]
                last_trek = tid

            tomorrow = datetime.now() + timedelta(days=1)
            dates = [tomorrow + timedelta(days=i) for i in range(DAYS_AHEAD)]

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                records = list(pool.map(
                    lambda d: check_one(session, csrf,
                                        trek["district_id"], tid, d), dates))

            cycle += 1
            now = datetime.now().isoformat()
            with lock:
                # only commit if user hasn't switched mid-sweep
                if selection["trek_id"] == tid:
                    for rec in records:
                        state["dates"][rec["date"]] = rec
                    state["cycle"] = cycle
                    state["last_update"] = now
                    state["trek_name"] = trek["name"]
                    state["error"] = None

            time.sleep(CYCLE_SLEEP)

        except Exception as e:
            with lock:
                state["error"] = str(e)
            print(f"[poll_loop] {e}")
            time.sleep(4)


# ── Flask ─────────────────────────────────────────────────────────────────── #

app = Flask(__name__)

@app.route("/")
def index():
    return DASHBOARD

@app.route("/api/treks")
def api_treks():
    with lock:
        return jsonify({
            "ready": registry["ready"],
            "error": registry["error"],
            "treks": registry["treks"],
            "current": selection["trek_id"],
        })

@app.route("/api/select", methods=["POST"])
def api_select():
    body = request.get_json(silent=True) or {}
    tid = body.get("trek_id")
    with lock:
        if any(t["id"] == tid for t in registry["treks"]):
            selection["trek_id"] = tid
            state["dates"] = {}            # reset immediately
            return jsonify({"ok": True, "trek_id": tid})
    return jsonify({"ok": False}), 400

@app.route("/api/data")
def api_data():
    with lock:
        return jsonify({**state, "current": selection["trek_id"]})


# ── Dashboard ─────────────────────────────────────────────────────────────── #

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Karnataka Trek Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080c14;--surface:#0f1520;--border:#1e2a40;--border-hi:#2a3a58;
  --text:#d4dae6;--text-dim:#6b7a94;--text-bright:#f0f4fa;
  --green:#22c55e;--red:#ef4444;--amber:#f59e0b;
  --sat:#facc15;--sat-glow:rgba(250,204,21,.12);--radius:10px;
}
html{font-size:15px}
body{background:var(--bg);color:var(--text);font-family:'Outfit',sans-serif;min-height:100vh;overflow-x:hidden}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.05) 2px,rgba(0,0,0,.05) 4px)}

.header{padding:20px 32px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;
  background:linear-gradient(180deg,#0e1422,var(--bg))}
.brand{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand h1{font-family:'IBM Plex Mono',monospace;font-size:1.05rem;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;color:var(--text-bright);
  display:flex;align-items:center;gap:9px;white-space:nowrap}
.brand h1 .peak{font-size:1.25rem}
.trek-select{background:var(--surface);color:var(--text-bright);border:1px solid var(--border-hi);
  border-radius:8px;padding:9px 14px;font-family:'Outfit',sans-serif;font-size:.9rem;font-weight:600;
  cursor:pointer;min-width:230px;max-width:340px}
.trek-select:focus{outline:none;border-color:var(--amber)}
.trek-select optgroup{background:#0a0e17;color:var(--text-dim);font-weight:700}
.trek-select option{background:var(--surface);color:var(--text);font-weight:500}
.status-bar{display:flex;align-items:center;gap:18px;font-size:.82rem;font-family:'IBM Plex Mono',monospace}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:pd 2s ease infinite}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pd{0%,100%{opacity:1}50%{opacity:.4}}
.dim{color:var(--text-dim)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;padding:28px 32px}

.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 16px;position:relative;overflow:hidden;transition:.25s}
.card:hover{transform:translateY(-2px);border-color:var(--border-hi)}
.day-label{font-family:'IBM Plex Mono',monospace;font-size:.72rem;font-weight:600;
  letter-spacing:.12em;text-transform:uppercase;color:var(--text-dim);margin-bottom:2px}
.date-label{font-size:1.15rem;font-weight:700;color:var(--text-bright);margin-bottom:14px}
.slot-row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;
  border-top:1px solid var(--border)}
.slot-name{color:var(--text-dim);font-size:.68rem;font-family:'IBM Plex Mono',monospace}
.seat-open{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:1.05rem;
  color:var(--green);text-shadow:0 0 8px rgba(34,197,94,.4)}
.seat-cap{font-weight:400;font-size:.75rem;color:var(--text-dim);text-shadow:none}
.seat-sold{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:.72rem;
  color:var(--red);letter-spacing:.06em;opacity:.7}
.checked-at{margin-top:10px;font-size:.65rem;color:var(--text-dim);
  font-family:'IBM Plex Mono',monospace;text-align:right}

.card.not-released{border-style:dashed;
  background:repeating-linear-gradient(-45deg,var(--surface),var(--surface) 6px,#111827 6px,#111827 12px)}
.nr-badge{font-family:'IBM Plex Mono',monospace;font-size:.78rem;color:var(--text-dim);
  text-align:center;padding:18px 0;letter-spacing:.05em}

.card.saturday{border-color:#614213;
  background:linear-gradient(135deg,var(--surface) 60%,rgba(245,158,11,.04))}
.card.saturday .day-label{color:var(--sat)}
.card.saturday.has-seats{border-color:var(--sat);
  box-shadow:0 0 25px var(--sat-glow),inset 0 0 25px var(--sat-glow);
  animation:sp 1.2s ease-in-out infinite}
@keyframes sp{0%,100%{box-shadow:0 0 20px var(--sat-glow),inset 0 0 20px var(--sat-glow)}
  50%{box-shadow:0 0 40px rgba(250,204,21,.25),inset 0 0 40px rgba(250,204,21,.18)}}

.alarm-banner{display:none;position:fixed;bottom:0;left:0;right:0;z-index:1000;padding:18px 32px;
  background:linear-gradient(90deg,#78350f,#92400e,#78350f);background-size:200% 100%;
  animation:ab 1.5s linear infinite;border-top:2px solid var(--sat);
  font-family:'IBM Plex Mono',monospace;align-items:center;justify-content:space-between}
.alarm-banner.visible{display:flex}
@keyframes ab{0%{background-position:0 50%}100%{background-position:200% 50%}}
.alarm-text{color:var(--sat);font-weight:700;font-size:1rem;display:flex;align-items:center;gap:10px}
.bell{animation:ring .4s ease infinite alternate}
@keyframes ring{0%{transform:rotate(-12deg)}100%{transform:rotate(12deg)}}
.alarm-banner button{background:rgba(0,0,0,.4);border:1px solid var(--sat);color:var(--sat);
  padding:8px 18px;border-radius:6px;cursor:pointer;font-family:'IBM Plex Mono',monospace;
  font-size:.8rem;font-weight:600}
.alarm-banner button:hover{background:rgba(0,0,0,.7)}

.error-bar{display:none;padding:10px 32px;background:#5c1a1a;color:var(--red);
  font-family:'IBM Plex Mono',monospace;font-size:.8rem;border-bottom:1px solid var(--red)}
.error-bar.visible{display:block}

@media(max-width:600px){.header{padding:14px 16px}
  .trek-select{min-width:100%}
  .grid{padding:16px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .card{padding:14px 12px}}
</style>
</head>
<body>

<div class="header">
  <div class="brand">
    <h1><span class="peak">⛰</span> TREK MONITOR</h1>
    <select class="trek-select" id="trekSelect" disabled>
      <option>Loading treks…</option>
    </select>
  </div>
  <div class="status-bar">
    <span><span class="dot" id="statusDot"></span> <span id="statusText">Connecting…</span></span>
    <span class="dim">Sweep <span id="cycleNum">0</span></span>
    <span class="dim" id="lastUpdate">—</span>
  </div>
</div>

<div class="error-bar" id="errorBar"></div>

<div class="grid" id="grid">
  <div style="grid-column:1/-1;text-align:center;padding:60px 0;color:var(--text-dim);font-family:'IBM Plex Mono',monospace">
    Waiting for first sweep…
  </div>
</div>

<div class="alarm-banner" id="alarmBanner">
  <div class="alarm-text"><span class="bell">🔔</span> <span id="alarmMsg">SATURDAY SLOT OPEN!</span></div>
  <button onclick="silenceAlarm()">SILENCE</button>
</div>

<script>
let audioCtx=null, alarmInterval=null, alarmSilenced=false, treksLoaded=false;

function startAlarm(){
  if(alarmInterval||alarmSilenced) return;
  try{ audioCtx = audioCtx || new (window.AudioContext||window.webkitAudioContext)(); }catch(e){return;}
  alarmInterval = setInterval(()=>{
    [0,200,400].forEach(delay=>setTimeout(()=>{
      try{
        const o=audioCtx.createOscillator(), g=audioCtx.createGain();
        o.connect(g); g.connect(audioCtx.destination); o.type='square';
        o.frequency.setValueAtTime(880,audioCtx.currentTime);
        o.frequency.setValueAtTime(660,audioCtx.currentTime+0.08);
        g.gain.setValueAtTime(0.15,audioCtx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.001,audioCtx.currentTime+0.15);
        o.start(audioCtx.currentTime); o.stop(audioCtx.currentTime+0.15);
      }catch(e){}
    },delay));
  },1200);
}
function stopAlarm(){ if(alarmInterval){clearInterval(alarmInterval);alarmInterval=null;} }
function silenceAlarm(){ alarmSilenced=true; stopAlarm();
  document.getElementById('alarmBanner').classList.remove('visible'); }

function ago(iso){
  if(!iso) return '—';
  const s=Math.round((Date.now()-new Date(iso).getTime())/1000);
  if(s<3) return 'just now';
  if(s<60) return s+'s ago';
  return Math.floor(s/60)+'m ago';
}

// ── Trek dropdown ──
async function loadTreks(){
  try{
    const r=await fetch('/api/treks'); const data=await r.json();
    if(!data.ready){ setTimeout(loadTreks,1500); return; }
    const sel=document.getElementById('trekSelect');
    const byDist={};
    data.treks.forEach(t=>{ (byDist[t.district_name]=byDist[t.district_name]||[]).push(t); });
    let html='';
    Object.keys(byDist).sort().forEach(dist=>{
      html+='<optgroup label="'+dist+'">';
      byDist[dist].forEach(t=>{
        html+='<option value="'+t.id+'"'+(t.id===data.current?' selected':'')+'>'+t.name+'</option>';
      });
      html+='</optgroup>';
    });
    sel.innerHTML=html; sel.disabled=false; treksLoaded=true;

    sel.addEventListener('change', async ()=>{
      const tid=parseInt(sel.value,10);
      document.getElementById('grid').innerHTML=
        '<div style="grid-column:1/-1;text-align:center;padding:60px 0;color:var(--text-dim);font-family:IBM Plex Mono,monospace">Switching trek…</div>';
      silenceAlarm(); alarmSilenced=false;
      await fetch('/api/select',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({trek_id:tid})});
    });
  }catch(e){ setTimeout(loadTreks,2000); }
}

function render(data){
  const grid=document.getElementById('grid');
  const dates=data.dates||{};
  const keys=Object.keys(dates).sort();
  if(!keys.length) return;

  let html='', satOpen=false, satList=[];
  for(const k of keys){
    const d=dates[k], isSat=d.saturday, released=d.released;
    const total = released ? d.slots.reduce((s,x)=>s+(x.available||0),0) : 0;
    if(isSat && released && total>0){ satOpen=true; satList.push(d.display+' ('+total+')'); }

    let cls='card';
    if(!released) cls+=' not-released';
    if(isSat) cls+=' saturday';
    if(isSat && released && total>0) cls+=' has-seats';

    html+='<div class="'+cls+'">';
    html+='<div class="day-label">'+d.day+(isSat?' ★':'')+'</div>';
    html+='<div class="date-label">'+d.display+'</div>';
    if(!released){
      html+='<div class="nr-badge">⏳ NOT RELEASED</div>';
    }else{
      for(const sl of d.slots){
        html+='<div class="slot-row"><span class="slot-name">'+sl.name+'</span>';
        if(sl.available>0)
          html+='<span class="seat-open">'+sl.available+'<span class="seat-cap">/'+sl.capacity+'</span></span>';
        else
          html+='<span class="seat-sold">SOLD OUT</span>';
        html+='</div>';
      }
    }
    html+='<div class="checked-at">'+ago(d.checked)+'</div></div>';
  }
  grid.innerHTML=html;

  const banner=document.getElementById('alarmBanner');
  if(satOpen){
    document.getElementById('alarmMsg').textContent='🔔 '+(data.trek_name||'')+' — SATURDAY OPEN: '+satList.join(' | ');
    banner.classList.add('visible');
    if(!alarmSilenced) startAlarm();
  }else{
    banner.classList.remove('visible'); stopAlarm(); alarmSilenced=false;
  }
}

function updateStatus(data){
  const dot=document.getElementById('statusDot'), txt=document.getElementById('statusText'),
        err=document.getElementById('errorBar');
  if(data.error){ dot.className='dot err'; txt.textContent='Error';
    err.textContent=data.error; err.classList.add('visible'); }
  else{ dot.className='dot'; txt.textContent=data.trek_name?('Live · '+data.trek_name):'Live';
    err.classList.remove('visible'); }
  document.getElementById('cycleNum').textContent=data.cycle||0;
  document.getElementById('lastUpdate').textContent=ago(data.last_update);
}

async function poll(){
  try{
    const r=await fetch('/api/data'); const data=await r.json();
    render(data); updateStatus(data);
  }catch(e){
    document.getElementById('statusDot').className='dot err';
    document.getElementById('statusText').textContent='Disconnected';
  }
}

loadTreks();
setInterval(poll,2000); poll();

document.addEventListener('click',()=>{
  if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)();
  if(audioCtx.state==='suspended') audioCtx.resume();
},{once:true});
</script>
</body>
</html>
"""

# ── Entry ─────────────────────────────────────────────────────────────────── #

if __name__ == "__main__":
    print("=" * 56)
    print("  KARNATAKA TREK SLOT MONITOR")
    print(f"  Discovering treks across {len(DISTRICTS)} districts…")
    print(f"  Window: tomorrow -> +{DAYS_AHEAD - 1} days  |  sweep ~{CYCLE_SLEEP}s")
    print("  http://localhost:5000")
    print("=" * 56)

    threading.Thread(target=poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
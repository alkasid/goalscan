"""
GOAL BOT — main.py
──────────────────────────────────────────────────────────────────
- Recupera match oggi + domani + dopodomani (solo campionati)
- Stagione rilevata dinamicamente per ogni lega
- Per ogni match: ultime 5 gare FT di HOME e AWAY nella stessa lega
- ALERT se ENTRAMBE hanno (goal fatti + goal subiti) >= soglia
- Verifica quote Bet365 solo sugli alert (chiamate minime)
- Genera docs/index.html diviso per giorno + fascia oraria
- ThreadPoolExecutor max_workers=3 (evita 429)
- Cache in memoria per evitare chiamate duplicate
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ── Config ───────────────────────────────────────────────────────────────────
with open("config.json", encoding="utf-8-sig") as f:
    CFG = json.load(f)

API_KEY   = (os.environ.get("API_FOOTBALL_KEY") or CFG.get("api_football_key", "")).strip()
THRESHOLD = int(CFG.get("goal_threshold", 14))
LAST_N    = int(CFG.get("last_matches_count", 5))
BASE_URL  = "https://v3.football.api-sports.io"
HEADERS   = {"x-apisports-key": API_KEY}
BET365_ID = 8
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT  = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

SKIP_KEYWORDS = ["u17","u18","u19","u20","u21","u23","youth","reserve","women"," w ","u-17","u-20","u-21","u-23"]

# ── Cache in memoria ─────────────────────────────────────────────────────────
_cache = {}
_api_sem = threading.Semaphore(3)  # max 3 chiamate contemporanee

# ── Cache persistente su disco (24h) ─────────────────────────────────────────
_DISK_CACHE_FILE = Path("cache_teams.json")

def _load_disk_cache():
    if _DISK_CACHE_FILE.exists():
        try:
            data = json.loads(_DISK_CACHE_FILE.read_text())
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") == today:
                return data.get("teams", {})
        except Exception:
            pass
    return {}

def _save_disk_cache(teams_data):
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _DISK_CACHE_FILE.write_text(json.dumps({"date": today, "teams": teams_data}))
    except Exception:
        pass

_disk_cache = _load_disk_cache()

# ── HTTP ─────────────────────────────────────────────────────────────────────
def api_get(endpoint, params=None, retries=3):
    with _api_sem:
        time.sleep(0.15)  # max ~6 req/sec per thread
        for attempt in range(retries + 1):
            try:
                r = requests.get(f"{BASE_URL}/{endpoint}",
                                 headers=HEADERS, params=params, timeout=15)
                if r.status_code == 200:
                    return r.json().get("response", [])
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    print(f"    [429] rate limit — attendo {wait}s...")
                    time.sleep(wait)
                    continue
                print(f"    [HTTP {r.status_code}] {endpoint} {params}")
            except Exception as e:
                print(f"    [ERR] {endpoint}: {e}")
        return []

# ── Fixtures 3 giorni (solo campionati) ──────────────────────────────────────
def get_all_fixtures():
    today = datetime.now(timezone.utc)
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]

    league_seasons     = {}
    fixtures_by_league = defaultdict(list)

    for date in dates:
        data = api_get("fixtures", {"date": date, "status": "NS-1H-HT-2H-ET-P-FT"})
        print(f"  {date}: {len(data)} match raw")
        for fix in data:
            name_lower = fix.get("league", {}).get("name", "").lower()
            if any(k in name_lower for k in SKIP_KEYWORDS):
                continue
            lid    = fix.get("league", {}).get("id")
            season = fix.get("league", {}).get("season")
            if lid is None:
                continue
            if lid not in league_seasons:
                league_seasons[lid] = season
            fix["_season"] = season
            fixtures_by_league[lid].append(fix)

    total = sum(len(v) for v in fixtures_by_league.values())
    print(f"  -> {len(league_seasons)} leghe | {total} match filtrati (3 giorni, no youth/women)")
    return league_seasons, fixtures_by_league

# ── Ultime N gare FT nella stessa lega (con cache) ───────────────────────────
def get_last_n(team_id, league_id, season):
    key = (team_id, league_id, season)
    if key in _cache:
        return _cache[key]

    # Controlla cache su disco (stessa giornata)
    disk_key = f"{team_id}_{league_id}_{season}"
    if disk_key in _disk_cache:
        result = _disk_cache[disk_key]
        _cache[key] = result
        return result

    data = api_get("fixtures", {
        "team":   team_id,
        "league": league_id,
        "season": season,
        "last":   LAST_N,
    })

    finished = [
        m for m in data
        if m.get("fixture", {}).get("status", {}).get("short") == "FT"
    ]

    if len(finished) < LAST_N:
        _cache[key] = None
        _disk_cache[disk_key] = None
        _save_disk_cache(_disk_cache)
        return None

    scored = conceded = 0
    for m in finished[:LAST_N]:
        goals   = m.get("goals", {})
        teams   = m.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        if is_home:
            scored += gh; conceded += ga
        else:
            scored += ga; conceded += gh

    result = {"scored": scored, "conceded": conceded,
              "total": scored + conceded,
              "qualifies": (scored + conceded) >= THRESHOLD}
    _cache[key] = result
    _disk_cache[disk_key] = result
    _save_disk_cache(_disk_cache)
    return result

# ── Verifica quote Bet365 per un fixture ─────────────────────────────────────
def has_bet365_odds(fixture_id):
    data = api_get("odds", {"fixture": fixture_id, "bookmaker": BET365_ID})
    return len(data) > 0

# ── Analisi singolo match ────────────────────────────────────────────────────
def analyze_fixture(fix):
    fixture     = fix.get("fixture", {})
    teams       = fix.get("teams", {})
    league      = fix.get("league", {})
    league_id   = fix["_league_id"]
    season      = fix["_season"]
    home_id     = teams.get("home", {}).get("id")
    away_id     = teams.get("away", {}).get("id")
    home_name   = teams.get("home", {}).get("name", "?")
    away_name   = teams.get("away", {}).get("name", "?")
    league_name = league.get("name", "?")
    country     = league.get("country", "?")
    fixture_id  = fixture.get("id")

    try:
        ko = (datetime.fromtimestamp(
            fixture.get("timestamp", 0), tz=timezone.utc
        ) + timedelta(hours=1)).strftime("%H:%M")
        match_date = (datetime.fromtimestamp(
            fixture.get("timestamp", 0), tz=timezone.utc
        ) + timedelta(hours=1)).strftime("%Y-%m-%d")
    except Exception:
        ko = "--:--"; match_date = "?"

    hs  = get_last_n(home_id, league_id, season)
    as_ = get_last_n(away_id, league_id, season)

    if hs is None or as_ is None:
        missing = []
        if hs  is None: missing.append(home_name)
        if as_ is None: missing.append(away_name)
        return None, f"SKIP: <{LAST_N} FT per {', '.join(missing)}"

    if not (hs["qualifies"] and as_["qualifies"]):
        reasons = []
        if not hs["qualifies"]: reasons.append(f"{home_name}:{hs['total']}<{THRESHOLD}")
        if not as_["qualifies"]: reasons.append(f"{away_name}:{as_['total']}<{THRESHOLD}")
        return None, f"✗ {' | '.join(reasons)}"

    # Passa il filtro goal — verifica quote Bet365
    odds_ok = has_bet365_odds(fixture_id)
    if not odds_ok:
        return None, f"✅ goal OK ma ❌ no quote Bet365 — {home_name}:{hs['total']} {away_name}:{as_['total']}"

    match_status = fixture.get("status", {}).get("short", "NS")
    return {"home": home_name, "away": away_name,
            "home_stats": hs, "away_stats": as_,
            "league": league_name, "country": country, "kickoff": ko,
            "date": match_date, "fixture_id": fixture_id,
            "status": match_status}, \
           f"✅✅ ALERT+QUOTE | {home_name}:{hs['total']} {away_name}:{as_['total']}"



# ── Telegram ─────────────────────────────────────────────────────────────────
def _tg_send(text):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=15)
        if r.status_code == 200:
            print("  [TG] Messaggio inviato")
        else:
            print("  [TG] Errore " + str(r.status_code) + ": " + r.text[:200])
    except Exception as e:
        print("  [TG] Exception: " + str(e))


def send_telegram(matches, total_analyzed, run_date):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  [TG] Token o Chat ID mancante — skip")
        return

    today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d1_s    = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    d2_s    = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    day_label = {today_s: "OGGI", d1_s: "DOMANI", d2_s: "DOPODOMANI"}

    if not matches:
        _tg_send("Nessun alert su " + str(total_analyzed) + " match analizzati.")
        return

    # Mappa paese → bandiera
    FLAGS = {
        "Afghanistan":"🇦🇫","Albania":"🇦🇱","Algeria":"🇩🇿","Argentina":"🇦🇷",
        "Armenia":"🇦🇲","Australia":"🇦🇺","Austria":"🇦🇹","Azerbaijan":"🇦🇿",
        "Bangladesh":"🇧🇩","Belarus":"🇧🇾","Belgium":"🇧🇪","Bolivia":"🇧🇴",
        "Bosnia and Herzegovina":"🇧🇦","Brazil":"🇧🇷","Bulgaria":"🇧🇬",
        "Cambodia":"🇰🇭","Cameroon":"🇨🇲","Canada":"🇨🇦","Chile":"🇨🇱",
        "China":"🇨🇳","Colombia":"🇨🇴","Costa Rica":"🇨🇷","Croatia":"🇭🇷",
        "Cyprus":"🇨🇾","Czech Republic":"🇨🇿","Denmark":"🇩🇰","Ecuador":"🇪🇨",
        "Egypt":"🇪🇬","England":"🏴󠁧󠁢󠁥󠁮󠁧󠁿","Estonia":"🇪🇪","Ethiopia":"🇪🇹",
        "Finland":"🇫🇮","France":"🇫🇷","Gambia":"🇬🇲","Georgia":"🇬🇪",
        "Germany":"🇩🇪","Ghana":"🇬🇭","Gibraltar":"🇬🇮","Greece":"🇬🇷",
        "Guatemala":"🇬🇹","Guinea":"🇬🇳","Honduras":"🇭🇳","Hungary":"🇭🇺",
        "Iceland":"🇮🇸","India":"🇮🇳","Indonesia":"🇮🇩","Iran":"🇮🇷",
        "Iraq":"🇮🇶","Ireland":"🇮🇪","Israel":"🇮🇱","Italy":"🇮🇹",
        "Jamaica":"🇯🇲","Japan":"🇯🇵","Jordan":"🇯🇴","Kazakhstan":"🇰🇿",
        "Kenya":"🇰🇪","Kosovo":"🇽🇰","Kuwait":"🇰🇼","Kyrgyzstan":"🇰🇬",
        "Laos":"🇱🇦","Latvia":"🇱🇻","Lesotho":"🇱🇸","Lithuania":"🇱🇹",
        "Luxembourg":"🇱🇺","Macedonia":"🇲🇰","Malaysia":"🇲🇾","Mali":"🇲🇱",
        "Malta":"🇲🇹","Mexico":"🇲🇽","Moldova":"🇲🇩","Montenegro":"🇲🇪",
        "Morocco":"🇲🇦","Myanmar":"🇲🇲","Netherlands":"🇳🇱","New Zealand":"🇳🇿",
        "Nicaragua":"🇳🇮","Nigeria":"🇳🇬","Northern Ireland":"🇬🇧","Norway":"🇳🇴",
        "Oman":"🇴🇲","Panama":"🇵🇦","Paraguay":"🇵🇾","Peru":"🇵🇪",
        "Philippines":"🇵🇭","Poland":"🇵🇱","Portugal":"🇵🇹","Romania":"🇷🇴",
        "Russia":"🇷🇺","Rwanda":"🇷🇼","Saudi Arabia":"🇸🇦","Scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿",
        "Senegal":"🇸🇳","Serbia":"🇷🇸","Singapore":"🇸🇬","Slovakia":"🇸🇰",
        "Slovenia":"🇸🇮","South Africa":"🇿🇦","South Korea":"🇰🇷","Spain":"🇪🇸",
        "Sweden":"🇸🇪","Switzerland":"🇨🇭","Syria":"🇸🇾","Tajikistan":"🇹🇯",
        "Tanzania":"🇹🇿","Thailand":"🇹🇭","Trinidad and Tobago":"🇹🇹",
        "Tunisia":"🇹🇳","Turkey":"🇹🇷","Uganda":"🇺🇬","Ukraine":"🇺🇦",
        "United Arab Emirates":"🇦🇪","Uruguay":"🇺🇾","USA":"🇺🇸",
        "Uzbekistan":"🇺🇿","Venezuela":"🇻🇪","Vietnam":"🇻🇳","Wales":"🏴󠁧󠁢󠁷󠁬󠁳󠁿",
        "World":"🌍","Yemen":"🇾🇪","Zambia":"🇿🇲","Zimbabwe":"🇿🇼",
        "Burundi":"🇧🇮","Congo DR":"🇨🇩","Faroe Islands":"🇫🇴",
        "Bosnia":"🇧🇦","Andorra":"🇦🇩","Bahrain":"🇧🇭","Benin":"🇧🇯",
    }

    # Formato data leggibile
    def fmt_day(date_str):
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            giorni = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"]
            mesi   = ["Gen","Feb","Mar","Apr","Mag","Giu","Lug","Ago","Set","Ott","Nov","Dic"]
            return giorni[dt.weekday()] + " " + str(dt.day) + " " + mesi[dt.month-1]
        except:
            return date_str

    lines = []
    lines.append("⚽ *GOAL SCAN* — " + run_date)
    lines.append("📊 Analizzati: *" + str(total_analyzed) + "* | 🎯 Alert: *" + str(len(matches)) + "*")
    lines.append("📌 Soglia: ≥" + str(THRESHOLD) + " goal | ultime " + str(LAST_N) + " gare | Bet365")

    # Raggruppa per giorno, ordina per orario
    days = {}
    for m in sorted(matches, key=lambda x: (x["date"], x["kickoff"])):
        d = m["date"]
        days.setdefault(d, []).append(m)

    for day in sorted(days):
        base_label = day_label.get(day, day)
        day_str    = fmt_day(day)
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("📅 *" + base_label + "* — " + day_str)
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        for m in days[day]:
            hs   = m["home_stats"]
            as_  = m["away_stats"]
            flag = FLAGS.get(m.get("country",""), "🏳️")
            lines.append("")
            lines.append(flag + " *" + m["home"] + "* vs *" + m["away"] + "*")
            lines.append("🕐 " + m["kickoff"] + "  |  🏆 " + m["league"])
            lines.append(
                "📈 Casa: +" + str(hs["scored"]) + " -" + str(hs["conceded"]) +
                " = *" + str(hs["total"]) + "*" +
                "  |  Trasferta: +" + str(as_["scored"]) + " -" + str(as_["conceded"]) +
                " = *" + str(as_["total"]) + "*"
            )

    msg = "\n".join(lines)
    # Telegram limite 4096 — se supera spezza in più messaggi
    if len(msg) <= 4096:
        _tg_send(msg)
    else:
        # Primo blocco: header + primo giorno
        chunks = []
        chunk = []
        for line in lines:
            chunk.append(line)
            if len("\n".join(chunk)) > 3800 and line == "":
                chunks.append("\n".join(chunk))
                chunk = []
        if chunk:
            chunks.append("\n".join(chunk))
        for i, c in enumerate(chunks):
            if i > 0:
                c = "_(continua...)_\n" + c
            _tg_send(c)

# ── Helpers HTML ─────────────────────────────────────────────────────────────
def badge_color(t):
    if t >= 20: return "#ff4757"
    if t >= 17: return "#ff8c00"
    return "#00e5a0"

def slot(ko):
    try:    return f"{int(ko.split(':')[0]):02d}:00"
    except: return "??:??"

# ── HTML ─────────────────────────────────────────────────────────────────────
def generate_html(matches, run_date, total_analyzed):
    from datetime import datetime, timezone, timedelta
    today     = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    d1_str    = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    d2_str    = (today + timedelta(days=2)).strftime("%Y-%m-%d")

    def fmt_short(d):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            mesi = ["Gen","Feb","Mar","Apr","Mag","Giu","Lug","Ago","Set","Ott","Nov","Dic"]
            return f"{dt.day} {mesi[dt.month-1]}"
        except: return d

    day_labels = {
        today_str: "📅 OGGI",
        d1_str:    "📅 DOMANI",
        d2_str:    "📅 DOPODOMANI",
    }

    # Range copertura bot
    date_range = f"{fmt_short(today_str)} → {fmt_short(d2_str)}"

    css = (
        "@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;700&family=DM+Mono:wght@400;500&display=swap');"
        ":root{--bg:#080d18;--surface:#0c1220;--card:#0f1827;--accent:#00e5a0;"
        "--red:#ff3a3a;--orange:#ff8c00;--text:#dde3f0;--muted:#4a5570;--border:rgba(255,255,255,0.06);}"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;padding-bottom:60px;}"
        # header
        "header{background:rgba(8,13,24,0.95);backdrop-filter:blur(16px);"
        "border-bottom:1px solid var(--border);padding:12px 24px;"
        "display:flex;align-items:center;gap:20px;position:sticky;top:0;z-index:50;}"
        ".logo{display:flex;align-items:center;gap:10px;}"
        ".logo-icon{font-size:1.4rem;}"
        ".logo-text{font-size:1.25rem;font-weight:700;"
        "background:linear-gradient(90deg,#fff 0%,var(--accent) 100%);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.01em;}"
        ".logo-sub{font-family:'DM Mono',monospace;font-size:.55rem;color:var(--muted);"
        "letter-spacing:.15em;display:block;margin-top:-3px;-webkit-text-fill-color:var(--muted);}"
        ".hdivider{width:1px;height:28px;background:var(--border);}"
        ".hstats{display:flex;gap:20px;}"
        ".hstat{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);"
        "display:flex;align-items:center;gap:5px;}"
        ".hstat strong{color:var(--accent);font-size:.85rem;}"
        ".hright{margin-left:auto;display:flex;align-items:center;gap:12px;}"
        ".pulse-dot{width:7px;height:7px;border-radius:50%;background:var(--red);"
        "box-shadow:0 0 6px var(--red);animation:pdot 1.4s infinite;}"
        "@keyframes pdot{0%,100%{opacity:1}50%{opacity:.3}}"
        ".live-tag{font-family:'DM Mono',monospace;font-size:.65rem;color:var(--red);font-weight:500;letter-spacing:.1em;}"
        ".update-time{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);}"
        # scanbar
        ".scanbar{background:rgba(0,229,160,0.03);border-bottom:1px solid rgba(0,229,160,0.08);"
        "padding:6px 24px;display:flex;gap:0;align-items:center;"
        "font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);overflow:hidden;white-space:nowrap;flex-wrap:wrap;}"
        ".scanbar-item{display:flex;align-items:center;gap:4px;padding:0 14px;border-right:1px solid rgba(255,255,255,0.06);}"
        ".scanbar-item:first-child{padding-left:0;}"
        ".scanbar-item::before{content:'›';color:var(--accent);margin-right:3px;}"
        ".scanbar span{color:var(--accent);}"
        # wrap
        ".wrap{padding:0 20px;}"
        # section head
        ".section-head{display:flex;align-items:center;gap:12px;padding:18px 0 10px;}"
        ".section-label{font-size:1rem;font-weight:700;letter-spacing:.02em;}"
        ".section-label.slive{color:var(--red);}"
        ".section-label.sday{color:var(--text);}"
        ".section-badge{font-family:'DM Mono',monospace;font-size:.62rem;color:var(--muted);"
        "background:rgba(255,255,255,0.04);padding:2px 9px;border-radius:100px;border:1px solid var(--border);}"
        ".section-line{flex:1;height:1px;background:linear-gradient(90deg,rgba(255,255,255,0.07),transparent);}"
        ".section-line.red{background:linear-gradient(90deg,rgba(255,58,58,0.3),transparent);}"
        # subsection
        ".sub-label{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);"
        "letter-spacing:.1em;text-transform:uppercase;padding:6px 0 8px;"
        "display:flex;align-items:center;gap:8px;}"
        ".sub-label::after{content:'';flex:1;height:1px;background:var(--border);}"
        ".sub-label.bl{color:#6aa3ff;}"
        ".sub-label.gr{color:var(--accent);}"
        # time group
        ".tgroup{margin-bottom:18px;}"
        ".th{display:flex;align-items:center;gap:10px;margin-bottom:8px;}"
        ".tl{font-family:'DM Mono',monospace;font-size:.78rem;font-weight:500;color:var(--accent);}"
        ".tc{font-size:.65rem;color:var(--muted);}"
        ".th::after{content:'';flex:1;height:1px;background:var(--border);}"
        # grid & card
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(278px,1fr));gap:8px;margin-bottom:14px;}"
        ".card{border-radius:10px;padding:11px 13px;border:1px solid var(--border);"
        "position:relative;overflow:hidden;transition:transform .15s,box-shadow .2s;}"
        ".card::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;"
        "background:linear-gradient(90deg,transparent,rgba(255,255,255,0.05),transparent);}"
        ".card:hover{transform:translateY(-2px);}"
        ".card.normal{background:var(--card);}"
        ".card.normal:hover{box-shadow:0 6px 24px rgba(0,0,0,.5);}"
        ".card.zerozero{background:linear-gradient(135deg,#0e3580 0%,#1452cc 40%,#1a6aff 60%,#0e3580 100%);"
        "border-color:rgba(60,130,255,0.75);"
        "box-shadow:0 0 24px rgba(40,110,255,0.25),inset 0 1px 0 rgba(255,255,255,0.1);}"
        ".card.zerozero:hover{box-shadow:0 0 36px rgba(40,110,255,0.45);}"
        ".card.zerozero::before{content:'';position:absolute;top:-100%;left:0;right:0;height:40%;"
        "background:linear-gradient(180deg,transparent,rgba(255,255,255,0.04),transparent);"
        "animation:scan 4s linear infinite;pointer-events:none;}"
        "@keyframes scan{to{top:200%;}}"
        ".card.scoring{background:linear-gradient(135deg,#061510 0%,#0a2018 50%,#061510 100%);"
        "border-color:rgba(0,229,160,.35);box-shadow:0 0 18px rgba(0,229,160,.1);}"
        ".card.scoring:hover{box-shadow:0 0 28px rgba(0,229,160,.22);}"
        # corner tag
        ".ctag{position:absolute;top:0;right:0;font-family:'DM Mono',monospace;font-size:.48rem;"
        "letter-spacing:.08em;color:var(--muted);background:rgba(255,255,255,0.04);"
        "padding:2px 6px;border-radius:0 10px 0 6px;"
        "border-left:1px solid var(--border);border-bottom:1px solid var(--border);}"
        # card internals
        ".ct{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px;}"
        ".league{font-size:.65rem;color:var(--muted);letter-spacing:.03em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:70%;}"
        ".card.zerozero .league{color:#8ab8ff;}"
        ".cright{display:flex;align-items:center;gap:6px;}"
        ".ko{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--accent);font-weight:500;}"
        ".live-score{display:none;font-family:'DM Mono',monospace;font-size:.6rem;font-weight:500;"
        "background:rgba(255,58,58,.12);color:var(--red);padding:1px 6px;border-radius:4px;"
        "border:1px solid rgba(255,58,58,.2);animation:lbpulse 1.4s infinite;}"
        ".live-score.ht{background:rgba(255,140,0,.1);color:var(--orange);border-color:rgba(255,140,0,.2);animation:none;}"
        ".live-score.ft{background:rgba(74,85,112,.1);color:var(--muted);border-color:rgba(74,85,112,.15);animation:none;}"
        "@keyframes lbpulse{0%,100%{opacity:1}50%{opacity:.35}}"
        ".mu{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:6px;}"
        ".side{display:flex;flex-direction:column;gap:4px;}"
        ".side.r{align-items:flex-end;text-align:right;}"
        ".tn{font-size:.82rem;font-weight:700;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px;}"
        ".pills{display:flex;gap:3px;align-items:center;}"
        ".side.r .pills{justify-content:flex-end;}"
        ".pill{font-size:.67rem;font-weight:700;padding:2px 5px;border-radius:3px;}"
        ".pill.g{background:rgba(0,229,160,.12);color:var(--accent);}"
        ".pill.rc{background:rgba(255,58,58,.12);color:var(--red);}"
        ".pill.tot{color:#05080f;border-radius:5px;padding:2px 7px;}"
        ".center{text-align:center;}"
        ".vs{font-size:.95rem;color:var(--muted);font-weight:700;}"
        ".score-val{font-family:'DM Mono',monospace;font-size:1.2rem;font-weight:700;color:#fff;line-height:1;display:none;}"
        ".card.zerozero .score-val{text-shadow:0 0 14px rgba(120,180,255,0.8);}"
        ".bet{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--accent);"
        "text-align:center;margin-top:7px;opacity:.55;letter-spacing:.05em;}"
        ".plane-bg{position:absolute;font-size:4rem;opacity:0.08;bottom:6px;right:10px;"
        "animation:fly 3s ease-in-out infinite;pointer-events:none;}"
        "@keyframes fly{0%{transform:translateX(0) rotate(-10deg)}50%{transform:translateX(8px) rotate(-5deg)}100%{transform:translateX(0) rotate(-10deg)}}"
        ".day-block{margin:0;}"
        ".empty{text-align:center;padding:80px 20px;color:var(--muted);}"
        ".empty h3{font-size:1.2rem;color:var(--text);margin-bottom:6px;}"
    )

    def make_card(m, corner=""):
        hs = m["home_stats"]; as_ = m["away_stats"]
        fid = m.get("fixture_id","")
        country = m.get("country","")
        league_display = f'{m["league"]} · {country}' if country and country != "World" else m["league"]
        ctag = f'<div class="ctag">{corner}</div>' if corner else ""
        return (
            f'<div class="card normal" data-fid="{fid}">{ctag}<div class="ct">'
            f'<span class="league">{league_display}</span>'
            f'<div class="cright">'
            f'<span class="live-score"></span>'
            f'<span class="ko">{m["kickoff"]}</span></div></div>'
            f'<div class="mu"><div class="side">'
            f'<span class="tn">{m["home"]}</span>'
            f'<div class="pills">'
            f'<span class="pill g">+{hs["scored"]}</span>'
            f'<span class="pill rc">-{hs["conceded"]}</span>'
            f'<span class="pill tot" style="background:{badge_color(hs["total"])}">{hs["total"]}</span>'
            f'</div></div>'
            f'<div class="center">'
            f'<span class="vs">VS</span>'
            f'<div class="score-val" data-score></div></div>'
            f'<div class="side r"><span class="tn">{m["away"]}</span>'
            f'<div class="pills">'
            f'<span class="pill g">+{as_["scored"]}</span>'
            f'<span class="pill rc">-{as_["conceded"]}</span>'
            f'<span class="pill tot" style="background:{badge_color(as_["total"])}">{as_["total"]}</span>'
            f'</div></div></div>'
            f'<div class="bet">✅ BET365 VERIFIED</div>'
            f'</div>'
        )

    if not matches:
        body = (f'<div class="empty"><h3>Nessun match qualificato</h3>'
                f'<p>Nessuna coppia soddisfa ≥{THRESHOLD} goal + quote Bet365<br>'
                f'nelle ultime {LAST_N} gare stessa lega.<br>'
                f'Match analizzati: <strong>{total_analyzed}</strong></p></div>')
    else:
        LIVE_STATUS = {"1H","HT","2H","ET","P"}

        live_matches  = [m for m in matches if m.get("status") in LIVE_STATUS]
        other_matches = [m for m in matches if m.get("status") not in LIVE_STATUS]

        sections = []

        # ── Sezione LIVE sempre presente ──
        live_hidden = ' style="display:none"' if not live_matches else ""
        live_section = (
            '<div class="day-block" id="live-section"' + live_hidden + '>'
            '<div class="section-head">'
            '<span class="section-label slive">🔴 LIVE</span>'
            f'<span class="section-badge">{len(live_matches)} in corso</span>'
            '<div class="section-line red"></div></div>'
            '<div class="sub-label bl" id="sub-00">⬤ &nbsp;0 — 0 &nbsp;·&nbsp; ancora aperti</div>'
            '<div class="grid" id="live-grid-00"></div>'
            '<div class="sub-label gr" id="sub-goal" style="display:none">✈️ &nbsp;in gol</div>'
            '<div class="grid" id="live-grid-goal"></div>'
            '</div>'
        )

        # Pre-popola con match live già noti al momento del run
        # (il JS poi riordina dinamicamente)
        if live_matches:
            pre_00   = "".join(make_card(m, "LIVE") for m in live_matches)
            live_section = live_section.replace(
                '<div class="grid" id="live-grid-00"></div>',
                f'<div class="grid" id="live-grid-00">{pre_00}</div>'
            )

        sections.append(live_section)

        # ── Resto NS/FT per data/orario ──
        days = {}
        for m in sorted(other_matches, key=lambda x: (x["date"], x["kickoff"])):
            d = m["date"]
            s = slot(m["kickoff"])
            days.setdefault(d, {}).setdefault(s, []).append(m)

        for day in sorted(days):
            label     = day_labels.get(day, f"📅 {day}")
            day_total = sum(len(v) for v in days[day].values())
            day_html  = (
                f'<div class="day-block">'
                f'<div class="section-head">'
                f'<span class="section-label sday">{label}</span>'
                f'<span class="section-badge">{day_total} alert</span>'
                f'<div class="section-line"></div></div>'
            )
            for ts in sorted(days[day]):
                cards = "".join(make_card(m) for m in days[day][ts])
                day_html += (
                    f'<div class="tgroup"><div class="th">'
                    f'<span class="tl">⏱ {ts}</span>'
                    f'<span class="tc">{len(days[day][ts])} match</span>'
                    f'</div><div class="grid">{cards}</div></div>'
                )
            day_html += '</div>'
            sections.append(day_html)

        body = "\n".join(sections)

    live_script = '''<script>
const PROXY='https://spring-hall-b29e.nwgir.workers.dev';
const LIVE_ST=['1H','2H','ET','P','HT'];
async function updateLive(){
  try{
    var all=[].slice.call(document.querySelectorAll('.card[data-fid]'));
    var ids=all.map(function(c){return c.getAttribute('data-fid');}).filter(Boolean);
    if(!ids.length)return;
    var fixtures=[];
    for(var i=0;i<ids.length;i+=20){
      var chunk=ids.slice(i,i+20).join('-');
      var r=await fetch(PROXY+'?endpoint=fixtures&ids='+chunk);
      if(!r.ok)continue;
      var data=await r.json();
      fixtures=fixtures.concat(data.response||[]);
    }
    var fixtureMap={};
    fixtures.forEach(function(fix){fixtureMap[String(fix.fixture.id)]=fix;});

    // Aggiorna ogni card
    all.forEach(function(card){
      var fid=card.getAttribute('data-fid');
      var fix=fixtureMap[fid]; if(!fix)return;
      var st=fix.fixture.status.short,min=fix.fixture.status.elapsed;
      var hg=fix.goals.home,ag=fix.goals.away;
      var b=card.querySelector('.live-score');
      if(!b)return;
      var isLive=LIVE_ST.indexOf(st)>=0,ht=st==='HT',ft=st==='FT';
      if(isLive||ht||ft){
        b.style.display='inline-flex';
        b.className='live-score'+(ht?' ht':ft?' ft':'');
        b.textContent=ft?'FT':ht?'HT':(min?min+"'":st);
      }
      if(hg!=null&&ag!=null){
        var s=card.querySelector('[data-score]');
        var v=card.querySelector('.vs');
        if(s){s.textContent=hg+' — '+ag;s.style.display='block';if(v)v.style.display='none';}
        var hasGoal=(hg+ag)>0;
        if(hasGoal&&!card.querySelector('.plane-bg')){
          card.classList.add('scoring');
          var p=document.createElement('div');p.className='plane-bg';p.textContent='\u2708\ufe0f';card.appendChild(p);
        } else if(!hasGoal){
          card.classList.remove('scoring');
          var pl=card.querySelector('.plane-bg');if(pl)pl.remove();
        }
      }
    });

    // Sposta card live nelle due griglie: 00 e goal
    var liveSection=document.getElementById('live-section');
    var grid00=document.getElementById('live-grid-00');
    var gridGoal=document.getElementById('live-grid-goal');
    var sub00=document.getElementById('sub-00');
    var subGoal=document.getElementById('sub-goal');
    if(!liveSection||!grid00||!gridGoal)return;

    fixtures.forEach(function(fix){
      var fid=String(fix.fixture.id);
      var st=fix.fixture.status.short;
      var hg=fix.goals.home||0, ag=fix.goals.away||0;
      if(LIVE_ST.indexOf(st)<0)return;
      var card=document.querySelector('.card[data-fid="'+fid+'"]');
      if(!card)return;
      var hasGoal=(hg+ag)>0;
      var targetGrid=hasGoal?gridGoal:grid00;
      if(card.parentElement!==targetGrid){
        var oldParent=card.parentElement;
        targetGrid.appendChild(card);
        liveSection.style.display='';
        if(oldParent&&oldParent!==grid00&&oldParent!==gridGoal){
          if(oldParent.querySelectorAll('.card').length===0){
            var ts=oldParent.closest('.tgroup');
            if(ts)ts.style.display='none';
          }
        }
      }
      // zerozero class
      if(!hasGoal){card.classList.add('zerozero');card.classList.remove('scoring');}
      else{card.classList.remove('zerozero');}
    });

    // Mostra/nascondi sub-label goal
    if(gridGoal.querySelectorAll('.card').length>0){subGoal.style.display='';}
    else{subGoal.style.display='none';}
    if(grid00.querySelectorAll('.card').length>0){sub00.style.display='';}
    else{sub00.style.display='none';}

    var ts=document.getElementById('live-ts');
    if(ts)ts.textContent='\ud83d\udd04 '+new Date().toLocaleTimeString();
  }catch(e){console.log('live',e);}
}
updateLive();setInterval(updateLive,15000);
</script>'''

    return (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>GoalScan · {run_date}</title>'
        f'<style>{css}</style></head><body>'
        f'<header>'
        f'<div class="logo"><span class="logo-icon">⚽</span><div>'
        f'<span class="logo-text">GoalScan</span>'
        f'<span class="logo-sub">LIVE INTELLIGENCE · BET365</span>'
        f'</div></div>'
        f'<div class="hdivider"></div>'
        f'<div class="hstats">'
        f'<div class="hstat"><strong>{total_analyzed}</strong> analizzati</div>'
        f'<div class="hstat"><strong>{len(matches)}</strong> alert</div>'
        f'<div class="hstat">soglia <strong>≥{THRESHOLD}</strong></div>'
        f'<div class="hstat">ultime <strong>{LAST_N}</strong> gare</div>'
        f'</div>'
        f'<div class="hright">'
        f'<div class="pulse-dot"></div>'
        f'<span class="live-tag">LIVE</span>'
        f'<span class="update-time" id="live-ts">⏳</span>'
        f'</div></header>'
        f'<div class="scanbar">'
        f'<div class="scanbar-item">soglia <span>≥{THRESHOLD} goal</span> ultime {LAST_N} gare stessa lega</div>'
        f'<div class="scanbar-item">quote <span>Bet365</span> verificate</div>'
        f'<div class="scanbar-item"><span>3 giorni</span> · solo campionati</div>'
        f'<div class="scanbar-item">aggiornamento <span>ogni 15s</span></div>'
        f'<div class="scanbar-item">copertura <span>{date_range}</span></div>'
        f'</div>'
        f'<div class="wrap">{body}</div>'
        f'{live_script}</body></html>'
    )


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"GOAL BOT  |  soglia ≥{THRESHOLD}  |  ultime {LAST_N} gare  |  Bet365")
    print("=" * 60)

    print("\n[1] Recupero match oggi + domani + dopodomani...")
    league_seasons, fixtures_by_league = get_all_fixtures()

    all_fixtures = []
    for lid, fixes in fixtures_by_league.items():
        for f in fixes:
            f["_league_id"] = lid
        all_fixtures.extend(fixes)

    print(f"\nTotale match da analizzare: {len(all_fixtures)}")

    if not all_fixtures:
        print("Nessun match trovato.")
        run_date = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        out = Path("docs/index.html")
        out.parent.mkdir(exist_ok=True)
        out.write_text(generate_html([], run_date, 0), encoding="utf-8")
        return

    print("\n[2] Analisi storico squadre (parallelo, max 3 workers)...\n")
    qualified = []
    total = len(all_fixtures)

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_fix = {executor.submit(analyze_fixture, fix): fix for fix in all_fixtures}
        done = 0
        for future in as_completed(future_to_fix):
            done += 1
            fix = future_to_fix[future]
            home_name = fix.get("teams", {}).get("home", {}).get("name", "?")
            away_name = fix.get("teams", {}).get("away", {}).get("name", "?")
            try:
                result, log = future.result()
                print(f"[{done:>4}/{total}] {home_name} vs {away_name} — {log}")
                if result:
                    qualified.append(result)
            except Exception as e:
                print(f"[{done:>4}/{total}] {home_name} vs {away_name} — ERR: {e}")

    print(f"\n{'='*60}")
    print(f"ALERT FINALI (goal + Bet365): {len(qualified)} / {total}")
    print(f"{'='*60}")
    for m in sorted(qualified, key=lambda x: (x["date"], x["kickoff"])):
        print(f"  {m['date']} {m['kickoff']}  {m['home']} ({m['home_stats']['total']}) vs "
              f"{m['away']} ({m['away_stats']['total']})  [{m['league']}]")

    run_date = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    run_slug = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    docs = Path("docs")
    docs.mkdir(exist_ok=True)

    # Salva report con timestamp (archivio permanente)
    archive_file = docs / f"report-{run_slug}.html"
    html_content = generate_html(qualified, run_date, total)
    archive_file.write_text(html_content.encode('utf-8', errors='replace').decode('utf-8'), encoding="utf-8")

    # Aggiorna index.html = ultimo report + link archivio
    # Raccoglie tutti i report esistenti
    reports = sorted(docs.glob("report-*.html"), reverse=True)
    archive_links = ""
    for r in reports:
        label = r.stem.replace("report-", "")
        dt = label[:8] + " " + label[9:11] + ":" + label[11:13] + " UTC"
        active = " style='font-weight:bold;color:#f59e0b'" if r == archive_file else ""
        archive_links += f"<li><a href='{r.name}'{active}>📄 {dt}</a></li>\n"

    index_html = html_content.replace(
        "</body>",
        f"""<div style='max-width:900px;margin:2rem auto;padding:1rem;background:#1e293b;border-radius:8px;'>
<h3 style='color:#94a3b8;margin-bottom:0.5rem'>📁 Report precedenti</h3>
<ul style='color:#cbd5e1;line-height:2'>{archive_links}</ul>
</div></body>"""
    )

    out = docs / "index.html"
    out.write_text(index_html.encode('utf-8', errors='replace').decode('utf-8'), encoding="utf-8")
    print(f"\nReport salvato: {archive_file.name} → aggiornato index.html")

    # Salva lista fixture_id per live_updater.py
    ids = [m["fixture_id"] for m in qualified if m.get("fixture_id")]
    (docs / "alert_ids.json").write_text(json.dumps(ids))
    print(f"alert_ids.json: {len(ids)} fixture da monitorare")

    print("\n[4] Invio Telegram...")
    send_telegram(qualified, total, run_date)

if __name__ == "__main__":
    main()

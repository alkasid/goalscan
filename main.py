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
THRESHOLD  = int(CFG.get("goal_threshold", 14))
LAST_N     = int(CFG.get("last_matches_count", 5))
MIN_SCORED = int(CFG.get("min_scored_each", 5))
MIN_CO_MAX = int(CFG.get("min_conceded_max", 8))
BASE_URL  = "https://v3.football.api-sports.io"
HEADERS   = {"x-apisports-key": API_KEY}
BET365_ID = 8
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT  = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
TELEGRAM_ENABLED = False  # disattivato temporaneamente

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

    raw_fixtures = []
    for date in dates:
        data = api_get("fixtures", {"date": date, "status": "NS-1H-HT-2H-ET-P-FT"})
        print(f"  {date}: {len(data)} match raw")
        raw_fixtures.extend(data)
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
    return league_seasons, fixtures_by_league, raw_fixtures

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
        "last":   LAST_N + 5,  # margine per PST/CANC/SUSP
    })

    finished = [
        m for m in data
        if m.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
    ]

    if len(finished) < LAST_N:
        _cache[key] = None
        _disk_cache[disk_key] = None
        _save_disk_cache(_disk_cache)
        return None

    scored = conceded = 0
    match_details = []
    for m in finished[:LAST_N]:
        goals   = m.get("goals", {})
        teams   = m.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        if is_home:
            scored += gh; conceded += ga
            match_details.append({"s": gh, "c": ga})
        else:
            scored += ga; conceded += gh
            match_details.append({"s": ga, "c": gh})

    result = {"scored": scored, "conceded": conceded,
              "total": scored + conceded,
              "matches": match_details,
              "qualifies": (scored + conceded) >= THRESHOLD and scored >= MIN_SCORED}
    _cache[key] = result
    _disk_cache[disk_key] = result
    _save_disk_cache(_disk_cache)
    return result

def get_last_n_any(team_id, league_id, season, min_games=1):
    """Come get_last_n ma senza soglia minima — restituisce i dati con qualsiasi numero di FT."""
    key = (team_id, league_id, season, "any")
    if key in _cache:
        return _cache[key]

    # Prima controlla se abbiamo già i dati completi in cache
    base_key = (team_id, league_id, season)
    if base_key in _cache and _cache[base_key] is not None:
        _cache[key] = _cache[base_key]
        return _cache[base_key]

    data = api_get("fixtures", {
        "team":   team_id,
        "league": league_id,
        "season": season,
        "last":   LAST_N + 5,
    })

    finished = [
        m for m in data
        if m.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
    ]

    if len(finished) < min_games:
        _cache[key] = None
        return None

    take = finished[:LAST_N]
    scored = conceded = 0
    match_details = []
    for m in take:
        goals  = m.get("goals", {})
        teams  = m.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        if is_home:
            scored += gh; conceded += ga
            match_details.append({"s": gh, "c": ga})
        else:
            scored += ga; conceded += gh
            match_details.append({"s": ga, "c": gh})

    result = {"scored": scored, "conceded": conceded,
              "total": scored + conceded,
              "games": len(take),
              "matches": match_details,
              "qualifies": (scored + conceded) >= THRESHOLD and scored >= MIN_SCORED}
    _cache[key] = result
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
        ) + timedelta(hours=2)).strftime("%H:%M")
        match_date = (datetime.fromtimestamp(
            fixture.get("timestamp", 0), tz=timezone.utc
        ) + timedelta(hours=2)).strftime("%Y-%m-%d")
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

    # ── Filtro ANTI 0-0: almeno una squadra deve subire >= MIN_CO_MAX ──
    if max(hs["conceded"], as_["conceded"]) < MIN_CO_MAX:
        return None, (f"⚠ ANTI-0-0: difese troppo solide — "
                      f"{home_name}:{hs['conceded']}s "
                      f"{away_name}:{as_['conceded']}s "
                      f"(serve almeno {MIN_CO_MAX})")

    # Passa il filtro goal — verifica quote Bet365
    odds_ok = has_bet365_odds(fixture_id)
    if not odds_ok:
        return None, f"✅ goal OK ma ❌ no quote Bet365 — {home_name}:{hs['total']} {away_name}:{as_['total']}"

    match_status = fixture.get("status", {}).get("short", "NS")
    goals        = fix.get("goals", {})
    return {"home": home_name, "away": away_name,
            "home_stats": hs, "away_stats": as_,
            "league": league_name, "country": country, "kickoff": ko,
            "date": match_date, "fixture_id": fixture_id,
            "status": match_status,
            "goals_home": goals.get("home"), "goals_away": goals.get("away")}, \
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
        ".radar-wrap{width:36px;height:36px;flex-shrink:0;}"
        ".radar-wrap canvas{width:36px;height:36px;image-rendering:crisp-edges;}"
        ".logo-icon{font-size:1.4rem;}"
        ".logo-text{font-size:1.25rem;font-weight:700;"
        "background:linear-gradient(90deg,#fff 0%,var(--accent) 100%);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.01em;}"
        ".logo-sub{font-family:'DM Mono',monospace;font-size:.55rem;color:var(--muted);"
        "letter-spacing:.15em;display:block;margin-top:-3px;-webkit-text-fill-color:var(--muted);}"
        ".hdivider{width:1px;height:28px;background:var(--border);}"
        ".nav-stats{font-family:'DM Mono',monospace;font-size:.63rem;color:var(--accent);text-decoration:none;padding:4px 12px;border-radius:5px;border:1px solid rgba(0,229,160,.3);background:rgba(0,229,160,.06);transition:all .2s;}"        ".nav-stats:hover{background:rgba(0,229,160,.12);}"
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
        FT_STATUS   = {"FT","AET","PEN"}

        live_matches  = [m for m in matches if m.get("status") in LIVE_STATUS]
        # Solo partite NON concluse e NON live (= NS, prossime)
        other_matches = [m for m in matches if m.get("status") not in LIVE_STATUS
                         and m.get("status") not in FT_STATUS]

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
            pre_00   = "".join(make_card(m, "LIVE") for m in live_matches
                                if (m.get('goals_home',0) or 0)+(m.get('goals_away',0) or 0)==0)
            pre_goal = "".join(make_card(m, "LIVE") for m in live_matches
                                if (m.get('goals_home',0) or 0)+(m.get('goals_away',0) or 0)>0)
            live_section = live_section.replace(
                '<div class="grid" id="live-grid-00"></div>',
                f'<div class="grid" id="live-grid-00">{pre_00}</div>'
            )
            live_section = live_section.replace(
                '<div class="grid" id="live-grid-goal"></div>',
                f'<div class="grid" id="live-grid-goal">{pre_goal}</div>'
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
const FT_ST=['FT','AET','PEN'];
const PLANE='\u2708\uFE0F';
const REFRESH='\uD83D\uDD04';

async function updateLive(){
  try{
    var all=[].slice.call(document.querySelectorAll('.card[data-fid]'));
    // OTTIMIZZATO: solo match con badge live visibile (1H,HT,2H,ET,P)
    // I match NS/FT non vengono interrogati ogni 15s — risparmio ~67% chiamate API
    var liveCards=all.filter(function(c){
      var b=c.querySelector('.live-score');
      // Includi anche card senza badge se sono nella sezione live (potrebbero essere appena iniziate)
      var inLive=(c.closest('#live-section')!==null);
      var hasLiveBadge=(b&&b.style.display!=='none'&&b.textContent!=='FT');
      return inLive||hasLiveBadge;
    });
    // Se non ci sono live, fai un check leggero ogni 60s su tutti per rilevare inizi
    var now=Date.now();
    var ids;
    if(liveCards.length>0){
      ids=liveCards.map(function(c){return c.getAttribute('data-fid');}).filter(Boolean);
    } else {
      // Nessun live — controlla tutti ogni 60s (non ogni 15s)
      if(window._lastFullCheck&&(now-window._lastFullCheck)<60000)return;
      window._lastFullCheck=now;
      ids=all.map(function(c){return c.getAttribute('data-fid');}).filter(Boolean);
    }
    if(!ids.length)return;
    var fixtures=[];
    for(var i=0;i<ids.length;i+=20){
      var chunk=ids.slice(i,i+20).join('-');
      var r=await fetch(PROXY+'?endpoint=fixtures&ids='+chunk);
      if(!r.ok)continue;
      var data=await r.json();
      fixtures=fixtures.concat(data.response||[]);
    }
    var fmap={};
    fixtures.forEach(function(f){fmap[String(f.fixture.id)]=f;});

    var liveSection=document.getElementById('live-section');
    var grid00=document.getElementById('live-grid-00');
    var gridGoal=document.getElementById('live-grid-goal');
    var sub00=document.getElementById('sub-00');
    var subGoal=document.getElementById('sub-goal');
    if(!liveSection||!grid00||!gridGoal)return;

    all.forEach(function(card){
      var fid=card.getAttribute('data-fid');
      var fix=fmap[fid]; if(!fix)return;
      var st=fix.fixture.status.short;
      var min=fix.fixture.status.elapsed;
      var hg=fix.goals.home; var ag=fix.goals.away;
      var isLive=LIVE_ST.indexOf(st)>=0;
      var isHT=st==='HT';
      var isFT=FT_ST.indexOf(st)>=0;
      var hasGoal=((hg||0)+(ag||0))>0;
      var inLiveSection=(card.parentElement===grid00||card.parentElement===gridGoal);

      // --- Aggiorna badge minuto ---
      var b=card.querySelector('.live-score');
      if(b){
        if(isLive||isHT||isFT){
          b.style.display='inline-flex';
          b.className='live-score'+(isHT?' ht':isFT?' ft':'');
          b.textContent=isFT?'FT':isHT?'HT':(min?min+"'":st);
        } else {
          b.style.display='none';
        }
      }

      // --- Aggiorna corner tag ---
      var ctag=card.querySelector('.ctag');
      if(ctag){
        if(isFT) ctag.textContent='FT';
        else if(isHT) ctag.textContent='HT';
        else if(isLive) ctag.textContent=(min?min+"'":'LIVE');
      }

      // --- Aggiorna punteggio ---
      if(hg!=null&&ag!=null){
        var s=card.querySelector('[data-score]');
        var v=card.querySelector('.vs');
        if(s){s.textContent=hg+' - '+ag;s.style.display='block';if(v)v.style.display='none';}
      }

      // --- FT: rimuovi completamente dalla dashboard (non mostrare partite concluse) ---
      if(isFT){
        card.remove();
        return;
      }

      // --- Sposta in live se partita iniziata (solo LIVE/HT, non FT) ---
      if((isLive||isHT)&&!isFT){
        var targetGrid=hasGoal?gridGoal:grid00;
        if(!inLiveSection){
          // Arriva da sezione oraria — spostala in cima
          var oldGrid=card.parentElement;
          targetGrid.appendChild(card);
          liveSection.style.display='';
          if(oldGrid){
            var tg=oldGrid.closest('.tgroup');
            if(tg&&tg.querySelectorAll('.card[data-fid]').length===0)tg.style.display='none';
            var db=oldGrid.closest('.day-block');
            if(db&&db.id!=='live-section'&&db.querySelectorAll('.tgroup:not([style*="none"])').length===0)db.style.display='none';
          }
        } else if(hasGoal&&card.parentElement===grid00){
          // Era 0-0, ora ha segnato → passa a grid-goal
          gridGoal.appendChild(card);
        } else if(!hasGoal&&card.parentElement===gridGoal){
          // Tornata a 0-0 (annullato) → torna a grid-00
          grid00.appendChild(card);
        }

        // Applica stile corretto
        if(hasGoal){
          card.classList.remove('zerozero');
          card.classList.add('scoring');
          if(!card.querySelector('.plane-bg')){
            var p=document.createElement('div');p.className='plane-bg';p.textContent=PLANE;card.appendChild(p);
          }
        } else {
          card.classList.add('zerozero');
          card.classList.remove('scoring');
          var pl=card.querySelector('.plane-bg');if(pl)pl.remove();
        }
      }
    });

    // Mostra/nascondi sub-label
    var n00=grid00.querySelectorAll('.card').length;
    var nGoal=gridGoal.querySelectorAll('.card').length;
    sub00.style.display=n00>0?'':'none';
    subGoal.style.display=nGoal>0?'':'none';
    // Nascondi intera sezione live se vuota
    if(n00===0&&nGoal===0) liveSection.style.display='none';

    // Nascondi fasce orarie e day-block vuoti dopo rimozione FT
    document.querySelectorAll('.tgroup').forEach(function(tg){
      if(tg.querySelectorAll('.card[data-fid]').length===0)tg.style.display='none';
    });
    document.querySelectorAll('.day-block:not(#live-section)').forEach(function(db){
      var vis=db.querySelectorAll('.tgroup:not([style*="display: none"]),.tgroup:not([style*="display:none"])');
      var hasVis=false;vis.forEach(function(t){if(t.style.display!=='none')hasVis=true;});
      if(!hasVis)db.style.display='none';
    });

    var ts=document.getElementById('live-ts');
    if(ts)ts.textContent=REFRESH+' '+new Date().toLocaleTimeString();
  }catch(e){console.log('live',e);}
}
updateLive();setInterval(updateLive,15000);

// ── RADAR + LOGO VARIATIO INITIALIS con onda RGB ──
(function(){
  var rc=document.getElementById('rc');
  if(!rc)return;
  var rx=rc.getContext('2d');
  var RW=rc.width,RX=RW/2,RY=RW/2,RR=RW*.42;
  var TGT=[{r:.28,a:.9},{r:.55,a:1.7},{r:.72,a:.35},{r:.42,a:2.6},{r:.61,a:4.0},{r:.83,a:3.4},{r:.35,a:5.1},{r:.78,a:5.7},{r:.48,a:2.1},{r:.66,a:.12},{r:.38,a:3.8},{r:.57,a:4.7},{r:.88,a:1.3},{r:.22,a:3.1},{r:.74,a:2.9},{r:.50,a:5.4}];
  var ht=TGT.map(function(){return -9999;});
  var rs=performance.now(),spin=true,sa=0;
  function radar(now){
    var el=now-rs;
    if(spin&&el>10000){spin=false;rs=now;sa=((now%2200)/2200)*Math.PI*2-Math.PI/2;}
    if(!spin&&el>600000){spin=true;rs=now;}
    var sw=spin?((now%2200)/2200)*Math.PI*2-Math.PI/2:sa;
    rx.clearRect(0,0,RW,RW);
    rx.beginPath();rx.arc(RX,RY,RR+2,0,Math.PI*2);rx.fillStyle='#010d06';rx.fill();
    for(var i=1;i<=4;i++){rx.beginPath();rx.arc(RX,RY,RR*i/4,0,Math.PI*2);rx.strokeStyle='rgba(0,200,100,'+(i===4?.45:.12)+')';rx.lineWidth=i===4?1.5:.8;rx.stroke();}
    for(var i=0;i<8;i++){var a=i*Math.PI/4;rx.beginPath();rx.moveTo(RX,RY);rx.lineTo(RX+Math.cos(a)*RR,RY+Math.sin(a)*RR);rx.strokeStyle='rgba(0,200,100,0.06)';rx.lineWidth=.7;rx.stroke();}
    for(var i=0;i<72;i++){var a=i*Math.PI/36,m=i%9===0,r1=RR*(m?.86:.92);rx.beginPath();rx.moveTo(RX+Math.cos(a)*r1,RY+Math.sin(a)*r1);rx.lineTo(RX+Math.cos(a)*RR,RY+Math.sin(a)*RR);rx.strokeStyle='rgba(0,229,160,'+(m?.5:.18)+')';rx.lineWidth=m?1.2:.6;rx.stroke();}
    if(spin){
      for(var i=0;i<120;i++){var a=sw-(i/120)*Math.PI*.55,al=Math.pow(1-i/120,1.5)*.28;rx.beginPath();rx.moveTo(RX,RY);rx.arc(RX,RY,RR,a,a+.02);rx.closePath();rx.fillStyle='rgba(0,229,160,'+al+')';rx.fill();}
      var sg=rx.createLinearGradient(RX,RY,RX+Math.cos(sw)*RR,RY+Math.sin(sw)*RR);
      sg.addColorStop(0,'rgba(0,229,160,0.05)');sg.addColorStop(.6,'rgba(0,229,160,0.6)');sg.addColorStop(1,'rgba(180,255,220,1)');
      rx.beginPath();rx.moveTo(RX,RY);rx.lineTo(RX+Math.cos(sw)*RR,RY+Math.sin(sw)*RR);rx.strokeStyle=sg;rx.lineWidth=2;rx.stroke();
      TGT.forEach(function(t,i){var d=sw-t.a;while(d<0)d+=Math.PI*2;if(d%(Math.PI*2)<.08)ht[i]=now;});
    }
    TGT.forEach(function(t,i){
      var tx=RX+Math.cos(t.a)*RR*t.r,ty=RY+Math.sin(t.a)*RR*t.r,age=now-ht[i];
      if(age>2200&&spin)return;
      var k=spin?Math.pow(1-Math.min(age,2200)/2200,.5):.22;
      var gr=rx.createRadialGradient(tx,ty,0,tx,ty,RW*.06);
      gr.addColorStop(0,'rgba(0,255,160,'+.9*k+')');gr.addColorStop(1,'rgba(0,229,160,0)');
      rx.beginPath();rx.arc(tx,ty,RW*.06,0,Math.PI*2);rx.fillStyle=gr;rx.fill();
      rx.beginPath();rx.arc(tx,ty,RW*.02,0,Math.PI*2);rx.fillStyle='rgba(200,255,230,'+k+')';rx.fill();
      var ch=RW*.038,gp=RW*.012;rx.strokeStyle='rgba(0,229,160,'+k*.7+')';rx.lineWidth=.9;
      rx.beginPath();rx.moveTo(tx-ch,ty);rx.lineTo(tx-gp,ty);rx.stroke();
      rx.beginPath();rx.moveTo(tx+gp,ty);rx.lineTo(tx+ch,ty);rx.stroke();
      rx.beginPath();rx.moveTo(tx,ty-ch);rx.lineTo(tx,ty-gp);rx.stroke();
      rx.beginPath();rx.moveTo(tx,ty+gp);rx.lineTo(tx,ty+ch);rx.stroke();
    });
    rx.beginPath();rx.arc(RX,RY,RW*.025,0,Math.PI*2);rx.fillStyle='#00e5a0';rx.fill();
    rx.beginPath();rx.arc(RX,RY,RW*.012,0,Math.PI*2);rx.fillStyle='#fff';rx.fill();
    rx.beginPath();rx.arc(RX,RY,RR+1,0,Math.PI*2);rx.strokeStyle='rgba(0,229,160,.6)';rx.lineWidth=1.8;rx.stroke();
    rx.beginPath();rx.arc(RX,RY,RR+5,0,Math.PI*2);rx.strokeStyle='rgba(0,229,160,.1)';rx.lineWidth=1.2;rx.stroke();
  }
  var lc=document.getElementById('lc');
  if(!lc){function loop0(){radar(performance.now());requestAnimationFrame(loop0);}loop0();return;}
  var lx=lc.getContext('2d');
  var LW=lc.width,LH=lc.height;
  var off=document.createElement('canvas');
  off.width=LW;off.height=LH;
  var ox=off.getContext('2d');
  ox.font='900 38px Cinzel,serif';
  ox.fillStyle='#1a6aff';
  ox.fillText('VARIATIO',2,34);
  ox.fillStyle='rgba(14,53,128,0.95)';
  ox.fillText('INITIALIS',2,74);
  var CYCLE=20000,WAVE=2000,PAUSE=18000;
  function logo(now){
    lx.clearRect(0,0,LW,LH);
    lx.drawImage(off,0,0);
    var ph=now%CYCLE;
    lx.beginPath();lx.moveTo(0,LH/2);lx.lineTo(LW,LH/2);
    lx.strokeStyle='rgba(0,229,160,0.2)';lx.lineWidth=1;lx.stroke();
    if(ph<PAUSE)return;
    var t=(ph-PAUSE)/WAVE;
    var cx2=(t*1.6-0.3)*LW,bw=LW*0.38;
    var g=lx.createLinearGradient(cx2-bw*1.4,0,cx2+bw*1.4,0);
    g.addColorStop(0,'rgba(0,0,0,0)');
    g.addColorStop(0.15,'rgba(220,0,0,0.75)');
    g.addColorStop(0.32,'rgba(255,255,255,0.95)');
    g.addColorStop(0.5,'rgba(0,229,160,1)');
    g.addColorStop(0.68,'rgba(255,255,255,0.95)');
    g.addColorStop(0.85,'rgba(0,80,255,0.75)');
    g.addColorStop(1,'rgba(0,0,0,0)');
    lx.save();
    lx.globalCompositeOperation='source-atop';
    lx.fillStyle=g;lx.fillRect(cx2-bw*1.4,0,bw*2.8,LH);
    lx.restore();
  }
  function loop(){var now=performance.now();radar(now);logo(now);requestAnimationFrame(loop);}
  loop();
})();

</script>'''

    return (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@900&display=swap"><title>Variatio Initialis · {run_date}</title>'
        f'<style>{css}</style></head><body>'
        f'<header>'
        f'<div class="logo">'
        f'<canvas id="rc" width="144" height="144" style="width:36px;height:36px;flex-shrink:0;image-rendering:crisp-edges;"></canvas>'
        f'<div style="position:relative;width:152px;height:42px;margin-left:8px;">'
        f'<canvas id="lc" width="304" height="84" style="position:absolute;top:0;left:0;width:152px;height:42px;"></canvas>'
        f'</div>'
        f'<div style="font-family:monospace;font-size:7px;letter-spacing:.25em;color:rgba(0,229,160,0.35);margin-left:6px;">LIVE<br>INTELLIGENCE</div>'
        f'</div>'
        f'<div class="hdivider"></div>'
        f'<a href="storico.html" class="nav-stats" style="margin-right:4px">📋 Storico</a>'
        f'<a href="stats.html" class="nav-stats" style="margin-right:4px">📊 Stats Avanzate</a>'
        f'<a href="global_stats.html" class="nav-stats">🌍 Stats Globali</a>'
        f'<div class="hstats">'
        f'<div class="hstat"><strong>{total_analyzed}</strong> analizzati</div>'
        f'<div class="hstat"><strong>{len(matches)}</strong> alert</div>'
        f'<div class="hstat">soglia <strong>≥{THRESHOLD}</strong></div>'
        f'<div class="hstat">ultime <strong>{LAST_N}</strong> gare</div>'
        f'</div>'
        f'<div class="hright">'
        f'<div class="pulse-dot"></div>'
        f'<span class="live-tag">LIVE</span>'
        f'<span class="update-time" id="live-ts"></span>'
        f'</div></header>'
        f'<div class="scanbar">'
        f'<div class="scanbar-item">ratio <span>≥{THRESHOLD} goal</span> ultime {LAST_N} gare stessa lega + 3FiltriAnti0-0</div>'
        f'<div class="scanbar-item">quote <span>Bet365</span> verificate</div>'
        f'<div class="scanbar-item"><span>3 giorni</span> · solo campionati</div>'
        f'<div class="scanbar-item">aggiornamento <span>ogni 15s</span></div>'
        f'<div class="scanbar-item">copertura <span>{date_range}</span></div>'
        f'</div>'
        f'<div class="wrap">{body}</div>'
        f'{live_script}</body></html>'
    )


# ── MAIN ─────────────────────────────────────────────────────────────────────

def get_fixture_events(fixture_id):
    """Recupera eventi goal di una partita FT — 1 chiamata API."""
    try:
        data = api_get("fixtures/events", {"fixture": fixture_id, "type": "Goal"})
        return data or []
    except Exception:
        return []


def generate_stats_html(matches, run_date, cover_start, cover_end):
    """Genera stats.html con statistiche avanzate sulle partite FT degli alert."""
    from datetime import datetime, timezone

    # Legge ft_history.json se esiste (aggiornato ogni 5 min da updater.py)
    # altrimenti fallback sui match in memoria
    history_file = Path("docs/ft_history.json")
    if history_file.exists():
        try:
            hist = json.loads(history_file.read_text())
            ft_matches = list(hist.values())
            print(f"stats: letti {len(ft_matches)} FT da ft_history.json")
        except Exception:
            ft_matches = [m for m in matches if m.get("status") in ("FT","AET","PEN")]
    else:
        ft_matches = [m for m in matches if m.get("status") in ("FT","AET","PEN")]

    total_ft  = len(ft_matches)
    total_all = len(matches)

    if total_ft == 0:
        return None

    first_goal_minutes = []
    total_goals_list   = []
    results_count      = {}
    league_stats       = {}
    match_events       = []

    for m in ft_matches:
        fid       = m.get("fixture_id")
        hg        = m.get("goals_home") or 0
        ag        = m.get("goals_away") or 0
        # Se first_min già cachato in history, salta la chiamata API
        cached_min = m.get("first_min_cached")
        evs = [] if cached_min is not None else (
            get_fixture_events(fid) if (fid and (hg + ag) > 0) else [])
        tot_g = hg + ag

        if cached_min is not None:
            first_min = cached_min
        else:
            mins = []
            for e in evs:
                if e.get("type") == "Goal" and e.get("detail") != "Missed Penalty":
                    raw   = e.get("time", {}).get("elapsed")
                    extra = e.get("time", {}).get("extra") or 0
                    if raw is not None:
                        mins.append(int(raw) + int(extra))
            mins.sort()
            first_min = mins[0] if mins else None
            # Salva in history per i prossimi run
            if history_file.exists() and fid and first_min is not None:
                try:
                    h2 = json.loads(history_file.read_text())
                    if str(fid) in h2:
                        h2[str(fid)]["first_min_cached"] = first_min
                        history_file.write_text(json.dumps(h2, ensure_ascii=False))
                except Exception:
                    pass
        if first_min is not None:
            first_goal_minutes.append(first_min)

        total_goals_list.append(tot_g)

        sc = f"{hg}-{ag}"
        results_count[sc] = results_count.get(sc, 0) + 1

        lg  = m.get("league", "?")
        nat = m.get("country", "")
        key = f"{lg}|{nat}"
        if key not in league_stats:
            league_stats[key] = {"n": 0, "goals": 0, "league": lg, "nation": nat}
        league_stats[key]["n"]     += 1
        league_stats[key]["goals"] += tot_g

        match_events.append({
            "home": m.get("home", "?"), "away": m.get("away", "?"),
            "league": lg, "nation": nat,
            "score": sc, "first_min": first_min, "total_goals": tot_g,
        })

    with_goal   = sum(1 for x in total_goals_list if x > 0)
    zero_zero   = total_ft - with_goal
    avg_goals   = round(sum(total_goals_list) / total_ft, 1) if total_ft else 0
    avg_first   = round(sum(first_goal_minutes) / len(first_goal_minutes), 1) if first_goal_minutes else 0
    min_first   = min(first_goal_minutes) if first_goal_minutes else 0
    max_first   = max(first_goal_minutes) if first_goal_minutes else 0
    strike_rate = round(with_goal / total_ft * 100) if total_ft else 0
    total_goals = sum(total_goals_list)
    over25      = sum(1 for x in total_goals_list if x > 2)
    over15      = sum(1 for x in total_goals_list if x > 1)
    gg          = sum(1 for m2 in ft_matches
                      if (m2.get("goals_home") or 0) > 0 and (m2.get("goals_away") or 0) > 0)

    fasce = [(1, 15), (16, 30), (31, 45), (46, 60), (61, 75), (76, 999)]
    fascia_data = []
    for lo, hi in fasce:
        n      = sum(1 for x in first_goal_minutes if lo <= x <= hi)
        pct    = round(n / len(first_goal_minutes) * 100, 1) if first_goal_minutes else 0
        mins_in = [x for x in first_goal_minutes if lo <= x <= hi]
        avg_m  = round(sum(mins_in) / len(mins_in), 1) if mins_in else 0
        lbl    = f"{lo}–90+'" if hi == 999 else f"{lo}–{hi}'"
        fascia_data.append({"lbl": lbl, "n": n, "pct": pct, "avg": avg_m})

    max_fascia_n = max((f["n"] for f in fascia_data), default=1) or 1

    hm_slots = []
    for i in range(18):
        lo2 = i * 5 + 1
        hi2 = (i + 1) * 5
        hm_slots.append(sum(1 for x in first_goal_minutes if lo2 <= x <= hi2))

    top_matches  = sorted(match_events, key=lambda x: x["total_goals"], reverse=True)[:10]
    top_leagues  = sorted(league_stats.values(), key=lambda x: x["n"], reverse=True)[:8]
    max_lg_n     = max((l["n"] for l in top_leagues), default=1) or 1
    quickest     = sorted([m2 for m2 in match_events if m2["first_min"] is not None],
                          key=lambda x: x["first_min"])[:7]

    ris_buckets = {
        "1-0|0-1": {"label": "1-0 / 0-1",  "sub": "vittoria scarto minimo",           "color": "var(--accent)", "n": 0},
        "2-1|1-2": {"label": "2-1 / 1-2",  "sub": "3 goal · GG sì",                  "color": "var(--blue)",   "n": 0},
        "2-0|0-2": {"label": "2-0 / 0-2",  "sub": "clean sheet",                      "color": "var(--yellow)", "n": 0},
        "3+":      {"label": "3+ diff",     "sub": "alta produttività",                "color": "var(--orange)", "n": 0},
        "0-0":     {"label": "0 – 0",       "sub": "nessun goal malgrado ≥12 ultime 5","color": "var(--red)",    "n": 0},
    }
    for sc, cnt in results_count.items():
        try:
            h2, a2 = int(sc.split("-")[0]), int(sc.split("-")[1])
        except Exception:
            continue
        if sc == "0-0":
            ris_buckets["0-0"]["n"] += cnt
        elif sc in ("1-0", "0-1"):
            ris_buckets["1-0|0-1"]["n"] += cnt
        elif sc in ("2-1", "1-2"):
            ris_buckets["2-1|1-2"]["n"] += cnt
        elif sc in ("2-0", "0-2"):
            ris_buckets["2-0|0-2"]["n"] += cnt
        else:
            ris_buckets["3+"]["n"] += cnt

    def pill_color(n):
        if n >= 7: return "#ff3a3a"
        if n >= 6: return "#ff8c00"
        if n >= 5: return "#f5c542"
        return "#4a5570"

    bar_colors = [
        "linear-gradient(90deg,#00e5a0,#00b87a)",
        "linear-gradient(90deg,#1a6aff,#0d4acc)",
        "linear-gradient(90deg,#f5c542,#d4a017)",
        "linear-gradient(90deg,#ff8c00,#cc6e00)",
        "linear-gradient(90deg,#ff3a3a,#cc2020)",
        "linear-gradient(90deg,#ff3a3a,#cc2020)",
    ]
    avg_colors = ["var(--accent)", "#6a9fff", "var(--yellow)", "var(--orange)", "var(--red)", "var(--red)"]

    fascia_rows = ""
    for i, f in enumerate(fascia_data):
        w = round(f["n"] / max_fascia_n * 100) if max_fascia_n else 0
        inner_lbl = str(f["n"]) if w > 30 else ""
        fascia_rows += (
            f'<tr><td><span class="flbl">{f["lbl"]}</span></td>'
            f'<td><div class="bwrap"><div class="bfill" style="width:{w}%;background:{bar_colors[i]}">{inner_lbl}</div></div></td>'
            f'<td class="nr">{f["n"]}</td><td class="pr">{f["pct"]}%</td>'
            f'<td class="ar" style="color:{avg_colors[i]}">{f["avg"]}\'</td></tr>'
        )

    hm_js = str(hm_slots)

    ris_html = ""
    ris_items = list(ris_buckets.values())
    for idx, r in enumerate(ris_items):
        pct  = round(r["n"] / total_ft * 100, 1) if total_ft else 0
        span = ' style="grid-column:span 2"' if (idx == len(ris_items) - 1 and len(ris_items) % 2 == 1) else ""
        ml   = ' style="margin-left:auto"' if span else ""
        ris_html += (
            f'<div class="ris-item"{span}>'
            f'<div class="ris-dot" style="background:{r["color"]}"></div>'
            f'<div><div class="ris-name">{r["label"]}</div><div class="ris-sub">{r["sub"]}</div></div>'
            f'<div{ml}><div class="ris-val" style="color:{r["color"]}">{r["n"]}</div>'
            f'<div class="ris-pct">{pct}%</div></div></div>'
        )

    def tm_row(m2, rk):
        pc = pill_color(m2["total_goals"])
        fm = f"{m2['first_min']}'" if m2["first_min"] is not None else "—"
        return (
            '<div style="display:grid;grid-template-columns:16px 1fr 46px 24px 22px;'
            'gap:5px;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.025)">'
            f'<div style="font-family:\'DM Mono\',monospace;font-size:.55rem;color:var(--muted)">{rk}</div>'
            f'<div><div style="font-size:.66rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
            f'{m2["home"]} vs {m2["away"]}</div>'
            f'<div style="font-size:.52rem;color:var(--muted)">{m2["league"]} · {m2["nation"]}</div></div>'
            f'<div style="font-family:\'DM Mono\',monospace;font-size:.68rem;font-weight:700;color:var(--accent);text-align:right">{m2["score"]}</div>'
            f'<div style="font-family:\'DM Mono\',monospace;font-size:.58rem;color:var(--orange);text-align:right">{fm}</div>'
            f'<div style="text-align:right"><span style="font-size:.55rem;font-weight:700;padding:1px 4px;'
            f'border-radius:3px;color:#05080f;background:{pc}">{m2["total_goals"]}</span></div></div>'
        )

    tm_html = "".join(tm_row(m2, i + 1) for i, m2 in enumerate(top_matches))

    lg_flags = {
        "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Germany": "🇩🇪", "Mexico": "🇲🇽", "Austria": "🇦🇹",
        "Serbia": "🇷🇸", "Brazil": "🇧🇷", "Nicaragua": "🇳🇮", "Chile": "🇨🇱",
        "Italy": "🇮🇹", "Spain": "🇪🇸", "France": "🇫🇷", "Portugal": "🇵🇹",
        "Argentina": "🇦🇷", "Colombia": "🇨🇴", "Peru": "🇵🇪", "Netherlands": "🇳🇱",
        "Belgium": "🇧🇪", "Poland": "🇵🇱", "Uruguay": "🇺🇾", "Ecuador": "🇪🇨",
    }
    lg_html = ""
    for lg in top_leagues:
        flag  = lg_flags.get(lg["nation"], "🌍")
        avg_g = round(lg["goals"] / lg["n"], 1) if lg["n"] else 0
        w     = round(lg["n"] / max_lg_n * 100)
        ac    = "var(--orange)" if avg_g >= 4 else "var(--yellow)" if avg_g >= 3 else "var(--muted)"
        lg_html += (
            f'<div class="lg-row">'
            f'<div class="lg-flag">{flag}</div>'
            f'<div class="lg-name">{lg["league"]}</div>'
            f'<div class="lg-bw"><div class="lg-bf" style="width:{w}%"></div></div>'
            f'<div class="lg-n">{lg["n"]}</div>'
            f'<div class="lg-avg" style="color:{ac}">{avg_g}</div></div>'
        )
    best_lg     = max(top_leagues, key=lambda x: x["goals"] / x["n"] if x["n"] else 0) if top_leagues else None
    best_lg_txt = f'{best_lg["league"]} {round(best_lg["goals"]/best_lg["n"],1)}' if best_lg else "—"

    dot_colors = ["var(--accent)", "var(--blue)", "var(--yellow)", "var(--orange)",
                  "var(--red)", "var(--purple)", "var(--muted)"]
    tl_html = ""
    for i, m2 in enumerate(quickest):
        c      = dot_colors[i % len(dot_colors)]
        shadow = f"box-shadow:0 0 5px {c}" if c != "var(--muted)" else ""
        rec    = ' <span style="color:var(--muted);font-size:.5rem">record</span>' if i == 0 else ""
        tl_html += (
            f'<div class="tl-item">'
            f'<div class="tl-dot" style="background:{c};{shadow}"></div>'
            f'<div class="tl-min" style="color:{c}">{m2["first_min"]}\'{rec}</div>'
            f'<div class="tl-match">{m2["home"]} vs {m2["away"]}</div>'
            f'<div class="tl-detail">{m2["league"]} · {m2["nation"]} · {m2["score"]} · {m2["total_goals"]} goal</div>'
            f'</div>'
        )

    over25_pct = round(over25 / total_ft * 100) if total_ft else 0
    over15_pct = round(over15 / total_ft * 100) if total_ft else 0
    gg_pct     = round(gg / total_ft * 100) if total_ft else 0
    early_pct  = round(sum(f["n"] for f in fascia_data[:2]) / len(first_goal_minutes) * 100) if first_goal_minutes else 0

    zz_pct  = round(zero_zero / total_ft * 100, 1) if total_ft else 0

    CSS = """
:root{--bg:#05080f;--card:#0c1220;--accent:#00e5a0;--blue:#1a6aff;--red:#ff3a3a;--orange:#ff8c00;--yellow:#f5c542;--purple:#b06aff;--text:#dde3f0;--muted:#4a5570;--border:rgba(255,255,255,0.06);}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;overflow-x:hidden;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;background:radial-gradient(ellipse 55% 35% at 15% 8%,rgba(0,229,160,0.045) 0%,transparent 70%),radial-gradient(ellipse 45% 30% at 85% 85%,rgba(26,106,255,0.05) 0%,transparent 70%),radial-gradient(ellipse 35% 25% at 55% 40%,rgba(255,58,58,0.025) 0%,transparent 70%);}
header{position:sticky;top:0;z-index:50;background:rgba(5,8,15,0.93);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:10px 26px;display:flex;align-items:center;gap:16px;}
.logo-text{font-size:1.1rem;font-weight:700;background:linear-gradient(90deg,#fff,var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.logo-sub{font-family:'DM Mono',monospace;font-size:.48rem;color:var(--muted);letter-spacing:.15em;display:block;margin-top:-2px;-webkit-text-fill-color:var(--muted);}
.hdiv{width:1px;height:22px;background:var(--border);}
.nav-link{font-family:'DM Mono',monospace;font-size:.63rem;color:var(--muted);text-decoration:none;padding:3px 9px;border-radius:5px;border:1px solid transparent;transition:all .2s;}
.nav-link:hover{color:var(--text);border-color:var(--border);}
.nav-link.active{color:var(--accent);border-color:rgba(0,229,160,.25);background:rgba(0,229,160,.06);}
.hright{margin-left:auto;font-family:'DM Mono',monospace;font-size:.57rem;color:var(--muted);}
.scanbar{background:rgba(0,229,160,.02);border-bottom:1px solid rgba(0,229,160,.07);padding:4px 26px;display:flex;flex-wrap:wrap;font-family:'DM Mono',monospace;font-size:.56rem;color:var(--muted);}
.si{padding:0 13px;border-right:1px solid rgba(255,255,255,.05);display:flex;gap:3px;align-items:center;}
.si::before{content:'›';color:var(--accent);}
.si b{color:var(--accent);font-weight:500;}
.wrap{padding:14px 26px;position:relative;z-index:1;}
.g5{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:11px;}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-bottom:11px;}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:11px;margin-bottom:11px;}
.panel{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:12px 14px;position:relative;overflow:hidden;animation:fadein .35s ease both;}
.panel::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.scope{font-family:'DM Mono',monospace;font-size:.5rem;color:var(--muted);margin-bottom:9px;display:flex;gap:5px;flex-wrap:wrap;align-items:center;}
.stag{padding:1px 6px;border-radius:3px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);color:rgba(255,255,255,.5);font-size:.49rem;}
.stag.g{border-color:rgba(0,229,160,.2);color:rgba(0,229,160,.7);background:rgba(0,229,160,.04);}
.stag.b{border-color:rgba(26,106,255,.2);color:rgba(100,160,255,.7);background:rgba(26,106,255,.04);}
.stag.y{border-color:rgba(245,197,66,.2);color:rgba(245,197,66,.7);background:rgba(245,197,66,.04);}
.ptitle{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;margin-bottom:9px;display:flex;align-items:center;gap:6px;}
.ptitle::after{content:'';flex:1;height:1px;background:var(--border);}
.kpi{border-radius:9px;overflow:hidden;transition:transform .15s;}
.kpi:hover{transform:translateY(-2px);}
.kpi-bar{height:2px;}
.k1 .kpi-bar{background:linear-gradient(90deg,var(--accent),transparent);}
.k2 .kpi-bar{background:linear-gradient(90deg,var(--blue),transparent);}
.k3 .kpi-bar{background:linear-gradient(90deg,var(--yellow),transparent);}
.k4 .kpi-bar{background:linear-gradient(90deg,var(--red),transparent);}
.k5 .kpi-bar{background:linear-gradient(90deg,var(--orange),transparent);}
.kpi-inner{padding:10px 12px 8px;}
.kpi-val{font-family:'DM Mono',monospace;font-size:1.6rem;font-weight:700;line-height:1;margin-bottom:2px;}
.k1 .kpi-val{color:var(--accent);}.k2 .kpi-val{color:var(--blue);}.k3 .kpi-val{color:var(--yellow);}.k4 .kpi-val{color:var(--red);}.k5 .kpi-val{color:var(--orange);}
.kpi-lbl{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
.kpi-sub{font-family:'DM Mono',monospace;font-size:.51rem;color:var(--muted);margin-top:2px;opacity:.6;}
.kpi-foot{padding:3px 12px;font-family:'DM Mono',monospace;font-size:.48rem;color:var(--muted);border-top:1px solid var(--border);display:flex;gap:4px;align-items:center;}
.kpi-foot::before{content:'↳';opacity:.35;}
.k1 .kpi-foot{background:rgba(0,229,160,.03);}.k2 .kpi-foot{background:rgba(26,106,255,.03);}.k3 .kpi-foot{background:rgba(245,197,66,.03);}.k4 .kpi-foot{background:rgba(255,58,58,.03);}.k5 .kpi-foot{background:rgba(255,140,0,.03);}
.ft{width:100%;border-collapse:collapse;}
.ft th{font-family:'DM Mono',monospace;font-size:.49rem;color:var(--muted);text-align:left;padding:0 5px 5px;letter-spacing:.07em;text-transform:uppercase;border-bottom:1px solid var(--border);}
.ft th.r{text-align:right;}
.ft td{padding:4px 5px;border-bottom:1px solid rgba(255,255,255,.025);vertical-align:middle;}
.ft tr:last-child td{border:none;}
.flbl{font-family:'DM Mono',monospace;font-size:.63rem;white-space:nowrap;}
.bwrap{background:rgba(255,255,255,.04);border-radius:2px;height:16px;overflow:hidden;}
.bfill{height:100%;border-radius:2px;display:flex;align-items:center;padding:0 5px;font-family:'DM Mono',monospace;font-size:.55rem;color:rgba(255,255,255,.9);font-weight:600;white-space:nowrap;}
.nr{font-family:'DM Mono',monospace;font-size:.63rem;text-align:right;}
.pr{font-family:'DM Mono',monospace;font-size:.59rem;text-align:right;color:var(--muted);}
.ar{font-family:'DM Mono',monospace;font-size:.59rem;text-align:right;}
.insight{margin-top:8px;padding-top:7px;border-top:1px solid var(--border);font-family:'DM Mono',monospace;font-size:.55rem;color:var(--muted);display:flex;gap:10px;}
.insight b{color:var(--accent);}
.hm-wrap{margin-top:9px;padding-top:8px;border-top:1px solid var(--border);}
.hm-title{font-family:'DM Mono',monospace;font-size:.5rem;color:var(--muted);margin-bottom:4px;letter-spacing:.08em;}
.hm-row{display:flex;gap:2px;}
.hm-cell{flex:1;height:20px;border-radius:2px;cursor:default;transition:transform .1s;}
.hm-cell:hover{transform:scaleY(1.25);}
.hm-labels{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.46rem;color:var(--muted);margin-top:3px;}
.hm-legend{display:flex;align-items:center;gap:5px;margin-top:4px;font-family:'DM Mono',monospace;font-size:.48rem;color:var(--muted);}
.hm-legend-bar{flex:1;height:5px;border-radius:2px;background:linear-gradient(90deg,rgba(0,229,160,.1),var(--accent));}
.ris-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:8px;}
.ris-item{background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:6px;padding:7px 9px;display:flex;align-items:center;gap:7px;}
.ris-dot{width:7px;height:7px;border-radius:2px;flex-shrink:0;}
.ris-name{font-size:.67rem;flex:1;}
.ris-sub{font-size:.54rem;color:var(--muted);}
.ris-val{font-family:'DM Mono',monospace;font-size:.72rem;font-weight:700;text-align:right;}
.ris-pct{font-family:'DM Mono',monospace;font-size:.54rem;color:var(--muted);text-align:right;}
.cross-row{display:flex;gap:7px;}
.cbox{flex:1;border-radius:6px;padding:6px 8px;text-align:center;border:1px solid;}
.cval{font-family:'DM Mono',monospace;font-size:.95rem;font-weight:700;}
.clbl{font-size:.53rem;color:var(--muted);margin-top:1px;line-height:1.3;}
.lg-row{display:flex;align-items:center;gap:7px;margin-bottom:6px;}
.lg-flag{font-size:.82rem;width:16px;text-align:center;}
.lg-name{font-size:.65rem;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lg-bw{width:80px;background:rgba(255,255,255,.04);border-radius:2px;height:5px;}
.lg-bf{height:5px;border-radius:2px;background:var(--accent);opacity:.65;}
.lg-n{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);width:20px;text-align:right;}
.lg-avg{font-family:'DM Mono',monospace;font-size:.56rem;color:var(--yellow);width:26px;text-align:right;}
.tl{position:relative;padding-left:16px;}
.tl::before{content:'';position:absolute;left:4px;top:0;bottom:0;width:1px;background:var(--border);}
.tl-item{position:relative;margin-bottom:8px;}
.tl-dot{position:absolute;left:-14px;top:4px;width:6px;height:6px;border-radius:50%;}
.tl-min{font-family:'DM Mono',monospace;font-size:.57rem;margin-bottom:1px;}
.tl-match{font-size:.68rem;font-weight:600;}
.tl-detail{font-size:.57rem;color:var(--muted);}
"""

    THRESHOLD_VAL = THRESHOLD
    LAST_N_VAL    = LAST_N

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GoalScan \u00b7 Stats Avanzate</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header>
  <div><span class="logo-text">GoalScan</span><span class="logo-sub">LIVE INTELLIGENCE \u00b7 BET365</span></div>
  <div class="hdiv"></div>
  <a href="index.html" class="nav-link">Dashboard</a>
  <a href="storico.html" class="nav-link">Storico</a>
  <a href="stats.html" class="nav-link active">Stats Avanzate</a>
  <a href="global_stats.html" class="nav-link">Stats Globali</a>
  <div class="hright">\U0001f4c5 {run_date}</div>
</header>
<div class="scanbar">
  <div class="si">alert analizzati <b>{total_all}</b></div>
  <div class="si">ratio <b>\u2265{THRESHOLD_VAL} goal ultime {LAST_N_VAL}</b> + 3FiltriAnti0-0</div>
  <div class="si">solo campionati <b>\u00b7 Bet365 verificate</b></div>
  <div class="si">copertura <b>{cover_start} \u2192 {cover_end}</b></div>
  <div class="si">partite FT analizzate <b>{total_ft}</b></div>
</div>
<div class="wrap">
<div class="g5">
  <div class="panel kpi k1"><div class="kpi-bar"></div><div class="kpi-inner"><div class="kpi-val">{total_ft}</div><div class="kpi-lbl">Partite finite (FT)</div><div class="kpi-sub">di {total_all} alert &middot; oggi</div></div><div class="kpi-foot">alert \u2265{THRESHOLD_VAL} &middot; Bet365 &middot; campionati</div></div>
  <div class="panel kpi k2"><div class="kpi-bar"></div><div class="kpi-inner"><div class="kpi-val">{avg_first}'</div><div class="kpi-lbl">Media 1&deg; goal</div><div class="kpi-sub">range {min_first}' &ndash; {max_first}'</div></div><div class="kpi-foot">su {with_goal} partite con \u22651 goal</div></div>
  <div class="panel kpi k3"><div class="kpi-bar"></div><div class="kpi-inner"><div class="kpi-val">{strike_rate}%</div><div class="kpi-lbl">Strike rate goal</div><div class="kpi-sub">{with_goal} con goal su {total_ft} FT</div></div><div class="kpi-foot">alert bot &middot; tutte le leghe</div></div>
  <div class="panel kpi k4"><div class="kpi-bar"></div><div class="kpi-inner"><div class="kpi-val">{zero_zero}</div><div class="kpi-lbl">Chiuse 0-0</div><div class="kpi-sub">{zz_pct}% degli FT</div></div><div class="kpi-foot">alert bot &middot; tutte le leghe</div></div>
  <div class="panel kpi k5"><div class="kpi-bar"></div><div class="kpi-inner"><div class="kpi-val">{avg_goals}</div><div class="kpi-lbl">Media goal/partita</div><div class="kpi-sub">{total_goals} goal totali</div></div><div class="kpi-foot">{total_ft} partite FT &middot; alert bot</div></div>
</div>
<div class="g3">
  <div class="panel">
    <div class="ptitle">\u23f1 Distribuzione 1&deg; goal</div>
    <div class="scope"><span class="stag g">oggi</span><span class="stag">{with_goal} partite con \u22651 goal</span><span class="stag b">alert bot &middot; tutte le leghe</span></div>
    <table class="ft"><thead><tr><th style="width:48px">FASCIA</th><th>BARRA <span style="opacity:.4;font-size:.43rem">n&deg; partite col 1&deg;goal in quel range</span></th><th class="r">N</th><th class="r">%</th><th class="r">AVG</th></tr></thead><tbody>{fascia_rows}</tbody></table>
    <div class="hm-wrap">
      <div class="hm-title">INTENSIT&Agrave; GOAL PER MINUTO (slot 5') &mdash; gradiente temperatura</div>
      <div class="hm-row" id="hm"></div>
      <div class="hm-labels"><span>1'</span><span>15'</span><span>30'</span><span>45'</span><span>60'</span><span>75'</span><span>90'</span></div>
      <div class="hm-legend"><span>meno</span><div class="hm-legend-bar"></div><span>pi&ugrave;</span></div>
    </div>
    <div class="insight">\U0001f4a1 <b>{early_pct}%</b> dei 1&deg; goal entro il 30' &nbsp;&middot;&nbsp; <b>{zero_zero}</b> partite rimaste 0-0</div>
  </div>
  <div class="panel">
    <div class="ptitle">\U0001f4ca Risultati finali &amp; mercati</div>
    <div class="scope"><span class="stag g">oggi</span><span class="stag">{total_ft} partite FT</span><span class="stag b">alert bot \u2265{THRESHOLD_VAL} &middot; Bet365</span></div>
    <div class="ris-grid">{ris_html}</div>
    <div style="font-family:'DM Mono',monospace;font-size:.5rem;color:var(--muted);margin:6px 0 5px;letter-spacing:.08em">SPLIT MERCATI &middot; {total_ft} FT</div>
    <div class="cross-row">
      <div class="cbox" style="border-color:rgba(187,134,252,.25);background:rgba(187,134,252,.05)"><div class="cval" style="color:#bb86fc">{over15_pct}%</div><div class="clbl">OVER 1.5<br><span style="color:#bb86fc;font-size:.57rem">{over15}/{total_ft}</span></div></div>
      <div class="cbox" style="border-color:rgba(0,229,160,.25);background:rgba(0,229,160,.05)"><div class="cval" style="color:var(--accent)">{over25_pct}%</div><div class="clbl">OVER 2.5<br><span style="color:var(--accent);font-size:.57rem">{over25}/{total_ft}</span></div></div>
      <div class="cbox" style="border-color:rgba(255,58,58,.2);background:rgba(255,58,58,.04)"><div class="cval" style="color:var(--red)">{100-over25_pct}%</div><div class="clbl">UNDER 2.5<br><span style="color:var(--red);font-size:.57rem">{total_ft-over25}/{total_ft}</span></div></div>
      <div class="cbox" style="border-color:rgba(26,106,255,.25);background:rgba(26,106,255,.05)"><div class="cval" style="color:#6a9fff">{gg_pct}%</div><div class="clbl">GG S&Igrave;<br><span style="color:#6a9fff;font-size:.57rem">{gg}/{total_ft}</span></div></div>
      <div class="cbox" style="border-color:rgba(245,197,66,.2);background:rgba(245,197,66,.04)"><div class="cval" style="color:var(--yellow)">{avg_goals}</div><div class="clbl">AVG GOAL<br><span style="color:var(--yellow);font-size:.57rem">{total_goals} tot</span></div></div>
    </div>
  </div>
  <div style="display:flex;flex-direction:column;gap:11px;">
    <div class="panel">
      <div class="ptitle">\U0001f30d Top leghe &middot; alert FT</div>
      <div class="scope"><span class="stag g">oggi</span><span class="stag">{total_ft} FT</span><span class="stag y">per n&deg; alert</span></div>
      <div style="display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.49rem;color:var(--muted);margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border)"><span>LEGA</span><span>ALERT &nbsp; AVG GOAL</span></div>
      {lg_html}
      <div style="margin-top:6px;padding-top:5px;border-top:1px solid var(--border);font-family:'DM Mono',monospace;font-size:.52rem;color:var(--muted)">\U0001f4a1 avg goal pi&ugrave; alto: <span style="color:var(--yellow)">{best_lg_txt}</span></div>
    </div>
    <div class="panel">
      <div class="ptitle">\u26a1 Primi goal pi&ugrave; veloci</div>
      <div class="scope"><span class="stag g">oggi</span><span class="stag">alert FT con 1&deg;goal pi&ugrave; precoce</span></div>
      <div class="tl">{tl_html}</div>
    </div>
  </div>
</div>
<div class="panel">
  <div class="ptitle">\U0001f3c6 Partite pi&ugrave; prolifiche &middot; top 10</div>
  <div class="scope"><span class="stag g">oggi</span><span class="stag">{total_ft} partite FT</span><span class="stag y">ordinate per goal totali</span><span class="stag b">alert bot \u2265{THRESHOLD_VAL} &middot; Bet365</span></div>
  <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:0 20px;">{tm_html}</div>
</div>
</div>
<script>
const hmData={hm_js};
const maxH=Math.max(...hmData);
const hmEl=document.getElementById('hm');
hmData.forEach((v,i)=>{{
  const d=document.createElement('div');d.className='hm-cell';
  const p=maxH>0?v/maxH:0;
  let r,g,b;
  if(p<0.33){{r=0;g=Math.round(100+p*3*129);b=Math.round(200-p*3*100);}}
  else if(p<0.66){{r=Math.round((p-0.33)*3*245);g=Math.round(229-((p-0.33)*3*50));b=50;}}
  else{{r=Math.round(200+p*55);g=Math.round(180-p*3*80);b=0;}}
  d.style.background=`rgba(${{r}},${{g}},${{b}},${{0.15+p*0.78}})`;
  const slot=(i+1)*5;d.title=`${{slot-4}}'-${{slot}}': ${{v}} partite`;
  d.onmouseenter=()=>d.style.transform='scaleY(1.25)';
  d.onmouseleave=()=>d.style.transform='';
  hmEl.appendChild(d);
}});
</script>
</body>
</html>"""


def generate_storico_html(run_date):
    """Genera docs/storico.html leggendo ft_history.json.
    Partite raggruppate per giorno, più recente in cima.
    Verde = con goal, Rosso = 0-0.
    """
    history_file = Path("docs/ft_history.json")
    if not history_file.exists():
        return None
    try:
        hist = json.loads(history_file.read_text())
    except Exception:
        return None

    matches = list(hist.values())
    if not matches:
        return None

    # Raggruppa per data
    from collections import defaultdict
    by_day = defaultdict(list)
    for m in matches:
        by_day[m.get("date", "?")].append(m)

    # Ordina giorni dal più recente
    sorted_days = sorted(by_day.keys(), reverse=True)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    total_matches = len(matches)
    total_goal    = sum(1 for m in matches if (m.get("goals_home") or 0) + (m.get("goals_away") or 0) > 0)
    total_zz      = total_matches - total_goal
    strike_rate   = round(total_goal / total_matches * 100) if total_matches else 0

    def fmt_day(d):
        try:
            from datetime import datetime as dt2
            dd = dt2.strptime(d, "%Y-%m-%d")
            giorni = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"]
            mesi   = ["Gen","Feb","Mar","Apr","Mag","Giu","Lug","Ago","Set","Ott","Nov","Dic"]
            return f"{giorni[dd.weekday()]} {dd.day} {mesi[dd.month-1]}"
        except:
            return d

    # Raggruppa per mese, poi per giorno
    from collections import OrderedDict
    MESI_FULL = ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                 "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    by_month = OrderedDict()
    for day in sorted_days:
        try:
            ym = day[:7]  # "2026-03"
            dd = datetime.strptime(day, "%Y-%m-%d")
            month_label = f"{MESI_FULL[dd.month-1]} {dd.year}"
        except:
            ym = "unknown"
            month_label = "Altro"
        by_month.setdefault((ym, month_label), []).append(day)

    days_html = ""
    for (ym, month_label), month_days in by_month.items():
        # Calcola stats del mese
        m_total = sum(len(by_day[d]) for d in month_days)
        m_goal  = sum(1 for d in month_days for m in by_day[d] if (m.get("goals_home") or 0)+(m.get("goals_away") or 0) > 0)
        m_zz    = m_total - m_goal
        m_strike = round(m_goal / m_total * 100) if m_total else 0

        # Il mese corrente parte aperto, gli altri chiusi
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        m_collapsed = "" if ym == current_month else " collapsed"
        m_arrow_cls = "" if ym == current_month else " closed"

        days_html += f"""
<div class="month-block">
  <div class="month-header" onclick="this.nextElementSibling.classList.toggle('collapsed');this.querySelector('.arrow').classList.toggle('closed')">
    <span class="month-label">📁 {month_label}</span>
    <span class="day-meta">
      <span class="tag-ok">{m_total} partite</span>
      <span class="tag-zz">{m_zz} × 0-0</span>
      <span class="tag-sr">{m_strike}% strike</span>
      <span class="arrow{m_arrow_cls}">&#9660;</span>
    </span>
  </div>
  <div class="month-content{m_collapsed}">"""

        FLAGS_ST = {
            "England":"\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f","Germany":"\U0001f1e9\U0001f1ea","Mexico":"\U0001f1f2\U0001f1fd","Italy":"\U0001f1ee\U0001f1f9",
            "Spain":"\U0001f1ea\U0001f1f8","France":"\U0001f1eb\U0001f1f7","Brazil":"\U0001f1e7\U0001f1f7","Argentina":"\U0001f1e6\U0001f1f7",
            "Portugal":"\U0001f1f5\U0001f1f9","Netherlands":"\U0001f1f3\U0001f1f1","Belgium":"\U0001f1e7\U0001f1ea","Poland":"\U0001f1f5\U0001f1f1",
            "Austria":"\U0001f1e6\U0001f1f9","Serbia":"\U0001f1f7\U0001f1f8","Chile":"\U0001f1e8\U0001f1f1","Colombia":"\U0001f1e8\U0001f1f4",
            "Uruguay":"\U0001f1fa\U0001f1fe","Ecuador":"\U0001f1ea\U0001f1e8","Peru":"\U0001f1f5\U0001f1ea","Greece":"\U0001f1ec\U0001f1f7",
            "Turkey":"\U0001f1f9\U0001f1f7","Romania":"\U0001f1f7\U0001f1f4","Slovenia":"\U0001f1f8\U0001f1ee","Bulgaria":"\U0001f1e7\U0001f1ec",
            "Croatia":"\U0001f1ed\U0001f1f7","Slovakia":"\U0001f1f8\U0001f1f0","Czech Republic":"\U0001f1e8\U0001f1ff","Hungary":"\U0001f1ed\U0001f1fa",
            "Ukraine":"\U0001f1fa\U0001f1e6","Russia":"\U0001f1f7\U0001f1fa","Sweden":"\U0001f1f8\U0001f1ea","Norway":"\U0001f1f3\U0001f1f4",
            "Denmark":"\U0001f1e9\U0001f1f0","Finland":"\U0001f1eb\U0001f1ee","Switzerland":"\U0001f1e8\U0001f1ed","Scotland":"\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
            "Wales":"\U0001f3f4\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f","Ireland":"\U0001f1ee\U0001f1ea","Nicaragua":"\U0001f1f3\U0001f1ee","Honduras":"\U0001f1ed\U0001f1f3",
            "Indonesia":"\U0001f1ee\U0001f1e9","Singapore":"\U0001f1f8\U0001f1ec","Myanmar":"\U0001f1f2\U0001f1f2","North Macedonia":"\U0001f1f2\U0001f1f0",
            "Lithuania":"\U0001f1f1\U0001f1f9","Latvia":"\U0001f1f1\U0001f1fb","Estonia":"\U0001f1ea\U0001f1ea","Moldova":"\U0001f1f2\U0001f1e9",
            "Albania":"\U0001f1e6\U0001f1f1","Kosovo":"\U0001f1fd\U0001f1f0","Bosnia":"\U0001f1e7\U0001f1e6","Montenegro":"\U0001f1f2\U0001f1ea",
            "USA":"\U0001f1fa\U0001f1f8","Japan":"\U0001f1ef\U0001f1f5","South Korea":"\U0001f1f0\U0001f1f7",
            "Saudi Arabia":"\U0001f1f8\U0001f1e6","Thailand":"\U0001f1f9\U0001f1ed","Vietnam":"\U0001f1fb\U0001f1f3",
            "Malaysia":"\U0001f1f2\U0001f1fe","Kazakhstan":"\U0001f1f0\U0001f1ff","Georgia":"\U0001f1ec\U0001f1ea",
            "Armenia":"\U0001f1e6\U0001f1f2","Azerbaijan":"\U0001f1e6\U0001f1ff",
            "Venezuela":"\U0001f1fb\U0001f1ea","Bolivia":"\U0001f1e7\U0001f1f4","Paraguay":"\U0001f1f5\U0001f1fe",
            "Panama":"\U0001f1f5\U0001f1e6","Costa Rica":"\U0001f1e8\U0001f1f7",
            "Egypt":"\U0001f1ea\U0001f1ec","Morocco":"\U0001f1f2\U0001f1e6","Nigeria":"\U0001f1f3\U0001f1ec",
            "World":"\U0001f30d",
        }
        def _g5_storico(stats):
            if not stats or stats.get("total") is None:
                return '<span style="color:var(--muted);font-size:.55rem">\u2014</span>'
            sc2 = stats.get("scored","")
            co2 = stats.get("conceded","")
            t2  = stats.get("total","")
            tc  = "#ff3a3a" if isinstance(t2,int) and t2>=20 else "#ff8c00" if isinstance(t2,int) and t2>=17 else "#f5c542" if isinstance(t2,int) and t2>=14 else "#00e5a0"
            return (f'<span class="pg">+{sc2}</span>'
                    f'<span class="pr">-{co2}</span>'
                    f'<span class="pt" style="background:{tc}">{t2}</span>')

        for day in month_days:
            day_matches = sorted(by_day[day], key=lambda x: x.get("kickoff",""), reverse=True)
            is_today    = day == today_str
            day_label   = ("\U0001f534 OGGI \u00b7 " if is_today else "") + fmt_day(day)
            day_goal    = sum(1 for m in day_matches if (m.get("goals_home") or 0)+(m.get("goals_away") or 0) > 0)
            day_zz      = len(day_matches) - day_goal
            day_strike  = round(day_goal/len(day_matches)*100) if day_matches else 0

            rows = ""
            for m in day_matches:
                hg  = m.get("goals_home") or 0
                ag  = m.get("goals_away") or 0
                tot = hg + ag
                sc  = m.get("score", f"{hg}-{ag}")
                fm  = m.get("first_min_cached")
                ko  = m.get("kickoff","?")
                lg  = m.get("league","?")
                nat = m.get("country","")

                if tot == 0:
                    sc_html  = f'<span class="sc-zz">0 \u2013 0</span>'
                    fm_html  = '<span class="fm-na">\u2014</span>'
                    row_cls  = "row-zz"
                else:
                    h, a     = sc.split("-") if "-" in sc else (hg, ag)
                    sc_html  = f'<span class="sc-ok">{h} \u2013 {a}</span>'
                    fm_html  = f'<span class="fm-ok">{fm}\'</span>' if fm else '<span class="fm-na">\u2014</span>'
                    row_cls  = "row-ok"

                flag = FLAGS_ST.get(nat, "\U0001f310")

                hs_st = m.get("home_stats") or {}
                as_st = m.get("away_stats") or {}
                g5_html = _g5_storico(hs_st) + '<span style="color:var(--muted);font-size:.5rem;margin:0 2px">|</span>' + _g5_storico(as_st)

                rows += (
                    f'<tr class="{row_cls}">'
                    f'<td class="td-ko">{ko}</td>'
                    f'<td class="td-teams"><span class="team-h">{m.get("home","?")}</span>'
                    f'<span class="vs">vs</span>'
                    f'<span class="team-a">{m.get("away","?")}</span></td>'
                    f'<td class="td-g5">{g5_html}</td>'
                    f'<td class="td-sc">{sc_html}</td>'
                    f'<td class="td-fm">{fm_html}</td>'
                    f'<td class="td-lg">{flag} {lg}</td>'
                    f'</tr>'
                )

            days_html += f"""
<div class="day-block">
  <div class="day-header" onclick="this.nextElementSibling.classList.toggle('collapsed');this.querySelector('.arrow').classList.toggle('closed')">
    <span class="day-label">{'<span class="today-dot"></span>' if is_today else ''}{day_label}</span>
    <span class="day-meta">
      <span class="tag-ok">{day_goal} con goal</span>
      <span class="tag-zz">{day_zz} &times; 0-0</span>
      <span class="tag-sr">{day_strike}% strike</span>
      <span class="arrow closed">&#9660;</span>
    </span>
  </div>
  <div class="table-wrap collapsed">
  <table class="mt">
    <thead><tr>
      <th class="th-ko">KO</th>
      <th class="th-teams">PARTITA</th>
      <th class="th-g5">G5</th>
      <th class="th-sc">SCORE</th>
      <th class="th-fm">1\u00b0 GOAL</th>
      <th class="th-lg">LEGA</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>"""

        # Chiudi il div del mese
        days_html += """
  </div>
</div>"""

    CSS = """
:root{--bg:#05080f;--card:#0c1220;--accent:#00e5a0;--red:#ff3a3a;--blue:#1a6aff;
--orange:#ff8c00;--yellow:#f5c542;--text:#dde3f0;--muted:#4a5570;--border:rgba(255,255,255,0.06);}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
background:radial-gradient(ellipse 55% 35% at 15% 8%,rgba(0,229,160,0.04) 0%,transparent 70%),
radial-gradient(ellipse 45% 30% at 85% 85%,rgba(26,106,255,0.05) 0%,transparent 70%);}
header{position:sticky;top:0;z-index:50;background:rgba(5,8,15,0.95);
backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
padding:10px 26px;display:flex;align-items:center;gap:16px;}
.logo-text{font-size:1.1rem;font-weight:700;
background:linear-gradient(90deg,#fff,var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;}
.logo-sub{font-family:'DM Mono',monospace;font-size:.48rem;color:var(--muted);
letter-spacing:.15em;display:block;margin-top:-2px;-webkit-text-fill-color:var(--muted);}
.hdiv{width:1px;height:22px;background:var(--border);}
.nav-link{font-family:'DM Mono',monospace;font-size:.63rem;color:var(--muted);
text-decoration:none;padding:3px 9px;border-radius:5px;border:1px solid transparent;transition:all .2s;}
.nav-link:hover{color:var(--text);border-color:var(--border);}
.nav-link.active{color:var(--accent);border-color:rgba(0,229,160,.25);background:rgba(0,229,160,.06);}
.hright{margin-left:auto;font-family:'DM Mono',monospace;font-size:.57rem;color:var(--muted);}
.scanbar{background:rgba(0,229,160,.02);border-bottom:1px solid rgba(0,229,160,.07);
padding:4px 26px;display:flex;flex-wrap:wrap;gap:0;
font-family:'DM Mono',monospace;font-size:.56rem;color:var(--muted);}
.si{padding:0 13px;border-right:1px solid rgba(255,255,255,.05);display:flex;gap:3px;align-items:center;}
.si::before{content:'›';color:var(--accent);}
.si b{color:var(--accent);}
.wrap{padding:14px 26px;position:relative;z-index:1;max-width:1100px;margin:0 auto;}
.day-block{margin-bottom:18px;}
.day-header{display:flex;align-items:center;justify-content:space-between;
padding:8px 12px;background:rgba(255,255,255,.03);
border:1px solid var(--border);border-radius:7px 7px 0 0;border-bottom:none;}
.day-label{font-family:'DM Mono',monospace;font-size:.7rem;font-weight:600;
color:var(--text);display:flex;align-items:center;gap:7px;}
.today-dot{width:7px;height:7px;border-radius:50%;background:var(--accent);
box-shadow:0 0 6px var(--accent);animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.day-meta{display:flex;gap:6px;align-items:center;}
.tag-ok{font-family:'DM Mono',monospace;font-size:.52rem;padding:2px 7px;
border-radius:3px;background:rgba(0,229,160,.08);color:var(--accent);border:1px solid rgba(0,229,160,.2);}
.tag-zz{font-family:'DM Mono',monospace;font-size:.52rem;padding:2px 7px;
border-radius:3px;background:rgba(255,58,58,.07);color:var(--red);border:1px solid rgba(255,58,58,.2);}
.tag-sr{font-family:'DM Mono',monospace;font-size:.52rem;padding:2px 7px;
border-radius:3px;background:rgba(255,255,255,.04);color:var(--muted);border:1px solid var(--border);}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:0 0 7px 7px;}
.mt{width:100%;border-collapse:collapse;}
.mt thead tr{background:rgba(255,255,255,.02);}
.mt th{font-family:'DM Mono',monospace;font-size:.48rem;color:var(--muted);text-align:left;padding:4px 8px;letter-spacing:.07em;text-transform:uppercase;border-bottom:1px solid var(--border);}
.mt td{padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);vertical-align:middle;white-space:nowrap;}
.mt tr:last-child td{border:none;}
.row-ok:hover{background:rgba(0,229,160,.04);}
.row-zz{background:rgba(255,58,58,.02);}
.row-zz:hover{background:rgba(255,58,58,.05);}
.td-ko{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);width:38px;}
.td-teams{font-size:.65rem;width:100%;}
.team-h{font-weight:600;}
.team-a{font-weight:600;}
.vs{font-family:'DM Mono',monospace;font-size:.52rem;color:var(--muted);margin:0 5px;}
.td-sc{width:72px;text-align:center;}
.sc-ok{font-family:'DM Mono',monospace;font-size:.68rem;font-weight:700;
color:var(--accent);background:rgba(0,229,160,.08);
padding:1px 7px;border-radius:4px;border:1px solid rgba(0,229,160,.2);white-space:nowrap;display:inline-block;}
.sc-zz{font-family:'DM Mono',monospace;font-size:.68rem;font-weight:700;
color:var(--red);background:rgba(255,58,58,.07);
padding:1px 7px;border-radius:4px;border:1px solid rgba(255,58,58,.2);white-space:nowrap;display:inline-block;}
.td-fm{width:52px;text-align:center;}
.fm-ok{font-family:'DM Mono',monospace;font-size:.62rem;color:var(--orange);font-weight:600;}
.fm-na{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);}
.td-lg{font-size:.6rem;color:var(--muted);white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis;}
.th-ko{width:38px;}.th-sc{width:72px;}.th-fm{width:52px;}
.month-block{margin:0 0 8px 0;}
.month-header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;
background:rgba(0,229,160,0.05);border:1px solid rgba(0,229,160,0.12);border-radius:8px;
cursor:pointer;user-select:none;margin:6px 0;}
.month-header:hover{background:rgba(0,229,160,0.08);}
.month-label{font-size:.85rem;font-weight:700;color:var(--accent);letter-spacing:.02em;}
.month-content{overflow:hidden;transition:max-height .3s ease,opacity .25s ease;max-height:9999px;opacity:1;}
.month-content.collapsed{max-height:0 !important;opacity:0;}
.day-header{cursor:pointer;user-select:none;}
.day-header:hover{background:rgba(255,255,255,.04);}
.table-wrap{overflow:hidden;transition:max-height .3s ease,opacity .25s ease;max-height:9999px;opacity:1;}
.table-wrap.collapsed{max-height:0 !important;opacity:0;border-color:transparent;}
.arrow{font-size:.55rem;color:var(--muted);margin-left:10px;display:inline-block;transition:transform .25s;}
.arrow.closed{transform:rotate(-90deg);}
.td-g5{width:120px;white-space:nowrap;}
.pg{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(0,229,160,.12);color:var(--accent);}
.pr{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(255,58,58,.12);color:var(--red);}
.pt{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;color:#05080f;}
"""

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GoalScan \u00b7 Storico Alert</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<header>
  <div><span class="logo-text">GoalScan</span><span class="logo-sub">LIVE INTELLIGENCE \u00b7 BET365</span></div>
  <div class="hdiv"></div>
  <a href="index.html" class="nav-link">Dashboard</a>
  <a href="storico.html" class="nav-link active">Storico</a>
  <a href="stats.html" class="nav-link">Stats Avanzate</a>
  <a href="global_stats.html" class="nav-link">Stats Globali</a>
  <div class="hright">\U0001f504 {run_date}</div>
</header>
<div class="scanbar">
  <div class="si">partite totali <b>{total_matches}</b></div>
  <div class="si">con goal <b>{total_goal}</b></div>
  <div class="si">0-0 <b>{total_zz}</b></div>
  <div class="si">strike rate <b>{strike_rate}%</b></div>
  <div class="si">giorni <b>{len(sorted_days)}</b></div>
</div>
<div class="wrap">
{days_html}
</div>
</body>
</html>"""


def analyze_fixture_global(fix):
    fixture    = fix.get("fixture", {})
    teams      = fix.get("teams", {})
    league     = fix.get("league", {})
    home_name  = teams.get("home", {}).get("name", "?")
    away_name  = teams.get("away", {}).get("name", "?")
    fixture_id = fixture.get("id")
    # Salta youth/women/reserve solo se esplicitamente richiesto — qui NO filtro
    # I raw fixtures non hanno _league_id, lo ricaviamo da league
    if "_league_id" not in fix:
        fix["_league_id"] = league.get("id")
    if "_season" not in fix:
        fix["_season"] = league.get("season")
    try:
        ko = (datetime.fromtimestamp(fixture.get("timestamp", 0), tz=timezone.utc) + timedelta(hours=2)).strftime("%H:%M")
        match_date = (datetime.fromtimestamp(fixture.get("timestamp", 0), tz=timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d")
    except Exception:
        ko = "--:--"; match_date = "?"
    if not has_bet365_odds(fixture_id):
        return None
    goals = fix.get("goals", {})
    home_id   = teams.get("home", {}).get("id")
    away_id   = teams.get("away", {}).get("id")
    league_id = fix.get("_league_id") or league.get("id")
    season    = fix.get("_season") or league.get("season")
    hs  = get_last_n_any(home_id, league_id, season) if home_id else None
    as_ = get_last_n_any(away_id, league_id, season) if away_id else None

    return {"home": home_name, "away": away_name,
        "league": league.get("name","?"), "country": league.get("country","?"),
        "kickoff": ko, "date": match_date, "fixture_id": fixture_id,
        "status": fixture.get("status",{}).get("short","NS"),
        "goals_home": goals.get("home"), "goals_away": goals.get("away"),
        "home_total": hs["total"] if hs else None,
        "away_total": as_["total"] if as_ else None,
        "home_stats": hs,
        "away_stats": as_}



def generate_global_stats_html(matches, run_date, global_hist=None):
    from datetime import datetime, timezone, timedelta
    all_matches  = [m for m in matches if m]
    if not all_matches:
        return None

    # Usa global_history.json come storico se disponibile
    ft_matches = [m for m in all_matches if m.get("status") in ("FT","AET","PEN")]
    if global_hist and len(global_hist) > 0:
        stat_matches = list(global_hist.values())
        stat_label   = f"storico ({len(stat_matches)} FT Bet365)"
    elif ft_matches:
        stat_matches = ft_matches
        stat_label   = "FT oggi"
    else:
        stat_matches = []
        stat_label   = "NS"
    has_ft   = len(stat_matches) > 0
    n_stat   = len(stat_matches)
    total_all = len(all_matches)
    total_goals_list = []; results_count = {}; league_stats = {}
    for m in stat_matches:
        hg = m.get("goals_home") or 0; ag = m.get("goals_away") or 0; tot = hg + ag
        total_goals_list.append(tot)
        sc = str(hg) + "-" + str(ag); results_count[sc] = results_count.get(sc, 0) + 1
        lg = m.get("league","?"); nat = m.get("country",""); key = lg + "|" + nat
        if key not in league_stats:
            league_stats[key] = {"n":0,"goals":0,"league":lg,"nation":nat}
        league_stats[key]["n"] += 1; league_stats[key]["goals"] += tot
    with_goal   = sum(1 for x in total_goals_list if x > 0)
    zero_zero   = n_stat - with_goal
    avg_goals   = round(sum(total_goals_list)/n_stat,1) if n_stat else 0
    strike_rate = round(with_goal/n_stat*100) if n_stat else 0
    total_goals = sum(total_goals_list)
    over25      = sum(1 for x in total_goals_list if x > 2)
    gg          = sum(1 for m2 in stat_matches if (m2.get("goals_home") or 0)>0 and (m2.get("goals_away") or 0)>0)
    over25_pct  = round(over25/n_stat*100) if n_stat else 0
    gg_pct      = round(gg/n_stat*100) if n_stat else 0
    zz_pct      = round(zero_zero/n_stat*100,1) if n_stat else 0
    all_leagues = sorted(league_stats.values(), key=lambda x: x["goals"], reverse=True)[:10]
    max_lg_n    = max((l["goals"] for l in all_leagues), default=1) or 1
    ris_data = [
        {"label":"0-0",     "color":"#ff3a3a","n":0},
        {"label":"1-0/0-1", "color":"#00e5a0","n":0},
        {"label":"2-1/1-2", "color":"#1a6aff","n":0},
        {"label":"2-0/0-2", "color":"#f5c542","n":0},
        {"label":"3+ diff", "color":"#ff8c00","n":0},
    ]
    for sc, cnt in results_count.items():
        try: h2 = int(sc.split("-")[0]); a2 = int(sc.split("-")[1])
        except: continue
        if sc == "0-0": ris_data[0]["n"] += cnt
        elif sc in ("1-0","0-1"): ris_data[1]["n"] += cnt
        elif sc in ("2-1","1-2"): ris_data[2]["n"] += cnt
        elif sc in ("2-0","0-2"): ris_data[3]["n"] += cnt
        else: ris_data[4]["n"] += cnt
    FLAGS = {"England":"\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
             "Germany":"\U0001f1e9\U0001f1ea","Italy":"\U0001f1ee\U0001f1f9",
             "Spain":"\U0001f1ea\U0001f1f8","France":"\U0001f1eb\U0001f1f7",
             "Brazil":"\U0001f1e7\U0001f1f7","Argentina":"\U0001f1e6\U0001f1f7",
             "Portugal":"\U0001f1f5\U0001f1f9","Netherlands":"\U0001f1f3\U0001f1f1",
             "Mexico":"\U0001f1f2\U0001f1fd","Colombia":"\U0001f1e8\U0001f1f4",
             "Chile":"\U0001f1e8\U0001f1f1","Austria":"\U0001f1e6\U0001f1f9",
             "Serbia":"\U0001f1f7\U0001f1f8","Belgium":"\U0001f1e7\U0001f1ea",
             "Poland":"\U0001f1f5\U0001f1f1","Turkey":"\U0001f1f9\U0001f1f7",
             "Greece":"\U0001f1ec\U0001f1f7","Sweden":"\U0001f1f8\U0001f1ea",
             "Denmark":"\U0001f1e9\U0001f1f0","Switzerland":"\U0001f1e8\U0001f1ed",
             "Norway":"\U0001f1f3\U0001f1f4","Romania":"\U0001f1f7\U0001f1f4",
             "Ukraine":"\U0001f1fa\U0001f1e6","Russia":"\U0001f1f7\U0001f1fa",
             "USA":"\U0001f1fa\U0001f1f8","Japan":"\U0001f1ef\U0001f1f5",
             "South Korea":"\U0001f1f0\U0001f1f7","World":"\U0001f30d",
             "Scotland":"\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
             "Wales":"\U0001f3f4\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f",
             "Croatia":"\U0001f1ed\U0001f1f7","Czech Republic":"\U0001f1e8\U0001f1ff",
             "Hungary":"\U0001f1ed\U0001f1fa","Slovakia":"\U0001f1f8\U0001f1f0",
             "Slovenia":"\U0001f1f8\U0001f1ee","Bulgaria":"\U0001f1e7\U0001f1ec",
             "Albania":"\U0001f1e6\U0001f1f1","Kosovo":"\U0001f1fd\U0001f1f0",
             "Montenegro":"\U0001f1f2\U0001f1ea","Bosnia":"\U0001f1e7\U0001f1e6",
             "Lithuania":"\U0001f1f1\U0001f1f9","Latvia":"\U0001f1f1\U0001f1fb",
             "Estonia":"\U0001f1ea\U0001f1ea","Finland":"\U0001f1eb\U0001f1ee",
             "Iceland":"\U0001f1ee\U0001f1f8","Ireland":"\U0001f1ee\U0001f1ea",
             "Israel":"\U0001f1ee\U0001f1f1","Egypt":"\U0001f1ea\U0001f1ec",
             "Morocco":"\U0001f1f2\U0001f1e6","Nigeria":"\U0001f1f3\U0001f1ec",
             "Saudi Arabia":"\U0001f1f8\U0001f1e6","Indonesia":"\U0001f1ee\U0001f1e9",
             "Thailand":"\U0001f1f9\U0001f1ed","Vietnam":"\U0001f1fb\U0001f1f3",
             "Malaysia":"\U0001f1f2\U0001f1fe","Singapore":"\U0001f1f8\U0001f1ec",
             "Kazakhstan":"\U0001f1f0\U0001f1ff","Georgia":"\U0001f1ec\U0001f1ea",
             "Armenia":"\U0001f1e6\U0001f1f2","Azerbaijan":"\U0001f1e6\U0001f1ff",
             "Peru":"\U0001f1f5\U0001f1ea","Ecuador":"\U0001f1ea\U0001f1e8",
             "Uruguay":"\U0001f1fa\U0001f1fe","Venezuela":"\U0001f1fb\U0001f1ea",
             "Bolivia":"\U0001f1e7\U0001f1f4","Paraguay":"\U0001f1f5\U0001f1fe",
             "Panama":"\U0001f1f5\U0001f1e6","Costa Rica":"\U0001f1e8\U0001f1f7",
             "Honduras":"\U0001f1ed\U0001f1f3","Nicaragua":"\U0001f1f3\U0001f1ee",
             "Guatemala":"\U0001f1ec\U0001f1f9","El Salvador":"\U0001f1f8\U0001f1fb"}
    rhtml = "".join(
        '<div class="ris-item">'
        + '<div class="ris-dot" style="background:' + r["color"] + '"></div>'
        + '<div class="ris-name">' + r["label"] + '</div>'
        + '<div style="margin-left:auto">'
        + '<div class="ris-val" style="color:' + r["color"] + '">' + str(r["n"]) + '</div>'
        + '<div class="ris-pct">' + str(round(r["n"]/n_stat*100,1) if n_stat else 0) + '%</div>'
        + '</div></div>'
        for r in ris_data)
    lhtml = "".join(
        '<div class="lg-row">'
        + '<div class="lg-flag">' + FLAGS.get(lg["nation"],"\U0001f310") + '</div>'
        + '<div class="lg-name">' + lg["league"] + '</div>'
        + '<div class="lg-bw"><div class="lg-bf" style="width:' + str(round(lg["n"]/max_lg_n*100)) + '%"></div></div>'
        + '<div class="lg-n">' + str(lg["n"]) + '</div>'
        + '<div class="lg-avg" style="color:' + ("var(--orange)" if lg["n"] and round(lg["goals"]/lg["n"],1)>=3 else "var(--muted)") + '">'
        + str(round(lg["goals"]/lg["n"],1) if lg["n"] else 0) + '</div>'
        + '</div>'
        for lg in all_leagues)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d1_str    = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    d2_str    = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    dlabels   = {today_str:"\U0001f4c5 OGGI", d1_str:"\U0001f4c5 DOMANI", d2_str:"\U0001f4c5 DOPODOMANI"}
    LIVE_ST   = {"1H","HT","2H","ET","P"}
    live_ms   = [m for m in all_matches if m.get("status") in LIVE_ST]
    other_ms  = [m for m in all_matches if m.get("status") not in LIVE_ST]
    def _g5pills(m):
        ht = m.get("home_total")
        at = m.get("away_total")
        if ht is None and at is None:
            return '<span style="color:var(--muted);font-size:.55rem">\u2014</span>'
        def tot_color(t):
            if t is None: return "#4a5570"
            if t >= 20: return "#ff3a3a"
            if t >= 17: return "#ff8c00"
            if t >= 14: return "#f5c542"
            return "#00e5a0"
        def match_color(s, c):
            tot = s + c
            if tot == 0: return "#ff3a3a"
            if tot >= 4: return "#00e5a0"
            if tot >= 3: return "#f5c542"
            return "#4a5570"
        hs2 = m.get("home_stats") or {}
        as2 = m.get("away_stats") or {}
        def team_pills(stats, total):
            if total is None: return '<span style="color:var(--muted);font-size:.55rem">\u2014</span>'
            tc = tot_color(total)
            mlist = stats.get("matches", []) if stats else []
            dots = ""
            for md in mlist:
                mc = match_color(md["s"], md["c"])
                dots += '<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:' + mc + ';margin:0 1px;vertical-align:middle" title="' + str(md["s"]) + '-' + str(md["c"]) + '"></span>'
            scored = stats.get("scored","") if stats else ""
            conceded = stats.get("conceded","") if stats else ""
            s = dots + ' '
            s += ('<span class="pg">+' + str(scored) + '</span>' if scored != "" else "")
            s += ('<span class="pr">-' + str(conceded) + '</span>' if conceded != "" else "")
            s += '<span class="pt" style="background:' + tc + '">' + str(total) + '</span>'
            return s
        return team_pills(hs2, ht) + '<span style="color:var(--muted);font-size:.5rem;margin:0 2px">|</span>' + team_pills(as2, at)

    days_html = ""
    if live_ms:
        lrows = "".join(
            '<tr class="row-live">'
            + '<td class="td-ko">' + m.get("kickoff","?") + '</td>'
            + '<td class="td-teams"><span class="team-h">' + m.get("home","?") + '</span>'
            + '<span class="vs">vs</span><span class="team-a">' + m.get("away","?") + '</span></td>'
            + '<td class="td-sc"><span class="sc-live">'
            + (str(m.get("goals_home","?"))+"-"+str(m.get("goals_away","?"))) + '</span></td>'
            + '<td class="td-st"><span class="badge-live">' + m.get("status","") + '</span></td>'
            + '<td class="td-lg">' + FLAGS.get(m.get("country",""),"\U0001f310") + " " + m.get("league","?") + '</td></tr>'
            for m in sorted(live_ms, key=lambda x: x.get("kickoff","")))
        days_html += ('<div class="day-block">'
            + '<div class="day-header-g">'
            + '<span class="day-label-g">\U0001f534 LIVE \u2014 ' + str(len(live_ms)) + ' in corso</span>'
            + '</div><div class="table-wrap"><table class="mt"><thead><tr>'
            + '<th>KO</th><th>PARTITA</th><th style="text-align:center;width:48px">G5</th><th>SCORE</th><th>ST</th><th>LEGA</th>'
            + '</tr></thead><tbody>' + lrows + '</tbody></table></div></div>')
    by_day = {}
    for m in sorted(other_ms, key=lambda x: (x.get("date",""), x.get("kickoff",""))):
        d = m.get("date","?")
        try: h = int(m.get("kickoff","00:00").split(":")[0])
        except: h = 0
        sl = str(h).zfill(2) + ":00"
        by_day.setdefault(d, {}).setdefault(sl, []).append(m)
    for day in sorted(by_day.keys()):
        dlabel    = dlabels.get(day, "\U0001f4c5 " + day)
        day_total = sum(len(v) for v in by_day[day].values())
        is_today  = (day == today_str)
        disp      = "block" if is_today else "none"
        arrow     = "\u25b4" if is_today else "\u25be"
        dhtml     = ('<div class="day-block">'
            + '<div class="day-header-g" onclick="var t=this.nextElementSibling;var a=this.querySelector(\'.day-arrow\');'
            + 'if(t.style.display===\'none\'){t.style.display=\'block\';a.textContent=\'\u25b4\'}else{t.style.display=\'none\';a.textContent=\'\u25be\'}">'
            + '<span class="day-label-g">' + dlabel + '</span>'
            + '<span class="day-meta-g">' + str(day_total) + ' partite <span class="day-arrow">' + arrow + '</span></span>'
            + '</div><div class="table-wrap" style="display:' + disp + '">')
        for sl in sorted(by_day[day].keys()):
            ms2 = by_day[day][sl]
            dhtml += ('<div class="slot-head" onclick="toggleSlot(this)">\u23f1 ' + sl + ' \u00b7 ' + str(len(ms2)) + ' match <span class="sl-arrow">\u25be</span></div>'
                + '<div class="slot-body" style="display:none"><table class="mt"><thead><tr>'
                + '<th>KO</th><th>PARTITA</th><th style="text-align:center;width:48px">G5</th><th>SCORE</th><th>ST</th><th>LEGA</th>'
                + '</tr></thead><tbody>')
            for m in ms2:
                hg2 = m.get("goals_home"); ag2 = m.get("goals_away")
                is_ft = m.get("status") in ("FT","AET","PEN")
                if is_ft and hg2 is not None and ag2 is not None:
                    sc2 = str(hg2)+"-"+str(ag2)
                    sc_cls = "sc-ok" if (hg2+ag2)>0 else "sc-zz"
                else:
                    sc2 = "\u2014"; sc_cls = "sc-ns"
                dhtml += ('<tr>'
                    + '<td class="td-ko">' + m.get("kickoff","?") + '</td>'
                    + '<td class="td-teams"><span class="team-h">' + m.get("home","?") + '</span>'
                    + '<span class="vs">vs</span><span class="team-a">' + m.get("away","?") + '</span></td>'
                    + '<td class="td-g5">' + _g5pills(m) + '</td>'
                + '<td class="td-sc"><span class="' + sc_cls + '">' + sc2 + '</span></td>'
                    + '<td class="td-st"><span class="badge-ns">' + m.get("status","NS") + '</span></td>'
                    + '<td class="td-lg">' + FLAGS.get(m.get("country",""),"\U0001f310") + " " + m.get("league","?") + '</td></tr>')
            dhtml += '</tbody></table></div>'
        dhtml += '</div></div>'
        days_html += dhtml
    css_dm   = "font-family:\'DM Sans\',sans-serif"
    css_mono = "font-family:\'DM Mono\',monospace"
    CSS = (":root{--bg:#05080f;--card:#0c1220;--accent:#00e5a0;--blue:#1a6aff;--red:#ff3a3a;"
        "--orange:#ff8c00;--yellow:#f5c542;--text:#dde3f0;--muted:#4a5570;--border:rgba(255,255,255,0.06);}"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "body{background:var(--bg);color:var(--text);" + css_dm + ";min-height:100vh;}"
        "header{position:sticky;top:0;z-index:50;background:rgba(5,8,15,0.93);backdrop-filter:blur(20px);"
        "border-bottom:1px solid var(--border);padding:10px 26px;display:flex;align-items:center;gap:16px;}"
        ".logo-text{font-size:1.1rem;font-weight:700;background:linear-gradient(90deg,#fff,var(--accent));"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;}"
        ".logo-sub{" + css_mono + ";font-size:.48rem;color:var(--muted);letter-spacing:.15em;display:block;-webkit-text-fill-color:var(--muted);}"
        ".hdiv{width:1px;height:22px;background:var(--border);}"
        ".nav-link{" + css_mono + ";font-size:.63rem;color:var(--muted);text-decoration:none;padding:3px 9px;"
        "border-radius:5px;border:1px solid transparent;transition:all .2s;}"
        ".nav-link:hover{color:var(--text);border-color:var(--border);}"
        ".nav-link.active{color:var(--accent);border-color:rgba(0,229,160,.25);background:rgba(0,229,160,.06);}"
        ".hright{margin-left:auto;" + css_mono + ";font-size:.57rem;color:var(--muted);}"
        ".scanbar{background:rgba(0,229,160,.02);border-bottom:1px solid rgba(0,229,160,.07);"
        "padding:4px 26px;display:flex;flex-wrap:wrap;" + css_mono + ";font-size:.56rem;color:var(--muted);}"
        ".si{padding:0 13px;border-right:1px solid rgba(255,255,255,.05);display:flex;gap:3px;align-items:center;}"
        ".si::before{content:\'\u203a\';color:var(--accent);}.si b{color:var(--accent);}"
        ".wrap{padding:14px 26px;}"
        ".g5{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:11px;}"
        ".g2{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-bottom:11px;}"
        ".panel{background:var(--card);border:1px solid var(--border);border-radius:9px;"
        "padding:12px 14px;position:relative;overflow:hidden;margin-bottom:11px;}"
        ".ptitle{" + css_mono + ";font-size:.6rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;"
        "margin-bottom:9px;display:flex;align-items:center;gap:6px;}"
        ".ptitle::after{content:\'\';flex:1;height:1px;background:var(--border);}"
        ".kpi{border-radius:9px;overflow:hidden;transition:transform .15s;}.kpi:hover{transform:translateY(-2px);}"
        ".kpi-bar{height:2px;}"
        ".k1 .kpi-bar{background:linear-gradient(90deg,var(--accent),transparent);}"
        ".k2 .kpi-bar{background:linear-gradient(90deg,var(--blue),transparent);}"
        ".k3 .kpi-bar{background:linear-gradient(90deg,var(--yellow),transparent);}"
        ".k4 .kpi-bar{background:linear-gradient(90deg,var(--red),transparent);}"
        ".k5 .kpi-bar{background:linear-gradient(90deg,var(--orange),transparent);}"
        ".kpi-inner{padding:10px 12px 8px;}.kpi-val{" + css_mono + ";font-size:1.6rem;font-weight:700;line-height:1;margin-bottom:2px;}"
        ".k1 .kpi-val{color:var(--accent);}.k2 .kpi-val{color:var(--blue);}.k3 .kpi-val{color:var(--yellow);}"
        ".k4 .kpi-val{color:var(--red);}.k5 .kpi-val{color:var(--orange);}"
        ".kpi-lbl{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}"
        ".kpi-sub{" + css_mono + ";font-size:.51rem;color:var(--muted);margin-top:2px;opacity:.6;}"
        ".kpi-foot{padding:3px 12px;" + css_mono + ";font-size:.48rem;color:var(--muted);"
        "border-top:1px solid var(--border);display:flex;gap:4px;align-items:center;}"
        ".kpi-foot::before{content:\'\u2197\';opacity:.35;}"
        ".k1 .kpi-foot{background:rgba(0,229,160,.03);}.k2 .kpi-foot{background:rgba(26,106,255,.03);}"
        ".k3 .kpi-foot{background:rgba(245,197,66,.03);}.k4 .kpi-foot{background:rgba(255,58,58,.03);}"
        ".k5 .kpi-foot{background:rgba(255,140,0,.03);}"
        ".ris-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:8px;}"
        ".ris-item{background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:6px;"
        "padding:7px 9px;display:flex;align-items:center;gap:7px;}"
        ".ris-dot{width:7px;height:7px;border-radius:2px;flex-shrink:0;}.ris-name{font-size:.67rem;flex:1;}"
        ".ris-val{" + css_mono + ";font-size:.72rem;font-weight:700;text-align:right;}"
        ".ris-pct{" + css_mono + ";font-size:.54rem;color:var(--muted);text-align:right;}"
        ".cross-row{display:flex;gap:7px;margin-top:8px;}"
        ".cbox{flex:1;border-radius:6px;padding:6px 8px;text-align:center;border:1px solid;}"
        ".cval{" + css_mono + ";font-size:.95rem;font-weight:700;}"
        ".clbl{font-size:.53rem;color:var(--muted);margin-top:1px;line-height:1.3;}"
        ".lg-row{display:flex;align-items:center;gap:7px;margin-bottom:5px;}"
        ".lg-flag{font-size:.82rem;width:18px;text-align:center;}"
        ".lg-name{font-size:.63rem;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
        ".lg-bw{width:90px;background:rgba(255,255,255,.04);border-radius:2px;height:5px;}"
        ".lg-bf{height:5px;border-radius:2px;background:var(--accent);opacity:.65;}"
        ".lg-n{" + css_mono + ";font-size:.58rem;color:var(--muted);width:22px;text-align:right;}"
        ".lg-avg{" + css_mono + ";font-size:.56rem;width:30px;text-align:right;}"
        ".day-block{margin-bottom:12px;}"
        ".day-header-g{display:flex;align-items:center;justify-content:space-between;padding:7px 12px;"
        "background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:7px 7px 0 0;"
        "border-bottom:none;cursor:pointer;user-select:none;}"
        ".day-header-g:hover{background:rgba(255,255,255,.05);}"
        ".day-label-g{" + css_mono + ";font-size:.68rem;font-weight:600;color:var(--text);}"
        ".day-meta-g{" + css_mono + ";font-size:.52rem;color:var(--muted);}"
        ".slot-head{" + css_mono + ";font-size:.57rem;color:var(--accent);padding:4px 8px;cursor:pointer;user-select:none;"
        ".slot-body{}"
        ".slot-body.hidden{display:none;}"
        "background:rgba(0,229,160,.04);border-bottom:1px solid rgba(0,229,160,.08);}"
        ".table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:0 0 7px 7px;}"
        ".mt{width:100%;border-collapse:collapse;}"
        ".mt th{" + css_mono + ";font-size:.48rem;color:var(--muted);text-align:left;padding:4px 8px;"
        "letter-spacing:.07em;text-transform:uppercase;border-bottom:1px solid var(--border);"
        "background:rgba(255,255,255,.02);}"
        ".mt td{padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);vertical-align:middle;white-space:nowrap;}"
        ".mt tr:last-child td{border:none;}.mt tr:hover{background:rgba(255,255,255,.025);}"
        ".row-live{background:rgba(255,58,58,.04);}"
        ".td-ko{" + css_mono + ";font-size:.58rem;color:var(--muted);width:38px;}"
        ".td-teams{font-size:.65rem;width:100%;}.team-h{font-weight:600;}.team-a{font-weight:600;}"
        ".vs{" + css_mono + ";font-size:.52rem;color:var(--muted);margin:0 5px;}"
        ".td-g5{width:120px;white-space:nowrap;}"".pg{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(0,229,160,.12);color:var(--accent);}"".pr{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(255,58,58,.12);color:var(--red);}"".pt{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;color:#05080f;}"
".td-sc{width:60px;text-align:center;}.td-st{width:42px;text-align:center;}"
        ".td-lg{font-size:.6rem;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;}"
        ".sc-ok{" + css_mono + ";font-size:.65rem;font-weight:700;color:var(--accent);"
        "background:rgba(0,229,160,.08);padding:1px 6px;border-radius:4px;border:1px solid rgba(0,229,160,.2);}"
        ".sc-zz{" + css_mono + ";font-size:.65rem;font-weight:700;color:var(--red);"
        "background:rgba(255,58,58,.07);padding:1px 6px;border-radius:4px;border:1px solid rgba(255,58,58,.2);}"
        ".sc-ns{" + css_mono + ";font-size:.62rem;color:var(--muted);}"
        ".sc-live{" + css_mono + ";font-size:.68rem;font-weight:700;color:var(--red);"
        "background:rgba(255,58,58,.12);padding:1px 6px;border-radius:4px;"
        "border:1px solid rgba(255,58,58,.2);animation:lbp 1.4s infinite;}"
        ".badge-live{" + css_mono + ";font-size:.52rem;color:var(--red);background:rgba(255,58,58,.1);"
        "padding:1px 5px;border-radius:3px;border:1px solid rgba(255,58,58,.2);}"
        ".badge-ns{" + css_mono + ";font-size:.52rem;color:var(--muted);background:rgba(255,255,255,.04);"
        "padding:1px 5px;border-radius:3px;border:1px solid var(--border);}"
        "@keyframes lbp{0%,100%{opacity:1}50%{opacity:.35}}")
    return (
        "<!DOCTYPE html><html lang=\"it\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>GoalScan \u00b7 Stats Globali Bet365</title>"
        "<link href=\"https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;700"
        "&family=DM+Mono:wght@400;500&display=swap\" rel=\"stylesheet\">"
        "<style>" + CSS + "</style></head><body>"
        "<header><div><span class=\"logo-text\">GoalScan</span>"
        "<span class=\"logo-sub\">LIVE INTELLIGENCE \u00b7 BET365</span></div>"
        "<div class=\"hdiv\"></div>"
        "<a href=\"index.html\" class=\"nav-link\">Dashboard</a>"
        "<a href=\"storico.html\" class=\"nav-link\">Storico</a>"
        "<a href=\"stats.html\" class=\"nav-link\">Stats Avanzate</a>"
        "<a href=\"global_stats.html\" class=\"nav-link active\">Stats Globali</a>"
        "<div class=\"hright\">\U0001f4c5 " + run_date + "</div></header>"
        "<div class=\"scanbar\">"
        "<div class=\"si\">partite Bet365 <b>" + str(total_all) + "</b></div>"
        "<div class=\"si\">statistiche su <b>" + str(n_stat) + " " + stat_label + "</b></div>"
        "<div class=\"si\">nessun filtro goal</div>"
        "<div class=\"si\">strike rate <b>" + str(strike_rate) + "%</b></div>"
        "<div class=\"si\">media goal <b>" + str(avg_goals) + "</b></div>"
        "</div><div class=\"wrap\">"
        "<div class=\"g5\">"
        + "<div class=\"panel kpi k1\"><div class=\"kpi-bar\"></div><div class=\"kpi-inner\"><div class=\"kpi-val\">" + str(total_all) + "</div><div class=\"kpi-lbl\">Partite Bet365</div><div class=\"kpi-sub\">trovate oggi</div></div><div class=\"kpi-foot\">nessun filtro goal</div></div>"
        + "<div class=\"panel kpi k2\"><div class=\"kpi-bar\"></div><div class=\"kpi-inner\"><div class=\"kpi-val\">" + str(strike_rate) + "%</div><div class=\"kpi-lbl\">Strike rate</div><div class=\"kpi-sub\">" + str(with_goal) + " con goal su " + str(n_stat) + "</div></div><div class=\"kpi-foot\">partite " + stat_label + "</div></div>"
        + "<div class=\"panel kpi k3\"><div class=\"kpi-bar\"></div><div class=\"kpi-inner\"><div class=\"kpi-val\">" + str(avg_goals) + "</div><div class=\"kpi-lbl\">Media goal</div><div class=\"kpi-sub\">" + str(total_goals) + " goal totali</div></div><div class=\"kpi-foot\">" + str(n_stat) + " " + stat_label + "</div></div>"
        + "<div class=\"panel kpi k4\"><div class=\"kpi-bar\"></div><div class=\"kpi-inner\"><div class=\"kpi-val\">" + str(zero_zero) + "</div><div class=\"kpi-lbl\">Chiuse 0-0</div><div class=\"kpi-sub\">" + str(zz_pct) + "%</div></div><div class=\"kpi-foot\">Bet365 \u00b7 tutte le leghe</div></div>"
        + "<div class=\"panel kpi k5\"><div class=\"kpi-bar\"></div><div class=\"kpi-inner\"><div class=\"kpi-val\">" + str(over25_pct) + "%</div><div class=\"kpi-lbl\">Over 2.5</div><div class=\"kpi-sub\">" + str(over25) + " su " + str(n_stat) + "</div></div><div class=\"kpi-foot\">Bet365 \u00b7 tutte le leghe</div></div>"
        + "</div>"
                + "<div class=\"panel\"><div class=\"ptitle\">\U0001f4cb Partite Bet365 \u00b7 per giorno e fascia oraria</div>"
        + days_html
        + "</div>"
        + "<div class=\"panel\"><div class=\"ptitle\">\U0001f30d Top 10 leghe Bet365 per goal \u00b7 ordine discendente</div>"
        + "<div style=\"display:flex;justify-content:space-between;font-size:.49rem;color:var(--muted);margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--border)\"><span>LEGA</span><span>N \u00b7 AVG GOAL</span></div>"
        + lhtml + "</div>"
        + "</div>"
        + "<script>function toggleSlot(el){var b=el.nextElementSibling;var a=el.querySelector('.sl-arrow');"
        + "if(b.style.display==='none'){b.style.display='block';if(a)a.textContent='\u25b4'}"
        + "else{b.style.display='none';if(a)a.textContent='\u25be'}}</script>"
        + "</div></body></html>")


def main():
    print("=" * 60)
    print(f"GOAL BOT  |  soglia ≥{THRESHOLD}  |  ultime {LAST_N} gare  |  Bet365")
    print("=" * 60)

    print("\n[1] Recupero match oggi + domani + dopodomani...")
    league_seasons, fixtures_by_league, raw_fixtures = get_all_fixtures()

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

    # Genera stats.html
    cover_start = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d/%m/%Y")
    cover_end   = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%d/%m/%Y")
    stats_html  = generate_stats_html(qualified, run_date, cover_start, cover_end)
    if stats_html:
        stats_file = docs / "stats.html"
        stats_file.write_text(stats_html.encode('utf-8', errors='replace').decode('utf-8'), encoding="utf-8")
        print(f"stats.html generato con dati FT oggi")
    else:
        print("Nessuna partita FT oggi — stats.html non generato")

    # Genera storico.html
    storico_html = generate_storico_html(run_date)
    if storico_html:
        (docs / "storico.html").write_text(storico_html.encode('utf-8', errors='replace').decode('utf-8'), encoding="utf-8")
        print(f"storico.html generato")

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

    # Statistiche Globali Bet365
    print("\n[5] Generazione Stats Globali Bet365...")
    global_bet365 = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut2 = {executor.submit(analyze_fixture_global, fix): fix for fix in raw_fixtures}
        for future in as_completed(fut2):
            try:
                result = future.result()
                if result:
                    global_bet365.append(result)
            except Exception:
                pass
    print(f"  Partite Bet365: {len(global_bet365)}")

    # Accumula storico partite FT Bet365 in global_history.json
    global_hist_file = docs / "global_history.json"
    global_hist = {}
    if global_hist_file.exists():
        try:
            global_hist = json.loads(global_hist_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            global_hist = {}
    # Aggiungi solo partite FT nuove
    for m in global_bet365:
        if m and m.get("status") in ("FT","AET","PEN") and m.get("fixture_id"):
            key = str(m["fixture_id"])
            if key not in global_hist:
                global_hist[key] = m
    global_hist_file.write_text(
        json.dumps(global_hist, ensure_ascii=False).encode("utf-8", errors="replace").decode("utf-8"),
        encoding="utf-8"
    )
    print(f"  global_history.json: {len(global_hist)} partite FT accumulate")

    global_html = generate_global_stats_html(global_bet365, run_date, global_hist)
    if global_html:
        (docs / "global_stats.html").write_text(
            global_html.encode("utf-8", errors="replace").decode("utf-8"),
            encoding="utf-8"
        )
        print("  global_stats.html generato")

    # Salva lista fixture_id per live_updater.py
    # Solo IDs di oggi e domani — non accumulare storici (causano chiamate eccessive dal Worker)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d1_str    = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    ids = [m["fixture_id"] for m in qualified
           if m.get("fixture_id") and m.get("date","") in (today_str, d1_str)]
    ids_file = docs / "alert_ids.json"
    ids_file.write_text(json.dumps(ids))
    print(f"alert_ids.json: {len(ids)} fixture (solo oggi+domani)")

    print("\n[4] Invio Telegram...")
    if TELEGRAM_ENABLED:
        send_telegram(qualified, total, run_date)

if __name__ == "__main__":
    main()

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
        data = api_get("fixtures", {"date": date, "status": "NS"})
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

    return {"home": home_name, "away": away_name,
            "home_stats": hs, "away_stats": as_,
            "league": league_name, "country": country, "kickoff": ko,
            "date": match_date, "fixture_id": fixture_id}, \
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
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_labels = {
        today_str: "📅 OGGI",
        (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"): "📅 DOMANI",
        (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"): "📅 DOPODOMANI",
    }

    css = (
        ":root{--bg:#080d18;--surface:#0f1623;--card:#151e2e;--accent:#00e5a0;"
        "--red:#ff4757;--text:#dde3f0;--muted:#556080;--border:rgba(255,255,255,0.06);}"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;padding-bottom:60px;}"
        "header{background:var(--surface);border-bottom:1px solid var(--border);"
        "padding:14px 28px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:20;}"
        ".htitle{font-size:1.4rem;font-weight:700;color:var(--accent);}"
        ".hsub{font-size:.72rem;color:var(--muted);margin-top:3px;}"
        ".hbadge{margin-left:auto;background:var(--accent);color:#080d18;"
        "font-weight:700;padding:6px 18px;border-radius:100px;font-size:1rem;white-space:nowrap;}"
        ".cbar{background:rgba(0,229,160,0.04);border-bottom:1px solid var(--border);"
        "padding:7px 28px;font-size:.73rem;color:var(--muted);display:flex;gap:24px;flex-wrap:wrap;}"
        ".cbar strong{color:var(--accent);}"
        ".legend{display:flex;gap:16px;padding:14px 20px 4px;flex-wrap:wrap;}"
        ".leg-item{display:flex;align-items:center;gap:6px;font-size:.72rem;color:var(--muted);}"
        ".leg-dot{width:10px;height:10px;border-radius:3px;}"
        ".day-block{margin:24px 0 0;}"
        ".day-header{padding:10px 16px 6px;display:flex;align-items:center;gap:12px;}"
        ".day-label{font-size:1.05rem;font-weight:700;color:#fff;letter-spacing:.04em;}"
        ".day-count{font-size:.72rem;color:var(--muted);background:rgba(255,255,255,0.07);"
        "padding:2px 10px;border-radius:100px;}"
        ".day-line{flex:1;height:2px;background:linear-gradient(90deg,rgba(0,229,160,.35),transparent);}"
        ".ts{margin:10px 16px 0;}"
        ".th{display:flex;align-items:center;gap:10px;margin-bottom:8px;}"
        ".tl{font-size:.85rem;font-weight:700;color:var(--accent);}"
        ".tc{font-size:.68rem;color:var(--muted);background:rgba(255,255,255,0.05);"
        "padding:2px 8px;border-radius:100px;}"
        ".th::after{content:'';flex:1;height:1px;background:var(--border);}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:8px;}"
        ".card{background:var(--card);border:1px solid var(--border);border-radius:12px;"
        "padding:11px 13px;transition:transform .15s,box-shadow .15s;}"
        ".card:hover{transform:translateY(-2px);box-shadow:0 6px 28px rgba(0,229,160,.12);}"
        ".ct{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px;}"
        ".league{font-size:.67rem;color:var(--muted);white-space:nowrap;overflow:hidden;"
        "text-overflow:ellipsis;max-width:72%;}"
        ".ko{font-size:.72rem;color:var(--accent);font-weight:700;}"
        ".mu{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:6px;}"
        ".side{display:flex;flex-direction:column;gap:5px;}"
        ".side.r{align-items:flex-end;text-align:right;}"
        ".tn{font-size:.82rem;font-weight:700;line-height:1.2;white-space:nowrap;"
        "overflow:hidden;text-overflow:ellipsis;max-width:118px;}"
        ".pills{display:flex;gap:3px;align-items:center;}"
        ".side.r .pills{justify-content:flex-end;}"
        ".pill{font-size:.7rem;font-weight:700;padding:2px 6px;border-radius:4px;}"
        ".pill.g{background:rgba(0,229,160,.15);color:var(--accent);}"
        ".pill.rc{background:rgba(255,71,87,.15);color:var(--red);}"
        ".pill.tot{color:#080d18;border-radius:6px;padding:2px 8px;font-size:.76rem;}"
        ".vs{font-size:1rem;color:var(--muted);font-weight:700;text-align:center;}"
        ".live-badge{display:inline-flex;align-items:center;gap:4px;font-size:.65rem;font-weight:700;"
        "background:rgba(255,71,87,.15);color:#ff4757;padding:2px 7px;border-radius:100px;"
        "animation:pulse 1.4s infinite;}"
        ".live-badge.ht{background:rgba(255,140,0,.15);color:#ff8c00;animation:none;}"
        ".live-badge.ft{background:rgba(85,96,128,.15);color:var(--muted);animation:none;}"
        "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}"
        ".score{font-size:1.1rem;font-weight:700;color:#fff;text-align:center;line-height:1;}"
        ".score.goal-home{background:rgba(0,229,160,.12);border-radius:6px;}"
        ".score.goal-away{background:rgba(0,229,160,.12);border-radius:6px;}"
        ".card.scoring{border-color:rgba(0,229,160,.5);box-shadow:0 0 16px rgba(0,229,160,.15);"
        "background:rgba(0,229,160,.06);}"
        ".plane-bg{position:absolute;font-size:4rem;opacity:0.08;bottom:6px;right:10px;"
        "animation:fly 3s ease-in-out infinite;pointer-events:none;}"
        "@keyframes fly{0%{transform:translateX(0) rotate(-10deg)}"
        "50%{transform:translateX(8px) rotate(-5deg)}"
        "100%{transform:translateX(0) rotate(-10deg)}}"
        ".card{position:relative;overflow:hidden;}"
        ".bet{font-size:.65rem;color:#00e5a0;text-align:center;margin-top:5px;opacity:.7;}"
        ".empty{text-align:center;padding:80px 20px;color:var(--muted);}"
        ".empty h3{font-size:1.2rem;color:var(--text);margin-bottom:6px;}"
    )

    legend = (
        '<div class="legend">'
        '<span class="leg-item"><span class="leg-dot" style="background:#00e5a0"></span>14–16 goal</span>'
        '<span class="leg-item"><span class="leg-dot" style="background:#ff8c00"></span>17–19 goal</span>'
        '<span class="leg-item"><span class="leg-dot" style="background:#ff4757"></span>≥20 goal</span>'
        '<span class="leg-item" style="margin-left:8px">+F=fatti &nbsp;|&nbsp; -S=subiti &nbsp;|&nbsp; TOT=somma 5 gare &nbsp;|&nbsp; ✅ quote Bet365</span>'
        '</div>'
    )

    def make_card(m):
        hs = m["home_stats"]; as_ = m["away_stats"]
        fid = m.get("fixture_id","")
        return (
            f'<div class="card" data-fid="{fid}"><div class="ct">'
            f'<span class="league">{m["league"]}</span>'
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'<span class="live-score" style="display:none"></span>'
            f'<span class="ko">{m["kickoff"]}</span></div></div>'
            f'<div class="mu"><div class="side">'
            f'<span class="tn">{m["home"]}</span>'
            f'<div class="pills">'
            f'<span class="pill g">+{hs["scored"]}</span>'
            f'<span class="pill rc">-{hs["conceded"]}</span>'
            f'<span class="pill tot" style="background:{badge_color(hs["total"])}">{hs["total"]}</span>'
            f'</div></div>'
            f'<div style="text-align:center">'
            f'<span class="vs">VS</span>'
            f'<div class="score" data-score style="display:none"></div></div>'
            f'<div class="side r"><span class="tn">{m["away"]}</span>'
            f'<div class="pills">'
            f'<span class="pill g">+{as_["scored"]}</span>'
            f'<span class="pill rc">-{as_["conceded"]}</span>'
            f'<span class="pill tot" style="background:{badge_color(as_["total"])}">{as_["total"]}</span>'
            f'</div></div></div>'
            f'<div class="bet">✅ Bet365</div>'
            f'</div>'
        )


    if not matches:
        body = (f'<div class="empty"><h3>Nessun match qualificato</h3>'
                f'<p>Nessuna coppia soddisfa ≥{THRESHOLD} goal + quote Bet365<br>'
                f'nelle ultime {LAST_N} gare stessa lega.<br>'
                f'Match analizzati: <strong>{total_analyzed}</strong></p></div>')
    else:
        days = {}
        for m in sorted(matches, key=lambda x: (x["date"], x["kickoff"])):
            d = m["date"]
            s = slot(m["kickoff"])
            days.setdefault(d, {}).setdefault(s, []).append(m)

        sections = []
        for day in sorted(days):
            label     = day_labels.get(day, f"📅 {day}")
            day_total = sum(len(v) for v in days[day].values())
            day_html  = (
                f'<div class="day-block">'
                f'<div class="day-header">'
                f'<span class="day-label">{label}</span>'
                f'<span class="day-count">{day_total} alert</span>'
                f'<div class="day-line"></div></div>'
            )
            for ts in sorted(days[day]):
                cards = "".join(make_card(m) for m in days[day][ts])
                day_html += (
                    f'<div class="ts"><div class="th">'
                    f'<span class="tl">⏱ {ts}</span>'
                    f'<span class="tc">{len(days[day][ts])} match</span>'
                    f'</div><div class="grid">{cards}</div></div>'
                )
            day_html += '</div>'
            sections.append(day_html)

        body = "\n".join(sections)

    live_script = '<script>\nconst PROXY=\'https://spring-hall-b29e.nwgir.workers.dev\';\nasync function updateLive(){\n  try{\n    var ids=[].slice.call(document.querySelectorAll(\'.card[data-fid]\')).map(function(c){return c.getAttribute(\'data-fid\');}).filter(Boolean).join(\'-\');\n    if(!ids)return;\n    fetch(PROXY+\'?endpoint=fixtures&ids=\'+ids).then(function(r){return r.json();}).then(function(data){\n      (data.response||[]).forEach(function(fix){\n        var fid=String(fix.fixture.id);\n        var card=document.querySelector(\'[data-fid="\'+fid+\'"]\');\n        if(!card)return;\n        var st=fix.fixture.status.short,min=fix.fixture.status.elapsed,hg=fix.goals.home,ag=fix.goals.away;\n        var b=card.querySelector(\'.live-score\');\n        if(!b)return;\n        var live=[\'1H\',\'2H\',\'ET\',\'P\'].indexOf(st)>=0,ht=st===\'HT\',ft=st===\'FT\';\n        if(live||ht||ft){b.style.display=\'inline-flex\';b.className=\'live-badge\'+(ht?\' ht\':ft?\' ft\':\'\');b.textContent=ft?\'FT\':ht?\'HT\':(min?min+"\'":st);}\n        if(hg!=null&&ag!=null){\n          var s=card.querySelector(\'[data-score]\');\n          if(s){s.textContent=hg+\' - \'+ag;s.style.display=\'block\';var v=card.querySelector(\'.vs\');if(v)v.style.display=\'none\';}\n          var ok=(hg===1&&ag===0)||(hg===0&&ag===1);\n          if(ok&&!card.classList.contains(\'scoring\')){card.classList.add(\'scoring\');var p=document.createElement(\'div\');p.className=\'plane-bg\';p.innerHTML=\'&#9992;&#65039;\';card.appendChild(p);}\n          else if(!ok){card.classList.remove(\'scoring\');var pl=card.querySelector(\'.plane-bg\');if(pl)pl.remove();}\n        }\n      });\n      var ts=document.getElementById(\'live-ts\');\n      if(ts)ts.textContent=\'\\ud83d\\udd04 \'+new Date().toLocaleTimeString();\n    });\n  }catch(e){console.log(\'live\',e);}\n}\nupdateLive();setInterval(updateLive,30000);\n</script>'

    return (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Goal Bot — {run_date}</title>'
        f'<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">'
        f'<style>{css}</style></head><body>'
        f'<header><div><div class="htitle">⚽ Goal Bot — {run_date}</div>'
        f'<div class="hsub">Entrambe ≥{THRESHOLD} goal + quote Bet365 — ultime {LAST_N} gare stessa lega'
        f' — {total_analyzed} match analizzati</div></div>'
        f'<div class="hbadge">{len(matches)} ALERT</div></header>'
        f'<div class="cbar">'
        f'<span>Soglia: <strong>≥{THRESHOLD}</strong> per squadra</span>'
        f'<span>Ultime <strong>{LAST_N}</strong> gare stessa lega</span>'
        f'<span>Solo campionati <strong>3 giorni</strong></span>'
        f'<span>Quote <strong>Bet365</strong> verificate</span>'
        f'</div>{legend}{body}{live_script}</body></html>'
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
    archive_file.write_text(html_content, encoding="utf-8")

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
    out.write_text(index_html, encoding="utf-8")
    print(f"\nReport salvato: {archive_file.name} → aggiornato index.html")

    # Salva lista fixture_id per live_updater.py
    ids = [m["fixture_id"] for m in qualified if m.get("fixture_id")]
    (docs / "alert_ids.json").write_text(json.dumps(ids))
    print(f"alert_ids.json: {len(ids)} fixture da monitorare")

    print("\n[4] Invio Telegram...")
    send_telegram(qualified, total, run_date)

if __name__ == "__main__":
    main()

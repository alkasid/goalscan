"""
GOAL BOT — main.py
------------------
DEBUG VERSION: mostra league_ids trovati per ogni SKIP
"""

import json
import os
import requests
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

with open("config.json", encoding="utf-8-sig") as f:
    CFG = json.load(f)

API_KEY   = os.environ.get("API_FOOTBALL_KEY") or CFG.get("api_football_key", "")
THRESHOLD = int(CFG.get("goal_threshold", 14))
LAST_N    = int(CFG.get("last_matches_count", 5))
BASE_URL  = "https://v3.football.api-sports.io"
HEADERS   = {"x-apisports-key": API_KEY}

def api_get(endpoint, params=None):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}",
                         headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("response", [])
        print(f"    [HTTP {r.status_code}] {endpoint} {params}")
    except Exception as e:
        print(f"    [ERR] {endpoint}: {e}")
    return []

def get_leagues_and_fixtures():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data  = api_get("fixtures", {"date": today, "status": "NS"})
    league_seasons     = {}
    fixtures_by_league = defaultdict(list)
    for fix in data:
        lid    = fix.get("league", {}).get("id")
        season = fix.get("league", {}).get("season")
        if lid is None:
            continue
        if lid not in league_seasons:
            league_seasons[lid] = season
        fix["_season"] = season
        fixtures_by_league[lid].append(fix)
    total = sum(len(v) for v in fixtures_by_league.values())
    print(f"  -> {len(league_seasons)} leghe | {total} match totali")
    return league_seasons, fixtures_by_league

def _calc(collected, team_id):
    scored = conceded = 0
    for m in collected:
        goals   = m.get("goals", {})
        teams   = m.get("teams", {})
        is_home = teams.get("home", {}).get("id") == team_id
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        if is_home:
            scored += gh; conceded += ga
        else:
            scored += ga; conceded += gh
    total = scored + conceded
    return {"scored": scored, "conceded": conceded, "total": total,
            "qualifies": total >= THRESHOLD}

def get_last_n(team_id, league_id, season):
    data = api_get("fixtures", {
        "team":   team_id,
        "season": season,
        "status": "FT",
        "last":   50,
    })

    collected = []
    league_ids_found = set()

    for m in data:
        if m.get("fixture", {}).get("status", {}).get("short") != "FT":
            continue
        found_lid = m.get("league", {}).get("id")
        league_ids_found.add(found_lid)
        if found_lid != league_id:
            continue
        collected.append(m)
        if len(collected) == LAST_N:
            break

    if len(collected) < LAST_N:
        print(f"             [DEBUG] cercavo league_id={league_id} season={season} | trovati ids={sorted(str(x) for x in league_ids_found)} | gare_lega={len(collected)} | totale_risposta={len(data)}")
        return None

    return _calc(collected, team_id)

def badge_color(t):
    if t >= 20: return "#ff4757"
    if t >= 17: return "#ff8c00"
    return "#00e5a0"

def slot(ko):
    try:    return f"{int(ko.split(':')[0]):02d}:00"
    except: return "??:??"

def generate_html(matches, run_date, total_analyzed):
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
        ".ts{margin:18px 16px 0;}"
        ".th{display:flex;align-items:center;gap:12px;margin-bottom:10px;}"
        ".tl{font-size:1.15rem;font-weight:700;color:var(--accent);}"
        ".tc{font-size:.72rem;color:var(--muted);background:rgba(255,255,255,0.05);padding:2px 10px;border-radius:100px;}"
        ".th::after{content:'';flex:1;height:1px;background:var(--border);}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:8px;}"
        ".card{background:var(--card);border:1px solid var(--border);border-radius:12px;"
        "padding:11px 13px;transition:transform .15s,box-shadow .15s;}"
        ".card:hover{transform:translateY(-2px);box-shadow:0 6px 28px rgba(0,229,160,.12);}"
        ".ct{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px;}"
        ".league{font-size:.67rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:72%;}"
        ".ko{font-size:.72rem;color:var(--accent);font-weight:700;}"
        ".mu{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:6px;}"
        ".side{display:flex;flex-direction:column;gap:5px;}"
        ".side.r{align-items:flex-end;text-align:right;}"
        ".tn{font-size:.82rem;font-weight:700;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:118px;}"
        ".pills{display:flex;gap:3px;align-items:center;}"
        ".side.r .pills{justify-content:flex-end;}"
        ".pill{font-size:.7rem;font-weight:700;padding:2px 6px;border-radius:4px;}"
        ".pill.g{background:rgba(0,229,160,.15);color:var(--accent);}"
        ".pill.rc{background:rgba(255,71,87,.15);color:var(--red);}"
        ".pill.tot{color:#080d18;border-radius:6px;padding:2px 8px;font-size:.76rem;}"
        ".vs{font-size:1rem;color:var(--muted);font-weight:700;text-align:center;}"
        ".empty{text-align:center;padding:80px 20px;color:var(--muted);}"
        ".empty h3{font-size:1.2rem;color:var(--text);margin-bottom:6px;}"
    )
    legend = (
        '<div class="legend">'
        '<span class="leg-item"><span class="leg-dot" style="background:#00e5a0"></span>14–16 goal</span>'
        '<span class="leg-item"><span class="leg-dot" style="background:#ff8c00"></span>17–19 goal</span>'
        '<span class="leg-item"><span class="leg-dot" style="background:#ff4757"></span>≥20 goal</span>'
        '<span class="leg-item" style="margin-left:8px">+F=fatti &nbsp;|&nbsp; -S=subiti &nbsp;|&nbsp; TOT=somma 5 gare</span>'
        '</div>'
    )
    if not matches:
        body = (f'<div class="empty"><h3>Nessun match qualificato oggi</h3>'
                f'<p>Nessuna coppia soddisfa ≥{THRESHOLD} goal per entrambe<br>'
                f'nelle ultime {LAST_N} gare stessa lega.<br>'
                f'Match analizzati: <strong>{total_analyzed}</strong></p></div>')
    else:
        groups = defaultdict(list)
        for m in sorted(matches, key=lambda x: x["kickoff"]):
            groups[slot(m["kickoff"])].append(m)
        sections = []
        for ts in sorted(groups):
            cards = []
            for m in groups[ts]:
                hs = m["home_stats"]; as_ = m["away_stats"]
                cards.append(
                    f'<div class="card"><div class="ct">'
                    f'<span class="league">{m["league"]}</span>'
                    f'<span class="ko">{m["kickoff"]}</span></div>'
                    f'<div class="mu"><div class="side">'
                    f'<span class="tn">{m["home"]}</span>'
                    f'<div class="pills">'
                    f'<span class="pill g">+{hs["scored"]}</span>'
                    f'<span class="pill rc">-{hs["conceded"]}</span>'
                    f'<span class="pill tot" style="background:{badge_color(hs["total"])}">{hs["total"]}</span>'
                    f'</div></div><span class="vs">VS</span>'
                    f'<div class="side r"><span class="tn">{m["away"]}</span>'
                    f'<div class="pills">'
                    f'<span class="pill g">+{as_["scored"]}</span>'
                    f'<span class="pill rc">-{as_["conceded"]}</span>'
                    f'<span class="pill tot" style="background:{badge_color(as_["total"])}">{as_["total"]}</span>'
                    f'</div></div></div></div>'
                )
            sections.append(
                f'<div class="ts"><div class="th">'
                f'<span class="tl">⏱ {ts}</span>'
                f'<span class="tc">{len(groups[ts])} match</span>'
                f'</div><div class="grid">{"".join(cards)}</div></div>'
            )
        body = "\n".join(sections)

    return (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Goal Bot — {run_date}</title>'
        f'<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">'
        f'<style>{css}</style></head><body>'
        f'<header><div><div class="htitle">⚽ Goal Bot — {run_date}</div>'
        f'<div class="hsub">Entrambe ≥{THRESHOLD} goal (fatti+subiti) nelle ultime {LAST_N} gare stessa lega'
        f' — {total_analyzed} match analizzati</div></div>'
        f'<div class="hbadge">{len(matches)} ALERT</div></header>'
        f'<div class="cbar">'
        f'<span>Soglia: <strong>≥{THRESHOLD}</strong> per squadra</span>'
        f'<span>Ultime <strong>{LAST_N}</strong> gare stessa lega</span>'
        f'<span>Solo gare <strong>FT</strong></span>'
        f'</div>{legend}{body}</body></html>'
    )

def main():
    print("=" * 60)
    print(f"GOAL BOT  |  soglia ≥{THRESHOLD}  |  ultime {LAST_N} gare stessa lega")
    print("=" * 60)

    print("\n[1] Recupero leghe e match oggi...")
    league_seasons, fixtures_by_league = get_leagues_and_fixtures()

    all_fixtures = []
    for lid, fixes in fixtures_by_league.items():
        for f in fixes:
            f["_league_id"] = lid
        all_fixtures.extend(fixes)

    print(f"Totale match da analizzare: {len(all_fixtures)}")

    if not all_fixtures:
        print("Nessun match trovato.")
        run_date = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        out = Path("docs/index.html")
        out.parent.mkdir(exist_ok=True)
        out.write_text(generate_html([], run_date, 0), encoding="utf-8")
        return

    print("\n[2] Analisi storico squadre...\n")
    qualified = []

    for i, fix in enumerate(all_fixtures, 1):
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

        try:
            ko = datetime.fromtimestamp(
                fixture.get("timestamp", 0), tz=timezone.utc
            ).strftime("%H:%M")
        except Exception:
            ko = "--:--"

        print(f"[{i:>4}/{len(all_fixtures)}] {home_name} vs {away_name} ({league_name} {season})")

        hs  = get_last_n(home_id, league_id, season)
        as_ = get_last_n(away_id, league_id, season)

        if hs is None or as_ is None:
            missing = []
            if hs  is None: missing.append(home_name)
            if as_ is None: missing.append(away_name)
            print(f"           SKIP: <{LAST_N} gare FT per {', '.join(missing)}")
            continue

        print(f"           HOME {home_name}: +{hs['scored']} -{hs['conceded']} = {hs['total']}")
        print(f"           AWAY {away_name}: +{as_['scored']} -{as_['conceded']} = {as_['total']}")

        if hs["qualifies"] and as_["qualifies"]:
            print("           ✅ ALERT")
            qualified.append({"home": home_name, "away": away_name,
                               "home_stats": hs, "away_stats": as_,
                               "league": league_name, "kickoff": ko})
        else:
            reasons = []
            if not hs["qualifies"]: reasons.append(f"{home_name}:{hs['total']}<{THRESHOLD}")
            if not as_["qualifies"]: reasons.append(f"{away_name}:{as_['total']}<{THRESHOLD}")
            print(f"           ✗  {' | '.join(reasons)}")

    print(f"\n{'='*60}")
    print(f"ALERT: {len(qualified)} / {len(all_fixtures)}")
    print(f"{'='*60}")
    for m in qualified:
        print(f"  {m['kickoff']}  {m['home']} ({m['home_stats']['total']}) vs "
              f"{m['away']} ({m['away_stats']['total']})  [{m['league']}]")

    run_date = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    out = Path("docs/index.html")
    out.parent.mkdir(exist_ok=True)
    out.write_text(generate_html(qualified, run_date, len(all_fixtures)), encoding="utf-8")
    print(f"\nReport salvato: {out}")

if __name__ == "__main__":
    main()

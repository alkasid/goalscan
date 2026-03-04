"""
TEST DIAGNOSTICO — API-Football v3
Verifica esatta di come si comportano:
  A) fixtures?team=X&league=Y&season=Z&last=5            (senza status)
  B) fixtures?team=X&league=Y&season=Z&status=FT&last=5  (con status FT)
  C) fixtures?team=X&league=Y&season=Z&status=FT&last=10 (FT + margine)

Squadra test: Inter (team=505), Serie A (league=135), stagione 2024
"""

import json, os, sys, requests
from datetime import datetime, timezone

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or input("Inserisci API key: ")).strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

TEAM   = 505   # Inter
LEAGUE = 135   # Serie A
SEASON = 2024

def call(params, label):
    r = requests.get(f"{BASE}/fixtures", headers=HDR, params=params, timeout=15)
    data = r.json()
    results = data.get("response", [])
    remaining = r.headers.get("x-ratelimit-requests-remaining", "?")

    print(f"\n{'='*60}")
    print(f"TEST {label}")
    print(f"Params: {params}")
    print(f"Calls rimanenti dopo questa: {remaining}")
    print(f"Totale risultati ricevuti: {len(results)}")
    print()

    for i, fix in enumerate(results):
        fxt    = fix.get("fixture", {})
        status = fxt.get("status", {}).get("short", "?")
        ts     = fxt.get("timestamp", 0)
        date   = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "?"
        home   = fix.get("teams", {}).get("home", {}).get("name", "?")
        away   = fix.get("teams", {}).get("away", {}).get("name", "?")
        gh     = fix.get("goals", {}).get("home")
        ga     = fix.get("goals", {}).get("away")
        score  = f"{gh}-{ga}" if gh is not None else "N/A"

        flag = "✅" if status == "FT" else f"⚠️ {status}"
        print(f"  [{i+1}] {date}  {home} {score} {away}  status={flag}")

    ft_count = sum(1 for f in results if f.get("fixture",{}).get("status",{}).get("short") == "FT")
    print(f"\n  → FT effettivi: {ft_count}/{len(results)}")

    # Calcola goal come farebbe il bot
    goals_scored = goals_conceded = 0
    ft_valid = [f for f in results if f.get("fixture",{}).get("status",{}).get("short") == "FT"]
    for m in ft_valid[:5]:
        teams  = m.get("teams", {})
        goals  = m.get("goals", {})
        is_home = teams.get("home", {}).get("id") == TEAM
        gh = int(goals.get("home") or 0)
        ga = int(goals.get("away") or 0)
        if is_home:
            goals_scored   += gh; goals_conceded += ga
        else:
            goals_scored   += ga; goals_conceded += gh

    total = goals_scored + goals_conceded
    print(f"  → Goal fatti: {goals_scored}  |  Subiti: {goals_conceded}  |  TOT: {total}")
    return results

# ── RUN 3 TEST ───────────────────────────────────────────────────────────────
print("\n🔍 AVVIO TEST DIAGNOSTICO API-Football v3")
print(f"   Squadra: Inter (ID={TEAM}) | League: Serie A ({LEAGUE}) | Season: {SEASON}")

# A — solo last=5 senza status
call({"team": TEAM, "league": LEAGUE, "season": SEASON, "last": 5}, "A — last=5 (no status)")

# B — last=5 + status=FT
call({"team": TEAM, "league": LEAGUE, "season": SEASON, "status": "FT", "last": 5}, "B — last=5 + status=FT")

# C — last=10 + status=FT (strategia sicura)
call({"team": TEAM, "league": LEAGUE, "season": SEASON, "status": "FT", "last": 10}, "C — last=10 + status=FT")

print("\n\n✅ TEST COMPLETATO — 3 chiamate usate")
print("   Confronta i risultati: se A e B danno gli stessi FT → last filtra già per FT")
print("   Se A contiene status != FT → serve obbligatoriamente B o C")

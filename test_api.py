import os, requests, json
from datetime import datetime, timezone

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

def api_get(endpoint, params):
    r = requests.get(f"{BASE}/{endpoint}", headers=HDR, params=params, timeout=15)
    data = r.json()
    if data.get("errors"):
        print(f"ERRORE: {data['errors']}")
        return []
    return data.get("response", [])

# 1. Recupera partite FT di oggi
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print(f"Recupero partite FT del {today}...")
fixtures = api_get("fixtures", {"date": today, "status": "FT"})
print(f"Partite FT trovate: {len(fixtures)}")

# 2. Filtra solo quelle negli alert (fixture IDs dal file alert_ids.json se disponibile)
# Altrimenti prende tutte le FT
results = []

for fix in fixtures:
    fid      = fix["fixture"]["id"]
    home     = fix["teams"]["home"]["name"]
    away     = fix["teams"]["away"]["name"]
    league   = fix["league"]["name"]
    hg       = fix["goals"]["home"] or 0
    ag       = fix["goals"]["away"] or 0
    
    # Recupera eventi per trovare primo goal
    events = api_get("fixtures/events", {"fixture": fid, "type": "Goal"})
    
    first_goal_min = None
    first_goal_team = None
    for ev in events:
        if ev.get("type") == "Goal" and ev.get("detail") != "Missed Penalty":
            first_goal_min = ev["time"]["elapsed"]
            first_goal_team = ev["team"]["name"]
            break
    
    results.append({
        "fixture": f"{home} vs {away}",
        "league": league,
        "score": f"{hg}-{ag}",
        "primo_goal_min": first_goal_min,
        "primo_goal_squadra": first_goal_team,
    })

# Ordina per minuto primo goal
results.sort(key=lambda x: x["primo_goal_min"] or 999)

print(f"\n{'='*65}")
print(f"{'PARTITA':<35} {'SCORE':<7} {'1°GOAL MIN':<12} {'SQUADRA'}")
print(f"{'='*65}")
for r in results:
    min_str = str(r["primo_goal_min"]) + "'" if r["primo_goal_min"] else "nessun goal"
    squadra = r["primo_goal_squadra"] or "-"
    print(f"{r['fixture'][:34]:<35} {r['score']:<7} {min_str:<12} {squadra[:20]}")

print(f"\nChiamate API usate: {1 + len(fixtures)} (1 per FT list + 1 per ogni partita)")

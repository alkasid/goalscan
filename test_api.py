import os, requests

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

THRESHOLD = 12
LAST_N = 5

# 1. Trova fixture Toluca vs Juarez oggi o nei prossimi giorni
print("=== CERCA FIXTURE TOLUCA vs JUAREZ ===")
from datetime import datetime, timezone, timedelta
for i in range(4):
    date = (datetime.now(timezone.utc) + timedelta(days=i-1)).strftime("%Y-%m-%d")
    fixes = api_get("fixtures", {"date": date, "league": 262, "season": 2025})
    for f in fixes:
        h = f["teams"]["home"]["name"]
        a = f["teams"]["away"]["name"]
        st = f["fixture"]["status"]["short"]
        fid = f["fixture"]["id"]
        print(f"  {date} {h} vs {a} | {st} | id={fid}")

# 2. Cerca team IDs cercando per nome
print("\n=== TEAM IDs Liga MX ===")
for name in ["Toluca", "Juarez"]:
    res = api_get("teams", {"search": name})
    for t in res[:3]:
        print(f"  {name}: id={t['team']['id']} nome={t['team']['name']} country={t['team']['country']}")

# 3. Ora prendi i primi risultati e controlla ultime 5 gare
print("\n=== ULTIME GARE FT PER SQUADRA (Liga MX season=2025) ===")
for name, tid in [("Toluca", 2283), ("Juarez", 2293)]:
    games = api_get("fixtures", {"team": tid, "league": 262, "season": 2025, "last": 10})
    ft = [g for g in games if g["fixture"]["status"]["short"] == "FT"]
    scored = conceded = 0
    for m in ft[:LAST_N]:
        is_home = m["teams"]["home"]["id"] == tid
        gh = int(m["goals"]["home"] or 0)
        ga = int(m["goals"]["away"] or 0)
        scored   += gh if is_home else ga
        conceded += ga if is_home else gh
    total = scored + conceded
    print(f"\n  {name} (id={tid}): {len(ft)} gare FT | +{scored} -{conceded} = TOT {total} | qualifica: {total >= THRESHOLD}")
    for g in ft[:LAST_N]:
        st = g["fixture"]["status"]["short"]
        print(f"    {g['fixture']['date'][:10]} {g['teams']['home']['name']} {g['goals']['home']}-{g['goals']['away']} {g['teams']['away']['name']} | {st}")

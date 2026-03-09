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

# Cerca Toluca e Juarez nella Liga MX
print("=== CERCA FIXTURE TOLUCA-JUAREZ ===")
fixtures = api_get("fixtures", {"team": 2287, "last": 10})  # Toluca
for f in fixtures:
    home = f["teams"]["home"]["name"]
    away = f["teams"]["away"]["name"]
    status = f["fixture"]["status"]["short"]
    league = f["league"]["name"]
    season = f["league"]["season"]
    date = f["fixture"]["date"][:10]
    print(f"  {date} {home} vs {away} | {status} | {league} | season={season}")

print("\n=== CERCA TEAM ID TOLUCA + JUAREZ ===")
for name, search in [("Toluca","Toluca"), ("Juarez","Juarez")]:
    teams = api_get("teams", {"search": search, "league": 262, "season": 2025})
    for t in teams[:3]:
        print(f"  {name}: id={t['team']['id']} nome={t['team']['name']}")

print("\n=== ULTIME 5 FT TOLUCA LIGA MX ===")
# Prima troviamo il team id giusto
teams = api_get("teams", {"search": "Toluca", "league": 262, "season": 2025})
if teams:
    tid = teams[0]["team"]["id"]
    print(f"  Toluca id={tid}")
    games = api_get("fixtures", {"team": tid, "league": 262, "season": 2025, "last": 10})
    ft = [g for g in games if g["fixture"]["status"]["short"] == "FT"]
    print(f"  Gare FT trovate: {len(ft)}")
    for g in ft:
        print(f"    {g['fixture']['date'][:10]} {g['teams']['home']['name']} {g['goals']['home']}-{g['goals']['away']} {g['teams']['away']['name']} | status={g['fixture']['status']['short']}")

print("\n=== ULTIME 5 FT JUAREZ LIGA MX ===")
teams2 = api_get("teams", {"search": "Juarez", "league": 262, "season": 2025})
if teams2:
    tid2 = teams2[0]["team"]["id"]
    print(f"  Juarez id={tid2}")
    games2 = api_get("fixtures", {"team": tid2, "league": 262, "season": 2025, "last": 10})
    ft2 = [g for g in games2 if g["fixture"]["status"]["short"] == "FT"]
    print(f"  Gare FT trovate: {len(ft2)}")
    for g in ft2:
        print(f"    {g['fixture']['date'][:10]} {g['teams']['home']['name']} {g['goals']['home']}-{g['goals']['away']} {g['teams']['away']['name']} | status={g['fixture']['status']['short']}")

print("\nChiamate API usate: ~6")

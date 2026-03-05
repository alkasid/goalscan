import os, requests

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

for team_id, name in [(2287, "Club America"), (2298, "FC Juarez")]:
    # Senza filtro lega — ultime 10 partite qualsiasi
    r = requests.get(f"{BASE}/fixtures", headers=HDR,
                     params={"team": team_id, "season": 2025, "last": 10}, timeout=15)
    data = r.json().get("response", [])
    ft = [m for m in data if m["fixture"]["status"]["short"] == "FT"]
    print(f"\n{name} — FT trovati: {len(ft)}")
    for m in ft:
        print(f"  {m['fixture']['date'][:10]}  {m['teams']['home']['name']} {m['goals']['home']}-{m['goals']['away']} {m['teams']['away']['name']}  league_id={m['league']['id']} ({m['league']['name']})")

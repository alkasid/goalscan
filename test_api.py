import os, requests

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

# Club America = 1 | Juarez = 2294 | Liga MX = 262 | season 2025
for team_id, name in [(1, "Club America"), (2294, "Juarez")]:
    r = requests.get(f"{BASE}/fixtures", headers=HDR,
                     params={"team": team_id, "league": 262, "season": 2025, "last": 5}, timeout=15)
    data = r.json().get("response", [])
    ft = [m for m in data if m.get("fixture",{}).get("status",{}).get("short") == "FT"]
    print(f"\n{name} — ultimi 5 richiesti, FT trovati: {len(ft)}")
    scored = conceded = 0
    for m in ft:
        gh = int(m["goals"]["home"] or 0)
        ga = int(m["goals"]["away"] or 0)
        is_home = m["teams"]["home"]["id"] == team_id
        if is_home: scored += gh; conceded += ga
        else:       scored += ga; conceded += gh
        print(f"  {m['fixture']['date'][:10]} {m['teams']['home']['name']} {gh}-{ga} {m['teams']['away']['name']} — status: {m['fixture']['status']['short']}")
    print(f"  TOTALE: fatti={scored} subiti={conceded} ratio={scored+conceded}")

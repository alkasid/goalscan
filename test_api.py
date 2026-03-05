import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

# Ieri
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

r = requests.get(f"{BASE}/fixtures", headers=HDR,
                 params={"date": yesterday}, timeout=15)
data = r.json().get("response", [])

print(f"Ieri {yesterday}: {len(data)} match totali\n")

# Calcola ratio per ogni match con risultato FT
results = []
for fix in data:
    if fix["fixture"]["status"]["short"] != "FT":
        continue
    gh = int(fix["goals"]["home"] or 0)
    ga = int(fix["goals"]["away"] or 0)
    results.append({
        "league": fix["league"]["name"],
        "country": fix["league"]["country"],
        "home": fix["teams"]["home"]["name"],
        "away": fix["teams"]["away"]["name"],
        "goals": gh + ga
    })

# Ordina per goal totali nel match
results.sort(key=lambda x: x["goals"], reverse=True)
print(f"Top 20 match per goal segnati ieri:")
for r in results[:20]:
    print(f"  {r['goals']} goal — {r['home']} vs {r['away']} [{r['league']} / {r['country']}]")

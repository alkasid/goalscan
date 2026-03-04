import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

r = requests.get(f"{BASE}/odds", headers=HDR,
                 params={"date": tomorrow, "bookmaker": 8}, timeout=15)
data = r.json()
results = data.get("response", [])
total   = data.get("results", 0)

print(f"Odds Bet365 domani: {total} fixture totali API")
print(f"Ricevuti: {len(results)}")
if results:
    print(f"Esempio fixture IDs con quote: {[r['fixture']['id'] for r in results[:5]]}")

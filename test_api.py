import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

# Prendi i primi 10 match di domani
r = requests.get(f"{BASE}/fixtures", headers=HDR,
                 params={"date": tomorrow, "status": "NS"}, timeout=15)
fixtures = r.json().get("response", [])[:10]
fixture_ids = [str(f["fixture"]["id"]) for f in fixtures]

print(f"Test odds su {len(fixture_ids)} fixture di domani")
print(f"IDs: {fixture_ids}\n")

# Controlla odds per ogni fixture
for fid in fixture_ids:
    r2 = requests.get(f"{BASE}/odds", headers=HDR,
                      params={"fixture": fid, "bookmaker": 8}, timeout=15)  # 8 = Bet365
    data = r2.json().get("response", [])
    name = next((f["teams"]["home"]["name"] + " vs " + f["teams"]["away"]["name"]
                 for f in fixtures if str(f["fixture"]["id"]) == fid), fid)
    has_odds = len(data) > 0
    print(f"  {'✅' if has_odds else '❌'} {name} — bet365 odds: {'SI' if has_odds else 'NO'}")

# Verifica anche bookmaker IDs disponibili per un fixture
print(f"\n--- Bookmaker disponibili per fixture {fixture_ids[0]} ---")
r3 = requests.get(f"{BASE}/odds", headers=HDR,
                  params={"fixture": fixture_ids[0]}, timeout=15)
bk_data = r3.json().get("response", [])
if bk_data:
    for bk in bk_data[0].get("bookmakers", []):
        print(f"  ID={bk['id']} — {bk['name']}")
else:
    print("  Nessun bookmaker trovato")

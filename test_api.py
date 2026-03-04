import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
day3 = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

for date in [today, tomorrow, day3]:
    r = requests.get(f"{BASE}/fixtures", headers=HDR,
                     params={"date": date, "status": "NS"}, timeout=15)
    data = r.json()
    results = data.get("response", [])
    total_api = data.get("results", 0)
    print(f"\n=== {date} ===")
    print(f"  results campo API: {total_api}")
    print(f"  ricevuti: {len(results)}")
    print(f"  paginazione necessaria: {'SI' if total_api > len(results) else 'NO'}")

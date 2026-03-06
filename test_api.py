import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

# Status key
r = requests.get(f"{BASE}/status", headers=HDR, timeout=15)
print("Status key:", r.json().get("response", {}).get("requests", "?"))

# Fixtures domani senza filtro status
tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
r = requests.get(f"{BASE}/fixtures", headers=HDR,
                 params={"date": tomorrow}, timeout=15)
data = r.json()
print(f"HTTP: {r.status_code}")
print(f"Results: {data.get('results', 0)}")
print(f"Errors: {data.get('errors')}")
if data.get('results', 0) > 0:
    fix = data['response'][0]
    print(f"Esempio: {fix['teams']['home']['name']} vs {fix['teams']['away']['name']}")
    print(f"Status: {fix['fixture']['status']['short']}")

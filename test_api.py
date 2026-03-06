import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

print(f"API_KEY presente: {'SI' if API_KEY else 'NO'}")
print(f"API_KEY lunghezza: {len(API_KEY)}")

tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
r = requests.get(f"{BASE}/fixtures", headers=HDR,
                 params={"date": tomorrow, "status": "NS"}, timeout=15)

print(f"HTTP: {r.status_code}")
print(f"Errors: {r.json().get('errors')}")
print(f"Results: {r.json().get('results')}")

# Conta chiamate rimanenti
print(f"Requests remaining: {r.headers.get('x-ratelimit-requests-remaining', 'N/A')}")
print(f"Requests limit: {r.headers.get('x-ratelimit-requests-limit', 'N/A')}")

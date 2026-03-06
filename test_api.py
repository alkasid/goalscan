import os, requests

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

r = requests.get(f"{BASE}/status", headers=HDR, timeout=15)
print(r.json())

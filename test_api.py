import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

for date in [today, tomorrow]:
    r = requests.get(f"{BASE}/fixtures", headers=HDR, params={"date": date, "status": "NS"}, timeout=15)
    data = r.json().get("response", [])
    types = {}
    for fix in data:
        t = fix.get("league", {}).get("type", "?")
        n = fix.get("league", {}).get("name", "?")
        types.setdefault(t, set()).add(n)
    print(f"\n=== {date} — {len(data)} match totali ===")
    for t, names in sorted(types.items()):
        print(f"  [{t}] {len(names)} leghe — es: {list(names)[:3]}")

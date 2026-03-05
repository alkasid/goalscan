import os, requests
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

# Trova il fixture Club America vs Juarez — cerca nei prossimi giorni
for days in range(5):
    date = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    r = requests.get(f"{BASE}/fixtures", headers=HDR,
                     params={"date": date, "status": "NS"}, timeout=15)
    for fix in r.json().get("response", []):
        home = fix["teams"]["home"]["name"]
        away = fix["teams"]["away"]["name"]
        if "america" in home.lower() or "america" in away.lower() or \
           "juarez" in home.lower() or "juarez" in away.lower():
            print(f"TROVATO: {home} vs {away}")
            print(f"  league_id={fix['league']['id']} league_name={fix['league']['name']}")
            print(f"  season={fix['league']['season']}")
            print(f"  home_id={fix['teams']['home']['id']} away_id={fix['teams']['away']['id']}")
            print(f"  date={date}")

import os, requests
from datetime import datetime, timezone
from collections import Counter

API_KEY = (os.environ.get("API_FOOTBALL_KEY") or "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

def api_get(endpoint, params):
    r = requests.get(f"{BASE}/{endpoint}", headers=HDR, params=params, timeout=15)
    data = r.json()
    if data.get("errors"):
        print(f"ERRORE: {data['errors']}")
        return []
    return data.get("response", [])

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print(f"Recupero partite FT del {today}...")
fixtures = api_get("fixtures", {"date": today, "status": "FT"})
print(f"Partite FT trovate: {len(fixtures)}\n")

first_goals = []
no_goal = 0

for fix in fixtures:
    fid  = fix["fixture"]["id"]
    hg   = fix["goals"]["home"] or 0
    ag   = fix["goals"]["away"] or 0
    if hg == 0 and ag == 0:
        no_goal += 1
        continue

    # Recupera tutti gli eventi della partita
    events = api_get("fixtures/events", {"fixture": fid})
    
    # Trova primo goal (escludi autogol e rigori falliti)
    for ev in events:
        t = ev.get("type","")
        detail = ev.get("detail","")
        if t == "Goal" and detail not in ("Missed Penalty", "Own Goal"):
            minute = ev["time"]["elapsed"]
            extra  = ev["time"].get("extra") or 0
            total_min = minute + extra
            first_goals.append(total_min)
            break

print(f"Partite con almeno 1 goal: {len(first_goals)}")
print(f"Partite 0-0: {no_goal}")
print(f"Partite senza eventi goal trovati: {len(fixtures) - no_goal - len(first_goals)}\n")

if not first_goals:
    print("Nessun dato disponibile")
    exit()

# Fasce minuti
fasce = [
    ("1-15",   1,  15),
    ("16-30",  16, 30),
    ("31-45",  31, 45),
    ("46-60",  46, 60),
    ("61-75",  61, 75),
    ("76-90+", 76, 200),
]

total = len(first_goals)
print("=" * 50)
print(f"{'FASCIA':<10} {'N':<6} {'%':<8} BARRA")
print("=" * 50)
for label, lo, hi in fasce:
    count = sum(1 for m in first_goals if lo <= m <= hi)
    pct   = count / total * 100
    bar   = "█" * int(pct / 3)
    print(f"{label:<10} {count:<6} {pct:>5.1f}%   {bar}")

print("=" * 50)
avg = sum(first_goals) / total
print(f"Media minuto 1° goal: {avg:.1f}'")
print(f"Min: {min(first_goals)}'  |  Max: {max(first_goals)}'")

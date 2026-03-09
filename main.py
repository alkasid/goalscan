import os, requests

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

THRESHOLD = 12
LAST_N = 5

print("=== ULTIME GARE FT PER SQUADRA (Liga MX league=262 season=2025) ===")
for name, tid in [("Toluca", 2281), ("FC Juarez", 2298)]:
    games = api_get("fixtures", {"team": tid, "league": 262, "season": 2025, "last": 15})
    all_st = set(g["fixture"]["status"]["short"] for g in games)
    ft = [g for g in games if g["fixture"]["status"]["short"] == "FT"]
    scored = conceded = 0
    for m in ft[:LAST_N]:
        is_home = m["teams"]["home"]["id"] == tid
        gh = int(m["goals"]["home"] or 0)
        ga = int(m["goals"]["away"] or 0)
        scored   += gh if is_home else ga
        conceded += ga if is_home else gh
    total = scored + conceded
    print(f"\n  {name} (id={tid})")
    print(f"  Tutti gli status trovati: {all_st}")
    print(f"  Gare FT: {len(ft)} | +{scored} -{conceded} = TOT {total} | qualifica: {total >= THRESHOLD}")
    for g in games[:8]:
        st = g["fixture"]["status"]["short"]
        date = g["fixture"]["date"][:10]
        h = g["teams"]["home"]["name"]
        a = g["teams"]["away"]["name"]
        gh = g["goals"]["home"]
        ga = g["goals"]["away"]
        print(f"    {date} {h} {gh}-{ga} {a} | {st}")

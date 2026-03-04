"""
TEST API — esegui su GitHub Actions come run manuale
Verifica che gli endpoint Sofascore restituiscano i dati corretti
prima di mettere in produzione il bot definitivo.
"""

import requests
import json
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

BASE = "https://api.sofascore.com/api/v1"

def get(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    print(f"  HTTP {r.status_code} — {url}")
    return r.json() if r.status_code == 200 else None

# ── TEST 1: scheduled-events ─────────────────────────────────────────────────
print("\n=== TEST 1: scheduled-events ===")
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
data = get(f"{BASE}/sport/football/scheduled-events/{today}")
events = data.get("events", []) if data else []
print(f"  -> {len(events)} match trovati oggi")

# Trova un match con tutti i campi necessari (Serie A o Premier League)
sample = None
for e in events:
    league_name = e.get("tournament", {}).get("uniqueTournament", {}).get("name", "")
    ut_id    = e.get("tournament", {}).get("uniqueTournament", {}).get("id")
    season_id = e.get("season", {}).get("id")
    home_id  = e.get("homeTeam", {}).get("id")
    away_id  = e.get("awayTeam", {}).get("id")
    if ut_id and season_id and home_id and away_id:
        sample = e
        if "Premier" in league_name or "Serie A" in league_name or "Serie B" in league_name:
            break

if not sample:
    print("  ❌ Nessun match valido trovato")
    exit(1)

home_name  = sample["homeTeam"]["name"]
away_name  = sample["awayTeam"]["name"]
league_name = sample["tournament"]["uniqueTournament"]["name"]
ut_id      = sample["tournament"]["uniqueTournament"]["id"]
season_id  = sample["season"]["id"]
home_id    = sample["homeTeam"]["id"]
away_id    = sample["awayTeam"]["id"]

print(f"\n  Match selezionato: {home_name} vs {away_name}")
print(f"  Lega: {league_name} | ut_id={ut_id} | season_id={season_id}")
print(f"  home_id={home_id} | away_id={away_id}")

# ── TEST 2: statistics/overall ───────────────────────────────────────────────
print("\n=== TEST 2: statistics/overall (HOME) ===")
url = f"{BASE}/team/{home_id}/unique-tournament/{ut_id}/season/{season_id}/statistics/overall"
stats_data = get(url)
if stats_data and "statistics" in stats_data:
    s = stats_data["statistics"]
    gs = s.get("goalsScored", "N/A")
    gc = s.get("goalsConceded", "N/A")
    mp = s.get("matchesPlayed", "N/A")
    print(f"  goalsScored={gs} goalsConceded={gc} matchesPlayed={mp}")
    if gs != "N/A" and gc != "N/A" and mp != "N/A" and int(mp) >= 5:
        ratio = gs + gc
        print(f"  ✅ Ratio (fatti+subiti) = {ratio} su {mp} partite")
        print(f"  Media per partita = {ratio/int(mp):.2f}")
    else:
        print(f"  ⚠️ Dati insufficienti o campi mancanti")
        print(f"  Chiavi disponibili: {list(s.keys())[:15]}")
else:
    print("  ❌ statistics/overall non ha restituito dati")
    print(f"  Risposta: {json.dumps(stats_data, indent=2)[:500] if stats_data else 'None'}")

# ── TEST 3: statistics/overall (AWAY) ────────────────────────────────────────
print("\n=== TEST 3: statistics/overall (AWAY) ===")
url2 = f"{BASE}/team/{away_id}/unique-tournament/{ut_id}/season/{season_id}/statistics/overall"
stats_data2 = get(url2)
if stats_data2 and "statistics" in stats_data2:
    s2 = stats_data2["statistics"]
    gs2 = s2.get("goalsScored", "N/A")
    gc2 = s2.get("goalsConceded", "N/A")
    mp2 = s2.get("matchesPlayed", "N/A")
    print(f"  goalsScored={gs2} goalsConceded={gc2} matchesPlayed={mp2}")
    if gs2 != "N/A" and gc2 != "N/A" and mp2 != "N/A" and int(mp2) >= 5:
        ratio2 = gs2 + gc2
        print(f"  ✅ Ratio (fatti+subiti) = {ratio2} su {mp2} partite")
        print(f"  Media per partita = {ratio2/int(mp2):.2f}")
else:
    print("  ❌ statistics/overall non ha restituito dati")
    print(f"  Risposta: {json.dumps(stats_data2, indent=2)[:500] if stats_data2 else 'None'}")

# ── VERDETTO ──────────────────────────────────────────────────────────────────
print("\n=== VERDETTO ===")
print("Se tutti e 3 i test mostrano ✅ l'architettura è confermata definitivamente.")
print("Se statistics/overall restituisce matchesPlayed totali (non ultime 5),")
print("bisogna usare team/{id}/events/last/{page} con filtro manuale.")

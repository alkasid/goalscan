"""
updater.py — gira ogni 5 min.
Legge alert_ids.json, aggiorna ft_history.json con partite FT.
Salva anche first_min_cached per evitare chiamate API ripetute nelle stats.
Chiamate API: ceil(n_ids/20) + 1 per ogni nuova FT con goal = ~10-25/run.
"""
import json, os, requests
from pathlib import Path
from datetime import datetime, timezone

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}
FT_ST   = {"FT", "AET", "PEN"}

DOCS         = Path("docs")
IDS_FILE     = DOCS / "alert_ids.json"
HISTORY_FILE = DOCS / "ft_history.json"
GLOBAL_HISTORY_FILE = DOCS / "global_history.json"

# Quante entry di global_history.json riempire per run (lazy backfill 1° goal).
# A 5 min/run con 30 fixture/run = 360 fixture/h → 5655 mancanti coperti in ~16h.
GLOBAL_BACKFILL_PER_RUN = int(os.environ.get("GLOBAL_BACKFILL_PER_RUN", "30"))

def api_get(endpoint, params):
    try:
        r = requests.get(f"{BASE}/{endpoint}", headers=HDR, params=params, timeout=15)
        return r.json().get("response", [])
    except Exception as e:
        print(f"  ERR {endpoint}: {e}")
        return []

def fetch_fixtures(ids):
    results = []
    for i in range(0, len(ids), 20):
        chunk = "-".join(str(x) for x in ids[i:i+20])
        results.extend(api_get("fixtures", {"ids": chunk}))
    return results

def get_first_goal_min(fixture_id):
    """1 chiamata API — restituisce minuto 1° goal o None."""
    evs = api_get("fixtures/events", {"fixture": fixture_id, "type": "Goal"})
    mins = []
    for e in evs:
        if e.get("type") == "Goal" and e.get("detail") != "Missed Penalty":
            raw   = e.get("time", {}).get("elapsed")
            extra = e.get("time", {}).get("extra") or 0
            if raw is not None:
                mins.append(int(raw) + int(extra))
    return min(mins) if mins else None

def main():
    if not IDS_FILE.exists():
        print("alert_ids.json non trovato.")
        return

    ids = json.loads(IDS_FILE.read_text())
    if not ids:
        print("Nessun alert ID.")
        return

    history = {}
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = {}

    print(f"[updater] {len(ids)} IDs · {len(history)} in history")

    fixtures = fetch_fixtures(ids)
    updated  = 0

    for f in fixtures:
        fid    = str(f["fixture"]["id"])
        status = f["fixture"]["status"]["short"]
        hg     = f["goals"]["home"]
        ag     = f["goals"]["away"]

        if status not in FT_ST or hg is None:
            continue

        tot = hg + ag

        if fid not in history:
            # Nuova FT — recupera minuto 1° goal se ci sono goal
            first_min = get_first_goal_min(int(fid)) if tot > 0 else None
            history[fid] = {
                "fixture_id":      int(fid),
                "home":            f["teams"]["home"]["name"],
                "away":            f["teams"]["away"]["name"],
                "league":          f["league"]["name"],
                "country":         f["league"]["country"],
                "date":            f["fixture"]["date"][:10],
                "kickoff":         f["fixture"]["date"][11:16],
                "status":          status,
                "goals_home":      hg,
                "goals_away":      ag,
                "score":           f"{hg}-{ag}",
                "first_min_cached": first_min,
                "saved_at":        datetime.now(timezone.utc).isoformat(),
            }
            updated += 1
            fm = f" · 1°goal {first_min}'" if first_min else ""
            print(f"  ✅ {f['teams']['home']['name']} {hg}-{ag} {f['teams']['away']['name']}{fm}")
        else:
            # Già salvata — aggiorna solo se punteggio cambia
            if history[fid]["score"] != f"{hg}-{ag}":
                history[fid].update({"goals_home": hg, "goals_away": ag,
                                     "score": f"{hg}-{ag}", "status": status})
                # Ricalcola first_min se era None e ora ci sono goal
                if history[fid].get("first_min_cached") is None and tot > 0:
                    history[fid]["first_min_cached"] = get_first_goal_min(int(fid))
                updated += 1

    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    print(f"[updater] +{updated} aggiornamenti · totale: {len(history)} FT")

    backfill_global_first_min()

def backfill_global_first_min():
    """Lazy backfill di first_min_cached su global_history.json.
    Ogni run prende fino a GLOBAL_BACKFILL_PER_RUN fixture senza minuto cachato
    (priorità: partite più recenti) e chiama fixtures/events per estrarre il
    1° goal. Le partite 0-0 non vengono toccate (first_min_cached resta None
    per design)."""
    if not GLOBAL_HISTORY_FILE.exists():
        return
    try:
        gl = json.loads(GLOBAL_HISTORY_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"[backfill] global_history non leggibile: {e}")
        return

    candidates = [
        (fid, v) for fid, v in gl.items()
        if v.get("first_min_cached") is None
        and ((v.get("goals_home") or 0) + (v.get("goals_away") or 0)) > 0
    ]
    candidates.sort(key=lambda kv: kv[1].get("date", ""), reverse=True)
    todo = candidates[:GLOBAL_BACKFILL_PER_RUN]
    if not todo:
        cached = sum(1 for v in gl.values() if v.get("first_min_cached") is not None)
        print(f"[backfill] global_history complete · {cached}/{len(gl)} cachate")
        return

    print(f"[backfill] {len(todo)} fixture da riempire (su {len(candidates)} totali)")
    filled = 0
    for fid, _ in todo:
        try:
            fm = get_first_goal_min(int(fid))
        except Exception as e:
            print(f"  err {fid}: {e}")
            continue
        if fm is not None:
            gl[fid]["first_min_cached"] = fm
            filled += 1

    GLOBAL_HISTORY_FILE.write_text(
        json.dumps(gl, ensure_ascii=False).encode("utf-8", errors="replace").decode("utf-8"),
        encoding="utf-8"
    )
    cached = sum(1 for v in gl.values() if v.get("first_min_cached") is not None)
    print(f"[backfill] +{filled}/{len(todo)} riempite · totale {cached}/{len(gl)} cachate")

if __name__ == "__main__":
    main()

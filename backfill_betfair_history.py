"""
backfill_betfair_history.py — popolamento retrospettivo di betfair_history.json.

Estrae da git history TUTTI i docs/betfair_markets.json mai pushati nelle
ultime N giornate (sample S commits/giorno per limitare costo CPU), unisce
gli event_name in un index, fa match con docs/global_history.json e popola
docs/betfair_history.json con le fixture FT che hanno mai avuto mercato
BF Exchange.

Idempotente: non rimuove entries esistenti, aggiunge solo i nuovi match.

Env vars:
  BF_BACKFILL_DAYS            giorni nel git log (default 30)
  BF_BACKFILL_SAMPLE_PER_DAY  commit/giorno (default 4 = uno ogni 6h)
"""
import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Riusa _normalize_team da main.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from main import _normalize_team

DOCS                 = Path("docs")
GLOBAL_HISTORY       = DOCS / "global_history.json"
BETFAIR_HISTORY      = DOCS / "betfair_history.json"
BETFAIR_MARKETS_PATH = "docs/betfair_markets.json"

DAYS_BACK      = int(os.environ.get("BF_BACKFILL_DAYS", "30"))
SAMPLE_PER_DAY = int(os.environ.get("BF_BACKFILL_SAMPLE_PER_DAY", "4"))


def get_commits_for_file(file_path: str, days: int):
    """Lista [(hash, iso_ts)] di commit che hanno toccato il file negli ultimi N giorni."""
    try:
        out = subprocess.check_output(
            ["git", "log", f"--since={days} days ago",
             "--pretty=format:%H|%ai", "--all", "--", file_path],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[bf-backfill] git log fallito: {e}", file=sys.stderr)
        return []
    commits = []
    for line in out.strip().splitlines():
        if "|" in line:
            h, ts = line.split("|", 1)
            commits.append((h.strip(), ts.strip()))
    return commits


def sample_commits_per_day(commits, sample_per_day: int):
    """Per giorno, prende fino a N commit campionati uniformemente."""
    by_day = {}
    for h, ts in commits:
        d = ts[:10]
        by_day.setdefault(d, []).append((h, ts))
    sampled = []
    for d in sorted(by_day.keys()):
        lst = by_day[d]
        if not lst:
            continue
        n = min(sample_per_day, len(lst))
        step = max(1, len(lst) // n)
        for i in range(0, len(lst), step):
            sampled.append(lst[i])
            if sum(1 for _ in [c for c in sampled if c[1][:10] == d]) >= n:
                break
    return sampled


def load_markets_at_commit(commit: str):
    """Carica docs/betfair_markets.json al commit specifico. Lista markets o []."""
    try:
        out = subprocess.check_output(
            ["git", "show", f"{commit}:{BETFAIR_MARKETS_PATH}"],
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        return data.get("markets", []) or []
    except Exception:
        return []


def build_bf_index(all_markets):
    """Index (date, norm_home, norm_away) -> info market.
    Mantiene il PRIMO match per chiave (sufficiente per popolare).
    Filtri leggeri: market_id Exchange (1.NNNN), event_name con ' v '."""
    idx = {}
    for m in all_markets:
        mid = m.get("market_id") or ""
        if not str(mid).startswith("1."):
            continue
        ev = (m.get("event_name") or "").strip()
        st = (m.get("start_time") or "").strip()
        if " v " not in ev or len(st) < 10:
            continue
        d = st[:10]  # YYYY-MM-DD
        parts = ev.split(" v ", 1)
        if len(parts) != 2:
            continue
        h_raw, a_raw = parts[0].strip(), parts[1].strip()
        n_h = _normalize_team(h_raw)
        n_a = _normalize_team(a_raw)
        if not n_h or not n_a:
            continue
        key = (d, n_h, n_a)
        if key not in idx:
            idx[key] = {
                "market_id":       mid,
                "event_name":      ev,
                "start_time":      st,
                "best_back_price": m.get("best_back_price"),
                "best_back_size":  m.get("best_back_size"),
            }
    return idx


def main():
    if not GLOBAL_HISTORY.exists():
        print("ERR: docs/global_history.json mancante", file=sys.stderr)
        sys.exit(1)

    print(f"[bf-backfill] DAYS_BACK={DAYS_BACK} SAMPLE_PER_DAY={SAMPLE_PER_DAY}")

    gh = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8", errors="replace"))
    print(f"[bf-backfill] global_history: {len(gh)} FT entries")

    bh = {}
    if BETFAIR_HISTORY.exists():
        try:
            bh = json.loads(BETFAIR_HISTORY.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            bh = {}
    print(f"[bf-backfill] betfair_history start: {len(bh)} entries")

    commits = get_commits_for_file(BETFAIR_MARKETS_PATH, DAYS_BACK)
    print(f"[bf-backfill] commit totali ({DAYS_BACK} gg): {len(commits)}")

    sampled = sample_commits_per_day(commits, SAMPLE_PER_DAY)
    print(f"[bf-backfill] sample {SAMPLE_PER_DAY}/giorno: {len(sampled)} commits da processare")

    all_markets = []
    for i, (h, ts) in enumerate(sampled, 1):
        m = load_markets_at_commit(h)
        all_markets.extend(m)
        if i % 25 == 0 or i == len(sampled):
            print(f"  progress {i}/{len(sampled)} · markets aggregati {len(all_markets)}")

    print(f"[bf-backfill] markets totali aggregati: {len(all_markets)}")

    bf_idx = build_bf_index(all_markets)
    print(f"[bf-backfill] bf_idx unique events: {len(bf_idx)}")

    added = 0
    no_match = 0
    for fid, m in gh.items():
        if fid in bh:
            continue
        d = (m.get("date") or "")[:10]
        h_raw = m.get("home") or ""
        a_raw = m.get("away") or ""
        n_h = _normalize_team(h_raw)
        n_a = _normalize_team(a_raw)
        key = (d, n_h, n_a)
        bf_match = bf_idx.get(key)
        if not bf_match:
            no_match += 1
            continue
        bh[fid] = {
            "fixture_id":      int(fid),
            "home":             h_raw,
            "away":             a_raw,
            "league":           m.get("league", ""),
            "country":          m.get("country", ""),
            "date":             d,
            "kickoff":          m.get("kickoff", ""),
            "status":           m.get("status", "FT"),
            "goals_home":       m.get("goals_home"),
            "goals_away":       m.get("goals_away"),
            "score":            f"{m.get('goals_home') or 0}-{m.get('goals_away') or 0}",
            "first_min_cached": m.get("first_min_cached"),
            "bf_market_id":     bf_match.get("market_id"),
            "bf_back_price":    bf_match.get("best_back_price"),
            "bf_back_size":     bf_match.get("best_back_size"),
            "saved_at":         datetime.now(timezone.utc).isoformat(),
            "backfilled":       True,
        }
        added += 1

    BETFAIR_HISTORY.write_text(
        json.dumps(bh, ensure_ascii=False).encode("utf-8", errors="replace").decode("utf-8"),
        encoding="utf-8",
    )

    cached = sum(1 for v in bh.values() if v.get("first_min_cached") is not None)
    pct = round(cached / len(bh) * 100, 1) if bh else 0
    print(f"[bf-backfill] DONE · betfair_history: {len(bh)} totali (+{added} nuovi · "
          f"{no_match} no-match in global_history)")
    print(f"[bf-backfill] cache 1° goal: {cached}/{len(bh)} ({pct}%)")


if __name__ == "__main__":
    main()

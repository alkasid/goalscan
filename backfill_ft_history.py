"""
backfill_ft_history.py — rebuild ft_history.json per gli alert
non catturati dall'updater quando era rotto.

Strategia:
  1. Estrae da git log tutti gli alert_ids.json storici dopo START_DATE
  2. Costruisce union dei fixture IDs alertati (~migliaia)
  3. Per ogni ID, cerca in global_history.json (versione corrente)
  4. Se la fixture è FT/AET/PEN e ha data >= START_DATE, la aggiunge
     a ft_history.json copiando i campi (incluso first_min_cached
     se già presente in global_history grazie al backfill globale).

Idempotente: non rimuove entries esistenti.

Env vars:
  BACKFILL_FT_START_DATE   data minima YYYY-MM-DD (default: 2026-04-26)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DOCS               = Path("docs")
FT_HISTORY         = DOCS / "ft_history.json"
GLOBAL_HISTORY     = DOCS / "global_history.json"
ALERT_IDS_PATH     = "docs/alert_ids.json"
START_DATE         = os.environ.get("BACKFILL_FT_START_DATE", "2026-04-26")


def get_commits(file_path: str, since_iso: str):
    """Lista [(hash, iso_ts)] di commit che hanno toccato il file dopo since."""
    cmd = [
        "git", "log", f"--since={since_iso}",
        "--pretty=format:%H|%ai", "--all", "--", file_path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True)
    except subprocess.CalledProcessError as e:
        print(f"ERR git log: {e}", file=sys.stderr)
        return []
    commits = []
    for line in out.strip().splitlines():
        if "|" in line:
            h, ts = line.split("|", 1)
            commits.append((h.strip(), ts.strip()))
    return commits


def load_alert_ids_at(commit: str):
    """Carica l'array di IDs di alert_ids.json al commit. Lista vuota se errore."""
    try:
        out = subprocess.check_output(
            ["git", "show", f"{commit}:{ALERT_IDS_PATH}"],
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        if isinstance(data, list):
            return [str(i) for i in data]
        return []
    except Exception:
        return []


def main():
    if not GLOBAL_HISTORY.exists():
        print("ERR: docs/global_history.json mancante", file=sys.stderr)
        sys.exit(1)

    print(f"[backfill-ft] START_DATE={START_DATE}")

    gh = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8", errors="replace"))
    print(f"[backfill-ft] global_history: {len(gh)} entries")

    fh = {}
    if FT_HISTORY.exists():
        try:
            fh = json.loads(FT_HISTORY.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            fh = {}
    print(f"[backfill-ft] ft_history start: {len(fh)} entries")

    commits = get_commits(ALERT_IDS_PATH, START_DATE)
    print(f"[backfill-ft] commit alert_ids dopo {START_DATE}: {len(commits)}")

    all_alert_ids = set()
    for i, (h, ts) in enumerate(commits, 1):
        ids = load_alert_ids_at(h)
        all_alert_ids.update(ids)
        if i % 25 == 0 or i == len(commits):
            print(f"  progress {i}/{len(commits)} · union {len(all_alert_ids)} alert IDs")

    print(f"[backfill-ft] union totale alert IDs: {len(all_alert_ids)}")

    added       = 0
    skip_old    = 0
    skip_no_gh  = 0
    skip_not_ft = 0
    cached_in   = 0

    for fid in all_alert_ids:
        if fid in fh:
            continue
        m = gh.get(fid)
        if not m:
            skip_no_gh += 1
            continue
        d = (m.get("date") or "")[:10]
        if d < START_DATE:
            skip_old += 1
            continue
        st = m.get("status")
        if st not in ("FT", "AET", "PEN"):
            skip_not_ft += 1
            continue

        first_min = m.get("first_min_cached")
        if first_min is not None:
            cached_in += 1

        fh[fid] = {
            "fixture_id":       int(fid),
            "home":             m.get("home", ""),
            "away":             m.get("away", ""),
            "league":           m.get("league", ""),
            "country":          m.get("country", ""),
            "date":             d,
            "kickoff":          m.get("kickoff", ""),
            "status":           st,
            "goals_home":       m.get("goals_home"),
            "goals_away":       m.get("goals_away"),
            "score":            f"{m.get('goals_home') or 0}-{m.get('goals_away') or 0}",
            "first_min_cached": first_min,
            "saved_at":         datetime.now(timezone.utc).isoformat(),
            "backfilled":       True,
        }
        added += 1

    # Stesso formato di updater.py: indent=2
    FT_HISTORY.write_text(
        json.dumps(fh, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cached_total = sum(1 for v in fh.values() if v.get("first_min_cached") is not None)
    pct = round(cached_total / len(fh) * 100, 1) if fh else 0
    print(f"[backfill-ft] DONE · ft_history: {len(fh)} totali (+{added} nuovi)")
    print(f"[backfill-ft]   skip: old={skip_old}, no_gh={skip_no_gh}, not_ft={skip_not_ft}")
    print(f"[backfill-ft]   cache 1° goal nei nuovi: {cached_in}/{added}")
    print(f"[backfill-ft]   cache 1° goal totale: {cached_total}/{len(fh)} ({pct}%)")


if __name__ == "__main__":
    main()

"""
backfill_global.py — bulk fetcher di first_min_cached per global_history.json.

Per ogni fixture FT con goal e senza first_min_cached, chiama
GET /fixtures/events?type=Goal e:
  1. salva first_min_cached in docs/global_history.json
  2. salva la lista completa di goal events in docs/goal_events_cache.json

Idempotente. Rate-limited. Salvataggio incrementale (resume-friendly).

Env vars:
  API_FOOTBALL_KEY        — required
  BACKFILL_MAX_PER_RUN    — cap fixture per run (default 20000)
  BACKFILL_MAX_WORKERS    — worker paralleli (default 4)
  BACKFILL_SAVE_EVERY     — save ogni N fixture (default 200)
  BACKFILL_RPS            — req/sec globali (default 20.0; alza con piani Mega+)
  BACKFILL_MAX_RETRIES    — tentativi su 429/5xx (default 5)
"""
import json
import os
import sys
import time
import threading
from collections import deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
BASE    = "https://v3.football.api-sports.io"
HDR     = {"x-apisports-key": API_KEY}

DOCS                = Path("docs")
GLOBAL_HISTORY_FILE = DOCS / "global_history.json"
EVENTS_CACHE_FILE   = DOCS / "goal_events_cache.json"

MAX_PER_RUN = int(os.environ.get("BACKFILL_MAX_PER_RUN", "20000"))
MAX_WORKERS = int(os.environ.get("BACKFILL_MAX_WORKERS", "4"))
SAVE_EVERY  = int(os.environ.get("BACKFILL_SAVE_EVERY", "200"))
RPS         = float(os.environ.get("BACKFILL_RPS", "20.0"))
MAX_RETRIES = int(os.environ.get("BACKFILL_MAX_RETRIES", "5"))

_save_lock      = threading.Lock()
_rate_lock      = threading.Lock()
_request_times  = deque()
_RATE_WINDOW    = 1.0  # secondi


def rate_limit_acquire():
    """Token bucket sliding-window: blocca finché c'è capacità per una richiesta."""
    while True:
        with _rate_lock:
            now    = time.monotonic()
            cutoff = now - _RATE_WINDOW
            while _request_times and _request_times[0] < cutoff:
                _request_times.popleft()
            if len(_request_times) < int(RPS * _RATE_WINDOW):
                _request_times.append(now)
                return
            sleep_for = (_request_times[0] + _RATE_WINDOW) - now
        time.sleep(max(sleep_for, 0.005))


def fetch_goal_events(fid: int):
    """Ritorna {'first_min': int|None, 'goals': [...]}.
    None solo se errori/429 esauriti dopo MAX_RETRIES."""
    last_err = None
    for attempt in range(MAX_RETRIES):
        rate_limit_acquire()
        try:
            r = requests.get(
                f"{BASE}/fixtures/events",
                headers=HDR,
                params={"fixture": fid, "type": "Goal"},
                timeout=25,
            )
        except Exception as e:
            last_err = f"transport: {e}"
            time.sleep(min(2 ** attempt, 30))
            continue

        if r.status_code == 429:
            # Rispetta Retry-After se presente, altrimenti backoff esponenziale
            retry_after = r.headers.get("Retry-After")
            try:
                backoff = float(retry_after) if retry_after else 2 ** (attempt + 1)
            except ValueError:
                backoff = 2 ** (attempt + 1)
            backoff = min(max(backoff, 1.0), 60.0)
            time.sleep(backoff)
            last_err = f"429 (attempt {attempt+1}/{MAX_RETRIES}, backoff {backoff:.1f}s)"
            continue

        if 500 <= r.status_code < 600:
            time.sleep(min(2 ** attempt, 30))
            last_err = f"HTTP {r.status_code}"
            continue

        if r.status_code != 200:
            return None  # 4xx non-429: non ritentare

        try:
            evs = r.json().get("response", []) or []
        except Exception:
            return None

        goals = []
        for e in evs:
            if e.get("type") != "Goal":
                continue
            if e.get("detail") == "Missed Penalty":
                continue
            t       = e.get("time") or {}
            elapsed = t.get("elapsed")
            extra   = t.get("extra") or 0
            if elapsed is None:
                continue
            goals.append({
                "min":    int(elapsed) + int(extra),
                "team":   (e.get("team")   or {}).get("name", ""),
                "player": (e.get("player") or {}).get("name", ""),
                "detail": e.get("detail", ""),
            })
        goals.sort(key=lambda g: g["min"])
        return {
            "first_min": goals[0]["min"] if goals else None,
            "goals":     goals,
        }

    print(f"  [{fid}] giving up: {last_err}", flush=True)
    return None


def save_state(gl: dict, events: dict):
    """Atomic write tmp+replace su entrambi i file."""
    with _save_lock:
        for path, data in [(GLOBAL_HISTORY_FILE, gl), (EVENTS_CACHE_FILE, events)]:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False).encode("utf-8", errors="replace").decode("utf-8"),
                encoding="utf-8",
            )
            tmp.replace(path)


def main():
    if not API_KEY:
        print("ERR: API_FOOTBALL_KEY mancante", file=sys.stderr)
        sys.exit(1)
    if not GLOBAL_HISTORY_FILE.exists():
        print(f"ERR: {GLOBAL_HISTORY_FILE} mancante", file=sys.stderr)
        sys.exit(1)

    gl = json.loads(GLOBAL_HISTORY_FILE.read_text(encoding="utf-8", errors="replace"))
    events = {}
    if EVENTS_CACHE_FILE.exists():
        try:
            events = json.loads(EVENTS_CACHE_FILE.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            events = {}

    candidates = [
        fid for fid, v in gl.items()
        if v.get("first_min_cached") is None
        and ((v.get("goals_home") or 0) + (v.get("goals_away") or 0)) > 0
    ]
    cached_at_start = sum(1 for v in gl.values() if v.get("first_min_cached") is not None)
    print(f"[backfill] start · {cached_at_start}/{len(gl)} già cachate · {len(candidates)} candidate", flush=True)
    print(f"[backfill] config: workers={MAX_WORKERS} rps={RPS} max_retries={MAX_RETRIES} save_every={SAVE_EVERY}", flush=True)

    if not candidates:
        print("[backfill] niente da fare — global_history complete", flush=True)
        return

    todo = candidates[:MAX_PER_RUN]
    print(f"[backfill] processo {len(todo)} fixture", flush=True)

    filled    = 0
    no_data   = 0
    failed    = 0
    last_save = 0
    start     = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        future_to_fid = {exe.submit(fetch_goal_events, int(fid)): fid for fid in todo}
        for i, fut in enumerate(as_completed(future_to_fid), 1):
            fid = future_to_fid[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{fid}] task error: {e}", flush=True)
                failed += 1
                continue

            if res is None:
                failed += 1
                continue

            gl[fid]["first_min_cached"] = res["first_min"]
            events[fid] = res
            if res["first_min"] is not None:
                filled += 1
            else:
                no_data += 1

            if i - last_save >= SAVE_EVERY:
                save_state(gl, events)
                elapsed = time.time() - start
                rate    = i / elapsed if elapsed > 0 else 0
                eta     = (len(todo) - i) / rate if rate > 0 else 0
                print(f"[backfill] {i}/{len(todo)} · filled={filled} no_data={no_data} failed={failed} · "
                      f"{rate:.1f}/s · eta={eta:.0f}s", flush=True)
                last_save = i

    save_state(gl, events)
    elapsed = time.time() - start
    cached  = sum(1 for v in gl.values() if v.get("first_min_cached") is not None)
    print(f"[backfill] DONE · filled={filled} no_data={no_data} failed={failed} in {elapsed:.1f}s", flush=True)
    print(f"[backfill] global_history: {cached}/{len(gl)} cachate ({cached/len(gl)*100:.1f}%)", flush=True)
    print(f"[backfill] events_cache: {len(events)} entry", flush=True)


if __name__ == "__main__":
    main()

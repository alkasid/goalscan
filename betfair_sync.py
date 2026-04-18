"""
BETFAIR SYNC — producer di docs/betfair_markets.json
──────────────────────────────────────────────────────────────────
Scopo: fetchare mercati OVER_UNDER_05 (calcio) dal Betfair Exchange
       con liquidita' reale e scrivere docs/betfair_markets.json
       nello schema consumato da main.py (vedi CLAUDE.md §Sync Betfair).

Gira su GitHub Actions (.github/workflows/betfair_sync.yml) ogni 15 min.
Stesso concurrency group 'goalscan-push' di bot.yml/updater.yml.

Secrets richiesti (Repository Settings -> Secrets and variables -> Actions):
  BETFAIR_APP_KEY        Application Key (sviluppata su developer.betfair.com)
  BETFAIR_USERNAME       username Betfair del conto bot
  BETFAIR_PASSWORD       password del conto (se 2FA attivo vedi note sotto)
  BETFAIR_ENDPOINT  (opz) "com" (default) | "it" | "es" | "ro" | "se"

2FA: Betfair Interactive Login NON supporta TOTP lato server in modo pulito.
     Se il conto ha 2FA attivo:
       - disattivarlo solo per questo sub-account dedicato al bot, oppure
       - passare al Non-Interactive Login (cert SSL client) — serve
         refactor di login() per usare identitysso-cert.betfair.<tld>.

Output schema (NON cambiare senza coordinarsi con main.py + goalscanbot):
  {
    "generated_at": "<ISO8601 UTC>",
    "total_markets": <int>,
    "markets": [
      {
        "market_id": "1.XXXXXXXXX",
        "event_name": "Home v Away",
        "start_time": "<ISO8601>",
        "runner_id": 5851482,
        "best_back_price": <float|null>,
        "best_back_size": <float|null>
      }
    ]
  }
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Config (env + default) ──────────────────────────────────────────────────
APP_KEY  = (os.environ.get("BETFAIR_APP_KEY") or "").strip()
USERNAME = (os.environ.get("BETFAIR_USERNAME") or "").strip()
PASSWORD = (os.environ.get("BETFAIR_PASSWORD") or "").strip()
ENDPOINT = (os.environ.get("BETFAIR_ENDPOINT") or "com").strip() or "com"

LOGIN_URL    = f"https://identitysso.betfair.{ENDPOINT}/api/login"
EXCHANGE_URL = "https://api.betfair.com/exchange/betting/rest/v1.0"

# ── Filtro mercati ──────────────────────────────────────────────────────────
EVENT_TYPE_SOCCER   = "1"          # ID Betfair per "Soccer"
MARKET_TYPE         = "OVER_UNDER_05"  # coerente con betfair.html "OVER/UNDER 0.5"
SELECTION_RUNNER_ID = 5851482      # runner_id storico del file (Under 0.5 Goals)

# Finestra: da -2h a +7gg. Consistente con main.py (_BF_MAX_PAST_HOURS=3,
# _BF_MAX_FUTURE_DAYS=7). Usiamo -2h qui per avere un po' di margine prima
# che il filtro di main.py inizi a scartare per 'stale_start'.
WINDOW_PAST_HOURS  = 2
WINDOW_FUTURE_DAYS = 7

# Limiti Betfair API
MAX_RESULTS_PER_CATALOGUE = 200   # listMarketCatalogue cap
MARKET_BOOK_CHUNK         = 30    # listMarketBook max marketIds per call

# Output
OUTPUT_PATH = Path("docs/betfair_markets.json")


def _require_secrets():
    missing = [k for k, v in
               [("BETFAIR_APP_KEY", APP_KEY),
                ("BETFAIR_USERNAME", USERNAME),
                ("BETFAIR_PASSWORD", PASSWORD)] if not v]
    if missing:
        print(f"[BetfairSync] ERRORE: secret mancanti: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)


def login():
    """Interactive Login -> ritorna sessionToken."""
    r = requests.post(
        LOGIN_URL,
        headers={
            "X-Application": APP_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={"username": USERNAME, "password": PASSWORD},
        timeout=20,
    )
    if r.status_code != 200:
        print(f"[BetfairSync] login HTTP {r.status_code}: {r.text[:300]}",
              file=sys.stderr)
        sys.exit(2)
    j = r.json()
    if j.get("status") != "SUCCESS":
        # Esempi di status: LIMITED_ACCESS, LOGIN_RESTRICTED_LOCATION,
        # INVALID_USERNAME_OR_PASSWORD, CERT_AUTH_REQUIRED, SECURITY_QUESTION_WRONG_3X
        print(f"[BetfairSync] login status={j.get('status')} "
              f"error={j.get('error', '?')}", file=sys.stderr)
        sys.exit(2)
    return j["token"]


def _api(session_token, method, payload, retries=2):
    url = f"{EXCHANGE_URL}/{method}/"
    headers = {
        "X-Application": APP_KEY,
        "X-Authentication": session_token,
        "Content-Type": "application/json",
    }
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"[BetfairSync] {method} network error: {e}", file=sys.stderr)
            sys.exit(3)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(3 * (attempt + 1))
            continue
        print(f"[BetfairSync] {method} HTTP {r.status_code}: {r.text[:300]}",
              file=sys.stderr)
        sys.exit(3)


def list_market_catalogue(token, start_from, start_to):
    """Ritorna la lista mercati OVER_UNDER_05 calcio nella finestra."""
    payload = {
        "filter": {
            "eventTypeIds": [EVENT_TYPE_SOCCER],
            "marketTypeCodes": [MARKET_TYPE],
            "marketStartTime": {
                "from": start_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   start_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        "marketProjection": ["EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
        "maxResults": MAX_RESULTS_PER_CATALOGUE,
        "sort": "MAXIMUM_TRADED",  # prima i piu' liquidi
    }
    return _api(token, "listMarketCatalogue", payload)


def list_market_books(token, market_ids):
    """Batcha a 30 per chiamata — ritorna prezzi back correnti."""
    out = []
    for i in range(0, len(market_ids), MARKET_BOOK_CHUNK):
        chunk = market_ids[i:i + MARKET_BOOK_CHUNK]
        payload = {
            "marketIds": chunk,
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
                "virtualise": True,
            },
        }
        out.extend(_api(token, "listMarketBook", payload))
    return out


def build_markets_list(catalogue, books):
    """Costruisce la lista nel formato atteso da main.py."""
    books_by_id = {b.get("marketId"): b for b in books}
    out = []
    missing_runner = 0
    for m in catalogue:
        mid = m.get("marketId")
        if not mid or not str(mid).startswith("1."):
            # sanity: deve essere formato Exchange '1.XXX'
            continue

        event = (m.get("event") or {}).get("name") or ""
        start = m.get("marketStartTime") or ""

        # Selection id target: 5851482 (Under 0.5 Goals — convenzione storica).
        # Se nel catalogue non c'e', skippa (non rompere il file).
        runners = m.get("runners") or []
        target = next((r for r in runners
                       if r.get("selectionId") == SELECTION_RUNNER_ID), None)
        if target is None:
            missing_runner += 1
            continue

        # Prezzo/liquidita' dal book
        price, size = None, None
        book = books_by_id.get(mid) or {}
        book_runners = book.get("runners") or []
        book_target = next((r for r in book_runners
                            if r.get("selectionId") == SELECTION_RUNNER_ID), None)
        if book_target and (book_target.get("status") or "ACTIVE") == "ACTIVE":
            backs = (book_target.get("ex") or {}).get("availableToBack") or []
            if backs:
                price = backs[0].get("price")
                size  = backs[0].get("size")

        out.append({
            "market_id": mid,
            "event_name": event,
            "start_time": start,
            "runner_id": SELECTION_RUNNER_ID,
            "best_back_price": price,
            "best_back_size": size,
        })

    if missing_runner:
        print(f"[BetfairSync] {missing_runner} mercati saltati "
              f"(nessun runner {SELECTION_RUNNER_ID})")
    return out


def main():
    _require_secrets()
    now = datetime.now(timezone.utc)
    start_from = now - timedelta(hours=WINDOW_PAST_HOURS)
    start_to   = now + timedelta(days=WINDOW_FUTURE_DAYS)

    print(f"[BetfairSync] endpoint={ENDPOINT} market={MARKET_TYPE} "
          f"runner={SELECTION_RUNNER_ID} window="
          f"{start_from.isoformat()} -> {start_to.isoformat()}")

    token = login()
    print("[BetfairSync] login OK")

    catalogue = list_market_catalogue(token, start_from, start_to)
    print(f"[BetfairSync] listMarketCatalogue -> {len(catalogue)} mercati")

    markets = []
    if catalogue:
        mids = [m["marketId"] for m in catalogue if m.get("marketId")]
        books = list_market_books(token, mids)
        print(f"[BetfairSync] listMarketBook   -> {len(books)} book")
        markets = build_markets_list(catalogue, books)

    payload = {
        "generated_at": now.isoformat(),
        "total_markets": len(markets),
        "markets": markets,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stats rapide per il log del workflow
    with_price = sum(1 for x in markets if x.get("best_back_price") is not None)
    with_liq   = sum(1 for x in markets
                     if (x.get("best_back_size") or 0) >= 1.0)
    print(f"[BetfairSync] scritto {OUTPUT_PATH} | "
          f"totali={len(markets)} con_price={with_price} con_liquidita>=1EUR={with_liq}")


if __name__ == "__main__":
    main()

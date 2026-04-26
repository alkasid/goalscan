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
        "market_id": "1.XXXXXXXXX",    // MATCH_ODDS Exchange market
        "event_name": "Home v Away",
        "start_time": "<ISO8601>",
        "runner_id": <int>,            // HOME team selectionId (per-market)
        "best_back_price": <float|null>, // back HOME (= "1" nel 1X2)
        "best_back_size": <float|null>
      }
    ]
  }
Nota: il market_id e' MATCH_ODDS, NON OVER_UNDER_05. goalscanbot al kickoff
apre il mercato OVER_UNDER_05 live interrogando Betfair API diretta con
l'event_id (derivabile dal market_id, o via listEvents). Il market_id
MATCH_ODDS qui serve come prova "questa partita esiste su BF Exchange".
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
# MATCH_ODDS (1X2) invece di OVER_UNDER_05 perche':
#   - OVER_UNDER_05 pre-match spesso non esiste o ha prezzi insignificanti
#     (back Over 0.5 = 1.02, back Under 0.5 alto ma senza liquidita')
#   - MATCH_ODDS e' disponibile ~100% degli eventi Exchange -> sonda affidabile
#     per "questa partita e' tradable su BF Exchange"
#   - goalscanbot apre il mercato OVER_UNDER_05 LIVE al kickoff via Betfair API
#     diretta (non serve averlo nel JSON pre-match)
# Runner HOME: per MATCH_ODDS ogni market ha 3 runners (Home, The Draw, Away)
# con selectionId per-market. Identifichiamo HOME matchando runnerName con il
# lato sinistro dell'event_name 'A v B'.
EVENT_TYPE_SOCCER   = "1"          # ID Betfair per "Soccer"
MARKET_TYPE         = "MATCH_ODDS" # 1X2, sonda "partita esiste su Exchange"

# Finestra: da -2h a +7gg. Consistente con main.py (_BF_MAX_PAST_HOURS=3,
# _BF_MAX_FUTURE_DAYS=7). Usiamo -2h qui per avere un po' di margine prima
# che il filtro di main.py inizi a scartare per 'stale_start'.
WINDOW_PAST_HOURS  = 2
WINDOW_FUTURE_DAYS = 7

# Limiti Betfair API
# 1000 e' il cap hard di listMarketCatalogue (oltre ignora). Ordine
# FIRST_TO_START garantisce copertura cronologica uniforme (OGGI + DOMANI
# + DOPODOMANI) invece che concentrarsi sui soli match a volume alto.
MAX_RESULTS_PER_CATALOGUE = 1000
MARKET_BOOK_CHUNK         = 30    # listMarketBook max marketIds per call
CATALOGUE_SORT            = "FIRST_TO_START"

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


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def login():
    """Interactive Login -> ritorna sessionToken.
    Manda un User-Agent realistico: Betfair WAF blocca client `python-requests/*`
    con HTTP 403 e response HTML (visto su GitHub Actions runner Azure)."""
    r = requests.post(
        LOGIN_URL,
        headers={
            "X-Application":  APP_KEY,
            "Content-Type":   "application/x-www-form-urlencoded",
            "Accept":         "application/json",
            "User-Agent":     _BROWSER_UA,
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
        "X-Application":    APP_KEY,
        "X-Authentication": session_token,
        "Content-Type":     "application/json",
        "Accept":           "application/json",
        "User-Agent":       _BROWSER_UA,
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
        "sort": CATALOGUE_SORT,
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


def _pick_home_runner(runners, event_name):
    """Per MATCH_ODDS: identifica il runner HOME.
    Strategia: matcha runnerName con lato sinistro di event_name 'A v B'.
    Fallback: runner con sortPriority piu' basso (BF convenzione: home=1).
    """
    if not runners:
        return None
    ev = event_name or ""
    parts = ev.split(" v ")
    if len(parts) == 2:
        home_lc = parts[0].strip().lower()
        for r in runners:
            rn = (r.get("runnerName") or "").strip().lower()
            if rn == home_lc:
                return r
        # Partial match: primi 10 char
        if len(home_lc) >= 3:
            for r in runners:
                rn = (r.get("runnerName") or "").strip().lower()
                if rn.startswith(home_lc[:10]) or home_lc.startswith(rn[:10]):
                    return r
    # Fallback per sortPriority (BF: 1=home, 2=draw, 3=away per MATCH_ODDS)
    runners_sorted = sorted(runners, key=lambda r: r.get("sortPriority") or 999)
    return runners_sorted[0]


def build_markets_list(catalogue, books):
    """Costruisce la lista nel formato atteso da main.py.
    Per MATCH_ODDS: runner_id = HOME team selectionId (per-market),
    best_back_price = quota back HOME team, best_back_size = liquidita' EUR."""
    books_by_id = {b.get("marketId"): b for b in books}
    out = []
    missing_runner = 0
    for m in catalogue:
        mid = m.get("marketId")
        if not mid or not str(mid).startswith("1."):
            continue

        event = (m.get("event") or {}).get("name") or ""
        start = m.get("marketStartTime") or ""

        # Identifica runner HOME nel catalogue
        runners = m.get("runners") or []
        home_runner = _pick_home_runner(runners, event)
        if home_runner is None:
            missing_runner += 1
            continue
        home_runner_id = home_runner.get("selectionId")
        if not home_runner_id:
            missing_runner += 1
            continue

        # Prezzo/liquidita' dal book (stesso runner)
        price, size = None, None
        book = books_by_id.get(mid) or {}
        book_runners = book.get("runners") or []
        book_home = next((r for r in book_runners
                          if r.get("selectionId") == home_runner_id), None)
        if book_home and (book_home.get("status") or "ACTIVE") == "ACTIVE":
            backs = (book_home.get("ex") or {}).get("availableToBack") or []
            if backs:
                price = backs[0].get("price")
                size  = backs[0].get("size")

        out.append({
            "market_id": mid,
            "event_name": event,
            "start_time": start,
            "runner_id": home_runner_id,
            "best_back_price": price,
            "best_back_size": size,
        })

    if missing_runner:
        print(f"[BetfairSync] {missing_runner} mercati saltati "
              f"(nessun runner HOME identificabile)")
    return out


def main():
    _require_secrets()
    now = datetime.now(timezone.utc)
    start_from = now - timedelta(hours=WINDOW_PAST_HOURS)
    start_to   = now + timedelta(days=WINDOW_FUTURE_DAYS)

    print(f"[BetfairSync] endpoint={ENDPOINT} market={MARKET_TYPE} "
          f"runner=HOME-per-market window="
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

    # Distribuzione per giorno (utile per diagnosticare copertura OGGI/DOMANI/...)
    by_day = {}
    for x in markets:
        st = x.get("start_time") or ""
        try:
            d = datetime.fromisoformat(st.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            d = "?"
        by_day[d] = by_day.get(d, 0) + 1
    if by_day:
        dist = ", ".join(f"{k}:{v}" for k, v in sorted(by_day.items()))
        print(f"[BetfairSync] distribuzione per giorno: {dist}")


if __name__ == "__main__":
    main()

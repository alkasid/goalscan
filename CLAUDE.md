# variatio-bot (logica dati — GitHub Actions)

Progetto 2 di 3 della suite GoalScan. **Bot Python** che chiama API-Football, calcola alert, genera gli HTML/JSON del sito variatio. Gira su GitHub Actions ogni ora (bot.yml) + updater ogni 5 min (updater.yml).

## Focus di questo progetto

Qui si lavora sulla **logica Python del bot**:
- `main.py` — motore principale (~4500 righe): fetch match, calcolo alert, verifica quote Bet365, generazione HTML/JSON
- `updater.py` — mini-bot che aggiorna `ft_history.json` ogni 5 min  
- `test_api.py` — smoke test dell'API
- `.github/workflows/*.yml` — orchestrazione GitHub Actions
- `config.json` — soglie/parametri
- `requirements.txt` — deps Python (solo `requests==2.31.0`)

**Non si lavora qui sul layout** → quello e' `variatio-site` (pero' attenzione: i template HTML sono dentro `main.py` come stringhe `f"""..."""`, quindi modifiche estetiche grosse passano comunque da qui).

## Stack

- Python 3.11
- `requests==2.31.0` (unica dipendenza runtime)
- GitHub Actions (`ubuntu-latest`, python 3.11)
- API-Football v3 (https://v3.football.api-sports.io)

## CONTRACT OUTPUT — cosa genera e chi lo legge

**IMPORTANTE**: questo bot produce file in `docs/` che sono letti da 2 altri sistemi. Modificare il formato senza coordinarsi rompe i consumatori.

| File generato | Produttore | Consumatore 1 | Consumatore 2 |
|---|---|---|---|
| `docs/index.html` | main.py | variatio-site (browser) | — |
| `docs/betfair.html` | main.py | variatio-site (browser) | **goalscanbot/scraper.py** (parsing BeautifulSoup) |
| `docs/betfair_stats.html` | main.py | variatio-site (browser) | — |
| `docs/global_stats.html` | main.py | variatio-site (browser) | — |
| `docs/matches.json` | main.py | variatio-site (JS) | — |
| `docs/alert_ids.json` | main.py | **updater.py** (stessa repo) | — |
| `docs/ft_history.json` | updater.py | main.py (next run) | — |
| `docs/global_history.json` | main.py | variatio-site | — |
| `docs/betfair_markets.json` | **betfair_sync** (vedi §Sync Betfair) | main.py (input) | **goalscanbot** (reference dati mercati) |
| `docs/report-YYYYMMDD-HHMM.html` | main.py | variatio-site (snapshot storici) | — |
| `cache_teams.json` | main.py | main.py (next run) | — |

### Contract STRICT (non cambiare senza sincronizzare goalscanbot)

1. **`docs/betfair.html`** — goalscanbot/scraper.py fa parsing HTML. Il parser si basa su:
   - Badge/classi CSS che indicano `BET365 VERIFIED`
   - Struttura table con colonne: lega | orario KO | home team | away team | goal_home | goal_away | ratio
   - `data-fixture-id` o simili attributi per identificare il match
   - Se cambi selettori CSS o struttura DOM → scraper.py si rompe
   - **Regola**: se modifichi la generazione di betfair.html, testare subito con `python -c "from scraper import fetch_alerts; print(len(fetch_alerts()))"` nel progetto goalscanbot prima di pushare

2. **`docs/alert_ids.json`** — array di fixture ID. Letto da updater.py (stesso repo) ma anche potenzialmente da goalscanbot.
   - Formato: `[123456, 789012, ...]` (lista di int)
   - Mantenere invariato

3. **`docs/*.json`** — struttura dict/list Python serializzata. Per i consumatori JS del sito, evitare trailing commas e stringhe non-escape. `json.dumps(..., ensure_ascii=False, indent=2)` consigliato.

## Sync Betfair — come arriva `docs/betfair_markets.json`

**Stato attuale (2026-04-18)**: producer eseguito sul **Raspberry Pi** (stessa macchina di goalscanbot) via cron. Script `betfair_sync.py` nel repo, lanciato da `scripts/run_betfair_sync.sh` che carica i secret da `.env` locale (fuori dal git) e pusha `docs/betfair_markets.json` al repo. Scelta motivata dalla volontà di **non esporre credenziali Betfair su GitHub**: le stesse creds che il bot di goalscanbot usa per scommettere vengono riusate per il sync.

⚠️ Il workflow `.github/workflows/betfair_sync.yml` è stato **rimosso** (opzione B abbandonata in favore del cron su Pi). Se serve tornare indietro: `git log --all -- .github/workflows/betfair_sync.yml` mostra il commit che lo introduceva (feat: e81a207 o simile).

### Setup sul Raspberry Pi

1. Clone del repo in una cartella dedicata (es. `/home/pi/goalscan`):
   ```bash
   git clone https://github.com/alkasid/goalscan.git /home/pi/goalscan
   cd /home/pi/goalscan
   pip install -r requirements.txt
   ```
2. Copia `.env.example` in `.env` e compila le 3 variabili:
   ```bash
   cp .env.example .env
   $EDITOR .env   # BETFAIR_APP_KEY, BETFAIR_USERNAME, BETFAIR_PASSWORD
   chmod 600 .env  # leggibile solo dall'utente
   ```
3. Configura git per push (SSH key o PAT HTTPS salvata nella keychain).
4. Rendi eseguibile il wrapper e aggiungilo a cron:
   ```bash
   chmod +x scripts/run_betfair_sync.sh
   crontab -e
   ```
   Aggiungi:
   ```cron
   */15 * * * * /home/pi/goalscan/scripts/run_betfair_sync.sh >> /var/log/betfair_sync.log 2>&1
   ```

### File coinvolti

| File | Stato | Contenuto |
|---|---|---|
| `betfair_sync.py` | nel repo | fetcher Betfair (legge env, scrive docs/betfair_markets.json) |
| `scripts/run_betfair_sync.sh` | nel repo | wrapper cron: `.env` + pull rebase + python + commit + push retry |
| `.env.example` | nel repo | template dei 3 secret, commentato |
| `.env` | **NON nel repo** (gitignored) | valori reali dei secret, solo sul Pi |

### 2FA

Interactive Login di Betfair non supporta TOTP lato server. Se il conto ha 2FA attivo:
- disattivarlo sul sub-account dedicato al bot (consigliato), oppure
- refactor `login()` in `betfair_sync.py` per Non-Interactive Login (`identitysso-cert.betfair.<tld>`) con cert SSL client in `.env` (path al `.crt` + `.key` caricati localmente).

### Scope mercati

`OVER_UNDER_05` calcio (eventTypeId `1`), runner_id `5851482` ("Under 0.5 Goals", coerente con il dato pre-esistente). Finestra kickoff -2h → +7gg. Ordinati per `MAXIMUM_TRADED` (prima i piu' liquidi). Cap 200 mercati per run.

Cambiare `MARKET_TYPE` o `SELECTION_RUNNER_ID` in `betfair_sync.py` cambia cosa finisce su `betfair.html`. Se cambi il runner, aggiorna anche il titolo/testo di `generate_betfair_html` in `main.py` per coerenza.

### Schema file (contract)

```json
{
  "generated_at": "<ISO8601 UTC>",
  "total_markets": <int>,
  "markets": [
    {
      "market_id": "1.XXXXXXXXX",     // Exchange only: formato 1.<digits>
      "event_name": "Home v Away",     // convenzione Exchange (' v ' non 'vs')
      "start_time": "<ISO8601>",       // kickoff
      "runner_id": 5851482,            // costante: stesso runner per tutti
      "best_back_price": <float|null>, // miglior quota back (null se no price)
      "best_back_size": <float|null>   // liquidita' EUR sul miglior back
    }
  ]
}
```

NON cambiare lo schema senza coordinarsi con main.py + goalscanbot.

### Log diagnostica

Nel log del cron (`/var/log/betfair_sync.log` o dove l'hai rediretto):
```
=== [run_betfair_sync] start <timestamp> ===
[BetfairSync] endpoint=com market=OVER_UNDER_05 runner=5851482 window=...
[BetfairSync] login OK
[BetfairSync] listMarketCatalogue -> X mercati
[BetfairSync] listMarketBook   -> X book
[BetfairSync] scritto docs/betfair_markets.json | totali=X con_price=Y con_liquidita>=1EUR=Z
[run_betfair_sync] push OK (attempt 1)
```
Se `con_liquidita` crolla per piu' run consecutivi → o e' un orario morto (notte italiana), o Betfair sta rifiutando il login. Controllare lo `status` del login nei log.

### Monitoring suggerito (TODO)

Per accorgersi se il cron muore: healthchecks.io (gratis). Nel cron aggiungere `&& curl -fsS --retry 3 https://hc-ping.com/<uuid>` dopo lo script. Se ping manca per >30 min healthchecks invia alert email/telegram.

### Filtro Exchange-only (implementato in main.py)

`main.py` applica `_is_exchange_market()` a ogni mercato prima del matching. Criteri STRICT, tutti obbligatori:

1. `market_id` match `^1\.\d+$` (formato Exchange; Sportsbook usa altro schema)
2. `event_name` contiene ` v ` (convenzione Exchange)
3. `runner_id` intero > 0
4. `best_back_price` in `[1.01, 1000]` (runner vivo + quota sensata)
5. `best_back_size` >= `1.0` EUR (liquidita' minima reale)
6. `start_time` non piu' di 3h nel passato ne' piu' di 7gg nel futuro

Soglie regolabili via costanti modulo `_BF_MIN_BACK_PRICE`, `_BF_MAX_BACK_PRICE`, `_BF_MIN_BACK_SIZE`, `_BF_MAX_PAST_HOURS`, `_BF_MAX_FUTURE_DAYS`. Cambiarle cambia cosa compare su `betfair.html`.

Nei log del workflow, la riga diagnostica e':
```
[Betfair] markets totali: X | exchange validi: Y | scartati: Z (no_back_price=A stale_start=B ...)
```
Se `scartati == totali` per piu' run consecutivi → il producer e' morto, controllare §Sync Betfair.

## Config (`config.json`)

```json
{
  "goal_threshold": 14,
  "min_scored_each": 5,
  "min_conceded_max": 8,
  "last_matches_count": 5
}
```

Modificare questi valori cambia quali alert escono e quindi TUTTO il comportamento downstream (il sito, il bot Betfair). Alzare la soglia = meno alert ma piu' affidabili; abbassarla = piu' alert ma rischio di falsi positivi.

## Environment variables / GitHub Secrets

Repository Settings -> Secrets and variables -> Actions:

| Secret | Obbligatorio | Uso |
|---|---|---|
| `API_FOOTBALL_KEY` | SI | chiave API-Football (piano Free 100 req/giorno, Pro 7500/giorno) |
| `TELEGRAM_BOT_TOKEN` | NO | bot Telegram (disattivato: `TELEGRAM_ENABLED = False` in main.py) |
| `TELEGRAM_CHAT_ID` | NO | chat ID Telegram |

Per sviluppo locale: `.env` (nel `.gitignore`) o `export API_FOOTBALL_KEY=...` prima di eseguire.

## Workflows GitHub Actions

### `bot.yml` — Goal Bot (ogni ora, cron `0 * * * *`)
- Esegue `python main.py`
- Commit + push `docs/` e `cache_teams.json`
- Retry push con rebase fino a 3 volte (gestisce concorrenza con updater)
- Concurrency group `goalscan-push`

### `updater.yml` — Live Updater (ogni 5 minuti, cron `*/5 * * * *`)
- Esegue `python updater.py`
- Aggiorna solo `docs/ft_history.json`
- Commit + push
- Stesso concurrency group di bot.yml

### `test.yml` — API Test (manual dispatch)
- Esegue `python test_api.py` per smoke test API

## Sviluppo locale

```bash
git clone https://github.com/alkasid/goalscan.git
cd goalscan
pip install -r requirements.txt
export API_FOOTBALL_KEY="your-key"

# Dry-run bot principale
python main.py

# Solo updater
python updater.py

# Smoke test API
python test_api.py
```

Output in `docs/`. Apri `docs/index.html` nel browser per vedere la dashboard.

**ATTENZIONE consumo API**: piano Free = 100 req/giorno. `main.py` fa facilmente 100+ chiamate per run. Per dev: limita leghe/giorni in main.py o usa una chiave Pro separata.

## Cose delicate

- **Rate limit API**: `ThreadPoolExecutor(max_workers=3)` per evitare 429. Se aumenti i workers rischi ban IP.
- **Cache su disco**: `cache_teams.json` (1 MB) condivisa tra run. Se la elimini, il prossimo run la rigenera con molte chiamate API in piu'.
- **Report storici `docs/report-*.html`**: crescono ad ogni run. 805+ file gia' accumulati. TODO: auto-cleanup (es. keep last 7 days) prima del commit.
- **`SKIP_KEYWORDS` in main.py**: salta u17/u18/u19/u20/u21/u23/youth/reserve/women. Se vuoi includerli, modifica questa lista.
- **Telegram disattivato**: per riattivarlo metti `TELEGRAM_ENABLED = True` e setta i secrets.
- **Concurrency push**: bot.yml + updater.yml possono entrare in conflitto se un run dura piu' di 5 min. Il retry rebase gestisce la maggior parte dei casi, ma in rari casi serve intervento manuale.

## Connessioni con gli altri progetti

- **variatio-site**: legge gli output di questo bot (`docs/*.html`, `docs/*.json`)
- **goalscanbot** (RPi): scraping HTTP di `docs/betfair.html` via il suo `scraper.py`. **Rispettare il contract output** per non rompere goalscanbot.

## Come Claude deve lavorare

1. **Git workflow**: `git pull origin main` -> modifiche -> test in locale -> `git add` -> `git commit -m "feat|fix|chore: ..."` -> `git push`
2. **Prima di pushare modifiche a main.py**: syntax check `python -c "import ast; ast.parse(open('main.py').read())"` (obbligatorio visto che main.py e' 4500+ righe)
3. **Se modifichi la generazione di `docs/betfair.html`**: testa lo scraper di goalscanbot in locale PRIMA di pushare, altrimenti rompi il bot sul Pi:
   ```bash
   cd ../goalscanbot
   python -c "from scraper import fetch_alerts; a = fetch_alerts(); print(f'alert parsed: {len(a)}'); print(a[0] if a else 'empty')"
   ```
4. **Non committare**: `.env`, chiavi API, file grandi generati localmente, `__pycache__/`, `.venv/`
5. **Commit messages**: `feat:` per feature nuove, `fix:` per bugfix, `chore:` per housekeeping, `docs:` per README/commenti, `refactor:` per refactor. Il bot firma come `goal-bot@users.noreply.github.com`.
6. **Debug alert anomali**: scarica `docs/alert_ids.json` e `docs/ft_history.json` per contesto; se un match dovrebbe essere in alert ma non c'e', controlla `cache_teams.json` (potrebbe avere la squadra con nome duplicato in leghe diverse).
7. **Rate limit check**: se i run in produzione falliscono con 429, rallenta `max_workers` (attualmente 3) o spalma il lavoro in piu' run.

## Info repo

- Repo: https://github.com/alkasid/goalscan
- Branch: `main`
- Clone: `git clone https://github.com/alkasid/goalscan.git`
- Actions: https://github.com/alkasid/goalscan/actions

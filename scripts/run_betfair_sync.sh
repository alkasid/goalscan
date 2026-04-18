#!/usr/bin/env bash
#
# run_betfair_sync.sh — wrapper eseguito da cron sul Raspberry Pi.
#
# Scopo: carica i secret Betfair da .env (fuori dal repo), lancia
#        betfair_sync.py, committa docs/betfair_markets.json e pusha
#        a GitHub con rebase retry (concorrenza con bot.yml/updater.yml).
#
# Esempio cron (crontab -e dell'utente che ha il clone):
#   */15 * * * * /home/pi/goalscan/scripts/run_betfair_sync.sh >> /var/log/betfair_sync.log 2>&1
#
# Prerequisiti sul Pi:
#   1. Clone del repo in /home/pi/goalscan (o path equivalente)
#   2. File .env nella root del repo con le 3 variabili (vedi .env.example)
#   3. git configurato con identita' e credenziali per push (SSH key o PAT)
#   4. python3 + requests installati (pip install -r requirements.txt)

set -u  # fail se variabili non definite. NO -e: vogliamo gestire retry manualmente.

# Directory del repo (script sta in scripts/, repo e' il parent)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || { echo "[run_betfair_sync] impossibile cd in $REPO_DIR"; exit 1; }

TS="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo "=== [run_betfair_sync] start $TS ==="

# ── 1. Carica .env (3 secret Betfair) ──────────────────────────────────────
if [ ! -f .env ]; then
  echo "[run_betfair_sync] .env non trovato in $REPO_DIR. Copia .env.example e compila i valori." >&2
  exit 1
fi

# Parser .env robusto (NON usa 'source' → immune a apostrofi/quote non matchati
# nei valori, che invece il login Betfair puo' generare).
# Regole:
#   - righe vuote o che iniziano con '#' sono ignorate
#   - formato KEY=VALUE (KEY alfanumerico + underscore, maiuscolo)
#   - se VALUE e' racchiuso da quote matching ('' o ""), li rimuove
#   - nessun expansion shell sul VALUE
while IFS= read -r _line || [ -n "${_line:-}" ]; do
  # Trim leading whitespace
  _stripped="${_line#"${_line%%[![:space:]]*}"}"
  [ -z "$_stripped" ] && continue
  case "$_stripped" in
    \#*) continue ;;
  esac
  case "$_stripped" in
    *=*) : ;;
    *) continue ;;
  esac
  _key="${_stripped%%=*}"
  _val="${_stripped#*=}"
  # Rimuovi quote opzionali se matching su entrambi i lati
  if [ "${#_val}" -ge 2 ]; then
    _first="${_val:0:1}"
    _last="${_val: -1}"
    if [ "$_first" = "$_last" ] && { [ "$_first" = "'" ] || [ "$_first" = '"' ]; }; then
      _val="${_val:1:${#_val}-2}"
    fi
  fi
  export "$_key=$_val"
done < .env
unset _line _stripped _key _val _first _last

if [ -z "${BETFAIR_APP_KEY:-}" ] || [ -z "${BETFAIR_USERNAME:-}" ] || [ -z "${BETFAIR_PASSWORD:-}" ]; then
  echo "[run_betfair_sync] .env incompleto (servono BETFAIR_APP_KEY/USERNAME/PASSWORD)" >&2
  exit 1
fi

# ── 2. Sync pre-emptivo con remote (evita divergenze dopo il fetch) ───────
if ! git pull --rebase -X theirs origin main; then
  echo "[run_betfair_sync] git pull --rebase fallito, abort" >&2
  exit 2
fi

# ── 3. Esegui betfair_sync.py ──────────────────────────────────────────────
if ! python3 betfair_sync.py; then
  echo "[run_betfair_sync] betfair_sync.py ha fallito" >&2
  exit 3
fi

# ── 4. Commit se c'e' diff ─────────────────────────────────────────────────
git add docs/betfair_markets.json
if git diff --staged --quiet; then
  echo "[run_betfair_sync] nessun diff su betfair_markets.json, skip commit"
  exit 0
fi

git -c user.name="goal-bot" \
    -c user.email="goal-bot@users.noreply.github.com" \
    commit -m "chore: betfair_markets.json $TS [pi-sync]"

# ── 5. Push con retry rebase (concorrenza con bot.yml/updater.yml) ────────
for attempt in 1 2 3; do
  if git pull --rebase -X theirs origin main && git push; then
    echo "[run_betfair_sync] push OK (attempt $attempt)"
    exit 0
  fi
  echo "[run_betfair_sync] push attempt $attempt fallito, retry in 5s..." >&2
  sleep 5
done

echo "[run_betfair_sync] tutti i retry di push sono falliti" >&2
exit 4

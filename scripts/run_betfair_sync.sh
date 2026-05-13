#!/usr/bin/env bash
#
# run_betfair_sync.sh — wrapper eseguito da cron sul Raspberry Pi.
#
# v2 (self-healing): aggiunta Phase 0 di auto-cleanup che gestisce
# automaticamente ogni stato git rotto (rebase stuck, merge stuck,
# detached HEAD, index sporco). In passato lo script si bloccava
# per giorni quando il pull --rebase incontrava una sola anomalia.
# Ora il prossimo run del cron ripara da solo.
#
# Esempio cron:
#   */15 * * * * /home/pi/goalscan/scripts/run_betfair_sync.sh >> /var/log/betfair_sync.log 2>&1
#
# Prerequisiti:
#   1. Clone del repo in /home/pi/goalscan
#   2. .env nella root con BETFAIR_APP_KEY/USERNAME/PASSWORD/ENDPOINT
#   3. git configurato con credenziali per push
#   4. python3 + requests installati

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || { echo "[run_betfair_sync] impossibile cd in $REPO_DIR"; exit 1; }

TS="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo "=== [run_betfair_sync] start $TS ==="

# ── PHASE 0: AUTO-CLEANUP — ripara qualsiasi stato git rotto ───────────────
# Abort rebase/merge in corso (succede dopo un cron killato a metà o conflict)
git rebase --abort 2>/dev/null && echo "[cleanup] rebase abortito"
git merge --abort  2>/dev/null && echo "[cleanup] merge abortito"
git am --abort     2>/dev/null && echo "[cleanup] am abortito"
git cherry-pick --abort 2>/dev/null && echo "[cleanup] cherry-pick abortito"

# Se siamo in detached HEAD, torna su main
CURRENT_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo 'DETACHED')"
if [ "$CURRENT_BRANCH" != "main" ]; then
  echo "[cleanup] branch attuale: $CURRENT_BRANCH → checkout main"
  git checkout main 2>/dev/null || git checkout -B main origin/main 2>/dev/null || true
fi

# Rimuovi file lock orfani in .git/
if [ -f .git/index.lock ]; then
  echo "[cleanup] rimuovo .git/index.lock orfano"
  rm -f .git/index.lock
fi

# Reset hard al remote (perdiamo modifiche locali non committate;
# il sync le ricostruisce comunque). Sicuro: .env è gitignored e
# git clean -fd NON tocca file gitignored.
if ! git fetch origin main; then
  echo "[cleanup] git fetch fallito (rete?) — esco" >&2
  exit 2
fi
git reset --hard origin/main
git clean -fd

# Verifica che .env sia sopravvissuto (è gitignored ma sanity check)
if [ ! -f .env ]; then
  echo "[run_betfair_sync] FATAL: .env mancante dopo cleanup!" >&2
  echo "[run_betfair_sync] Ripristina .env (vedi CLAUDE.md §Sync Betfair)" >&2
  exit 3
fi

echo "[cleanup] stato git pulito · HEAD=$(git rev-parse --short HEAD)"

# ── PHASE 1: Carica .env ───────────────────────────────────────────────────
# Parser .env robusto (no 'source' → immune a quote non matchati)
while IFS= read -r _line || [ -n "${_line:-}" ]; do
  _stripped="${_line#"${_line%%[![:space:]]*}"}"
  [ -z "$_stripped" ] && continue
  case "$_stripped" in
    \#*) continue ;;
    *=*) : ;;
    *) continue ;;
  esac
  _key="${_stripped%%=*}"
  _val="${_stripped#*=}"
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
  exit 4
fi

# ── PHASE 2: Esegui betfair_sync.py ───────────────────────────────────────
if ! python3 betfair_sync.py; then
  echo "[run_betfair_sync] betfair_sync.py ha fallito (login Betfair? rete?)" >&2
  exit 5
fi

# ── PHASE 3: Commit & push con pattern cp+reset (no git stash quirks) ────
if [ ! -f docs/betfair_markets.json ]; then
  echo "[run_betfair_sync] betfair_markets.json non generato — esco" >&2
  exit 6
fi

# Snapshot fisico del file generato (bypassa qualsiasi quirk git)
cp docs/betfair_markets.json /tmp/betfair_markets.json

# Sync hard col remote
git fetch origin main
git reset --hard origin/main

# Ripristina il file generato
cp /tmp/betfair_markets.json docs/betfair_markets.json

git add docs/betfair_markets.json
if git diff --staged --quiet; then
  echo "[run_betfair_sync] nessuna modifica vs remote, skip commit"
  exit 0
fi

git -c user.name="goal-bot" \
    -c user.email="goal-bot@users.noreply.github.com" \
    commit -m "chore: betfair_markets.json $TS [pi-sync]"

# Push retry x5 con amend automatico (gestisce concorrenza con bot.yml/updater.yml)
for attempt in 1 2 3 4 5; do
  if git push origin main; then
    echo "[run_betfair_sync] push OK (attempt $attempt)"
    exit 0
  fi
  echo "[run_betfair_sync] push attempt $attempt fallito, rifetch + amend"
  git fetch origin main
  git reset --soft origin/main
  git add docs/betfair_markets.json
  git -c user.name="goal-bot" \
      -c user.email="goal-bot@users.noreply.github.com" \
      commit --amend -C HEAD --no-edit || true
  sleep $((attempt * 3))
done

echo "[run_betfair_sync] tutti i retry di push sono falliti" >&2
exit 7

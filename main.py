#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOAL BOT - Scanner Statistico Diretta-style
Architettura: API-Football + GitHub Actions + Telegram Alert
"""

import requests
import json
import os
from datetime import datetime, timedelta
import pytz
from telegram import Bot

# ==================== CONFIGURAZIONE ====================
def load_config():
    """Carica configurazione da config.json o variabili ambiente"""
    if os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    
    # Fallback per GitHub Actions (variabili ambiente)
    return {
        "api_football_key": os.getenv('API_FOOTBALL_KEY'),
        "telegram_bot_token": os.getenv('TELEGRAM_BOT_TOKEN'),
        "telegram_chat_id": os.getenv('TELEGRAM_CHAT_ID'),
        "goal_threshold": int(os.getenv('GOAL_THRESHOLD', 14)),
        "last_matches_count": int(os.getenv('LAST_MATCHES', 5)),
        "leagues_to_monitor": [135, 140, 78, 61, 39]
    }

# ==================== CACHE MANAGER ====================
def load_cache():
    """Carica cache da file JSON"""
    if os.path.exists('cache.json'):
        with open('cache.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"teams": {}, "last_update": None}

def save_cache(cache):
    """Salva cache su file JSON"""
    cache["last_update"] = datetime.now().isoformat()
    with open('cache.json', 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

# ==================== API FOOTBALL ====================
class APIFootballClient:
    """Client per API-Football con gestione errori e cache"""
    
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {
            'x-rapidapi-key': api_key,
            'x-rapidapi-host': 'v3.football.api-sports.io'
        }
    
    def _request(self, endpoint, params=None):
        """Esegue richiesta API con retry"""
        try:
            response = requests.get(
                f"{self.BASE_URL}{endpoint}",
                headers=self.headers,
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return data.get('response', [])
        except Exception as e:
            print(f"❌ Errore API: {e}")
            return []
    
    def get_fixtures_today(self, league_id, timezone='Europe/Rome'):
        """Ottiene partite di oggi per un campionato"""
        today = datetime.now(timezone=pytz.timezone(timezone)).strftime('%Y-%m-%d')
        return self._request('/fixtures', {
            'league': league_id,
            'season': datetime.now().year,
            'date': today
        })
    
    def get_team_last_matches(self, team_id, league_id, last=5):
        """Ottiene ultime N partite di una squadra in un campionato"""
        matches = self._request('/fixtures', {
            'team': team_id,
            'league': league_id,
            'season': datetime.now().year,
            'last': last
        })
        # Filtra solo partite giocate (status FT)
        return [m for m in matches if m.get('fixture', {}).get('status', {}).get('short') == 'FT']
    
    def get_leagues(self):
        """Ottiene lista campionati disponibili"""
        return self._request('/leagues', {'current': 'true'})

# ==================== CALCOLO STATISTICHE ====================
def calculate_team_goals_sum(matches):
    """
    Calcola somma goal fatti + subiti nelle partite fornite
    Returns: int (somma totale)
    """
    total = 0
    for match in matches:
        goals = match.get('goals', {})
        home = goals.get('home', 0) or 0
        away = goals.get('away', 0) or 0
        total += home + away
    return total

def analyze_match(fixture, api_client, cache, config):
    """
    Analizza una singola partita per il criterio >= 14 goal ultime 5
    Returns: dict con dati alert o None
    """
    teams_data = fixture.get('teams', {})
    home_team = teams_data.get('home', {})
    away_team = teams_data.get('away', {})
    
    home_id = home_team.get('id')
    away_id = away_team.get('id')
    league_id = fixture.get('league', {}).get('id')
    
    if not all([home_id, away_id, league_id]):
        return None
    
    # Check cache per evitare chiamate API duplicate
    cache_key_home = f"{home_id}_{league_id}"
    cache_key_away = f"{away_id}_{league_id}"
    
    teams_cache = cache.get('teams', {})
    
    # Recupera o calcola statistiche Casa
    if cache_key_home in teams_cache:
        home_sum = teams_cache[cache_key_home]['sum']
    else:
        home_matches = api_client.get_team_last_matches(home_id, league_id, config['last_matches_count'])
        home_sum = calculate_team_goals_sum(home_matches)
        teams_cache[cache_key_home] = {
            'sum': home_sum,
            'matches': len(home_matches),
            'updated': datetime.now().isoformat()
        }
    
    # Recupera o calcola statistiche Ospite
    if cache_key_away in teams_cache:
        away_sum = teams_cache[cache_key_away]['sum']
    else:
        away_matches = api_client.get_team_last_matches(away_id, league_id, config['last_matches_count'])
        away_sum = calculate_team_goals_sum(away_matches)
        teams_cache[cache_key_away] = {
            'sum': away_sum,
            'matches': len(away_matches),
            'updated': datetime.now().isoformat()
        }
    
    # Aggiorna cache
    cache['teams'] = teams_cache
    
    # Verifica criterio alert
    threshold = config['goal_threshold']
    if home_sum >= threshold and away_sum >= threshold:
        return {
            'home_team': home_team.get('name'),
            'away_team': away_team.get('name'),
            'home_sum': home_sum,
            'away_sum': away_sum,
            'combined': home_sum + away_sum,
            'league': fixture.get('league', {}).get('name'),
            'time': fixture.get('fixture', {}).get('date', '')[:16].replace('T', ' '),
            'status': fixture.get('fixture', {}).get('status', {}).get('long', 'NS')
        }
    
    return None

# ==================== GENERAZIONE TABELLA ====================
def generate_markdown_table(alerts):
    """
    Genera tabella Markdown divisa per orari
    Formato avanzato e sintetico per colpo d'occhio immediato
    """
    if not alerts:
        return "📊 **NESSUN ALERT OGGI**\n\nNessuna partita soddisfa i criteri (entrambe squadre >= 14 goal ultime 5)."
    
    # Raggruppa per fascia oraria
    time_groups = {}
    for alert in alerts:
        hour = alert['time'][:13] + ":00"  # Raggruppa per ora
        if hour not in time_groups:
            time_groups[hour] = []
        time_groups[hour].append(alert)
    
    # Costruisci markdown
    output = ["🔥 **GOAL ALERT - SCANNER STATISTICO** 🔥\n"]
    output.append(f"_Criterio: Ultime 5 partite (stessa competizione) - Somma GF+GS >= 14 per entrambe_")
    output.append(f"_Generato: {datetime.now().strftime('%d/%m/%Y %H:%M')}_\n")
    output.append("=" * 50)
    
    for time_slot, matches in sorted(time_groups.items()):
        output.append(f"\n🕒 **{time_slot}**\n")
        output.append("| Match | Form Casa | Form Ospite | TOT |")
        output.append("|-------|:---------:|:-----------:|:---:|")
        
        for m in matches:
            home_icon = "🔥" if m['home_sum'] >= 18 else "✅"
            away_icon = "🔥" if m['away_sum'] >= 18 else "✅"
            combined_icon = "🚨" if m['combined'] >= 35 else "⚡"
            
            output.append(
                f"| {m['home_team']} vs {m['away_team']} | "
                f"**{m['home_sum']}** {home_icon} | "
                f"**{m['away_sum']}** {away_icon} | "
                f"**{m['combined']}** {combined_icon} |"
            )
        
        output.append("")
    
    output.append("=" * 50)
    output.append("\n📈 *Legenda: 🔥 Super Form (>=18) | ✅ Form OK (>=14) | 🚨 Potenziale Altissimo*")
    
    return "\n".join(output)

# ==================== TELEGRAM ALERT ====================
def send_telegram_alert(message, config):
    """Invia alert su Telegram"""
    try:
        bot = Bot(token=config['telegram_bot_token'])
        bot.send_message(
            chat_id=config['telegram_chat_id'],
            text=message,
            parse_mode='Markdown'
        )
        print("✅ Alert inviato su Telegram")
        return True
    except Exception as e:
        print(f"❌ Errore Telegram: {e}")
        return False

# ==================== MAIN EXECUTION ====================
def main():
    """Funzione principale eseguita da GitHub Actions"""
    print("🚀 Avvio Goal Bot Scanner...")
    
    # Carica configurazioni
    config = load_config()
    cache = load_cache()
    
    # Inizializza client API
    api = APIFootballClient(config['api_football_key'])
    
    # Raccolgi tutti gli alert
    all_alerts = []
    
    # Scansiona tutti i campionati configurati
    for league_id in config.get('leagues_to_monitor', []):
        print(f"📊 Scansione campionato ID: {league_id}")
        
        fixtures = api.get_fixtures_today(league_id)
        
        for fixture in fixtures:
            result = analyze_match(fixture, api, cache, config)
            if result:
                all_alerts.append(result)
                print(f"✅ ALERT: {result['home_team']} vs {result['away_team']}")
    
    # Salva cache aggiornata
    save_cache(cache)
    
    # Genera e invia report
    if all_alerts:
        report = generate_markdown_table(all_alerts)
        send_telegram_alert(report, config)
        print(f"🎯 Trovati {len(all_alerts)} alert")
    else:
        print("📭 Nessun alert trovato oggi")
        # Opzionale: invia comunque report "nessun alert"
        # send_telegram_alert(generate_markdown_table([]), config)
    
    print("✅ Esecuzione completata")

if __name__ == "__main__":
    main()# Incolla qui il codice Python completo fornito prima

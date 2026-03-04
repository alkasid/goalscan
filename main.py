#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOAL BOT - Scanner Statistico
"""

import requests
import json
import os
from datetime import datetime
import pytz
from telegram import Bot

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)

def load_cache():
    cache_path = os.path.join(os.path.dirname(__file__), 'cache.json')
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    return {"teams": {}}

def save_cache(cache):
    cache_path = os.path.join(os.path.dirname(__file__), 'cache.json')
    cache["last_update"] = datetime.now().isoformat()
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

class APIFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key):
        self.headers = {
            'x-rapidapi-key': api_key,
            'x-rapidapi-host': 'v3.football.api-sports.io'
        }
    
    def _request(self, endpoint, params=None):
        try:
            response = requests.get(f"{self.BASE_URL}{endpoint}", headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            return response.json().get('response', [])
        except Exception as e:
            print(f"❌ API Error: {e}")
            return []
    
    def get_fixtures_today(self, league_id, timezone='Europe/Rome'):
        today = datetime.now(pytz.timezone(timezone)).strftime('%Y-%m-%d')
        return self._request('/fixtures', {'league': league_id, 'season': datetime.now().year, 'date': today})
    
    def get_team_last_matches(self, team_id, league_id, last=5):
        matches = self._request('/fixtures', {'team': team_id, 'league': league_id, 'season': datetime.now().year, 'last': last})
        return [m for m in matches if m.get('fixture', {}).get('status', {}).get('short') == 'FT']

def calculate_team_goals_sum(matches):
    total = 0
    for match in matches:
        goals = match.get('goals', {})
        total += (goals.get('home', 0) or 0) + (goals.get('away', 0) or 0)
    return total

def analyze_match(fixture, api_client, cache, config):
    teams_data = fixture.get('teams', {})
    home_team = teams_data.get('home', {})
    away_team = teams_data.get('away', {})
    
    home_id = home_team.get('id')
    away_id = away_team.get('id')
    league_id = fixture.get('league', {}).get('id')
    
    if not all([home_id, away_id, league_id]):
        return None
    
    teams_cache = cache.get('teams', {})
    cache_key_home = f"{home_id}_{league_id}"
    cache_key_away = f"{away_id}_{league_id}"
    
    if cache_key_home in teams_cache:
        home_sum = teams_cache[cache_key_home]['sum']
    else:
        home_matches = api_client.get_team_last_matches(home_id, league_id, config['last_matches_count'])
        home_sum = calculate_team_goals_sum(home_matches)
        teams_cache[cache_key_home] = {'sum': home_sum, 'matches': len(home_matches)}
    
    if cache_key_away in teams_cache:
        away_sum = teams_cache[cache_key_away]['sum']
    else:
        away_matches = api_client.get_team_last_matches(away_id, league_id, config['last_matches_count'])
        away_sum = calculate_team_goals_sum(away_matches)
        teams_cache[cache_key_away] = {'sum': away_sum, 'matches': len(away_matches)}
    
    cache['teams'] = teams_cache
    
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
        }
    return None

def generate_markdown_table(alerts):
    if not alerts:
        return "📊 **NESSUN ALERT OGGI**\n\nNessuna partita soddisfa i criteri (>= 14 goal ultime 5)."
    
    time_groups = {}
    for alert in alerts:
        hour = alert['time'][:13] + ":00"
        if hour not in time_groups:
            time_groups[hour] = []
        time_groups[hour].append(alert)
    
    output = ["🔥 **GOAL ALERT - SCANNER STATISTICO** 🔥\n"]
    output.append(f"_Criterio: Ultime 5 partite - Somma GF+GS >= 14 per entrambe_")
    output.append(f"_Generato: {datetime.now().strftime('%d/%m/%Y %H:%M')}_\n")
    output.append("=" * 50)
    
    for time_slot, matches in sorted(time_groups.items()):
        output.append(f"\n🕒 **{time_slot}**\n")
        output.append("| Match | Form Casa | Form Ospite | TOT |")
        output.append("|-------|:---------:|:-----------:|:---:|")
        
        for m in matches:
            home_icon = "🔥" if m['home_sum'] >= 18 else "✅"
            away_icon = "🔥" if m['away_sum'] >= 18 else "✅"
            output.append(f"| {m['home_team']} vs {m['away_team']} | **{m['home_sum']}** {home_icon} | **{m['away_sum']}** {away_icon} | **{m['combined']}** ⚡ |")
        output.append("")
    
    output.append("=" * 50)
    return "\n".join(output)

async def send_telegram_alert(message, config):
    try:
        bot = Bot(token=config['telegram_bot_token'])
        await bot.send_message(chat_id=config['telegram_chat_id'], text=message, parse_mode='Markdown')
        print("✅ Alert inviato")
        return True
    except Exception as e:
        print(f"❌ Telegram Error: {e}")
        return False

async def main():
    print("🚀 Avvio Goal Bot...")
    config = load_config()
    cache = load_cache()
    api = APIFootballClient(config['api_football_key'])
    
    all_alerts = []
    for league_id in config.get('leagues_to_monitor', []):
        print(f"📊 Scansione campionato: {league_id}")
        fixtures = api.get_fixtures_today(league_id)
        for fixture in fixtures:
            result = analyze_match(fixture, api, cache, config)
            if result:
                all_alerts.append(result)
    
    save_cache(cache)
    
    if all_alerts:
        report = generate_markdown_table(all_alerts)
        await send_telegram_alert(report, config)
        print(f"🎯 Trovati {len(all_alerts)} alert")
    else:
        print("📭 Nessun alert oggi")
    
    print("✅ Completato")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
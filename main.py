#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOAL BOT v3 - Debug Completo
"""

import requests, json, os, asyncio
from datetime import datetime
import pytz
from telegram import Bot

def load_config():
    with open('config.json', 'r', encoding='utf-8-sig') as f:
        return json.load(f)

def load_cache():
    if os.path.exists('cache.json'):
        with open('cache.json', 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    return {"teams": {}, "api_calls": 0}

def save_cache(cache):
    cache["last_update"] = datetime.now().isoformat()
    with open('cache.json', 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

class APIFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key, cache):
        self.headers = {'x-rapidapi-key': api_key, 'x-rapidapi-host': 'v3.football.api-sports.io'}
        self.cache = cache
    
    def _request(self, endpoint, params=None):
        try:
            self.cache['api_calls'] = self.cache.get('api_calls', 0) + 1
            if self.cache['api_calls'] > 100:
                print(f"⛔ STOP API: Limite 100 raggiunto")
                return []
            response = requests.get(f"{self.BASE_URL}{endpoint}", headers=self.headers, params=params, timeout=10)
            if response.status_code == 429:
                print("❌ API Rate Limit 429")
                return []
            return response.json().get('response', [])
        except Exception as e:
            print(f"❌ API Error: {e}")
            return []
    
    def get_fixtures_today(self, league_id):
        today = datetime.now(pytz.timezone('Europe/Rome')).strftime('%Y-%m-%d')
        return self._request('/fixtures', {'league': league_id, 'season': datetime.now().year, 'date': today})
    
    def get_team_last_matches(self, team_id, league_id, last=5):
        matches = self._request('/fixtures', {'team': team_id, 'league': league_id, 'season': datetime.now().year, 'last': last})
        return [m for m in matches if m.get('fixture', {}).get('status', {}).get('short') == 'FT']

def calculate_goals_sum(matches):
    total = 0
    for m in matches:
        goals = m.get('goals', {})
        total += (goals.get('home', 0) or 0) + (goals.get('away', 0) or 0)
    return total

async def main():
    print("🚀 GOAL BOT v3 - DEBUG MODE")
    config = load_config()
    cache = load_cache()
    api = APIFootballClient(config['api_football_key'], cache)
    
    alerts = []
    threshold = config['goal_threshold']
    
    for league_id in config.get('leagues_to_monitor', []):
        print(f"\n{'='*60}")
        print(f"📊 CAMPIONATO ID: {league_id}")
        print('='*60)
        
        fixtures = api.get_fixtures_today(league_id)
        print(f" Partite trovate oggi: {len(fixtures)}")
        
        for fixture in fixtures:
            home = fixture.get('teams', {}).get('home', {})
            away = fixture.get('teams', {}).get('away', {})
            home_name = home.get('name', '?')
            away_name = away.get('name', '?')
            home_id = home.get('id')
            away_id = away.get('id')
            
            print(f"\n⚽ {home_name} vs {away_name}")
            
            # Stats Casa
            home_matches = api.get_team_last_matches(home_id, league_id, 5)
            home_sum = calculate_goals_sum(home_matches)
            print(f"   🏠 {home_name}: {len(home_matches)} partite | Goal sum: {home_sum}")
            
            # Stats Ospite
            away_matches = api.get_team_last_matches(away_id, league_id, 5)
            away_sum = calculate_goals_sum(away_matches)
            print(f"   ✈️  {away_name}: {len(away_matches)} partite | Goal sum: {away_sum}")
            
            # Check criterio
            if home_sum >= threshold and away_sum >= threshold:
                print(f"   ✅ ALERT: {home_sum} + {away_sum} >= {threshold}")
                alerts.append({
                    'home': home_name, 'away': away_name,
                    'home_sum': home_sum, 'away_sum': away_sum,
                    'league': fixture.get('league', {}).get('name'),
                    'time': fixture.get('fixture', {}).get('date', '')[:16].replace('T', ' ')
                })
            else:
                print(f"   ❌ Scartata: {home_sum} o {away_sum} < {threshold}")
    
    save_cache(cache)
    
    # Report Telegram
    if alerts:
        msg = f"🔥 **ALERT: {len(alerts)} partite**\n\n"
        for a in alerts:
            msg += f"🕒 {a['time']} | {a['league']}\n{a['home']} ({a['home_sum']}🔥) vs {a['away']} ({a['away_sum']}🔥)\n\n"
    else:
        msg = f"📭 **Nessun alert oggi**\n\nAPI calls: {cache.get('api_calls', 0)}/100"
    
    print(f"\n{'='*60}")
    print(f"📊 TOTALE: {len(alerts)} alert | API: {cache.get('api_calls', 0)}/100")
    print('='*60)
    
    # Invia Telegram
    try:
        bot = Bot(token=config['telegram_bot_token'])
        await bot.send_message(chat_id=config['telegram_chat_id'], text=msg, parse_mode='Markdown')
        print("✅ Telegram inviato!")
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GOAL BOT v4 - Versione Corretta e Ottimizzata
"""

import requests, json, os, asyncio
from datetime import datetime
import pytz
from telegram import Bot

def load_config():
    with open('config.json', 'r', encoding='utf-8-sig') as f:
        return json.load(f)

class APIFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self, api_key):
        self.headers = {'x-rapidapi-key': api_key, 'x-rapidapi-host': 'v3.football.api-sports.io'}
        self.calls = 0
    
    def _request(self, endpoint, params=None):
        try:
            self.calls += 1
            if self.calls > 95:
                print(f"⛔ Limite API: {self.calls}/100")
                return []
            response = requests.get(f"{self.BASE_URL}{endpoint}", headers=self.headers, params=params, timeout=15)
            if response.status_code == 429:
                print("❌ Rate Limit 429")
                return []
            data = response.json()
            return data.get('response', [])
        except Exception as e:
            print(f"❌ Error: {e}")
            return []
    
    def get_fixtures_today(self, league_id):
        today = datetime.now(pytz.timezone('Europe/Rome')).strftime('%Y-%m-%d')
        return self._request('/fixtures', {'league': league_id, 'season': 2025, 'date': today})
    
    def get_team_matches(self, team_id, league_id, last=5):
        fixtures = self._request('/fixtures', {'team': team_id, 'league': league_id, 'season': 2025, 'last': last})
        return [f for f in fixtures if f.get('fixture', {}).get('status', {}).get('short') == 'FT']

def calc_goals(matches):
    return sum((m['goals']['home'] or 0) + (m['goals']['away'] or 0) for m in matches)

async def main():
    print("🚀 GOAL BOT v4 - START")
    config = load_config()
    api = APIFootballClient(config['api_football_key'])
    
    alerts = []
    leagues = config.get('leagues_to_monitor', [140])
    threshold = config['goal_threshold']
    
    print(f"📋 Campionati da scansionare: {len(leagues)}")
    
    for lid in leagues:
        print(f"\n📊 League {lid}...")
        fixtures = api.get_fixtures_today(lid)
        print(f"   Trovate {len(fixtures)} partite")
        
        for fx in fixtures:
            home = fx['teams']['home']
            away = fx['teams']['away']
            h_name, h_id = home['name'], home['id']
            a_name, a_id = away['name'], away['id']
            
            # Stats ultime 5
            h_matches = api.get_team_matches(h_id, lid, 5)
            a_matches = api.get_team_matches(a_id, lid, 5)
            
            h_sum = calc_goals(h_matches)
            a_sum = calc_goals(a_matches)
            
            print(f"   ⚽ {h_name} vs {a_name}: {h_sum} | {a_sum}")
            
            if h_sum >= threshold and a_sum >= threshold:
                alerts.append({
                    'h': h_name, 'a': a_name,
                    'hs': h_sum, 'as': a_sum,
                    'time': fx['fixture']['date'][:16].replace('T', ' '),
                    'league': fx['league']['name']
                })
                print(f"   ✅ ALERT!")
    
    # Telegram
    msg = f"🔥 **GOAL ALERT - {len(alerts)} match**\n\n" if alerts else "📭 **Nessun alert oggi**\n\n"
    for a in alerts:
        msg += f"🕒 {a['time']} | {a['league']}\n{a['h']} ({a['hs']}🔥) vs {a['a']} ({a['as']}🔥)\n\n"
    
    msg += f"\n_API calls: {api.calls}/100_"
    
    print(f"\n📊 Totale: {api.calls} API calls")
    
    try:
        bot = Bot(token=config['telegram_bot_token'])
        await bot.send_message(chat_id=config['telegram_chat_id'], text=msg, parse_mode='Markdown')
        print("✅ Telegram inviato!")
    except Exception as e:
        print(f"❌ Telegram: {e}")

if __name__ == "__main__":
    asyncio.run(main())
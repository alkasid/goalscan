#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests, json, asyncio
from datetime import datetime
import pytz
from telegram import Bot

def load_config():
    with open('config.json', 'r', encoding='utf-8-sig') as f:
        return json.load(f)

class API:
    def __init__(self, key):
        self.headers = {'x-rapidapi-key': key, 'x-rapidapi-host': 'v3.football.api-sports.io'}
        self.calls = 0
    
    def get(self, url, params={}):
        try:
            self.calls += 1
            if self.calls > 95:
                return []
            r = requests.get(f'https://v3.football.api-sports.io{url}', headers=self.headers, params=params, timeout=15)
            return r.json().get('response', []) if r.status_code == 200 else []
        except:
            return []
    
    def today_fixtures(self, league):
        # Usa data Italia e stagione 2025
        today = datetime.now(pytz.timezone('Europe/Rome')).strftime('%Y-%m-%d')
        return self.get('/fixtures', {'league': league, 'season': 2025, 'date': today})
    
    def team_matches(self, team, league):
        return [m for m in self.get('/fixtures', {'team': team, 'league': league, 'season': 2025, 'last': 5}) 
                if m.get('fixture', {}).get('status', {}).get('short') == 'FT']

def goals_sum(matches):
    return sum((m['goals']['home'] or 0) + (m['goals']['away'] or 0) for m in matches)

async def main():
    cfg = load_config()
    api = API(cfg['api_football_key'])
    alerts = []
    th = cfg['goal_threshold']
    
    print(f"🚀 Start - Data: {datetime.now(pytz.timezone('Europe/Rome')).strftime('%Y-%m-%d')}")
    
    for lid in cfg.get('leagues_to_monitor', []):
        fixtures = api.today_fixtures(lid)
        print(f"\n📊 League {lid}: {len(fixtures)} partite")
        
        for fx in fixtures:
            h, a = fx['teams']['home'], fx['teams']['away']
            hs = goals_sum(api.team_matches(h['id'], lid))
            as_ = goals_sum(api.team_matches(a['id'], lid))
            
            print(f"  {h['name']} ({hs}) vs {a['name']} ({as_})")
            
            if hs >= th and as_ >= th:
                alerts.append(f"🕒 {fx['fixture']['date'][:16].replace('T',' ')}\n{h['name']} ({hs}🔥) vs {a['name']} ({as_}🔥)\n{fx['league']['name']}\n")
    
    msg = f"🔥 **ALERT: {len(alerts)}**\n\n" + "\n".join(alerts) if alerts else "📭 **Nessun alert**\n"
    msg += f"\n_API: {api.calls}/100_"
    
    try:
        await Bot(token=cfg['telegram_bot_token']).send_message(chat_id=cfg['telegram_chat_id'], text=msg, parse_mode='Markdown')
        print("✅ Telegram OK")
    except Exception as e:
        print(f"❌ Telegram: {e}")

asyncio.run(main())
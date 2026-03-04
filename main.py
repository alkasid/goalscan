#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import requests, json, asyncio
from datetime import datetime
import pytz
from telegram import Bot

def load_config():
    with open('config.json', 'r', encoding='utf-8-sig') as f:
        return json.load(f)

async def main():
    cfg = load_config()
    api_key = cfg['api_football_key']
    alerts = []
    th = cfg['goal_threshold']
    calls = 0
    
    headers = {'x-rapidapi-key': api_key, 'x-rapidapi-host': 'v3.football.api-sports.io'}
    today = datetime.now(pytz.timezone('Europe/Rome')).strftime('%Y-%m-%d')
    
    print(f"🚀 Data: {today}")
    print(f"🔑 API Key: {api_key[:10]}...")
    
    # TEST API CONNECTION
    print("\n📡 Test connessione API...")
    try:
        r = requests.get('https://v3.football.api-sports.io/fixtures/league', 
                        headers=headers, params={'league': 140, 'season': 2025}, timeout=10)
        print(f"Status: {r.status_code}")
        if r.status_code == 401:
            print("❌ API Key INVALIDA!")
            msg = "❌ **Errore API Key**\n\nLa chiave non è valida o è scaduta."
        elif r.status_code == 429:
            print("❌ Rate Limit raggiunto")
            msg = "❌ **Rate Limit**\n\nHai superato le 100 chiamate giornaliere."
        else:
            data = r.json()
            print(f"Response: {data}")
    except Exception as e:
        print(f"❌ Error: {e}")
        msg = f"❌ **Errore connessione**: {e}"
        await Bot(token=cfg['telegram_bot_token']).send_message(chat_id=cfg['telegram_chat_id'], text=msg, parse_mode='Markdown')
        return
    
    # SCAN LEAGUES
    print("\n📊 Scansione campionati...")
    for lid in cfg.get('leagues_to_monitor', [])[:5]:  # Solo primi 5 per test
        calls += 1
        try:
            r = requests.get('https://v3.football.api-sports.io/fixtures',
                           headers=headers, params={'league': lid, 'season': 2025, 'date': today}, timeout=10)
            if r.status_code == 200:
                fixtures = r.json().get('response', [])
                print(f"\n📊 League {lid}: {len(fixtures)} partite")
                
                for fx in fixtures:
                    h, a = fx['teams']['home'], fx['teams']['away']
                    print(f"  ⚽ {h['name']} vs {a['name']}")
                    
                    # Stats (altre chiamate API)
                    calls += 1
                    hm = requests.get('https://v3.football.api-sports.io/fixtures',
                                    headers=headers, params={'team': h['id'], 'league': lid, 'season': 2025, 'last': 5}, timeout=10)
                    am = requests.get('https://v3.football.api-sports.io/fixtures',
                                    headers=headers, params={'team': a['id'], 'league': lid, 'season': 2025, 'last': 5}, timeout=10)
                    
                    h_matches = [m for m in hm.json().get('response', []) if m.get('fixture',{}).get('status',{}).get('short')=='FT']
                    a_matches = [m for m in am.json().get('response', []) if m.get('fixture',{}).get('status',{}).get('short')=='FT']
                    
                    hs = sum((m['goals']['home'] or 0) + (m['goals']['away'] or 0) for m in h_matches)
                    as_ = sum((m['goals']['home'] or 0) + (m['goals']['away'] or 0) for m in a_matches)
                    
                    print(f"     {h['name']}: {hs} | {a['name']}: {as_}")
                    
                    if hs >= th and as_ >= th:
                        alerts.append(f"{h['name']} ({hs}) vs {a['name']} ({as_})")
            else:
                print(f"❌ League {lid}: Error {r.status_code}")
        except Exception as e:
            print(f"❌ League {lid}: {e}")
    
    msg = f"🔥 **Alert: {len(alerts)}**\n\n" + "\n".join(alerts) if alerts else "📭 **Nessun alert**\n"
    msg += f"\n_API calls: {calls}/100_"
    
    try:
        await Bot(token=cfg['telegram_bot_token']).send_message(chat_id=cfg['telegram_chat_id'], text=msg, parse_mode='Markdown')
        print("✅ Telegram OK")
    except Exception as e:
        print(f"❌ Telegram: {e}")

asyncio.run(main())
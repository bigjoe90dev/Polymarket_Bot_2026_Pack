#!/usr/bin/env python3
"""
Discovery script for real 1H and 4H Up or Down crypto markets.

This script finds real Polymarket markets by:
1. Generating candidate slugs from known patterns
2. Fetching market details via Gamma API (by slug)
3. Filtering for ~60 min (1H) or ~240 min (4H) resolution windows

Output: JSON file with discovered markets + proof fields

Usage:
    python scripts/discover_real_markets.py [--asset BTC] [--days 30]
"""

import argparse
import requests
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Asset configurations
ASSET_CONFIGS = {
    'BTC': {
        'prefixes': ['bitcoin', 'btc'],
        'monthly_glob': 'bitcoin-up-or-down-{month}-{day}',
    },
    'ETH': {
        'prefixes': ['ethereum', 'eth'],
        'monthly_glob': 'ethereum-up-or-down-{month}-{day}',
    },
    'SOL': {
        'prefixes': ['solana', 'sol'],
        'monthly_glob': 'solana-up-or-down-{month}-{day}',
    },
    'XRP': {
        'prefixes': ['xrp'],
        'monthly_glob': 'xrp-up-or-down-{month}-{day}',
    },
}

MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 
          'july', 'august', 'september', 'october', 'november', 'december']

GAMMA_API = 'https://gamma-api.polymarket.com/markets'

def generate_candidate_slugs(asset: str, days: int = 30) -> list:
    """Generate candidate slugs for the given asset and number of days."""
    slugs = []
    today = datetime.now(timezone.utc)
    
    # Get current month for slug generation
    for day_offset in range(0, days):
        day = today + timedelta(days=day_offset)
        month_name = MONTHS[day.month - 1]
        
        # Generate hour slots: typically 8AM-11PM ET for crypto markets
        # This translates to 1PM-4AM UTC next day
        for hour in range(8, 24):  # 8AM to 11PM ET
            # 1H market pattern
            slug_1h = f'{asset.lower()}-up-or-down-{month_name}-{day.day}-{hour}pm-et'
            slugs.append(slug_1h)
    
    return slugs

def fetch_market_by_slug(slug: str) -> dict:
    """Fetch market details by slug."""
    try:
        url = f'{GAMMA_API}?slug={slug}'
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
    except:
        pass
    return None

def parse_market_duration(market: dict) -> tuple:
    """Parse market start and end times, return duration in minutes."""
    start_time = market.get('startTime', market.get('eventStartTime', ''))
    end = market.get('endDate', '')
    
    if not start_time or not end:
        return None, None
    
    try:
        # Parse start time
        start_time = start_time.replace('Z', '+00:00')
        if '.' in start_time:
            start_time = start_time.split('.')[0] + '+00:00'
        s = datetime.fromisoformat(start_time)
        
        # Parse end time
        end = end.replace('Z', '+00:00')
        if '.' in end:
            end = end.split('.')[0] + '+00:00'
        e = datetime.fromisoformat(end)
        
        dur = (e - s).total_seconds() / 60
        return dur, e
    except:
        return None, None

def discover_markets(asset: str, days: int = 30) -> dict:
    """Discover 1H and 4H markets for the given asset."""
    slugs = generate_candidate_slugs(asset, days)
    
    one_hour_markets = []
    four_hour_markets = []
    total_found = 0
    total_checked = 0
    
    print(f'Discovering {asset} markets for {days} days...')
    print(f'Testing {len(slugs)} candidate slugs...')
    
    for i, slug in enumerate(slugs):
        total_checked += 1
        market = fetch_market_by_slug(slug)
        
        if market:
            total_found += 1
            dur, end_dt = parse_market_duration(market)
            
            if dur is None:
                continue
            
            # Parse token IDs
            token_ids = json.loads(market.get('clobTokenIds', '[]'))
            
            market_data = {
                'market_title': market.get('question'),
                'slug': slug,
                'condition_id': market.get('conditionId'),
                'end_date': end_dt.isoformat() if end_dt else None,
                'yes_token_id': token_ids[0] if len(token_ids) > 0 else None,
                'no_token_id': token_ids[1] if len(token_ids) > 1 else None,
                'duration_min': dur,
            }
            
            # Filter for 1H (50-70 min) or 4H (230-250 min)
            if 50 < dur < 70:
                one_hour_markets.append(market_data)
            elif 230 < dur < 250:
                four_hour_markets.append(market_data)
        
        # Rate limiting
        if i > 0 and i % 20 == 0:
            time.sleep(0.3)
        
        # Progress
        if i > 0 and i % 100 == 0:
            print(f'  Progress: {i}/{len(slugs)} - Found {len(one_hour_markets)} 1H, {len(four_hour_markets)} 4H')
    
    # Sort by end date
    one_hour_markets.sort(key=lambda x: x['end_date'])
    four_hour_markets.sort(key=lambda x: x['end_date'])
    
    return {
        'asset': asset,
        'one_hour': one_hour_markets,
        'four_hour': four_hour_markets,
        'stats': {
            'total_checked': total_checked,
            'total_found': total_found,
            'one_hour_count': len(one_hour_markets),
            'four_hour_count': len(four_hour_markets),
        }
    }

def main():
    parser = argparse.ArgumentParser(description='Discover real 1H/4H crypto markets')
    parser.add_argument('--asset', default='BTC', choices=['BTC', 'ETH', 'SOL', 'XRP'])
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--all', action='store_true', help='Discover for all assets')
    args = parser.parse_args()
    
    print('=' * 70)
    print('REAL MARKET DISCOVERY')
    print('=' * 70)
    
    results = {}
    
    assets = ['BTC', 'ETH', 'SOL', 'XRP'] if args.all else [args.asset]
    
    for asset in assets:
        result = discover_markets(asset, args.days)
        results[asset] = result
        
        print(f'\n{asset} Results:')
        print(f'  1H markets: {len(result[\"one_hour\"])}')
        print(f'  4H markets: {len(result[\"four_hour\"])}')
    
    # Save to JSON
    output_path = 'data/discovered_real_markets.json'
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f'\nSaved to {output_path}')
    
    # Print proof for first asset
    print('\n' + '=' * 70)
    print('PROOF: First 10 1H Markets')
    print('=' * 70)
    
    for asset in assets:
        markets = results[asset]['one_hour'][:10]
        if markets:
            print(f'\n{asset}:')
            for m in markets:
                print(json.dumps({
                    'market_title': m['market_title'],
                    'slug': m['slug'],
                    'condition_id': m['condition_id'],
                    'end_date': m['end_date'],
                    'yes_token_id': m['yes_token_id'],
                    'no_token_id': m['no_token_id'],
                }, indent=2))
                print()
    
    return results

if __name__ == '__main__':
    main()

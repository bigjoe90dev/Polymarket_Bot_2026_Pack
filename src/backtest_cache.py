"""
Backtest Data Caching Module
============================
Disk caching for historical market data and price timeseries.

Provides:
- Market list caching (JSON)
- Token price history caching (CSV for efficiency)
- Cache management (clear, check, load)
"""

import os
import json
import csv
from datetime import datetime
from typing import Optional, Dict, List, Any

CACHE_DIR = "data/backtest_cache"


def ensure_cache_dir():
    """Ensure cache directory exists."""
    os.makedirs(CACHE_DIR, exist_ok=True)


def get_market_cache_path(days: int) -> str:
    """Get path for cached market list."""
    ensure_cache_dir()
    return f"{CACHE_DIR}/markets_{days}days.json"


def get_timeseries_cache_path(token_id: str, start: int, end: int) -> str:
    """Get path for cached timeseries data."""
    ensure_cache_dir()
    # Use first 16 chars of token_id for brevity
    short_token = token_id[:16] if token_id else "unknown"
    return f"{CACHE_DIR}/ts_{short_token}_{start}_{end}.csv"


def load_market_cache(days: int) -> Optional[Dict]:
    """Load cached market list if exists and not expired."""
    path = get_market_cache_path(days)
    if not os.path.exists(path):
        return None
    
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        
        # Check if cache is stale (24 hours)
        fetched_at = data.get('fetched_at', '')
        if fetched_at:
            try:
                fetched_dt = datetime.fromisoformat(fetched_at)
                age_hours = (datetime.now() - fetched_dt).total_seconds() / 3600
                if age_hours > 24:
                    print(f"   Cache stale ({age_hours:.1f}h old), refreshing...")
                    return None
            except:
                pass
        
        return data
    except Exception as e:
        print(f"   Warning: Failed to load cache: {e}")
        return None


def save_market_cache(days: int, markets: List[Dict], assets: List[str]):
    """Save market list to cache."""
    path = get_market_cache_path(days)
    data = {
        'fetched_at': datetime.now().isoformat(),
        'days': days,
        'assets': assets,
        'num_markets': len(markets),
        'markets': markets
    }
    
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"   Cached {len(markets)} markets to {path}")
    except Exception as e:
        print(f"   Warning: Failed to save cache: {e}")


def load_timeseries_cache(token_id: str, start: int, end: int) -> Optional[List[Dict]]:
    """Load cached timeseries data."""
    path = get_timeseries_cache_path(token_id, start, end)
    if not os.path.exists(path):
        return None
    
    try:
        prices = []
        with open(path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                prices.append({
                    'timestamp': int(row['timestamp']),
                    'price': float(row['price']),
                    'side': row.get('side', 'unknown')
                })
        return prices
    except Exception as e:
        print(f"   Warning: Failed to load timeseries cache: {e}")
        return None


def save_timeseries_cache(token_id: str, start: int, end: int, prices: List[Dict]):
    """Save timeseries data to cache."""
    path = get_timeseries_cache_path(token_id, start, end)
    
    try:
        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['timestamp', 'price', 'side'])
            writer.writeheader()
            for p in prices:
                writer.writerow({
                    'timestamp': p.get('timestamp', 0),
                    'price': p.get('price', 0),
                    'side': p.get('side', 'unknown')
                })
    except Exception as e:
        print(f"   Warning: Failed to save timeseries cache: {e}")


def clear_all_cache():
    """Clear all backtest cache files."""
    ensure_cache_dir()
    count = 0
    for f in os.listdir(CACHE_DIR):
        if f.startswith('markets_') or f.startswith('ts_'):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
                count += 1
            except:
                pass
    print(f"Cleared {count} cache files from {CACHE_DIR}")


def get_cache_stats() -> Dict:
    """Get cache statistics."""
    ensure_cache_dir()
    stats = {
        'market_caches': 0,
        'timeseries_caches': 0,
        'total_size_mb': 0
    }
    
    total_size = 0
    for f in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, f)
        if os.path.isfile(path):
            total_size += os.path.getsize(path)
            if f.startswith('markets_'):
                stats['market_caches'] += 1
            elif f.startswith('ts_'):
                stats['timeseries_caches'] += 1
    
    stats['total_size_mb'] = total_size / (1024 * 1024)
    return stats

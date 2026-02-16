"""
Backtest Historical Market Data Fetcher
========================================
Fetches historical markets and price timeseries from Polymarket API.

Provides:
- Historical market fetching with pagination
- Timeseries price data fetching
- Filtering for 1H Up/Down crypto markets
- Synthetic data generation for backtesting
"""

import time
import random
import requests
import re
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta

# Gamma API endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HOST = "https://clob.polymarket.com"


def is_1h_crypto_up_down(market_title: str, assets: List[str]) -> bool:
    """Check if market is 1H timeframe "Up or Down" crypto market.
    
    Args:
        market_title: Market question/title
        assets: List of allowed assets (e.g., ['BTC', 'ETH'])
    
    Returns:
        True if market matches criteria
    """
    if not market_title:
        return False
    
    title_lower = market_title.lower()
    
    # Must be crypto
    is_crypto = False
    for asset in assets:
        asset_lower = asset.lower()
        # Handle common aliases
        aliases = {
            'btc': ['bitcoin', 'btc'],
            'eth': ['ethereum', 'eth'],
            'sol': ['solana', 'sol'],
            'xrp': ['xrp', 'ripple']
        }
        if asset_lower in aliases:
            is_crypto = any(a in title_lower for a in aliases[asset_lower])
        else:
            is_crypto = asset_lower in title_lower
        if is_crypto:
            break
    
    if not is_crypto:
        return False
    
    # Must be Up or Down format
    if "up or down" not in title_lower and "up/down" not in title_lower:
        return False
    
    # Must have 1H timeframe indicators
    timeframe_indicators = [
        "1h", "1 hour", "1 hr", "60 min", "60minute",
        "one hour", "hourly"
    ]
    has_timeframe = any(t in title_lower for t in timeframe_indicators)
    
    # Also check for patterns like "in 1 hour"
    if not has_timeframe:
        if re.search(r'in\s+1\s+hour', title_lower):
            has_timeframe = True
    
    return has_timeframe


def fetch_historical_markets(
    lookback_days: int,
    assets: List[str],
    clear_cache: bool = False
) -> List[Dict]:
    """Fetch historical markets with caching.
    
    Args:
        lookback_days: Number of days of history to fetch
        assets: List of assets to filter (e.g., ['BTC'])
        clear_cache: Force refresh cached data
    
    Returns:
        List of market dictionaries
    """
    from src.backtest_cache import load_market_cache, save_market_cache
    
    # Check cache first
    if not clear_cache:
        cached = load_market_cache(lookback_days)
        if cached:
            markets = cached.get('markets', [])
            print(f"   Loaded {len(markets)} markets from cache")
            return markets
    
    print(f"   Fetching markets from Polymarket API...")
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days)
    
    # Fetch closed markets (resolved) within date range
    all_markets = []
    cursor = None
    page = 0
    max_pages = 50  # Safety limit
    
    while page < max_pages:
        page += 1
        try:
            params = {
                "closed": "true",
                "limit": 100,
                "active": "false",  # Only resolved markets
            }
            if cursor:
                params["cursor"] = cursor
            
            resp = requests.get(GAMMA_API_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            if isinstance(data, dict):
                markets_page = data.get('data', [])
                cursor = data.get('next_cursor')
            else:
                markets_page = data
                cursor = None
            
            if not markets_page:
                break
            
            # Process each market
            for m in markets_page:
                # Parse end date
                end_date_str = m.get('endDate') or m.get('end_date_iso') or m.get('end_date')
                if not end_date_str:
                    continue
                
                try:
                    # Try to parse the end date
                    if 'T' in end_date_str:
                        market_end = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    else:
                        # Try parsing as date string
                        market_end = datetime.strptime(end_date_str, '%Y-%m-%d')
                    
                    # Skip if outside our date range
                    if market_end < start_date:
                        continue
                    if market_end > end_date:
                        continue
                except:
                    # If we can't parse, include it anyway
                    pass
                
                # Get title
                title = m.get('question', '')
                if not title:
                    continue
                
                # Check if it matches our criteria
                if not is_1h_crypto_up_down(title, assets):
                    continue
                
                # Extract token IDs
                clob_token_ids = m.get('clobTokenIds', '')
                no_clob_token_ids = m.get('noClobTokenIds', '')
                
                try:
                    yes_tokens = eval(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                    no_tokens = eval(no_clob_token_ids) if isinstance(no_clob_token_ids, str) else no_clob_token_ids
                except:
                    yes_tokens = []
                    no_tokens = []
                
                if not yes_tokens or not no_tokens:
                    continue
                
                # Get resolution
                resolution = m.get('resolution') or m.get('result')
                
                # Get current prices (if available)
                yes_price = m.get('yesPrice', 0)
                no_price = m.get('noPrice', 0)
                
                market_data = {
                    'condition_id': m.get('conditionId', m.get('condition_id', '')),
                    'question': title,
                    'yes_token_id': yes_tokens[0] if yes_tokens else '',
                    'no_token_id': no_tokens[0] if no_tokens else '',
                    'yes_price': float(yes_price) if yes_price else 0,
                    'no_price': float(no_price) if no_price else 0,
                    'end_date': end_date_str,
                    'resolution': resolution,
                    'volume': m.get('volume', 0),
                    'liquidity': m.get('liquidity', 0),
                }
                all_markets.append(market_data)
            
            print(f"   Page {page}: processed {len(markets_page)} markets, total filtered: {len(all_markets)}")
            
            if not cursor:
                break
                
        except requests.exceptions.RequestException as e:
            print(f"   Error fetching page {page}: {e}")
            break
        except Exception as e:
            print(f"   Error processing page {page}: {e}")
            break
    
    # Save to cache
    save_market_cache(lookback_days, all_markets, assets)
    
    print(f"   Found {len(all_markets)} markets matching criteria")
    return all_markets


def fetch_token_timeseries(
    token_id: str,
    start_time: int,
    end_time: int,
    clear_cache: bool = False
) -> List[Dict]:
    """Fetch price history for a token.
    
    Uses the CLOB API to get price history.
    
    Args:
        token_id: Token ID to fetch
        start_time: Start timestamp (Unix)
        end_time: End timestamp (Unix)
        clear_cache: Force refresh cached data
    
    Returns:
        List of price dictionaries with timestamp, price, side
    """
    from src.backtest_cache import load_timeseries_cache, save_timeseries_cache
    
    # Check cache
    if not clear_cache:
        cached = load_timeseries_cache(token_id, start_time, end_time)
        if cached:
            return cached
    
    # Try to fetch from CLOB API
    prices = []
    
    try:
        # Use the price history endpoint
        url = f"{CLOB_HOST}/price-history"
        params = {
            "token_id": token_id,
            "start_time": start_time,
            "end_time": end_time,
            "bucket": "1m"  # 1-minute buckets
        }
        
        resp = requests.get(url, params=params, timeout=30)
        
        if resp.status_code == 200:
            data = resp.json()
            history = data.get('history', [])
            
            for item in history:
                prices.append({
                    'timestamp': item.get('t', 0),
                    'price': float(item.get('p', 0)),
                    'side': item.get('side', 'unknown')
                })
            
            # Cache results
            if prices:
                save_timeseries_cache(token_id, start_time, end_time, prices)
            
            return prices
    except Exception as e:
        print(f"   Warning: Failed to fetch timeseries for {token_id[:16]}: {e}")
    
    # If API fails, return empty list
    return prices


def fetch_market_timeseries(
    market: Dict,
    clear_cache: bool = False
) -> Tuple[List[Dict], List[Dict]]:
    """Fetch timeseries for both YES and NO tokens of a market.
    
    Args:
        market: Market dictionary with yes_token_id, no_token_id, end_date
        clear_cache: Force refresh
    
    Returns:
        Tuple of (yes_prices, no_prices)
    """
    # Parse end date to get timestamp range
    end_date_str = market.get('end_date', '')
    try:
        if end_date_str:
            if 'T' in end_date_str:
                end_dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            else:
                end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_time = int(end_dt.timestamp())
            # Start 1 day before end
            start_time = end_time - (24 * 60 * 60)
        else:
            # Default to last 24 hours
            end_time = int(datetime.now().timestamp())
            start_time = end_time - (24 * 60 * 60)
    except:
        end_time = int(datetime.now().timestamp())
        start_time = end_time - (24 * 60 * 60)
    
    yes_token = market.get('yes_token_id', '')
    no_token = market.get('no_token_id', '')
    
    yes_prices = []
    no_prices = []
    
    if yes_token:
        yes_prices = fetch_token_timeseries(yes_token, start_time, end_time, clear_cache)
    
    if no_token:
        no_prices = fetch_token_timeseries(no_token, start_time, end_time, clear_cache)
    
    return yes_prices, no_prices


# =============================================================================
# SYNTHETIC DATA GENERATOR
# =============================================================================

def generate_synthetic_markets(
    num_markets: int = 50,
    assets: List[str] = None,
    days_back: int = 90,
    random_seed: int = 42
) -> List[Dict]:
    """Generate synthetic 1H Up/Down crypto markets for backtesting.
    
    Since real 1H Up/Down markets don't exist on Polymarket currently,
    this generates realistic synthetic data to test the strategy.
    
    Args:
        num_markets: Number of markets to generate
        assets: List of assets (BTC, ETH, etc.)
        days_back: How many days back to generate markets
        random_seed: Seed for reproducible results
    
    Returns:
        List of synthetic market dictionaries
    """
    if assets is None:
        assets = ['BTC']
    
    rng = random.Random(random_seed)
    markets = []
    
    # Generate markets spread across the date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    for i in range(num_markets):
        # Random end date within range (each market resolves in ~1 hour windows)
        days_offset = rng.uniform(0, days_back)
        market_end = start_date + timedelta(days=days_offset)
        
        # Round to nearest hour for 1H markets
        minute = market_end.minute
        hour = market_end.hour
        # Round to hour
        if minute >= 30:
            hour = (hour + 1) % 24
        market_end = market_end.replace(minute=0, second=0, microsecond=0)
        
        asset = rng.choice(assets)
        
        market = {
            'condition_id': f"synthetic_{i:04d}_{asset.lower()}",
            'question': f"Will {asset} be up or down in the next 1 hour?",
            'yes_token_id': f"yes_token_{i:04d}_{asset.lower()}",
            'no_token_id': f"no_token_{i:04d}_{asset.lower()}",
            'yes_price': 0.5,  # Start at even odds
            'no_price': 0.5,
            'end_date': market_end.isoformat() + 'Z',
            'resolution': None,
            'volume': rng.uniform(1000, 50000),
            'liquidity': rng.uniform(500, 10000),
            'asset': asset,
            'is_synthetic': True  # Flag to indicate synthetic data
        }
        markets.append(market)
    
    return markets


def generate_synthetic_timeseries(
    market: Dict,
    duration_hours: float = 1.0,
    points_per_minute: int = 6,  # 10-second intervals
    random_seed: int = 42,
    volatility: float = 0.02,
    trend_bias: float = None
) -> Tuple[List[Dict], List[Dict]]:
    """Generate synthetic price timeseries for a market.
    
    Creates realistic OHLC-like data with:
    - Random walk behavior
    - Occasional trends
    - Choppy (non-trending) periods
    - Volatility clusters
    
    Args:
        market: Market dictionary
        duration_hours: How long the market runs (typically 1 hour)
        points_per_minute: Data points per minute
        random_seed: Seed for reproducible results
        volatility: Price volatility (0.02 = 2% moves)
        trend_bias: Bias towards up (positive) or down (negative).
                   If None, will be randomly chosen per market.
    
    Returns:
        Tuple of (yes_prices, no_prices)
    """
    rng = random.Random(random_seed)
    
    # Parse end date
    end_date_str = market.get('end_date', '')
    try:
        if end_date_str:
            end_dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            end_time = int(end_dt.timestamp())
        else:
            end_time = int(datetime.now().timestamp())
    except:
        end_time = int(datetime.now().timestamp())
    
    start_time = end_time - int(duration_hours * 3600)
    
    # Number of points
    num_points = int(duration_hours * 60 * points_per_minute)
    interval = int(60 / points_per_minute)  # seconds between points
    
    # If no trend bias provided, randomly choose one per market
    # 30% trending up, 30% trending down, 40% choppy
    if trend_bias is None:
        r = rng.random()
        if r < 0.3:
            trend_bias = 0.03  # Up trend
        elif r < 0.6:
            trend_bias = -0.03  # Down trend
        else:
            trend_bias = 0.0  # Choppy
    
    # Starting price (around 0.5 for binary)
    yes_price = 0.5
    no_price = 0.5
    
    yes_prices = []
    no_prices = []
    
    # Generate price path
    for i in range(num_points):
        ts = start_time + i * interval
        
        # Random walk with occasional trends
        # 50% random walk, 35% trending, 15% reversal
        r = rng.random()
        
        if r < 0.5:
            # Random walk (chop)
            change = rng.uniform(-volatility, volatility)
        elif r < 0.85:
            # Trend continuation (stronger when bias is present)
            if trend_bias > 0:
                change = rng.uniform(0, volatility * 2)  # Stronger up moves
            elif trend_bias < 0:
                change = rng.uniform(-volatility * 2, 0)  # Stronger down moves
            else:
                change = rng.uniform(-volatility, volatility)
        else:
            # Reversal
            if trend_bias > 0:
                change = rng.uniform(-volatility, 0)  # Pullback
            elif trend_bias < 0:
                change = rng.uniform(0, volatility)  # Bounce
            else:
                change = rng.uniform(-volatility, volatility)
        
        yes_price = max(0.01, min(0.99, yes_price + change))
        no_price = 1 - yes_price  # Binary: YES + NO = 1
        
        yes_prices.append({
            'timestamp': ts,
            'price': yes_price,
            'side': 'trade'
        })
        no_prices.append({
            'timestamp': ts,
            'price': no_price,
            'side': 'trade'
        })
    
    return yes_prices, no_prices

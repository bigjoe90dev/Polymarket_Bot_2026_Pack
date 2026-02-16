#!/usr/bin/env python3
"""
Backtest 1H Trend-Following Strategy
====================================
Entry point for running historical backtest on 1H Up or Down crypto markets.

Usage:
    python3 scripts/backtest_1h.py              # Full backtest (365 days, synthetic data)
    python3 scripts/backtest_1h.py --sanity    # Quick sanity check (5 example trades)
    python3 scripts/backtest_1h.py --days 30    # 30-day backtest
    python3 scripts/backtest_1h.py --clear-cache # Force refresh cached data
    python3 scripts/backtest_1h.py --synthetic  # Use synthetic data (default when no real data)
    python3 scripts/backtest_1h.py --real       # Try to use real Polymarket data

Output:
    - summary.json: Performance metrics
    - trades.csv: Individual trade records
    - decisions.csv: All decision logs
"""

import sys
import os
import argparse
import json
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest_cache import clear_all_cache, get_cache_stats
from src.backtest_data import (
    fetch_historical_markets,
    fetch_market_timeseries,
    generate_synthetic_markets,
    generate_synthetic_timeseries
)
from src.backtest_engine import BacktestEngine


def load_config() -> dict:
    """Load configuration for backtest."""
    # Try to load from config.json
    config_path = "config/config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except:
            config = {}
    else:
        config = {}
    
    # Set defaults for backtest
    defaults = {
        'BACKTEST_LOOKBACK_DAYS': 365,
        'BACKTEST_TEST_SPLIT': 0.2,
        'BACKTEST_CLEAR_CACHE': False,
        'BACKTEST_RANDOM_SEED': 42,
        'BACKTEST_COST_PER_SIDE_CENTS': 2,
        'BACKTEST_MISSED_FILL_PROBABILITY': 0.15,
        'BACKTEST_FEE_BPS': 0,
        'BACKTEST_INITIAL_BALANCE': 100.0,
        
        # Strategy parameters (from v15)
        'TREND_TIMEFRAME': '1h',
        'TREND_ASSETS': ['BTC'],
        'TREND_MIN_DATA_SECONDS': 30,
        'TREND_MIN_HISTORY_MINUTES': 15,
        'TREND_TRENDINESS_THRESHOLD': 0.3,
        'TREND_BREAKOUT_TICKS': 1,
        'TREND_RETURN_THRESHOLD': 0.005,
        'TREND_COOLDOWN_MINUTES': 30,
        'TREND_TIME_LEFT_THRESHOLD': 12,
        'TREND_TP_TICKS': 8,
        'TREND_SL_CENTS': 3,
        'TREND_TRAILING_MA_PERIODS': 20,
        'TREND_MAX_HOLD_MINUTES': 45,
        'TREND_CONFIDENCE_THRESHOLD': 0.5,
        'MOMENTUM_SIZE': 5.0,
    }
    
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
    
    return config


def run_sanity_check(config: dict, markets: list):
    """Run a quick sanity check with 5 example trades."""
    print("\n" + "="*60)
    print("SANITY CHECK MODE")
    print("="*60)
    print("Showing first 5 potential trade signals...\n")
    
    example_count = 0
    for market in markets[:10]:
        # Get timeseries
        yes_prices, no_prices = fetch_market_timeseries(
            market, 
            clear_cache=config.get('BACKTEST_CLEAR_CACHE', False)
        )
        
        if not yes_prices or len(yes_prices) < 20:
            continue
        
        # Show some price samples
        print(f"Market: {market.get('question', '')[:60]}...")
        print(f"  End Date: {market.get('end_date', 'N/A')}")
        print(f"  YES Token: {market.get('yes_token_id', '')[:16]}...")
        print(f"  NO Token: {market.get('no_token_id', '')[:16]}...")
        print(f"  Price samples (first 5):")
        
        for i, p in enumerate(yes_prices[:5]):
            ts = datetime.fromtimestamp(p['timestamp']).strftime('%Y-%m-%d %H:%M')
            print(f"    {ts}: ${p['price']:.2f}")
        
        print(f"  Total price points: {len(yes_prices)}")
        print()
        
        example_count += 1
        if example_count >= 5:
            break
    
    if example_count == 0:
        print("No markets with sufficient price data found.")
        print("This may indicate:")
        print("  1. API rate limiting")
        print("  2. No historical data available for the selected assets")
        print("  3. Cache needs clearing (--clear-cache)")
    
    print(f"\nSanity check complete: showed {example_count} markets")
    return example_count


def main():
    parser = argparse.ArgumentParser(
        description="Backtest 1H Trend-Following Strategy on Polymarket"
    )
    parser.add_argument(
        '--days',
        type=int,
        default=None,
        help='Number of days to look back (default: from config, usually 365)'
    )
    parser.add_argument(
        '--assets',
        type=str,
        default=None,
        help='Comma-separated assets to backtest (default: BTC)'
    )
    parser.add_argument(
        '--sanity',
        action='store_true',
        help='Quick sanity check with example trades'
    )
    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='Force refresh cached data'
    )
    parser.add_argument(
        '--seed',
        type=int,
        default=None,
        help='Random seed for deterministic results'
    )
    parser.add_argument(
        '--cost',
        type=int,
        default=None,
        help='Cost per side in cents'
    )
    parser.add_argument(
        '--missed',
        type=float,
        default=None,
        help='Missed fill probability (0-1)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='data/backtest_outputs',
        help='Output directory'
    )
    parser.add_argument(
        '--synthetic',
        action='store_true',
        help='Use synthetic data (default when no real data available)'
    )
    parser.add_argument(
        '--real',
        action='store_true',
        help='Try to use real Polymarket data only'
    )
    parser.add_argument(
        '--num-markets',
        type=int,
        default=50,
        help='Number of synthetic markets to generate'
    )
    
    args = parser.parse_args()
    
    # Load config
    config = load_config()
    
    # Override with CLI args
    if args.days:
        config['BACKTEST_LOOKBACK_DAYS'] = args.days
    if args.clear_cache:
        config['BACKTEST_CLEAR_CACHE'] = True
    if args.seed:
        config['BACKTEST_RANDOM_SEED'] = args.seed
    if args.cost:
        config['BACKTEST_COST_PER_SIDE_CENTS'] = args.cost
    if args.missed is not None:
        config['BACKTEST_MISSED_FILL_PROBABILITY'] = args.missed
    if args.assets:
        config['TREND_ASSETS'] = [a.strip() for a in args.assets.split(',')]
    
    print("\n" + "="*60)
    print("BACKTEST CONFIGURATION")
    print("="*60)
    print(f"Lookback Days: {config['BACKTEST_LOOKBACK_DAYS']}")
    print(f"Assets: {config['TREND_ASSETS']}")
    print(f"Test Split: {config['BACKTEST_TEST_SPLIT']*100:.0f}%")
    print(f"Random Seed: {config['BACKTEST_RANDOM_SEED']}")
    print(f"Cost per Side: {config['BACKTEST_COST_PER_SIDE_CENTS']}c")
    print(f"Missed Fill Prob: {config['BACKTEST_MISSED_FILL_PROBABILITY']*100:.0f}%")
    print(f"Fee (BPS): {config['BACKTEST_FEE_BPS']}")
    print(f"Clear Cache: {config['BACKTEST_CLEAR_CACHE']}")
    print("="*60 + "\n")
    
    # Clear cache if requested
    if config.get('BACKTEST_CLEAR_CACHE'):
        print("Clearing cache...")
        clear_all_cache()
        print()
    
    # Show cache stats
    stats = get_cache_stats()
    print(f"Cache: {stats['market_caches']} market caches, "
          f"{stats['timeseries_caches']} timeseries caches, "
          f"{stats['total_size_mb']:.1f} MB\n")
    
    # Determine whether to use synthetic or real data
    use_synthetic = args.synthetic or not args.real
    
    if use_synthetic:
        print(f"Using SYNTHETIC data mode")
        print(f"Generating {args.num_markets} synthetic markets...")
        markets = generate_synthetic_markets(
            num_markets=args.num_markets,
            assets=config['TREND_ASSETS'],
            days_back=config['BACKTEST_LOOKBACK_DAYS'],
            random_seed=config['BACKTEST_RANDOM_SEED']
        )
    else:
        # Try to fetch real data
        print(f"Fetching markets for {config['TREND_ASSETS']} "
              f"(last {config['BACKTEST_LOOKBACK_DAYS']} days)...")
        markets = fetch_historical_markets(
            lookback_days=config['BACKTEST_LOOKBACK_DAYS'],
            assets=config['TREND_ASSETS'],
            clear_cache=config.get('BACKTEST_CLEAR_CACHE', False)
        )
        
        if not markets:
            print("\nWARNING: No real markets found!")
            print("Falling back to synthetic data...")
            print()
            markets = generate_synthetic_markets(
                num_markets=args.num_markets,
                assets=config['TREND_ASSETS'],
                days_back=config['BACKTEST_LOOKBACK_DAYS'],
                random_seed=config['BACKTEST_RANDOM_SEED']
            )
            use_synthetic = True
    
    print(f"\nFound {len(markets)} markets ({'SYNTHETIC' if use_synthetic else 'REAL'})")
    
    # Sanity check mode
    if args.sanity:
        run_sanity_check(config, markets)
        return 0
    
    # Fetch/generate timeseries for each market
    print(f"\nFetching/generating price timeseries for {len(markets)} markets...")
    # Limit to avoid excessive processing
    max_markets = min(len(markets), config.get('BACKTEST_MAX_MARKETS', 100))
    
    for i, market in enumerate(markets[:max_markets]):
        if (i + 1) % 10 == 0:
            print(f"   Processed {i+1}/{min(len(markets), max_markets)} markets...")
        
        # Use synthetic timeseries for synthetic markets or if real data fails
        if use_synthetic or market.get('is_synthetic'):
            # Generate unique seed per market for reproducibility
            market_seed = config['BACKTEST_RANDOM_SEED'] + i
            yes_prices, no_prices = generate_synthetic_timeseries(
                market,
                duration_hours=1.0,
                points_per_minute=6,
                random_seed=market_seed,
                volatility=0.02,
                trend_bias=0.0  # No bias - let strategy find its own edge
            )
        else:
            yes_prices, no_prices = fetch_market_timeseries(
                market,
                clear_cache=config.get('BACKTEST_CLEAR_CACHE', False)
            )
        
        market['yes_prices'] = yes_prices
        market['no_prices'] = no_prices
    
    # Filter markets with data
    markets_with_data = [m for m in markets[:max_markets]
                        if m.get('yes_prices') and m.get('no_prices') and len(m.get('yes_prices', [])) > 10]
    
    print(f"\nMarkets with price data: {len(markets_with_data)}")
    
    if len(markets_with_data) < 5:
        print("\nWARNING: Very few markets with data. Results may not be meaningful.")
        print("Try adjusting the lookback period or assets.")
    
    # Run backtest
    print("\nStarting backtest...")
    engine = BacktestEngine(config, markets_with_data)
    results = engine.run()
    
    # Print summary
    print("\n" + "="*60)
    print("BACKTEST RESULTS SUMMARY")
    print("="*60)
    
    train = results['train']
    test = results['test']
    combined = results['combined']
    
    print(f"\nTRAIN SET:")
    print(f"  P&L: ${train['total_pnl']:.2f}")
    print(f"  Win Rate: {train['win_rate']*100:.1f}%")
    print(f"  Avg Win: ${train['avg_win']:.2f}")
    print(f"  Avg Loss: ${train['avg_loss']:.2f}")
    print(f"  Trades: {train['num_trades']}")
    print(f"  Max Drawdown: -${-train['max_drawdown']:.2f}")
    
    print(f"\nTEST SET:")
    print(f"  P&L: ${test['total_pnl']:.2f}")
    print(f"  Win Rate: {test['win_rate']*100:.1f}%")
    print(f"  Avg Win: ${test['avg_win']:.2f}")
    print(f"  Avg Loss: ${test['avg_loss']:.2f}")
    print(f"  Trades: {test['num_trades']}")
    print(f"  Max Drawdown: -${-test['max_drawdown']:.2f}")
    
    print(f"\nCOMBINED:")
    print(f"  P&L: ${combined['total_pnl']:.2f}")
    print(f"  Win Rate: {combined['win_rate']*100:.1f}%")
    print(f"  Total Trades: {combined['num_trades']}")
    
    print(f"\n{'='*60}")
    
    # Save outputs
    engine.save_outputs(args.output)
    
    # Save summary to output dir
    with open(f"{args.output}/summary.json", 'w') as f:
        json.dump({
            'train': train,
            'test': test,
            'combined': combined,
            'config': results['config'],
            'timestamp': datetime.now().isoformat()
        }, f, indent=2)
    
    print(f"\nResults saved to {args.output}/")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

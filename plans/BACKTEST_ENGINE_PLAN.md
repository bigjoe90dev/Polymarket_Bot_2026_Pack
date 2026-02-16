# Backtest Engine Plan: 1H Trend-Following Strategy

## Overview
Build an offline backtest script that replays the Phase 1 trend strategy on real Polymarket historical price data for 1H "Up or Down" crypto markets.

---

## 1) PLAN: Files Added/Changed, Run Command, Config Keys

### New Files
| File | Purpose |
|------|---------|
| `scripts/backtest_1h.py` | Main entry point |
| `src/backtest_cache.py` | Disk caching for markets + timeseries |
| `src/backtest_data.py` | Historical market fetching with pagination |
| `src/backtest_engine.py` | Core backtest replay logic |
| `src/backtest_shared.py` | Shared decision logic (refactored from momentum_strategy) |
| `config/backtest_config.json` | Default backtest parameters |

### Config Keys (add to config.py v16)
```python
# Backtest Parameters
LOOKBACK_DAYS = 365              # Days of history to fetch
TEST_SPLIT = 0.2                 # 20% for test, 80% for train
CLEAR_CACHE = False              # Force refresh cached data
RANDOM_SEED = 42                 # For deterministic missed fills
COST_PER_SIDE_CENTS = 2          # Spread penalty per side (entry + exit)
MISSED_FILL_PROBABILITY = 0.15   # 15% of entries missed
FEE_BPS = 0                      # Fee in basis points (0 = use API if available)
LOCKED_PARAMS = True             # Prevent parameter optimization
```

### Run Command
```bash
# Full backtest
python3 scripts/backtest_1h.py

# With custom config
python3 scripts/backtest_1h.py --config config/backtest_config.json

# Sanity mode (5 example trades)
python3 scripts/backtest_1h.py --sanity

# Force cache refresh
python3 scripts/backtest_1h.py --clear-cache
```

---

## 2) IMPLEMENTATION: Module Structure + Key Code Blocks

### Module Structure
```
scripts/
  backtest_1h.py          # Entry point + CLI

src/
  backtest_cache.py       # Disk caching (JSON/CSV files)
  backtest_data.py        # Historical market fetching
  backtest_engine.py      # Price replay + trade simulation
  backtest_shared.py      # Decision logic shared with live
```

### Key Code: backtest_cache.py
```python
import os, json, hashlib
from datetime import datetime

CACHE_DIR = "data/backtest_cache"

def get_cache_path(key: str) -> str:
    """Get path for cached data."""
    return f"{CACHE_DIR}/{key}.json"

def load_cached(key: str) -> Optional[dict]:
    """Load from cache if exists."""
    path = get_cache_path(key)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None

def save_cached(key: str, data: dict):
    """Save to cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = get_cache_path(key)
    with open(path, 'w') as f:
        json.dump(data, f)
```

### Key Code: backtest_data.py
```python
def fetch_historical_markets(config: dict) -> List[dict]:
    """Fetch historical markets with caching."""
    cache_key = f"markets_{config.get('LOOKBACK_DAYS', 365)}"
    
    # Check cache
    if not config.get('CLEAR_CACHE'):
        cached = load_cached(cache_key)
        if cached:
            return cached['markets']
    
    # Fetch from Polymarket API
    client = ClobClient("https://clob.polymarket.com")
    markets = []
    
    # Paginated fetch
    cursor = None
    while True:
        resp = client.get_markets(
            limit=1000,
            cursor=cursor,
            # Filter for resolved markets
            closed=True
        )
        markets.extend(resp.get('data', []))
        cursor = resp.get('next_cursor')
        if not cursor:
            break
    
    # Filter for 1H Up/Down crypto
    filtered = []
    for m in markets:
        title = m.get('question', '').lower()
        if 'up or down' in title and ('1 hour' in title or '1h' in title or '60 min' in title):
            if any(a in title for a in config.get('TREND_ASSETS', ['bitcoin', 'btc'])):
                filtered.append(m)
    
    # Cache
    save_cached(cache_key, {'markets': filtered, 'fetched_at': datetime.now().isoformat()})
    return filtered

def fetch_token_timeseries(token_id: str, start: int, end: int, config: dict) -> List[dict]:
    """Fetch price history for a token."""
    cache_key = f"timeseries_{token_id}_{start}_{end}"
    
    if not config.get('CLEAR_CACHE'):
        cached = load_cached(cache_key)
        if cached:
            return cached['prices']
    
    # Fetch from CLOB timeseries endpoint
    client = ClobClient("https://clob.polymarket.com")
    resp = client.get_price_history(
        token_id=token_id,
        start_time=start,
        end_time=end,
        bucket='1m'  # 1-minute buckets for 1H strategy
    )
    
    prices = resp.get('history', [])
    save_cached(cache_key, {'prices': prices})
    return prices
```

### Key Code: backtest_engine.py
```python
class BacktestEngine:
    def __init__(self, config: dict, markets: List[dict], price_data: dict):
        self.config = config
        self.markets = markets
        self.price_data = price_data  # token_id -> list of (timestamp, price)
        
        # Train/test split
        split_idx = int(len(markets) * (1 - config.get('TEST_SPLIT', 0.2)))
        self.train_markets = markets[:split_idx]
        self.test_markets = markets[split_idx:]
        
        # Random for missed fills
        self.rng = random.Random(config.get('RANDOM_SEED', 42))
        
        # Results storage
        self.trades = []
        self.equity_curve = []
        
    def run(self) -> dict:
        """Run backtest on train and test sets."""
        # Run on train
        train_results = self._run_split(self.train_markets, "TRAIN")
        
        # Run on test  
        test_results = self._run_split(self.test_markets, "TEST")
        
        # Combined
        combined = self._combine_results(train_results, test_results)
        
        return {
            "train": train_results,
            "test": test_results,
            "combined": combined
        }
    
    def _run_split(self, markets: List[dict], label: str) -> dict:
        """Run backtest on a market split."""
        for market in markets:
            self._simulate_market(market)
        
        return self._compute_metrics(label)
    
    def _simulate_market(self, market: dict):
        """Simulate trading on one market's price history."""
        # Get price data
        yes_token = market['yes_token_id']
        no_token = market['no_token_id']
        
        yes_prices = self.price_data.get(yes_token, [])
        no_prices = self.price_data.get(no_token, [])
        
        if not yes_prices or not no_prices:
            return
        
        # Merge by timestamp and sort
        merged = self._merge_prices(yes_prices, no_prices)
        
        # Initialize strategy state
        state = self._create_strategy_state()
        
        # Replay prices
        for ts, yes_price, no_price in merged:
            # Update strategy with new price
            state = self._update_strategy(state, ts, yes_price, no_price)
            
            # Check for entry signal
            decision = self._check_entry(state, market, ts)
            if decision:
                # Apply missed fill probability
                if self.rng.random() < self.config.get('MISSED_FILL_PROBABILITY', 0.15):
                    continue  # Skip - missed fill
                
                # Apply spread penalty
                entry_price = decision['price'] - (self.config.get('COST_PER_SIDE_CENTS', 2) / 100)
                
                # Record entry
                self.trades.append({
                    'market_id': market['condition_id'],
                    'token_id': decision['token_id'],
                    'outcome': decision['outcome'],
                    'entry_time': ts,
                    'entry_price': entry_price,
                    'size': self.config.get('MOMENTUM_SIZE', 5.0),
                })
            
            # Check for exit
            self._check_exit(state, ts, yes_price, no_price)
    
    def _check_entry(self, state: dict, market: dict, ts: int) -> Optional[dict]:
        """Check if strategy signals entry (reuse live logic)."""
        # Import and use shared decision logic
        from src.backtest_shared import check_entry_signal
        return check_entry_signal(state, market, ts, self.config)
    
    def _check_exit(self, state: dict, ts: int, yes_price: float, no_price: float):
        """Check exit conditions."""
        # TP/SL/trailing/45min max hold
        # Apply exit spread penalty
        pass
    
    def _compute_metrics(self, label: str) -> dict:
        """Compute performance metrics."""
        # P&L, win rate, avg win/loss, trades/day, drawdown
        pass
```

### Key Code: backtest_shared.py (refactored from momentum_strategy)
```python
def check_entry_signal(state: dict, market: dict, ts: int, config: dict) -> Optional[dict]:
    """Shared entry logic - used by both live and backtest."""
    # Layer 0: Data sanity
    if not state.get('has_enough_data'):
        return None
    
    # Layer 1: Trendiness
    trendiness = compute_trendiness(state['price_buffer'])
    if trendiness < config.get('TREND_TRENDINESS_THRESHOLD', 0.3):
        return None
    
    # Layer 2: Breakout + return
    # ... (same logic as live)
    
    return signal
```

---

## 3) EXAMPLE OUTPUT

### summary.json
```json
{
  "train": {
    "total_pnl": 125.50,
    "win_rate": 0.58,
    "avg_win": 8.20,
    "avg_loss": -5.10,
    "trades_per_day": 0.8,
    "max_drawdown": -45.20,
    "num_trades": 292,
    "num_markets": 45
  },
  "test": {
    "total_pnl": 38.20,
    "win_rate": 0.52,
    "avg_win": 7.80,
    "avg_loss": -5.50,
    "trades_per_day": 0.6,
    "max_drawdown": -28.10,
    "num_trades": 78,
    "num_markets": 12
  },
  "combined": {
    "total_pnl": 163.70,
    "win_rate": 0.56,
    "avg_win": 8.05,
    "avg_loss": -5.25,
    "trades_per_day": 0.7,
    "max_drawdown": -52.30,
    "num_trades": 370,
    "num_markets": 57
  },
  "config": {
    "lookback_days": 365,
    "test_split": 0.2,
    "random_seed": 42,
    "cost_per_side_cents": 2,
    "missed_fill_probability": 0.15
  },
  "earliest_market_date": "2024-01-15",
  "latest_market_date": "2026-02-01"
}
```

### trades.csv (sample rows)
```csv
market_id,token_id,outcome,entry_time,entry_price,exit_time,exit_price,pnl,reason,train_test
0xabc123,0xyes,YES,2024-06-15T10:00:00Z,0.52,2024-06-15T10:45:00Z,0.58,0.06,TP:8ticks,TRAIN
0xdef456,0xno,NO,2024-07-20T14:00:00Z,0.48,2024-07-20T14:30:00Z,0.45,-0.03,SL:3cents,TRAIN
0xghi789,0xyes,YES,2025-01-10T08:00:00Z,0.55,2025-01-10T09:00:00Z,0.61,0.06,TP:8ticks,TEST
```

### equity_curve.csv
```csv
date,train_equity,test_equity,combined_equity
2024-01-01,100.00,100.00,100.00
2024-01-02,100.50,100.00,100.50
...
```

### Console Output
```
============================================================
BACKTEST RESULTS: 1H Trend-Following (BTC)
============================================================
Lookback: 365 days | Train: 292 markets | Test: 78 markets
Random Seed: 42 | Cost/Side: 2c | Missed Fill: 15%

TRAIN SET:
  P&L: +$125.50 | Win Rate: 58% | Avg Win: $8.20 | Avg Loss: -$5.10
  Trades/Day: 0.8 | Max Drawdown: -$45.20

TEST SET:
  P&L: +$38.20 | Win Rate: 52% | Avg Win: $7.80 | Avg Loss: -$5.50
  Trades/Day: 0.6 | Max Drawdown: -$28.10

COMBINED:
  P&L: +$163.70 | Win Rate: 56% | Trades: 370

Best Markets: BTC Up or Down 1h (Feb 2025), BTC Up or Down 1h (Jan 2025)
Worst Markets: BTC Up or Down 1h (Aug 2024), BTC Up or Down 1h (Dec 2024)
============================================================
```

---

## Acceptance Tests

| Test | Expected |
|------|----------|
| 30-day BTC backtest | Completes, saves output files |
| 365-day BTC backtest | Completes with caching |
| 730-day backtest | Reports earliest market if insufficient data |
| Train/test split | Shows separate metrics |
| Deterministic | Same RANDOM_SEED = same results |
| Time-left gate | Uses end_date metadata when available |
| --sanity flag | Shows 5 example trades with reasoning |

---

*Plan created: 2026-02-16*
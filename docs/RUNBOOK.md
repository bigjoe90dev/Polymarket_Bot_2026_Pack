# Phase 1 Runbook: 1H Trend-Following Bot

## Running Paper Trading 24/7 on MacBook

### Prerequisites

1. **Prevent Mac from sleeping** (use caffeinate - safer than sudo pmset)
   ```bash
   # Run bot with caffeinate to prevent sleep
   # -d: prevent display sleep
   # -i: prevent idle sleep
   # -m: prevent disk idle
   # -s: create a system sleep assertion (most effective)
   # -u: indicate user is active (for full prevention)
   caffeinate -dimsu python3 run.py
   ```

### Starting the Bot

```bash
# From project directory
cd "/Users/joe/2026 projects/Polymarket_Bot_2026_Pack"

# Run with caffeinate to prevent Mac from sleeping
caffeinate -dimsu python3 run.py

# Or run in background with nohup (survives terminal close)
nohup python3 run.py > logs/bot.log 2>&1 &

# Or use screen (recommended for easy reattach)
screen -S polybot
python3 run.py
# Press Ctrl+A, then D to detach
```

### Checking Status

```bash
# View running processes
ps aux | grep python | grep run.py

# View recent logs
tail -f logs/bot.log

# View paper state
cat data/paper_state.json | python -m json.tool

# View decisions log
grep "TREND]" logs/bot.log | tail -50
```

### Viewing Structured Logs

All trade decisions are logged with the `[TREND]` prefix:

```bash
# All decisions
grep "\[TREND\]" logs/bot.log

# Only entries
grep "ENTER_" logs/bot.log

# Only exits
grep "EXIT" logs/bot.log

# Only skips with reasons
grep "SKIP" logs/bot.log
```

### Log Format

Each decision logs:
```
[TREND] ACTION | ASSET | TIMEFRAME | MARKET_TITLE | price=$X.XX | trend=X.XX | breakout=XXX | time_left=X.Xmin | conf=X.XX | REASON
```

Example:
```
[TREND] SKIP | BTC | 1H | Bitcoin Up or Down in 1 hour - Feb 16... | price=0.52 | trend=0.15 | breakout=N/A | time_left=45.2min | conf=0.00 | LAYER1:Trendiness=0.15<0.3
```

### Resetting Paper State

```bash
# Stop the bot first
pkill -f "python run.py"

# Backup existing state
cp data/paper_state.json data/paper_state_backup_$(date +%Y%m%d_%H%M%S).json

# Reset to fresh state (optional - deletes all history)
echo '{"version":3,"starting_balance":100.0,"cash_balance":100.0,"total_trades":0}' > data/paper_state.json

# Restart bot
python run.py
```

### Configuration

Key settings in `config/config.json`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TREND_TIMEFRAME` | "1h" | Timeframe (1H only in Phase 1) |
| `TREND_ASSETS` | ["BTC"] | Assets to trade |
| `USE_CLOB_WEBSOCKET` | true | Use WebSocket for prices |
| `TREND_POLL_INTERVAL` | 10 | REST polling interval (seconds) |
| `TREND_TP_TICKS` | 8 | Take profit (+8 ticks) |
| `TREND_SL_CENTS` | 3 | Stop loss (-3 cents) |
| `TREND_MAX_HOLD_MINUTES` | 45 | Max hold time |
| `TREND_CONFIDENCE_THRESHOLD` | 0.5 | Min confidence to act |

### Safety Features

1. **No Hard Stop**: Unlike live trading, paper mode doesn't stop after X losses
2. **Drawdown Warning**: Logs warning if daily drawdown > $20
3. **Kill Switch**: Respects `data/kill_switch` file
4. **Max Positions**: 1 position per asset
5. **Health Monitoring**: Auto-restarts if main loop hangs

### Troubleshooting

**Bot crashes on start**:
```bash
# Check for missing dependencies
pip install -r requirements.txt

# Check config syntax
python -c "import json; json.load(open('config/config.json'))"
```

**No markets found**:
```bash
# Check API connectivity
python -c "from src.market import MarketDataService; m = MarketDataService({}); print(len(m.get_active_markets()))"
```

**WebSocket issues**:
```bash
# Set USE_CLOB_WEBSOCKET to false for REST-only mode
# Edit config/config.json and set "USE_CLOB_WEBSOCKET": false
```

### Monitoring Commands Summary

```bash
# Real-time log monitoring
tail -f logs/bot.log | grep "\[TREND\]"

# Check open positions
cat data/paper_state.json | grep -A 10 '"positions"'

# Check PnL
cat data/paper_state.json | grep -E '"cash_balance"|"total_realized_pnl"'

# Count trades today
grep "ENTER_" logs/bot.log | grep $(date +%Y-%m-%d) | wc -l
```

---

*Last updated: 2026-02-16*
*Phase 1: 1H Trend-Following, Paper Only*

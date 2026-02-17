# Plan: 1H BTC Up/Down Paper Trading - Real Markets Only

## Task Summary
Paper trade REAL 1-hour BTC "Up or Down" markets on Polymarket tonight, with full dashboard visibility. No backtesting, no synthetic markets, no manual whitelists.

## Hard Rules (Must Follow)
1. **NO manual whitelist file** - No `discovered_hourly_markets.json`
2. **NO synthetic markets** - Must be real Polymarket markets
3. **NO title-based duration guessing** - Must use start/end times to compute actual duration (end-start ≈ 60 min)
4. **Strict filters**: BTC only, Up/Down only, duration ≈ 60 min, active/tradable only
5. **Fail closed**: If can't prove 60 min, don't trade; if 0 valid markets, stop with error

## Current Issues Identified

### Issue 1: Config has manual whitelist reference
- **File**: `config/config.json`
- **Problem**: Line 4 has `"HOURLY_MARKET_IDS_FILE": "data/discovered_hourly_markets.json"`
- **Fix**: Remove this line (or set to empty/null)

### Issue 2: momentum_strategy.py uses title-based filtering
- **File**: `src/momentum_strategy.py`
- **Problem**: `_is_1h_crypto_up_down()` method (lines 367-408) filters by words like "1h", "1 hour" in title
- **This violates Hard Rule 3**: "Do NOT guess '1 hour' from words"
- **Fix**: Remove this title-based filter. The market.py already filters by duration (50-70 min)

### Issue 3: Dashboard doesn't filter for 1H markets
- **File**: `src/web_server.py`
- **Problem**: `/api/markets` endpoint shows all markets, not just 1H BTC markets
- **Fix**: Update to show only the 1H BTC markets from market.py

### Issue 4: Missing startup proof logging
- **File**: `src/market.py` (already has some, but need to verify)
- **Required**: Print:
  - "Found N valid 1H BTC Up/Down markets"
  - 5 example titles + start time + end time + computed duration
  - Which market it will trade now

## What Already Works (Don't Change)

### market.py `_discover_hourly_markets()` (lines 26-180)
This method already does the RIGHT thing:
- ✅ Auto-discovers markets via slug generation (no manual whitelist)
- ✅ Filters by BTC + Up/Down (lines 80-84)
- ✅ Filters by duration 50-70 minutes (lines 108-110)
- ✅ Filters by active/tradable (lines 71-77)
- ✅ Hard fails if 0 markets (lines 163-166)
- ✅ Prints startup proof (lines 158-180)

## Implementation Plan

### Step 1: Fix Config
Remove `HOURLY_MARKET_IDS_FILE` from config:
```json
// Remove this line from config/config.json:
"HOURLY_MARKET_IDS_FILE": "data/discovered_hourly_markets.json",
```

### Step 2: Fix momentum_strategy.py
Replace `_is_1h_crypto_up_down()` with a simpler filter that trusts market.py:
```python
def _is_1h_crypto_up_down(self, market_name: str) -> bool:
    """Check if market is BTC Up/Down crypto market.
    
    NOTE: Duration filtering (1H) is already done by market.py.
    This method only checks for BTC + Up/Down format.
    """
    if not market_name:
        return False
    
    name_lower = market_name.lower()
    
    # Must be crypto (BTC)
    allowed_assets = self.config.get("TREND_ASSETS", ["BTC"])
    is_crypto = any(asset.lower() in name_lower for asset in allowed_assets)
    if not is_crypto:
        return False
    
    # Must be Up or Down format
    return "up or down" in name_lower or "up/down" in name_lower
```

### Step 3: Fix web_server.py
Update `/api/markets` to show only 1H BTC markets:
- The bot already stores 1H markets in `bot.market._hourly_markets`
- Update the endpoint to return these instead of all markets

### Step 4: Verify startup logging
Ensure market.py prints:
- "Found N valid 1H BTC Up/Down markets"
- 5 example markets with title, start, end, duration
- Which market will trade first (sorted by hours_until)

## Expected Startup Output
```
[*] Discovering 1H BTC Up/Down markets from Gamma API...
[*] Testing 112 candidate slugs...
============================================================
FOUND 8 VALID 1H BTC UP/DOWN MARKETS
============================================================

Example markets:
  1. Bitcoin Up or Down by 9pm ET on Feb 17?
     Start: 2026-02-18T02:00:00
     End: 2026-02-18T03:00:00
     Duration: 60 min
     Resolves in: 0.5 hours

  2. Bitcoin Up or Down by 10pm ET on Feb 17?
     Start: 2026-02-18T03:00:00
     End: 2026-02-18T04:00:00
     Duration: 60 min
     Resolves in: 1.5 hours
...

============================================================

[*] Trading market: Bitcoin Up or Down by 9pm ET on Feb 17?
[*] Bot warming up...
[*] Found 8 active markets
```

## Dashboard Endpoints After Fix

### /api/markets
Should show only 1H BTC markets:
```json
{
  "markets": [
    {
      "condition_id": "abc123...",
      "title": "Bitcoin Up or Down by 9pm ET on Feb 17?",
      "duration_min": 60,
      "resolves_in_hours": 0.5,
      "status": "ACTIVE"
    }
  ],
  "total_tracked": 8
}
```

### /api/trades
Should show paper trades only on 1H BTC markets

### /api/positions
Should show open positions only on 1H BTC markets

## Files to Modify
1. `config/config.json` - Remove HOURLY_MARKET_IDS_FILE
2. `src/momentum_strategy.py` - Remove title-based 1H filtering
3. `src/web_server.py` - Update /api/markets to show 1H markets

## Commands to Run Tonight
```bash
# Keep Mac awake (run in separate terminal)
caffeinate -d -i -s

# Run the bot (paper mode)
python run.py

# Access dashboard
open http://localhost:8080/?token=polymarket
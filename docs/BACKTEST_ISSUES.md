# Polymarket Backtest API Issues - Summary

## Objective
Build a backtest for a 1H (hourly) "Up or Down" crypto trading strategy on Polymarket.

## Issue: Cannot find hourly markets via API

### What We Need
- Markets like: "Bitcoin Up or Down - February 16, 4PM ET"
- Visible at: https://polymarket.com/crypto/hourly
- Duration: ~60 minutes

### What I've Tried

#### 1. Gamma API (primary)
```python
url = "https://gamma-api.polymarket.com/markets"
params = {"closed": "false", "limit": 500}
```
- Returns ~500 markets
- **Problem**: All markets have end dates from Dec 2025 - Feb 2026 (long-term)
- No hourly markets found
- No "Up or Down" markets found

#### 2. CLOB API
```python
url = "https://clob.polymarket.com/markets"
```
- Same results as Gamma API
- Pagination returns same 1000 markets repeatedly (cursor not working)

#### 3. Various Filters Tried
- `hourly=true` - returns same long-term markets
- `category=crypto` - returns same markets (but labeled crypto)
- `groupItemTitle` field - all markets have `null` value
- Searching for "up or down", "1h", "hour", "minute" in titles - 0 matches
- Searching for timestamp patterns like "4PM ET" - 0 matches

#### 4. Date-Based Filtering
- Looking for markets ending within 24 hours - 0 found
- Calculating duration from startDate to endDate - no ~60 minute markets found

### Current API Response Sample
```
question: "Will Trump deport less than 250,000?"
startDate: "2025-01-05T18:49:12.543209Z"
endDate: "2025-12-31T12:00:00Z"
groupItemTitle: null
```

### Questions for ChatGPT

1. **Is there a different API endpoint for hourly markets?**
   - The website shows hourly markets but API doesn't return them
   - Is there a GraphQL endpoint? Private API? WebSocket subscription?

2. **How does Polymarket generate hourly markets?**
   - Are they created dynamically and not stored in the main market database?
   - Is there a separate service/endpoint for "Hourly Crypto"?

3. **Alternative approaches?**
   - Can we access historical hourly market data?
   - Is there documentation for the hourly market system?
   - Any partner/API keys needed?

4. **Workaround options?**
   - Use long-term crypto markets for strategy testing?
   - Build a scraper for the hourly page?
   - Wait for hourly markets to become available in API?

## Files Created So Far
- `src/backtest_cache.py` - caching module
- `src/backtest_data.py` - data fetching (adapted for generic markets)
- `src/backtest_shared.py` - strategy logic (shared with live)
- `src/backtest_engine.py` - backtest replay engine
- `scripts/backtest_1h.py` - CLI entry point

## What Works
- Backtest engine architecture is complete
- Strategy logic (trend-following 3-layer gating) implemented
- Train/test split works
- Realism penalties (spread, missed fills, fees) applied
- Output formats (summary.json, trades.csv) work

## What Doesn't Work
- Cannot fetch actual hourly "Up or Down" market data from API
- Synthetic data not acceptable (user rejected)
- Need real historical hourly market data

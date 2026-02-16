"""
Backtest Shared Decision Logic
==============================
Shared signal logic used by both live trading and backtest.

Refactored from momentum_strategy.py to enable:
- Identical decision logic in backtest and live
- Deterministic signal generation for backtest replay
"""

import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class PricePoint:
    """Single price point with timestamp."""
    timestamp: float
    price: float


def compute_trendiness(prices: List[float], timestamps: List[float]) -> float:
    """Compute trendiness score.
    
    trendiness = |return_10min| / sum(|step_changes|)
    
    Args:
        prices: List of prices (most recent last)
        timestamps: List of timestamps (most recent last)
    
    Returns:
        Trendiness score (0 = no trend, 1 = strong trend)
    """
    if len(prices) < 10 or len(timestamps) < 10:
        return 0.0
    
    # Get 10-minute window
    now = timestamps[-1]
    cutoff = now - 600  # 10 minutes
    recent_prices = []
    for i, ts in enumerate(timestamps):
        if ts >= cutoff:
            recent_prices.append(prices[i])
    
    if len(recent_prices) < 10:
        return 0.0
    
    # Calculate return
    if recent_prices[0] == 0:
        return 0.0
    return_10min = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
    
    # Calculate sum of absolute changes
    steps = [abs(recent_prices[i] - recent_prices[i-1]) for i in range(1, len(recent_prices))]
    total_steps = sum(steps)
    
    if total_steps == 0:
        return 0.0
    
    # Normalize by first price
    trendiness = abs(return_10min) / (total_steps / recent_prices[0])
    return min(1.0, trendiness)  # Cap at 1.0


def compute_return_5min(prices: List[float], timestamps: List[float]) -> float:
    """Compute 5-minute return.
    
    Args:
        prices: List of prices
        timestamps: List of timestamps
    
    Returns:
        Return as fraction (e.g., 0.05 = 5%)
    """
    if len(prices) < 2 or len(timestamps) < 2:
        return 0.0
    
    # Get 5-minute window
    now = timestamps[-1]
    cutoff = now - 300  # 5 minutes
    recent_prices = []
    for i, ts in enumerate(timestamps):
        if ts >= cutoff:
            recent_prices.append(prices[i])
    
    if len(recent_prices) < 2:
        return 0.0
    
    if recent_prices[0] == 0:
        return 0.0
    
    return (recent_prices[-1] - recent_prices[0]) / recent_prices[0]


def get_rolling_high_low(
    prices: List[float],
    timestamps: List[float],
    minutes: int = 10
) -> Tuple[Optional[float], Optional[float]]:
    """Get rolling high and low over N minutes.
    
    Args:
        prices: List of prices
        timestamps: List of timestamps
        minutes: Window size in minutes
    
    Returns:
        Tuple of (high, low) or (None, None) if insufficient data
    """
    if not prices or not timestamps:
        return None, None
    
    now = timestamps[-1]
    cutoff = now - (minutes * 60)
    recent_prices = []
    for i, ts in enumerate(timestamps):
        if ts >= cutoff:
            recent_prices.append(prices[i])
    
    if not recent_prices:
        return None, None
    
    return max(recent_prices), min(recent_prices)


def compute_confidence(
    trendiness: float,
    breakout_magnitude: float,
    time_left_minutes: Optional[float],
    config: Dict
) -> float:
    """Compute confidence score (0-1).
    
    Args:
        trendiness: Current trendiness score
        breakout_magnitude: How far price broke out (in cents)
        time_left_minutes: Minutes remaining in market
        config: Strategy config
    
    Returns:
        Confidence score (0-1)
    """
    trendiness_threshold = config.get('TREND_TRENDINESS_THRESHOLD', 0.3)
    breakout_ticks = config.get('TREND_BREAKOUT_TICKS', 1)
    time_left_threshold = config.get('TREND_TIME_LEFT_THRESHOLD', 12)
    
    # Trendiness factor (0-1)
    trend_factor = min(1.0, trendiness / trendiness_threshold)
    
    # Breakout magnitude factor (0-1)
    breakout_factor = min(1.0, breakout_magnitude / (breakout_ticks * 2))
    
    # Time remaining factor
    if time_left_minutes is None or time_left_minutes > 30:
        time_factor = 1.0
    else:
        time_factor = max(0.0, time_left_minutes / time_left_threshold)
    
    confidence = trend_factor * breakout_factor * time_factor
    return confidence


def check_exit_conditions(
    entry_price: float,
    current_price: float,
    entry_time: float,
    current_time: float,
    outcome: str,
    ma_value: Optional[float],
    config: Dict
) -> Tuple[bool, str, float]:
    """Check if we should exit a position.
    
    Args:
        entry_price: Entry price
        current_price: Current price
        entry_time: Entry timestamp
        current_time: Current timestamp
        outcome: "YES" or "NO"
        ma_value: Current moving average value
        config: Strategy config
    
    Returns:
        Tuple of (should_exit, reason, pnl_ticks)
    """
    # Calculate PnL in cents (assuming $1 token = 100 cents)
    pnl_cents = (current_price - entry_price) * 100
    pnl_ticks = pnl_cents  # 1 cent = 1 tick on $1 token
    
    tp_ticks = config.get('TREND_TP_TICKS', 8)
    sl_cents = config.get('TREND_SL_CENTS', 3)
    max_hold_minutes = config.get('TREND_MAX_HOLD_MINUTES', 45)
    
    # Check take profit (+8 ticks)
    if pnl_ticks >= tp_ticks:
        return True, f"TP:+{pnl_ticks:.1f}Ticks", pnl_ticks
    
    # Check stop loss (-3 cents)
    if pnl_ticks <= -sl_cents:
        return True, f"SL:{pnl_ticks:.1f}Ticks", pnl_ticks
    
    # Check trailing MA
    if ma_value is not None:
        if outcome == "YES" and current_price < ma_value:
            return True, f"TRAIL_MA:Price<MA", pnl_ticks
        elif outcome == "NO" and current_price > ma_value:
            return True, f"TRAIL_MA:Price>MA", pnl_ticks
    
    # Check max hold (45 minutes)
    hold_minutes = (current_time - entry_time) / 60
    if hold_minutes >= max_hold_minutes:
        return True, f"MAX_HOLD:{hold_minutes:.0f}min", pnl_ticks
    
    return False, "", pnl_ticks


def parse_time_left(end_date_str: str, current_time: float) -> Tuple[Optional[float], str]:
    """Parse time remaining from end_date.
    
    Args:
        end_date_str: ISO format end date string
        current_time: Current timestamp
    
    Returns:
        Tuple of (minutes_remaining, source)
    """
    if not end_date_str:
        return None, "none"
    
    try:
        from datetime import datetime
        # Parse ISO date
        end_date = end_date_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(end_date)
        resolves_at = dt.timestamp()
        minutes_left = (resolves_at - current_time) / 60
        if minutes_left > 0:
            return minutes_left, "metadata"
    except:
        pass
    
    return None, "none"


def check_entry_signal(
    state: Dict,
    market: Dict,
    current_time: float,
    config: Dict
) -> Optional[Dict]:
    """Check if strategy signals entry.
    
    Args:
        state: Strategy state containing price buffers, etc.
        market: Market info dict
        current_time: Current timestamp
        config: Strategy config
    
    Returns:
        Signal dict if entry triggered, None otherwise
    """
    # Extract config
    trendiness_threshold = config.get('TREND_TRENDINESS_THRESHOLD', 0.3)
    breakout_ticks = config.get('TREND_BREAKOUT_TICKS', 1)
    return_threshold = config.get('TREND_RETURN_THRESHOLD', 0.005)
    time_left_threshold = config.get('TREND_TIME_LEFT_THRESHOLD', 12)
    confidence_threshold = config.get('TREND_CONFIDENCE_THRESHOLD', 0.5)
    min_history_minutes = config.get('TREND_MIN_HISTORY_MINUTES', 15)
    
    # Get state data
    prices = state.get('prices', [])
    timestamps = state.get('timestamps', [])
    outcome = market.get('outcome', 'YES')
    end_date = market.get('end_date', '')
    
    # Layer 0: Check we have enough history
    if not prices or not timestamps:
        return None
    
    # Check history duration
    history_duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
    if history_duration < min_history_minutes * 60:
        return None
    
    # Current price
    current_price = prices[-1]
    
    # Layer 0: Time-left gate
    time_left, time_source = parse_time_left(end_date, current_time)
    if time_left is not None and time_left < time_left_threshold:
        return None
    
    # Layer 1: Regime filter (trendiness)
    trendiness = compute_trendiness(prices, timestamps)
    if trendiness < trendiness_threshold:
        return None
    
    # Layer 2: Entry trigger
    return_5min = compute_return_5min(prices, timestamps)
    rolling_high, rolling_low = get_rolling_high_low(prices, timestamps, minutes=10)
    
    if rolling_high is None or rolling_low is None:
        return None
    
    # Check breakout
    is_breakout = False
    breakout_direction = "NONE"
    breakout_magnitude = 0
    
    if outcome == "YES":
        # LONG: price breaks above 10-min high
        if current_price > rolling_high:
            ticks_above = (current_price - rolling_high) * 100
            if ticks_above >= breakout_ticks:
                is_breakout = True
                breakout_direction = "LONG"
                breakout_magnitude = ticks_above
    else:
        # SHORT: price breaks below 10-min low
        if current_price < rolling_low:
            ticks_below = (rolling_low - current_price) * 100
            if ticks_below >= breakout_ticks:
                is_breakout = True
                breakout_direction = "SHORT"
                breakout_magnitude = ticks_below
    
    if not is_breakout:
        return None
    
    # Check return direction
    if outcome == "YES" and return_5min <= return_threshold:
        return None
    if outcome == "NO" and return_5min >= -return_threshold:
        return None
    
    # Check confidence
    confidence = compute_confidence(trendiness, breakout_magnitude, time_left, config)
    if confidence < confidence_threshold:
        return None
    
    # All conditions met - return signal
    return {
        'outcome': outcome,
        'price': current_price,
        'confidence': confidence,
        'trendiness': trendiness,
        'breakout': breakout_direction,
        'time_left': time_left,
        'time_left_source': time_source
    }


def create_strategy_state() -> Dict:
    """Create initial strategy state."""
    return {
        'prices': [],
        'timestamps': [],
        'ma_prices': [],
        'entry': None,  # {'price': x, 'time': y, 'outcome': 'YES/NO'}
        'cooldowns': {},  # token_id -> last_trade_time
    }


def update_strategy_state(
    state: Dict,
    price: float,
    timestamp: float,
    config: Dict
):
    """Update strategy state with new price.
    
    Args:
        state: Strategy state to update
        price: New price
        timestamp: New timestamp
        config: Strategy config
    """
    state['prices'].append(price)
    state['timestamps'].append(timestamp)
    state['ma_prices'].append(price)
    
    # Keep buffers bounded
    max_buffer = config.get('TREND_MIN_DATA_SECONDS', 30) * 10  # 30s * 10 = 5min buffer
    if len(state['prices']) > max_buffer:
        state['prices'] = state['prices'][-max_buffer:]
        state['timestamps'] = state['timestamps'][-max_buffer:]
        state['ma_prices'] = state['ma_prices'][-max_buffer:]


def compute_ma(prices: List[float], periods: int = 20) -> Optional[float]:
    """Compute simple moving average.
    
    Args:
        prices: List of prices
        periods: MA period
    
    Returns:
        MA value or None if insufficient data
    """
    if len(prices) < periods:
        return None
    return sum(prices[-periods:]) / periods

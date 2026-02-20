"""Order book fill simulation for paper trading."""


def simulate_fill(order_book_side, size, slippage_multiplier=1.0):
    """
    Walk the order book to simulate a fill.

    Args:
        order_book_side: [[price, size], ...] sorted ascending for asks,
                         descending for bids.
        size: number of shares to fill.
        slippage_multiplier: multiplier for slippage (for paper safety).

    Returns dict with fill details.
    """
    if not order_book_side or size <= 0:
        return {
            "filled": False,
            "fill_price": 0.0,
            "fill_size": 0.0,
            "slippage": 0.0,
            "levels_consumed": 0,
            "fills": [],
        }

    best_price = float(order_book_side[0][0])
    remaining = size
    total_cost = 0.0
    fills = []
    levels = 0

    for level in order_book_side:
        price = float(level[0])
        available = float(level[1])
        levels += 1

        take = min(remaining, available)
        total_cost += take * price
        fills.append((price, take))
        remaining -= take

        if remaining <= 0:
            break

    filled_size = size - remaining
    if filled_size <= 0:
        return {
            "filled": False,
            "fill_price": 0.0,
            "fill_size": 0.0,
            "slippage": 0.0,
            "levels_consumed": 0,
            "fills": [],
        }

    vwap = total_cost / filled_size
    raw_slippage = abs(vwap - best_price)
    # Apply multiplier for paper safety simulation
    slippage = raw_slippage * slippage_multiplier

    return {
        "filled": remaining <= 0,
        "fill_price": round(vwap, 6),
        "fill_size": round(filled_size, 2),
        "slippage": round(slippage, 6),
        "levels_consumed": levels,
        "fills": fills,
    }


def simulate_two_leg_fill(asks_yes, asks_no, size, yes_fee_bps=0, no_fee_bps=0, slippage_multiplier=1.0):
    """
    Simulate filling both legs of a locked-profit trade.

    Returns dict with both fills and combined metrics.
    """
    from src.paper_fees import calculate_trading_fee

    yes_fill = simulate_fill(asks_yes, size, slippage_multiplier)
    no_fill = simulate_fill(asks_no, size, slippage_multiplier)

    both_filled = yes_fill["filled"] and no_fill["filled"]

    yes_fee = calculate_trading_fee(yes_fill["fill_price"], yes_fill["fill_size"], yes_fee_bps)
    no_fee = calculate_trading_fee(no_fill["fill_price"], no_fill["fill_size"], no_fee_bps)

    total_cost = (
        yes_fill["fill_price"] * yes_fill["fill_size"]
        + no_fill["fill_price"] * no_fill["fill_size"]
        + yes_fee + no_fee
    )

    # Use the smaller fill size (can only lock profit on matched amounts)
    matched_size = min(yes_fill["fill_size"], no_fill["fill_size"])
    settlement = matched_size * 1.00
    expected_profit = settlement - total_cost if both_filled else 0.0

    return {
        "yes_fill": yes_fill,
        "no_fill": no_fill,
        "both_filled": both_filled,
        "matched_size": round(matched_size, 2),
        "yes_fee": round(yes_fee, 6),
        "no_fee": round(no_fee, 6),
        "total_cost": round(total_cost, 6),
        "expected_profit": round(expected_profit, 6),
    }

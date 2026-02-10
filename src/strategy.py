"""Strategy engine optimized for free infrastructure.

Free-infra edge: COVERAGE over speed. We scan all 1000+ markets while
fast bots focus on the top 50. We find arbitrage in the long tail.

Strategy: LOCKED_PROFIT
  Buy YES + NO when ask_yes + ask_no + cost_buffer < $1.00.
  Guaranteed $1.00 payout at settlement regardless of outcome.
  Aggressive thresholds: MIN_PROFIT=0.003, COST_BUFFER=0.002.
"""


def check_opportunity(market_book, config):
    """Check a single market for locked-profit arbitrage.

    Returns a plan dict if opportunity found, None otherwise.
    Plan includes overround tracking for data collection.
    """
    if not market_book:
        return None

    asks_yes = market_book.get('asks_yes', [])
    asks_no = market_book.get('asks_no', [])

    if not asks_yes or not asks_no:
        return None

    best_ask_yes = float(asks_yes[0][0])
    best_ask_no = float(asks_no[0][0])
    best_size_yes = float(asks_yes[0][1])
    best_size_no = float(asks_no[0][1])

    # Liquidity check: need minimum size available on each side
    min_liquidity = config.get("MIN_LIQUIDITY", 0.5)
    if best_size_yes < min_liquidity or best_size_no < min_liquidity:
        return None

    # Size: take the smaller of both sides, capped at max
    max_size = config.get("MAX_ORDER_SIZE", 25.0)
    tradeable_size = min(best_size_yes, best_size_no, max_size)

    # Cost buffer for slippage/rounding (most Polymarket markets: 0 fees)
    cost_buffer = config.get("COST_BUFFER", 0.002)
    min_profit = config.get("MIN_PROFIT", 0.003)

    total_unit_cost = best_ask_yes + best_ask_no + cost_buffer
    overround = round(best_ask_yes + best_ask_no - 1.0, 6)

    if total_unit_cost < 1.00:
        expected_profit = 1.00 - total_unit_cost

        if expected_profit >= min_profit:
            return {
                "type": "LOCKED_PROFIT",
                "condition_id": market_book.get("condition_id"),
                "yes_token_id": market_book.get("yes_token_id"),
                "no_token_id": market_book.get("no_token_id"),
                "buy_yes": best_ask_yes,
                "buy_no": best_ask_no,
                "size": tradeable_size,
                "expected_profit": expected_profit,
                "overround": overround,
            }

    return None

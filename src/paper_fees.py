"""Polymarket fee calculation for paper trading simulation."""


def calculate_trading_fee(price, size, fee_rate_bps):
    """
    Polymarket fee formula.
    Most markets: fee_rate_bps=0 (no fee).
    15-min crypto markets: fee_rate_bps=1000 (curved formula).

    Formula: fee_per_unit = fee_rate_bps / 10000 * price * (1 - price)
    """
    if fee_rate_bps <= 0:
        return 0.0

    fee_per_unit = (fee_rate_bps / 10000.0) * price * (1.0 - price)
    return fee_per_unit * size


def calculate_withdrawal_haircut(net_profit, haircut_pct=0.02):
    """2% of net profits, only applies to positive profits."""
    if net_profit <= 0:
        return 0.0
    return net_profit * haircut_pct


def estimate_locked_profit_cost(buy_yes_price, buy_no_price, size,
                                 yes_fee_bps=0, no_fee_bps=0):
    """
    Full cost breakdown for a locked-profit trade.
    Returns dict with all components.
    """
    yes_cost = buy_yes_price * size
    no_cost = buy_no_price * size
    yes_fee = calculate_trading_fee(buy_yes_price, size, yes_fee_bps)
    no_fee = calculate_trading_fee(buy_no_price, size, no_fee_bps)

    total_cost = yes_cost + no_cost + yes_fee + no_fee
    settlement_value = size * 1.00  # both sides settle to $1 total
    gross_profit = settlement_value - total_cost
    withdrawal_fee = calculate_withdrawal_haircut(gross_profit)
    net_profit = gross_profit - withdrawal_fee

    return {
        "yes_cost": round(yes_cost, 6),
        "no_cost": round(no_cost, 6),
        "yes_fee": round(yes_fee, 6),
        "no_fee": round(no_fee, 6),
        "total_cost": round(total_cost, 6),
        "settlement_value": round(settlement_value, 6),
        "gross_profit": round(gross_profit, 6),
        "withdrawal_fee": round(withdrawal_fee, 6),
        "net_profit": round(net_profit, 6),
    }

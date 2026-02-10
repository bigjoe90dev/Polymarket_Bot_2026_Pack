import time

_last_report = 0
_REPORT_INTERVAL = 10  # Print status every 10 seconds


def report_status(bot):
    global _last_report
    now = time.time()
    if now - _last_report < _REPORT_INTERVAL:
        return
    _last_report = now

    uptime = int(now - bot._start_time)
    mins, secs = divmod(uptime, 60)
    hours, mins = divmod(mins, 60)

    total = len(bot._current_markets)
    offset = bot._market_offset
    pct = round(offset / max(total, 1) * 100)
    hot_count = sum(1 for v in bot._market_heat.values() if v < 0.01)

    lb_wallets = len(bot.whale_tracker.tracked_wallets)
    net_wallets = len(bot.whale_tracker.network_wallets)
    copies = bot._copy_trades

    parts = [
        f"cycle={bot._cycle_count}",
        f"markets={total}",
        f"rotation={pct}%",
        f"hot={hot_count}",
        f"track={lb_wallets}+{net_wallets}",
        f"copies={copies}/{bot._copy_exits}",
        f"up={hours}h{mins:02d}m",
    ]

    if bot.execution.paper_engine:
        summary = bot.execution.paper_engine.get_portfolio_summary()
        parts.append(f"bal=${summary['cash_balance']:.2f}")
        parts.append(f"trades={summary['total_trades']}")
        rpnl = summary['realized_pnl']
        sign = "+" if rpnl >= 0 else ""
        parts.append(f"real={sign}${rpnl:.2f}")

    # Scorer stats
    if hasattr(bot, 'wallet_scorer'):
        ss = bot.wallet_scorer.get_summary()
        scored = ss.get("scored_wallets", 0)
        hot = ss.get("hot_wallets", 0)
        cut = ss.get("cutoff_wallets", 0)
        if scored > 0:
            parts.append(f"scored={scored}/hot={hot}/cut={cut}")

    print(f"[STATUS] {' | '.join(parts)}")

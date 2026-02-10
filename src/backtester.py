"""Backtesting engine — replays collected order book snapshots against strategy variants.

Usage:
    from src.backtester import Backtester
    bt = Backtester("data/snapshots/session_1738800000.jsonl")
    results = bt.compare_strategies([
        {"MIN_PROFIT": 0.01, "COST_BUFFER": 0.005},
        {"MIN_PROFIT": 0.02, "COST_BUFFER": 0.005},
        {"MIN_PROFIT": 0.01, "COST_BUFFER": 0.01},
    ])
"""

import json
import time
from src.data_collector import load_snapshots
from src.paper_fills import simulate_two_leg_fill


class Backtester:
    """Replays order book snapshots to evaluate strategy parameters."""

    def __init__(self, snapshot_file):
        self.snapshots = load_snapshots(snapshot_file)
        self.snapshot_file = snapshot_file
        print(f"[BACKTEST] Loaded {len(self.snapshots)} snapshots from {snapshot_file}")

    def run(self, config):
        """Run a single strategy config against all snapshots.

        Config keys:
            MIN_PROFIT:     Minimum profit threshold per unit (default 0.02)
            COST_BUFFER:    Cost buffer added to spread (default 0.005)
            MIN_LIQUIDITY:  Minimum size at best ask (default 1.0)
            MAX_ORDER_SIZE: Max shares per trade (default 10.0)
            STARTING_BALANCE: Paper balance to start with (default 1000.0)
        """
        min_profit = config.get("MIN_PROFIT", 0.02)
        cost_buffer = config.get("COST_BUFFER", 0.005)
        min_liq = config.get("MIN_LIQUIDITY", 1.0)
        max_size = config.get("MAX_ORDER_SIZE", 10.0)
        balance = config.get("STARTING_BALANCE", 1000.0)
        starting_balance = balance

        trades = []
        opportunities_found = 0
        opportunities_filled = 0
        total_fees = 0.0
        positions = {}  # cid -> total_cost, for tracking exposure
        markets_seen = set()

        for snap in self.snapshots:
            cid = snap.get("cid", "")
            markets_seen.add(cid)

            yes_ask = snap.get("yes_ask")
            no_ask = snap.get("no_ask")
            yes_depth = snap.get("yes_depth", [])
            no_depth = snap.get("no_depth", [])

            if yes_ask is None or no_ask is None:
                continue
            if not yes_depth or not no_depth:
                continue

            # Liquidity check
            best_size_yes = float(yes_depth[0][1]) if yes_depth else 0
            best_size_no = float(no_depth[0][1]) if no_depth else 0
            if best_size_yes < min_liq or best_size_no < min_liq:
                continue

            # Strategy check
            total_unit_cost = yes_ask + no_ask + cost_buffer
            if total_unit_cost >= 1.00:
                continue

            expected_profit = 1.00 - total_unit_cost
            if expected_profit < min_profit:
                continue

            opportunities_found += 1

            # Determine size
            tradeable_size = min(best_size_yes, best_size_no, max_size)

            # Simulate fill against recorded order book
            result = simulate_two_leg_fill(
                yes_depth, no_depth, tradeable_size
            )

            if not result["both_filled"]:
                continue

            total_cost = result["total_cost"]

            # Balance check
            if total_cost > balance:
                continue

            opportunities_filled += 1
            balance -= total_cost
            total_fees += result["yes_fee"] + result["no_fee"]

            # For locked-profit, payout = matched_size * $1 at settlement
            payout = result["matched_size"] * 1.0
            realized_profit = payout - total_cost

            # Add payout back (simulate instant settlement for backtest)
            balance += payout

            trade = {
                "timestamp": snap.get("ts"),
                "condition_id": cid,
                "yes_price": result["yes_fill"]["fill_price"],
                "no_price": result["no_fill"]["fill_price"],
                "size": result["matched_size"],
                "total_cost": round(total_cost, 6),
                "payout": round(payout, 6),
                "profit": round(realized_profit, 6),
                "fees": round(result["yes_fee"] + result["no_fee"], 6),
                "yes_slippage": result["yes_fill"]["slippage"],
                "no_slippage": result["no_fill"]["slippage"],
            }
            trades.append(trade)

        # Compute summary metrics
        total_profit = sum(t["profit"] for t in trades)
        total_slippage = sum(t["yes_slippage"] + t["no_slippage"] for t in trades)
        winning = [t for t in trades if t["profit"] > 0]
        losing = [t for t in trades if t["profit"] <= 0]

        # Time range
        timestamps = [s["ts"] for s in self.snapshots if "ts" in s]
        duration_hours = (max(timestamps) - min(timestamps)) / 3600 if len(timestamps) >= 2 else 0

        return {
            "config": config,
            "snapshot_file": self.snapshot_file,
            "total_snapshots": len(self.snapshots),
            "unique_markets": len(markets_seen),
            "duration_hours": round(duration_hours, 2),
            "opportunities_found": opportunities_found,
            "opportunities_filled": opportunities_filled,
            "fill_rate_pct": round(opportunities_filled / max(opportunities_found, 1) * 100, 1),
            "total_trades": len(trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate_pct": round(len(winning) / max(len(trades), 1) * 100, 1),
            "starting_balance": starting_balance,
            "ending_balance": round(balance, 2),
            "total_profit": round(total_profit, 4),
            "total_fees": round(total_fees, 4),
            "net_profit": round(total_profit - total_fees, 4),
            "total_slippage": round(total_slippage, 6),
            "avg_profit_per_trade": round(total_profit / max(len(trades), 1), 6),
            "best_trade": max(trades, key=lambda t: t["profit"], default=None),
            "worst_trade": min(trades, key=lambda t: t["profit"], default=None),
            "trades": trades,
        }

    def compare_strategies(self, configs):
        """Run multiple configs and return comparison."""
        results = []
        for i, cfg in enumerate(configs):
            print(f"[BACKTEST] Running strategy {i+1}/{len(configs)}: {cfg}")
            result = self.run(cfg)
            results.append(result)

        return results

    def generate_report(self, results):
        """Generate a text report comparing strategy results."""
        lines = []
        lines.append("=" * 70)
        lines.append("BACKTEST REPORT")
        lines.append("=" * 70)
        lines.append(f"Snapshot file: {self.snapshot_file}")
        lines.append(f"Total snapshots: {len(self.snapshots)}")
        lines.append("")

        for i, r in enumerate(results):
            lines.append(f"--- Strategy {i+1} ---")
            cfg = r["config"]
            lines.append(f"  Config: MIN_PROFIT={cfg.get('MIN_PROFIT')}, "
                         f"COST_BUFFER={cfg.get('COST_BUFFER')}, "
                         f"MIN_LIQUIDITY={cfg.get('MIN_LIQUIDITY')}, "
                         f"MAX_ORDER_SIZE={cfg.get('MAX_ORDER_SIZE')}")
            lines.append(f"  Duration: {r['duration_hours']}h across {r['unique_markets']} markets")
            lines.append(f"  Opportunities: {r['opportunities_found']} found, "
                         f"{r['opportunities_filled']} filled ({r['fill_rate_pct']}%)")
            lines.append(f"  Trades: {r['total_trades']} "
                         f"({r['winning_trades']}W / {r['losing_trades']}L, "
                         f"{r['win_rate_pct']}% win rate)")
            lines.append(f"  Profit: ${r['total_profit']:.4f} "
                         f"(fees: ${r['total_fees']:.4f}, "
                         f"net: ${r['net_profit']:.4f})")
            lines.append(f"  Avg per trade: ${r['avg_profit_per_trade']:.6f}")
            lines.append(f"  Balance: ${r['starting_balance']:.2f} -> ${r['ending_balance']:.2f}")
            lines.append("")

        # Winner
        if results:
            best = max(results, key=lambda r: r["net_profit"])
            best_idx = results.index(best) + 1
            lines.append(f"BEST STRATEGY: #{best_idx} with ${best['net_profit']:.4f} net profit")

        lines.append("=" * 70)
        return "\n".join(lines)

    def export_for_llm(self, results):
        """Export backtest results in a format optimized for LLM analysis."""
        export = {
            "type": "polymarket_backtest_results",
            "generated_at": time.time(),
            "snapshot_file": self.snapshot_file,
            "total_snapshots": len(self.snapshots),
            "strategies_tested": len(results),
            "results": [],
        }

        for r in results:
            # Strip individual trades for size, keep summary + top/bottom trades
            entry = {k: v for k, v in r.items() if k != "trades"}
            entry["sample_trades"] = r["trades"][:10]  # First 10 for context
            export["results"].append(entry)

        export["analysis_prompt"] = (
            "Analyze these Polymarket arbitrage backtest results. "
            "Compare the strategy configurations and recommend which parameters "
            "to use for a locked-profit arbitrage bot. Consider: trade frequency, "
            "profit per trade, fill rate, and total net profit. "
            "Suggest additional parameters worth testing."
        )

        return export

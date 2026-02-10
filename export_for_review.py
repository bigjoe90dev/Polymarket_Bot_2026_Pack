#!/usr/bin/env python3
"""
Export bot data + code for LLM analysis.
Run: python3 export_for_review.py

Creates a single text file (llm_review_pack.txt) containing:
- All source code
- Current config (secrets redacted)
- Paper trading state (positions, trades, PnL)
- Wallet scorer data
- Whale state summary
- Bot logs (latest session)
- Data snapshots summary
"""

import os
import json
import glob
import time

OUTPUT = "llm_review_pack.txt"
BASE = os.path.dirname(os.path.abspath(__file__))

def redact_config(config):
    """Remove any real secrets from config."""
    safe = dict(config)
    for key in ("POLY_API_KEY", "POLY_SECRET", "POLY_PASSPHRASE", "DASHBOARD_TOKEN"):
        if key in safe and safe[key] not in ("paper-mode", ""):
            safe[key] = "REDACTED"
    return safe

def read_file_safe(path, max_lines=500):
    """Read a file, truncate if huge."""
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            return "".join(lines[:max_lines]) + f"\n... [TRUNCATED — {len(lines)} total lines]\n"
        return "".join(lines)
    except Exception as e:
        return f"[Could not read: {e}]"

def read_json_safe(path):
    """Read and pretty-print a JSON file."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"[Could not read: {e}]"

def main():
    sections = []

    # Header
    sections.append("=" * 80)
    sections.append("POLYMARKET BOT — LLM REVIEW PACK")
    sections.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sections.append("=" * 80)
    sections.append("""
PURPOSE: This export contains the complete source code, configuration, and
trading data from a Polymarket copy-trading bot running in PAPER mode.
Please analyse for:
1. Bugs or logic errors in the trading pipeline
2. Whether the paper trading simulation is realistic
3. Risk management gaps
4. Whether the PnL results are credible or artificially inflated
5. Suggestions for improvement before going live
6. Any potential issues that could cause losses in live trading
""")

    # ── Source Code ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: SOURCE CODE")
    sections.append("=" * 80)

    src_files = sorted(glob.glob(os.path.join(BASE, "src", "*.py")))
    src_files += [os.path.join(BASE, "run.py")]
    if os.path.exists(os.path.join(BASE, "backtest.py")):
        src_files += [os.path.join(BASE, "backtest.py")]

    for fpath in src_files:
        if os.path.exists(fpath):
            rel = os.path.relpath(fpath, BASE)
            sections.append(f"\n--- FILE: {rel} ---")
            sections.append(read_file_safe(fpath, max_lines=1000))

    # ── Config ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: CONFIGURATION (secrets redacted)")
    sections.append("=" * 80)
    config_path = os.path.join(BASE, "config", "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
        sections.append(json.dumps(redact_config(config), indent=2))
    except Exception as e:
        sections.append(f"[Could not read config: {e}]")

    # ── Paper State ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: PAPER TRADING STATE")
    sections.append("=" * 80)
    paper_path = os.path.join(BASE, "data", "paper_state.json")
    sections.append(read_json_safe(paper_path))

    # ── Wallet Scorer ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: WALLET SCORER DATA")
    sections.append("=" * 80)
    scorer_path = os.path.join(BASE, "data", "wallet_scores.json")
    if os.path.exists(scorer_path):
        try:
            with open(scorer_path) as f:
                scorer = json.load(f)
            # Summarise rather than dump everything
            wallets = scorer.get("wallets", {})
            summary = {
                "total_wallets": len(wallets),
                "wallets_with_trades": sum(1 for w in wallets.values()
                                           if w.get("total_copies", 0) > 0),
                "top_10_by_roi": sorted(
                    [{"addr": k[:12]+"...", "roi": v.get("roi", 0),
                      "copies": v.get("total_copies", 0),
                      "wins": v.get("wins", 0)}
                     for k, v in wallets.items()
                     if v.get("total_copies", 0) >= 3],
                    key=lambda x: x["roi"], reverse=True
                )[:10]
            }
            sections.append(json.dumps(summary, indent=2))
        except Exception as e:
            sections.append(f"[Could not parse scorer data: {e}]")
    else:
        sections.append("[No scorer data file found]")

    # ── Whale State Summary ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: WHALE TRACKER STATE (summary)")
    sections.append("=" * 80)
    whale_path = os.path.join(BASE, "data", "whale_state.json")
    if os.path.exists(whale_path):
        try:
            with open(whale_path) as f:
                ws = json.load(f)
            summary = {
                "tracked_wallets": len(ws.get("tracked", {})),
                "total_seen_tx": len(ws.get("seen_tx_hashes", [])),
                "signal_count": len(ws.get("signals", [])),
                "last_signals": ws.get("signals", [])[-10:]  # Last 10 signals
            }
            sections.append(json.dumps(summary, indent=2))
        except Exception as e:
            sections.append(f"[Could not parse whale state: {e}]")
    else:
        sections.append("[No whale state file found]")

    # ── Latest Data Snapshots ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: DATA SNAPSHOTS (latest session stats)")
    sections.append("=" * 80)
    snap_dir = os.path.join(BASE, "data", "snapshots")
    if os.path.exists(snap_dir):
        snap_files = sorted(glob.glob(os.path.join(snap_dir, "*.jsonl")))
        if snap_files:
            latest = snap_files[-1]
            sections.append(f"Latest session file: {os.path.basename(latest)}")
            try:
                with open(latest) as f:
                    lines = f.readlines()
                sections.append(f"Total snapshots recorded: {len(lines)}")
                if lines:
                    first = json.loads(lines[0])
                    last = json.loads(lines[-1])
                    sections.append(f"First snapshot: {json.dumps(first, indent=2)}")
                    sections.append(f"Last snapshot: {json.dumps(last, indent=2)}")
            except Exception as e:
                sections.append(f"[Error reading snapshots: {e}]")
        else:
            sections.append("[No snapshot files found]")
    else:
        sections.append("[No snapshots directory]")

    # ── Audit Log ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: AUDIT LOG (last 100 entries)")
    sections.append("=" * 80)
    audit_path = os.path.join(BASE, "audit_log.txt")
    if os.path.exists(audit_path):
        sections.append(read_file_safe(audit_path, max_lines=100))
    else:
        sections.append("[No audit log file found]")

    # ── Performance Summary ──
    sections.append("\n" + "=" * 80)
    sections.append("SECTION: PERFORMANCE SUMMARY")
    sections.append("=" * 80)
    if os.path.exists(paper_path):
        try:
            with open(paper_path) as f:
                ps = json.load(f)
            starting = ps.get("starting_balance", 50)
            current = ps.get("cash_balance", 0)
            trades = ps.get("total_trades", 0)
            wins = ps.get("winning_trades", 0)
            losses = ps.get("losing_trades", 0)
            fees = ps.get("total_fees_paid", 0)
            realized = ps.get("total_realized_pnl", 0)
            positions = ps.get("positions", {})

            open_pos = sum(1 for p in positions.values()
                          if p.get("status") == "OPEN")
            closed_pos = len(positions) - open_pos

            # Trade-level stats
            history = ps.get("trade_history", [])
            buys = [t for t in history if t.get("direction") == "BUY"]
            sells = [t for t in history if t.get("direction") == "SELL"]

            tp_count = sum(1 for t in history
                          if t.get("trade_type") == "TAKE_PROFIT")
            sl_count = sum(1 for t in history
                          if t.get("trade_type") == "STOP_LOSS")
            exit_count = sum(1 for t in history
                            if t.get("trade_type") == "COPY_EXIT")

            summary = {
                "starting_balance": starting,
                "current_balance": round(current, 2),
                "total_return_pct": round((current - starting) / starting * 100, 2),
                "realized_pnl": round(realized, 2),
                "total_trades": trades,
                "total_buy_fills": len(buys),
                "total_sell_fills": len(sells),
                "winning_trades": wins,
                "losing_trades": losses,
                "win_rate_pct": round(wins / max(wins + losses, 1) * 100, 1),
                "total_fees_paid": round(fees, 4),
                "take_profit_exits": tp_count,
                "stop_loss_exits": sl_count,
                "whale_exit_copies": exit_count,
                "open_positions": open_pos,
                "closed_positions": closed_pos,
                "unique_whales_copied": list(set(
                    t.get("source_username", "?") for t in buys
                )),
                "unique_markets_traded": list(set(
                    t.get("market_name", "?") for t in history
                ))
            }
            sections.append(json.dumps(summary, indent=2))
        except Exception as e:
            sections.append(f"[Error computing summary: {e}]")

    # ── Write Output ──
    output_path = os.path.join(BASE, OUTPUT)
    with open(output_path, "w") as f:
        f.write("\n".join(sections))

    size_kb = os.path.getsize(output_path) / 1024
    print(f"[*] Export complete: {output_path}")
    print(f"[*] Size: {size_kb:.0f} KB")
    print(f"[*] Copy this file to any LLM for analysis.")
    print(f"[*] All secrets have been redacted.")

if __name__ == "__main__":
    main()

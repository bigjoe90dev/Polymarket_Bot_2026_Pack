"""Backtest runner — replay collected data against strategy variants.

Usage:
    python3 backtest.py                    # Use latest snapshot, default strategies
    python3 backtest.py --file session.jsonl  # Use specific snapshot file
    python3 backtest.py --export           # Save results as JSON for LLM review
"""

import sys
import os
import json
import time

from src.data_collector import list_snapshot_files
from src.backtester import Backtester


# Strategy variants to test
DEFAULT_STRATEGIES = [
    {
        "name": "Conservative",
        "MIN_PROFIT": 0.02,
        "COST_BUFFER": 0.01,
        "MIN_LIQUIDITY": 5.0,
        "MAX_ORDER_SIZE": 10.0,
    },
    {
        "name": "Balanced",
        "MIN_PROFIT": 0.015,
        "COST_BUFFER": 0.005,
        "MIN_LIQUIDITY": 2.0,
        "MAX_ORDER_SIZE": 15.0,
    },
    {
        "name": "Aggressive",
        "MIN_PROFIT": 0.005,
        "COST_BUFFER": 0.003,
        "MIN_LIQUIDITY": 1.0,
        "MAX_ORDER_SIZE": 25.0,
    },
    {
        "name": "Micro-Profit",
        "MIN_PROFIT": 0.001,
        "COST_BUFFER": 0.001,
        "MIN_LIQUIDITY": 0.5,
        "MAX_ORDER_SIZE": 50.0,
    },
]


def main():
    print("=" * 60)
    print("  POLYMARKET BACKTESTER")
    print("  Replay collected data against strategy variants")
    print("=" * 60)

    # Parse args
    export_mode = "--export" in sys.argv
    specific_file = None
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            specific_file = sys.argv[idx + 1]

    # Find snapshot file
    if specific_file:
        if not os.path.exists(specific_file):
            # Try in snapshots dir
            specific_file = os.path.join("data/snapshots", specific_file)
        if not os.path.exists(specific_file):
            print(f"[!] File not found: {specific_file}")
            sys.exit(1)
        snapshot_file = specific_file
    else:
        files = list_snapshot_files()
        if not files:
            print("[!] No snapshot files found in data/snapshots/")
            print("[*] Run the bot first to collect data:")
            print("    python3 run.py")
            print("[*] The bot records order book snapshots as it runs.")
            print("[*] Let it run for a few hours, then come back and run this.")
            sys.exit(1)

        # Use the largest file (most data)
        files.sort(key=lambda f: f["snapshots"], reverse=True)
        snapshot_file = files[0]["path"]
        print(f"\nAvailable snapshot files:")
        for f in files:
            marker = " <-- using this" if f["path"] == snapshot_file else ""
            print(f"  {f['filename']}: {f['snapshots']} snapshots, "
                  f"{f['size_kb']}KB{marker}")

    print(f"\n[*] Loading: {snapshot_file}")

    # Run backtest
    bt = Backtester(snapshot_file)

    if len(bt.snapshots) < 10:
        print(f"[!] Only {len(bt.snapshots)} snapshots — need more data.")
        print("[*] Let the bot run longer to collect more order book data.")
        sys.exit(1)

    print(f"\n[*] Testing {len(DEFAULT_STRATEGIES)} strategy variants...\n")
    results = bt.compare_strategies(DEFAULT_STRATEGIES)

    # Print report
    report = bt.generate_report(results)
    print("\n" + report)

    # Export
    if export_mode:
        export_data = bt.export_for_llm(results)
        export_path = f"data/backtest_results_{int(time.time())}.json"
        os.makedirs("data", exist_ok=True)
        with open(export_path, "w") as f:
            json.dump(export_data, f, indent=2)
        print(f"\n[*] Results exported to: {export_path}")
        print("[*] You can paste this file into ChatGPT/Claude for analysis.")
    else:
        print("\nTip: Run with --export to save results as JSON for LLM review")


if __name__ == "__main__":
    main()

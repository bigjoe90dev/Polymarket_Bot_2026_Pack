"""Data collector for order book snapshots and strategy decisions.

Records every market scan cycle so the data can be replayed
by the backtester or exported for LLM analysis.

Data format (JSONL — one JSON object per line):
{
  "ts": 1738800000.0,
  "cid": "0xabc...",
  "yes_ask": 0.45,   "no_ask": 0.52,
  "yes_bid": 0.44,   "no_bid": 0.51,
  "yes_depth": [[0.45, 100], [0.46, 50]],
  "no_depth":  [[0.52, 80], [0.53, 40]],
  "spread": 0.03,
  "opp": null | { plan dict }
}
"""

import os
import json
import time


SNAPSHOT_DIR = "data/snapshots"


class DataCollector:
    """Records order book snapshots to JSONL files for backtesting."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._buffer = []
        self._flush_interval = 30  # seconds
        self._last_flush = time.time()
        self._snap_count = 0

        if self.enabled:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            self._filepath = os.path.join(
                SNAPSHOT_DIR,
                f"session_{int(time.time())}.jsonl"
            )
            print(f"[DATA] Recording snapshots to {self._filepath}")

    def record(self, market, book, opportunity=None):
        """Record a single order book observation."""
        if not self.enabled or not book:
            return

        asks_yes = book.get("asks_yes", [])
        asks_no = book.get("asks_no", [])
        bids_yes = book.get("bids_yes", [])
        bids_no = book.get("bids_no", [])

        snap = {
            "ts": round(time.time(), 2),
            "cid": market.get("condition_id", ""),
            "yes_ask": float(asks_yes[0][0]) if asks_yes else None,
            "no_ask": float(asks_no[0][0]) if asks_no else None,
            "yes_bid": float(bids_yes[0][0]) if bids_yes else None,
            "no_bid": float(bids_no[0][0]) if bids_no else None,
            "yes_depth": [[float(a[0]), float(a[1])] for a in asks_yes[:5]],
            "no_depth": [[float(a[0]), float(a[1])] for a in asks_no[:5]],
            "yes_bid_depth": [[float(b[0]), float(b[1])] for b in bids_yes[:5]],
            "no_bid_depth": [[float(b[0]), float(b[1])] for b in bids_no[:5]],
        }

        # Add spread calculation
        if snap["yes_ask"] is not None and snap["no_ask"] is not None:
            snap["spread"] = round(snap["yes_ask"] + snap["no_ask"] - 1.0, 6)
        else:
            snap["spread"] = None

        # Record strategy decision
        if opportunity:
            snap["opp"] = {
                "type": opportunity.get("type"),
                "buy_yes": opportunity.get("buy_yes"),
                "buy_no": opportunity.get("buy_no"),
                "size": opportunity.get("size"),
                "expected_profit": opportunity.get("expected_profit"),
            }
        else:
            snap["opp"] = None

        self._buffer.append(snap)
        self._snap_count += 1

        # Flush to disk periodically
        now = time.time()
        if now - self._last_flush >= self._flush_interval:
            self.flush()

    def flush(self):
        """Write buffered snapshots to disk."""
        if not self.enabled or not self._buffer:
            return

        try:
            with open(self._filepath, "a") as f:
                for snap in self._buffer:
                    f.write(json.dumps(snap) + "\n")
            self._buffer = []
            self._last_flush = time.time()
        except Exception as e:
            print(f"[!] Data collector flush error: {e}")

    def get_stats(self):
        """Return collection stats for the web UI."""
        file_size = 0
        if self.enabled and os.path.exists(self._filepath):
            file_size = os.path.getsize(self._filepath)

        return {
            "enabled": self.enabled,
            "snapshots_recorded": self._snap_count,
            "buffer_size": len(self._buffer),
            "file": self._filepath if self.enabled else None,
            "file_size_kb": round(file_size / 1024, 1),
        }

    def get_session_file(self):
        """Return the current session file path."""
        return self._filepath if self.enabled else None


def list_snapshot_files():
    """List all snapshot session files available for backtesting."""
    if not os.path.exists(SNAPSHOT_DIR):
        return []
    files = []
    for f in sorted(os.listdir(SNAPSHOT_DIR)):
        if f.endswith(".jsonl"):
            path = os.path.join(SNAPSHOT_DIR, f)
            size = os.path.getsize(path)
            # Count lines
            with open(path, "r") as fh:
                lines = sum(1 for _ in fh)
            files.append({
                "filename": f,
                "path": path,
                "size_kb": round(size / 1024, 1),
                "snapshots": lines,
            })
    return files


def load_snapshots(filepath):
    """Load all snapshots from a JSONL file."""
    snapshots = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                snapshots.append(json.loads(line))
    return snapshots

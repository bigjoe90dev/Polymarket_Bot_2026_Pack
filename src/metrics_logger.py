"""Production-grade metrics logging and monitoring.

Provides structured logging with multiple output formats:
- CSV files for time-series analysis
- JSON for structured queries
- Console for real-time monitoring

Tracks all critical metrics:
- Trade execution quality (latency, slippage, fill rate)
- Signal quality (staleness, decode accuracy, dedup rate)
- System health (uptime, reconnects, errors, queue depth)
- Performance (throughput, memory, CPU-ish proxies)
"""

import os
import csv
import json
import time
import threading
from datetime import datetime
from collections import defaultdict, deque


class MetricsLogger:
    """Thread-safe metrics collection and logging system."""

    def __init__(self, config):
        self.config = config
        self.enabled = config.get("METRICS_LOGGING_ENABLED", True)
        self.log_dir = "data/metrics"
        self.interval = config.get("METRICS_LOG_INTERVAL_SEC", 60)

        # Thread safety
        self._lock = threading.RLock()
        self._running = False
        self._thread = None

        # Metric counters (reset periodically)
        self.counters = defaultdict(int)
        self.gauges = {}
        self.timings = defaultdict(list)  # Store recent timings for percentiles
        self.events = deque(maxlen=1000)  # Recent significant events

        # Aggregated stats (cumulative)
        self.cumulative = {
            "total_trades_attempted": 0,
            "total_trades_executed": 0,
            "total_signals_received": 0,
            "total_signals_deduplicated": 0,
            "total_blockchain_reconnects": 0,
            "total_parity_mismatches": 0,
            "uptime_start": time.time(),
        }

        # Initialize log directory
        os.makedirs(self.log_dir, exist_ok=True)

        # Start background logger
        if self.enabled:
            self.start()

    # ── Public API ────────────────────────────────────────────

    def increment(self, metric_name, value=1):
        """Increment a counter metric."""
        with self._lock:
            self.counters[metric_name] += value

    def set_gauge(self, metric_name, value):
        """Set a gauge metric (point-in-time value)."""
        with self._lock:
            self.gauges[metric_name] = value

    def record_timing(self, metric_name, duration_ms):
        """Record a timing measurement (in milliseconds)."""
        with self._lock:
            self.timings[metric_name].append(duration_ms)
            # Keep only last 100 samples per metric
            if len(self.timings[metric_name]) > 100:
                self.timings[metric_name] = self.timings[metric_name][-100:]

    def record_event(self, event_type, details):
        """Record a significant event for debugging."""
        with self._lock:
            self.events.append({
                "timestamp": time.time(),
                "type": event_type,
                "details": details,
            })

    def increment_cumulative(self, metric_name, value=1):
        """Increment a cumulative (never-reset) counter."""
        with self._lock:
            if metric_name in self.cumulative:
                self.cumulative[metric_name] += value

    # ── Context Managers for Timing ──────────────────────────

    class Timer:
        """Context manager for easy timing measurements."""
        def __init__(self, logger, metric_name):
            self.logger = logger
            self.metric_name = metric_name
            self.start = None

        def __enter__(self):
            self.start = time.time()
            return self

        def __exit__(self, *args):
            duration_ms = (time.time() - self.start) * 1000
            self.logger.record_timing(self.metric_name, duration_ms)

    def timer(self, metric_name):
        """Create a timing context manager.

        Usage:
            with metrics.timer("whale_poll_duration"):
                # ... do work ...
                pass
        """
        return self.Timer(self, metric_name)

    # ── Background Logging ────────────────────────────────────

    def start(self):
        """Start background logging thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._logging_loop, daemon=True)
        self._thread.start()
        print("[METRICS] Logger started (interval: {}s)".format(self.interval))

    def stop(self):
        """Stop background logging and flush final metrics."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._flush_metrics()
        print("[METRICS] Logger stopped")

    def _logging_loop(self):
        """Background thread that periodically flushes metrics."""
        while self._running:
            time.sleep(self.interval)
            self._flush_metrics()

    def _flush_metrics(self):
        """Write current metrics to disk and reset counters."""
        with self._lock:
            # Snapshot current state
            snapshot = {
                "timestamp": time.time(),
                "datetime": datetime.now().isoformat(),
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "timings_summary": self._summarize_timings(),
                "cumulative": dict(self.cumulative),
            }

            # Write to CSV
            self._write_csv(snapshot)

            # Write to JSON (for detailed analysis)
            self._write_json(snapshot)

            # Reset interval-based metrics (keep cumulative)
            self.counters.clear()
            self.timings.clear()

    def _summarize_timings(self):
        """Calculate percentiles for timing metrics."""
        summary = {}
        for metric, values in self.timings.items():
            if not values:
                continue

            sorted_vals = sorted(values)
            n = len(sorted_vals)

            summary[metric] = {
                "count": n,
                "min": round(sorted_vals[0], 2),
                "p50": round(sorted_vals[n // 2], 2),
                "p95": round(sorted_vals[int(n * 0.95)], 2) if n > 1 else sorted_vals[0],
                "p99": round(sorted_vals[int(n * 0.99)], 2) if n > 1 else sorted_vals[0],
                "max": round(sorted_vals[-1], 2),
                "avg": round(sum(sorted_vals) / n, 2),
            }

        return summary

    def _write_csv(self, snapshot):
        """Append metrics to CSV file (one file per day)."""
        try:
            date_str = datetime.fromtimestamp(snapshot["timestamp"]).strftime("%Y-%m-%d")
            csv_path = os.path.join(self.log_dir, f"metrics_{date_str}.csv")

            # Flatten the snapshot for CSV
            row = {
                "timestamp": snapshot["timestamp"],
                "datetime": snapshot["datetime"],
            }

            # Add counters
            for k, v in snapshot["counters"].items():
                row[f"counter_{k}"] = v

            # Add gauges
            for k, v in snapshot["gauges"].items():
                row[f"gauge_{k}"] = v

            # Add timing summaries (p50, p95 only for CSV)
            for metric, stats in snapshot["timings_summary"].items():
                row[f"timing_{metric}_p50"] = stats["p50"]
                row[f"timing_{metric}_p95"] = stats["p95"]

            # Add key cumulative stats
            row["cumulative_trades_executed"] = snapshot["cumulative"].get("total_trades_executed", 0)
            row["cumulative_signals_received"] = snapshot["cumulative"].get("total_signals_received", 0)

            # Write (append mode, create header if new file)
            file_exists = os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=sorted(row.keys()))
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)

        except Exception as e:
            print(f"[METRICS] CSV write error: {e}")

    def _write_json(self, snapshot):
        """Append full metrics snapshot to JSON lines file."""
        try:
            date_str = datetime.fromtimestamp(snapshot["timestamp"]).strftime("%Y-%m-%d")
            json_path = os.path.join(self.log_dir, f"metrics_{date_str}.jsonl")

            with open(json_path, "a") as f:
                f.write(json.dumps(snapshot) + "\n")

        except Exception as e:
            print(f"[METRICS] JSON write error: {e}")

    # ── Reporting ─────────────────────────────────────────────

    def get_current_stats(self):
        """Get current metrics snapshot (for dashboard/API)."""
        with self._lock:
            return {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "timings": self._summarize_timings(),
                "cumulative": dict(self.cumulative),
                "recent_events": list(self.events)[-20:],  # Last 20 events
            }

    def get_health_summary(self):
        """Get high-level health metrics for monitoring."""
        with self._lock:
            uptime = time.time() - self.cumulative.get("uptime_start", time.time())

            return {
                "uptime_seconds": round(uptime, 0),
                "uptime_hours": round(uptime / 3600, 1),
                "signals_per_hour": round(
                    self.cumulative.get("total_signals_received", 0) / max(uptime / 3600, 0.01), 1
                ),
                "trades_per_hour": round(
                    self.cumulative.get("total_trades_executed", 0) / max(uptime / 3600, 0.01), 1
                ),
                "dedup_rate": round(
                    self.cumulative.get("total_signals_deduplicated", 0) /
                    max(self.cumulative.get("total_signals_received", 1), 1) * 100, 1
                ),
                "reconnects": self.cumulative.get("total_blockchain_reconnects", 0),
                "parity_mismatches": self.cumulative.get("total_parity_mismatches", 0),
            }


# ── Convenience Functions ─────────────────────────────────────

def create_metrics_logger(config):
    """Factory function to create and start metrics logger."""
    return MetricsLogger(config)

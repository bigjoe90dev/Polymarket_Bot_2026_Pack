"""Web server for the 60s retro paper trading dashboard.
Uses stdlib http.server — no Flask dependency needed.

Security: All routes require ?token=<DASHBOARD_TOKEN> query param.
Dashboard is READ-ONLY — no control endpoints, no write access.
Default bind: 127.0.0.1 (localhost only). Set DASHBOARD_BIND=0.0.0.0 for cloud."""

import threading
import json
import time
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class RetroRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler with token-authenticated JSON API routes."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── Token authentication ──
        # Every request must include ?token=<DASHBOARD_TOKEN>
        expected_token = self.server.dashboard_token
        provided_token = params.get("token", [""])[0]

        if not expected_token or provided_token != expected_token:
            self._send_json({"error": "Unauthorized. Add ?token=YOUR_TOKEN to the URL."}, 401)
            return

        routes = {
            "/": self._serve_index,
            "/api/status": self._api_status,
            "/api/portfolio": self._api_portfolio,
            "/api/positions": self._api_positions,
            "/api/trades": self._api_trades,
            "/api/markets": self._api_markets,
            "/api/risk": self._api_risk,
            "/api/charts/pnl": self._api_chart_pnl,
            "/api/metrics": self._api_metrics,
            "/api/export": self._api_export,
            "/api/data": self._api_data_stats,
            "/api/whales": self._api_whales,
            "/api/scorer": self._api_scorer,
            "/api/flows": self._api_flows,
            "/api/stress": self._api_stress,
            "/api/blockchain": self._api_blockchain,
            "/api/live_trades": self._api_live_trades,
        }

        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_error(404)

    # ── Static Files ─────────────────────────────────────────────

    def _serve_index(self):
        html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "index.html"
        )
        try:
            with open(html_path, "r") as f:
                content = f.read()
            self._send_html(content)
        except FileNotFoundError:
            self._send_html("<h1>static/index.html not found</h1>", 404)

    # ── API Routes ───────────────────────────────────────────────

    def _api_status(self):
        bot = self.server.bot_ref
        pe = self.server.paper_engine

        # Speed metrics
        total_markets = len(bot._current_markets)
        hot_count = sum(1 for v in bot._market_heat.values() if v < 0.01)
        rotation_pct = round(bot._market_offset / max(total_markets, 1) * 100)

        data = {
            "mode": bot.config.get("MODE", "?"),
            "running": bot.running,
            "uptime_seconds": round(time.time() - bot._start_time, 0),
            "markets_watching": total_markets,
            "cycle_count": bot._cycle_count,
            "markets_per_cycle": bot._markets_per_cycle,
            "rotation_pct": rotation_pct,
            "hot_markets": hot_count,
            "snapshots": bot.collector._snap_count,
        }

        if pe:
            summary = pe.get_portfolio_summary()
            data.update({
                "balance": summary["cash_balance"],
                "total_value": summary["total_value"],
                "net_profit": summary["net_profit"],
                "total_trades": summary["total_trades"],
                "open_positions": summary["open_positions"],
            })

        self._send_json(data)

    def _api_portfolio(self):
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"error": "Paper trading not active"})
            return
        self._send_json(pe.get_portfolio_summary())

    def _api_positions(self):
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"positions": []})
            return
        self._send_json({"positions": pe.get_positions()})

    def _api_trades(self):
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"trades": []})
            return
        self._send_json({"trades": pe.get_trade_history(limit=100)})

    def _api_markets(self):
        bot = self.server.bot_ref
        heat = bot._market_heat

        # Show hottest markets (closest to arb) instead of first 10
        if heat:
            hot_sorted = sorted(heat.items(), key=lambda x: x[1])[:15]
            market_list = []
            for cid, overround in hot_sorted:
                market_list.append({
                    "condition_id": cid[:20] + "...",
                    "overround": round(overround, 4),
                    "status": "ARB" if overround < 0 else "NEAR" if overround < 0.01 else "WATCH",
                })
        else:
            market_list = [
                {"condition_id": m.get("condition_id", "")[:20] + "...",
                 "overround": round(m.get("yes_price", 0) + m.get("no_price", 0) - 1.0, 4),
                 "status": "SCANNING"}
                for m in bot._current_markets[:15]
            ]
        self._send_json({"markets": market_list, "total_tracked": len(heat)})

    def _api_risk(self):
        bot = self.server.bot_ref
        self._send_json({
            "max_exposure": bot.risk.max_exposure,
            "current_exposure": bot.risk.current_exposure,
            "daily_loss": bot.risk.daily_loss,
            "max_daily_loss": bot.risk.max_daily_loss,
            "kill_switch": bot.risk.kill_switch,
            "kill_switch_file": os.path.exists("STOP_TRADING"),
        })

    def _api_chart_pnl(self):
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"data": []})
            return
        self._send_json({"data": pe.get_pnl_chart_data()})

    def _api_metrics(self):
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"error": "Paper trading not active"})
            return
        self._send_json(pe.get_metrics())

    def _api_export(self):
        """Export complete portfolio state + config for LLM analysis."""
        pe = self.server.paper_engine
        bot = self.server.bot_ref
        if not pe:
            self._send_json({"error": "Paper trading not active"})
            return

        export = pe.export_full_state()

        # Add data collection stats
        if hasattr(bot, "collector"):
            export["data_collection"] = bot.collector.get_stats()

        self._send_json(export)

    def _api_data_stats(self):
        """Return data collection stats for the UI."""
        bot = self.server.bot_ref
        if hasattr(bot, "collector"):
            self._send_json(bot.collector.get_stats())
        else:
            self._send_json({"enabled": False})

    def _api_whales(self):
        """Return whale tracking data: tracked wallets, recent signals, stats."""
        bot = self.server.bot_ref
        wt = bot.whale_tracker
        data = {
            "wallets": wt.get_tracked_wallets(),
            "signals": wt.get_recent_signals(limit=30),
            "stats": wt.get_stats(),
            "copy_trades_executed": bot._copy_trades,
            "copy_exits_executed": bot._copy_exits,
        }
        # Include scorer summary for dashboard whale intelligence panel
        if hasattr(bot, "wallet_scorer"):
            data["scorer"] = bot.wallet_scorer.get_summary()
        self._send_json(data)

    def _api_scorer(self):
        """Return wallet performance rankings and scoring data."""
        bot = self.server.bot_ref
        if not hasattr(bot, "wallet_scorer"):
            self._send_json({"error": "Wallet scorer not active"})
            return
        scorer = bot.wallet_scorer
        self._send_json({
            "summary": scorer.get_summary(),
            "rankings": scorer.get_rankings(top_n=50),
            "market_type_stats": scorer.get_market_type_stats(),
        })

    def _api_flows(self):
        """Return current money flow analysis — where smart money is going."""
        bot = self.server.bot_ref
        if not hasattr(bot, "wallet_scorer"):
            self._send_json({"flows": []})
            return
        scorer = bot.wallet_scorer
        self._send_json({
            "hot_flows": scorer.get_hot_flows(min_wallets=2, top_n=10),
            "summary": scorer.get_summary(),
        })

    def _api_stress(self):
        """Return stress simulation statistics."""
        pe = self.server.paper_engine
        if not pe:
            self._send_json({"error": "Paper trading not active"})
            return
        self._send_json(pe.stress.get_stats())

    def _api_blockchain(self):
        """Return blockchain monitor status and metrics."""
        bot = self.server.bot_ref
        if not bot.blockchain_monitor:
            self._send_json({"enabled": False, "reason": "Blockchain monitor not configured"})
            return

        monitor = bot.blockchain_monitor
        data = {
            "enabled": True,
            "connected": monitor.web3.is_connected() if hasattr(monitor, 'web3') else False,
            "running": monitor.running,
            "wallets_tracked": len(monitor.tracked_wallets),
            "events_processed": monitor.events_received,
            "signals_emitted": monitor.signals_emitted,
            "last_event_time": monitor.last_event_time,
        }

        # Get current block if connected
        if data["connected"]:
            try:
                data["current_block"] = monitor.web3.eth.block_number
            except:
                data["current_block"] = None

        self._send_json(data)

    def _api_live_trades(self):
        """Return recent trades in real-time feed format."""
        bot = self.server.bot_ref
        wt = bot.whale_tracker

        # Get recent signals (last 20)
        signals = wt.get_recent_signals(limit=20)

        # Format for live feed display
        trades = []
        for sig in signals:
            trades.append({
                "time": sig.get("detected_at", 0),
                "wallet": sig.get("source_username", "Unknown")[:20],
                "market": sig.get("market_title", "Unknown")[:50],
                "side": sig.get("outcome", "?"),
                "price": sig.get("whale_price", 0),
                "size": sig.get("size", 0),
                "source": sig.get("source", "api"),
                "staleness": round(time.time() - sig.get("timestamp", time.time()), 1),
            })

        self._send_json({"trades": trades, "count": len(trades)})

    # ── Response Helpers ─────────────────────────────────────────

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, status=200):
        body = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress per-request logging to keep console clean


class RetroHTTPServer(ThreadingHTTPServer):
    """HTTP server that holds references to bot and paper engine."""

    def __init__(self, server_address, handler_class, bot, paper_engine, token):
        self.bot_ref = bot
        self.paper_engine = paper_engine
        self.dashboard_token = token
        super().__init__(server_address, handler_class)


def start_web_server(config, bot):
    """Start the web server in a daemon thread. Returns the thread."""
    port = config.get("WEB_PORT", 8080)
    bind = config.get("DASHBOARD_BIND", "127.0.0.1")
    token = config.get("DASHBOARD_TOKEN", "")

    paper_engine = None
    if hasattr(bot, "execution") and hasattr(bot.execution, "paper_engine"):
        paper_engine = bot.execution.paper_engine

    server = RetroHTTPServer(
        (bind, port), RetroRequestHandler, bot, paper_engine, token
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Print the access URL with token so user can click/copy
    if bind == "127.0.0.1":
        url = f"http://localhost:{port}/?token={token}"
        print(f"[*] Dashboard: {url}")
        print(f"[*] Local only. Set DASHBOARD_BIND=0.0.0.0 in config for network access.")
    else:
        url = f"http://0.0.0.0:{port}/?token={token}"
        print(f"[*] Dashboard: {url}")
        print(f"[*] WARNING: Accessible from network. Token required for access.")

    return thread

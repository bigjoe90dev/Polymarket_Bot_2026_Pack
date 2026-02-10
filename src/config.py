import os
import json
import getpass
import uuid

CONFIG_PATH = "config/config.json"
CONFIG_VERSION = 12  # v12: Real-time blockchain monitoring (2-3s latency vs 5-12min polling)


def load_or_create_config():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            try: config = json.load(f)
            except: config = {}

    dirty = False

    # Auto-upgrade old configs to aggressive free-infra defaults
    if config.get("_config_version", 0) < CONFIG_VERSION:
        config["_config_version"] = CONFIG_VERSION
        config["MIN_PROFIT"] = 0.003
        config["COST_BUFFER"] = 0.002
        config["MIN_LIQUIDITY"] = 0.5
        config["MAX_ORDER_SIZE"] = 3.0
        config["MARKETS_PER_CYCLE"] = 20
        config["FETCH_WORKERS"] = 4
        config["COPY_TRADE_SIZE"] = 2.0
        config["COPY_RATIO"] = 0.01          # Copy 1% of whale's trade size
        # ── Dynamic percentage-based risk (scales with balance) ──
        config["RISK_PER_TRADE_PCT"] = 0.01   # 1% of balance per trade (minimum)
        config["RISK_MAX_TRADE_PCT"] = 0.03   # 3% of balance per trade (maximum)
        config["RISK_MAX_MARKET_PCT"] = 0.06  # 6% of balance per market
        config["RISK_MAX_EXPOSURE_PCT"] = 0.50  # 50% of balance total exposure
        config["RISK_MAX_DAILY_LOSS_PCT"] = 0.30  # 30% of balance daily loss
        # Fallback fixed values (used by RiskGuard init before dynamic update)
        config["MAX_EXPOSURE"] = 25.0
        config["MAX_DAILY_LOSS"] = 15.0
        # Dynamic TP/SL: reward > risk ratio, market-type aware
        config["TP_FAST_PCT"] = 0.20         # Fast markets (crypto/sports): +20% TP
        config["SL_FAST_PCT"] = 0.12         # Fast markets: -12% SL
        config["TP_SLOW_PCT"] = 0.30         # Other markets: +30% TP (let winners run)
        config["SL_SLOW_PCT"] = 0.15         # Other markets: -15% SL (cut losers early)
        # Arb scanner disabled: negative EV (all 4 LLM reviews agreed)
        config["ENABLE_ARB_SCANNER"] = False  # HFT competition + two-leg risk = losses
        dirty = True
        print("[*] Config v10 — Dynamic TP/SL: fast 20/12%, slow 30/15%, reward > risk")

    # v11: FATAL bug fixes
    if config.get("_config_version", 0) < 11:
        config["_config_version"] = 11
        config["PAPER_BALANCE"] = 100.0  # Start paper trading at $100
        dirty = True
        print("[*] Config v11 — FATAL FIXES:")
        print("    - Fee calculation: now uses curved Polymarket formula")
        print("    - Staleness: now measures from whale's trade time (not detection time)")
        print("    - Exposure persistence: now saved to data/risk_state.json")
        print("    - Arb scanner: DISABLED (negative EV per 4 LLM reviews)")
        print("    - Paper balance: starting at $100 (was $1000)")

    # v12: Real-time blockchain monitoring
    if config.get("_config_version", 0) < 12:
        config["_config_version"] = 12
        # Blockchain monitoring via Polygon RPC WebSocket (2-3s latency vs 5-12min polling)
        config["USE_BLOCKCHAIN_MONITOR"] = True  # Enable real-time blockchain monitoring
        config["POLYGON_RPC_WSS"] = ""  # User must provide Alchemy/Infura WebSocket URL
        dirty = True
        print("[*] Config v12 — REAL-TIME BLOCKCHAIN MONITORING:")
        print("    - Monitors CTFExchange contract on Polygon for whale trades")
        print("    - Latency: 2-3 seconds (block time) vs 5-12 minutes (polling)")
        print("    - Requires Alchemy/Infura WebSocket URL in POLYGON_RPC_WSS")
        print("    - Get free WSS URL: https://dashboard.alchemy.com/apps")

    # Select mode FIRST so we know whether credentials are needed
    if "MODE" not in config:
        print("\nSelect Mode:")
        print("1. Paper (Simulate only)")
        print("2. Shadow (Watch live, no orders)")
        print("3. Live (Real money)")
        sel = input("Choice [1]: ").strip() or "1"
        config["MODE"] = {"1": "PAPER", "2": "SHADOW", "3": "LIVE"}.get(sel, "PAPER")
        dirty = True

    # Polygon RPC WebSocket URL (for blockchain monitoring)
    if config.get("USE_BLOCKCHAIN_MONITOR", False) and not config.get("POLYGON_RPC_WSS"):
        print("\n[*] Blockchain monitoring enabled — requires Polygon RPC WebSocket URL")
        print("    Get free URL from Alchemy: https://dashboard.alchemy.com/apps")
        print("    1. Create app → Polygon Mainnet")
        print("    2. Copy the WEBSOCKETS URL (wss://polygon-mainnet.g.alchemy.com/v2/...)")
        print("    3. Paste below (or press Enter to skip and use polling mode)")
        wss_url = input("Polygon RPC WSS URL: ").strip()
        if wss_url:
            config["POLYGON_RPC_WSS"] = wss_url
        else:
            config["USE_BLOCKCHAIN_MONITOR"] = False
            print("    [!] Blockchain monitor disabled — using polling mode (5-12min latency)")
        dirty = True

    # API credentials — PAPER mode doesn't need real keys
    if not config.get("POLY_API_KEY"):
        if config["MODE"] == "PAPER":
            config["POLY_API_KEY"] = "paper-mode"
            config["POLY_SECRET"] = "paper-mode"
            config["POLY_PASSPHRASE"] = "paper-mode"
        else:
            print("\n[!] API Key missing. Please provide Polymarket credentials.")
            config["POLY_API_KEY"] = getpass.getpass("Enter Polymarket API Key: ").strip()
            config["POLY_SECRET"] = getpass.getpass("Enter Polymarket Secret: ").strip()
            config["POLY_PASSPHRASE"] = getpass.getpass("Enter Polymarket Passphrase: ").strip()
        dirty = True

    if "MAX_EXPOSURE" not in config:
        config["MAX_EXPOSURE"] = 50.0
        dirty = True

    # Private key only required for LIVE trading (order signing)
    # ────────────────────────────────────────────────────────────────
    # LIVE MODE SETUP: You need a Polygon wallet with USDC.
    #   Option A — Rabby Wallet (recommended):
    #     1. Install Rabby browser extension (rabby.io)
    #     2. Create/import a wallet on Polygon network
    #     3. Fund with USDC on Polygon
    #     4. Export private key from Rabby: Settings → Manage Address → Export
    #     5. Paste the hex private key below when prompted
    #   Option B — MetaMask: Same steps, export from Account Details
    #   IMPORTANT: This key signs on-chain orders. Keep it safe.
    #              NEVER share it or commit it to source control.
    # ────────────────────────────────────────────────────────────────
    if config["MODE"] == "LIVE" and not config.get("POLY_PRIVATE_KEY"):
        print("\n[!] LIVE mode requires a private key for signing orders.")
        print("    Export from Rabby: Settings → Manage Address → Export Private Key")
        print("    Or from MetaMask: Account Details → Export Private Key")
        config["POLY_PRIVATE_KEY"] = getpass.getpass("Enter Polymarket Private Key (hex): ").strip()
        dirty = True

    # Paper trading settings
    if "PAPER_BALANCE" not in config:
        config["PAPER_BALANCE"] = 50.0
        dirty = True

    if "WEB_PORT" not in config:
        config["WEB_PORT"] = 8080
        dirty = True

    # Data collection (for backtesting)
    if "COLLECT_DATA" not in config:
        config["COLLECT_DATA"] = True
        dirty = True

    # Dashboard security: auto-generate access token
    if "DASHBOARD_TOKEN" not in config:
        config["DASHBOARD_TOKEN"] = uuid.uuid4().hex[:16]
        dirty = True
        print(f"[*] Dashboard token generated (required for access)")

    # Dashboard bind: 127.0.0.1 = local only, 0.0.0.0 = network/cloud
    if "DASHBOARD_BIND" not in config:
        config["DASHBOARD_BIND"] = "127.0.0.1"
        dirty = True

    # Telegram alerts (optional)
    if "TELEGRAM_BOT_TOKEN" not in config:
        config["TELEGRAM_BOT_TOKEN"] = ""
        config["TELEGRAM_CHAT_ID"] = ""
        dirty = True

    # Free tier (no infra simulation)
    config["INFRA_TIER"] = 1

    if dirty:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)

    return config

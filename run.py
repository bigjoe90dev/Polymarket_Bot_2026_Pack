import time
import sys
import traceback
from src.config import load_or_create_config
from src.bot import TradingBot

def main():
    print("--- Polymarket Bot v0.1 (Spec Implementation) ---")

    # 1. Load Config (Prompts user if needed) - Spec 6.1
    config = load_or_create_config()
    print("[*] Config loaded.")

    # 2. Initialize Bot - Spec 7.1
    bot = TradingBot(config)

    # 3. Start Web UI (daemon thread — dies with main)
    from src.web_server import start_web_server
    port = config.get("WEB_PORT", 8080)
    start_web_server(config, bot)
    print(f"[*] Web UI running at http://localhost:{port}")

    # 4. Start Loop - Spec 7.2
    print("[*] Starting main loop (Press Ctrl+C to stop)...")
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\n[!] Manual Stop triggered.")
        bot.shutdown()
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] Critical Error: {e}")
        print(traceback.format_exc())
        bot.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    main()

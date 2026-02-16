#!/usr/bin/env python3
"""
v14 Configuration Validation Script

Validates that config.json has all required fields for v14 production hardening.
Run this before starting the bot to catch configuration issues early.

Usage:
    python3 validate_config.py
"""

import json
import os
import sys


def check_config():
    """Validate config.json for v14 requirements."""
    print("[*] Validating config.json for v14...")

    # 1. Check file exists
    if not os.path.exists("config/config.json"):
        print("❌ FAIL: config/config.json not found")
        print("   Run: python3 run.py (will create config)")
        return False

    # 2. Load and parse JSON
    try:
        with open("config/config.json", "r") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ FAIL: config.json is invalid JSON: {e}")
        return False

    print("✅ config.json exists and is valid JSON")

    # 3. Check CONFIG_VERSION
    version = config.get("_config_version", 0)
    if version < 14:
        print(f"❌ FAIL: CONFIG_VERSION is {version}, expected 14")
        print("   Run the bot once to auto-upgrade config")
        return False

    print(f"✅ CONFIG_VERSION is {version}")

    # 4. Check required base fields
    required_base = [
        "MODE",
        "POLY_API_KEY",
        "MAX_EXPOSURE",
        "PAPER_BALANCE",
        "WEB_PORT",
    ]

    missing_base = [field for field in required_base if field not in config]
    if missing_base:
        print(f"❌ FAIL: Missing required base fields: {missing_base}")
        return False

    print("✅ All required base fields present")

    # 5. Check v14 fields
    required_v14 = [
        "METRICS_LOGGING_ENABLED",
        "METRICS_LOG_INTERVAL_SEC",
        "PARITY_CHECK_ENABLED",
        "HEALTH_MONITOR_ENABLED",
        "HEALTH_CHECK_INTERVAL_SEC",
        "MIN_BLOCKCHAIN_CONFIRMATIONS",
        "MAX_PRICE_CHASE_PCT",
        "STATE_BACKUP_GENERATIONS",
    ]

    missing_v14 = [field for field in required_v14 if field not in config]
    if missing_v14:
        print(f"❌ FAIL: Missing v14 fields: {missing_v14}")
        print("   Run the bot once to auto-upgrade config")
        return False

    print("✅ All v14 fields present")

    # 6. Check blockchain monitor config (if enabled)
    if config.get("USE_BLOCKCHAIN_MONITOR", False):
        wss_url = config.get("POLYGON_RPC_WSS", "")
        if not wss_url:
            print("⚠️  WARNING: USE_BLOCKCHAIN_MONITOR=True but POLYGON_RPC_WSS is empty")
            print("   Bot will use polling mode (5-12 min latency)")
            print("   Get free WSS URL: https://dashboard.alchemy.com/apps")
        elif not wss_url.startswith("wss://"):
            print(f"❌ FAIL: POLYGON_RPC_WSS must start with wss://, got: {wss_url[:20]}...")
            return False
        else:
            print(f"✅ Blockchain monitor configured (WSS: {wss_url[:30]}...)")
    else:
        print("⏭️  Blockchain monitor disabled (using polling mode)")

    # 7. Check mode
    mode = config.get("MODE", "")
    if mode not in ["PAPER", "SHADOW", "LIVE"]:
        print(f"❌ FAIL: MODE must be PAPER/SHADOW/LIVE, got: {mode}")
        return False

    print(f"✅ MODE: {mode}")

    # 8. Check risk settings
    if config.get("MAX_EXPOSURE", 0) <= 0:
        print("❌ FAIL: MAX_EXPOSURE must be > 0")
        return False

    if config.get("PAPER_BALANCE", 0) <= 0:
        print("❌ FAIL: PAPER_BALANCE must be > 0")
        return False

    print("✅ Risk settings valid")

    # 9. Check v14 numeric ranges
    if not (10 <= config.get("METRICS_LOG_INTERVAL_SEC", 60) <= 300):
        print("⚠️  WARNING: METRICS_LOG_INTERVAL_SEC should be 10-300 seconds")

    if not (10 <= config.get("HEALTH_CHECK_INTERVAL_SEC", 30) <= 300):
        print("⚠️  WARNING: HEALTH_CHECK_INTERVAL_SEC should be 10-300 seconds")

    if not (0 <= config.get("MIN_BLOCKCHAIN_CONFIRMATIONS", 0) <= 10):
        print("⚠️  WARNING: MIN_BLOCKCHAIN_CONFIRMATIONS should be 0-10 blocks")

    if not (0.01 <= config.get("MAX_PRICE_CHASE_PCT", 0.05) <= 0.20):
        print("⚠️  WARNING: MAX_PRICE_CHASE_PCT should be 0.01-0.20 (1%-20%)")

    if not (3 <= config.get("STATE_BACKUP_GENERATIONS", 5) <= 10):
        print("⚠️  WARNING: STATE_BACKUP_GENERATIONS should be 3-10")

    # 10. Check directories exist
    required_dirs = ["data", "config", "static"]
    for directory in required_dirs:
        if not os.path.exists(directory):
            print(f"⚠️  WARNING: Directory {directory}/ does not exist (will be created on startup)")

    # 11. Summary
    print("\n" + "="*60)
    print("✅ Configuration validation PASSED")
    print("="*60)
    print("\nRecommended next steps:")
    print("1. Review VALIDATION_CHECKLIST.md")
    print("2. Run: python3 run.py")
    print("3. Monitor for 24+ hours before shadow mode")
    print("4. Target: >95% parity match rate, <1% side error rate")

    return True


def check_python_version():
    """Check Python version is compatible."""
    import sys
    major, minor = sys.version_info[:2]

    if major < 3 or (major == 3 and minor < 9):
        print(f"❌ FAIL: Python {major}.{minor} detected, need Python 3.9+")
        print("   py-clob-client requires Python >=3.9.10")
        return False

    print(f"✅ Python {major}.{minor}.{sys.version_info.micro}")
    return True


def check_dependencies():
    """Check required Python packages are installed."""
    required = [
        "web3",
        "requests",
        "py_clob_client",
    ]

    missing = []
    for package in required:
        try:
            __import__(package)
        except ImportError:
            missing.append(package)

    if missing:
        print(f"❌ FAIL: Missing required packages: {missing}")
        print("   Install: pip3 install web3 requests py-clob-client")
        return False

    print("✅ All required packages installed")
    return True


def main():
    """Run all validation checks."""
    print("v14 Production Validation")
    print("="*60 + "\n")

    checks = [
        ("Python version", check_python_version),
        ("Dependencies", check_dependencies),
        ("Configuration", check_config),
    ]

    results = []
    for name, check_fn in checks:
        print(f"\n[{name}]")
        try:
            passed = check_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"❌ EXCEPTION: {e}")
            results.append((name, False))

    # Final summary
    print("\n" + "="*60)
    print("VALIDATION SUMMARY")
    print("="*60)

    all_passed = all(passed for _, passed in results)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")

    if all_passed:
        print("\n✅ ALL CHECKS PASSED - Ready to start bot")
        return 0
    else:
        print("\n❌ SOME CHECKS FAILED - Fix issues before starting bot")
        return 1


if __name__ == "__main__":
    sys.exit(main())

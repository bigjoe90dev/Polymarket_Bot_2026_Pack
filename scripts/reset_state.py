#!/usr/bin/env python3
"""
Reset script for BTC_1H_ONLY mode.
Deletes all state files including whale/copy/parity data.

Usage:
    python scripts/reset_state.py [--mode BTC_1H_ONLY]
"""

import argparse
import os
import glob
import shutil

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Files to delete in BTC_1H_ONLY mode
STATE_FILES = [
    "paper_state.json",
    "risk_state.json", 
    "whale_state.json",
    "wallet_scores.json",
    "parity_state.json",
]

# Backup files to delete
BACKUP_PATTERNS = [
    "*.bak*",
    "*.bak1",
    "*.bak2", 
    "*.bak3",
    "*.bak4",
    "*.bak5",
]

# Directories to clean
SNAPSHOT_DIRS = [
    "snapshots",
]


def delete_files(pattern):
    """Delete files matching pattern."""
    for f in glob.glob(os.path.join(DATA_DIR, pattern)):
        try:
            os.remove(f)
            print(f"  Deleted: {f}")
        except Exception as e:
            print(f"  Error deleting {f}: {e}")


def delete_dirs(pattern):
    """Delete directories matching pattern."""
    for d in glob.glob(os.path.join(DATA_DIR, pattern)):
        try:
            shutil.rmtree(d)
            print(f"  Deleted dir: {d}")
        except Exception as e:
            print(f"  Error deleting {d}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Reset bot state")
    parser.add_argument("--mode", default="BTC_1H_ONLY", choices=["BTC_1H_ONLY", "FULL"],
                        help="Reset mode")
    args = parser.parse_args()
    
    print(f"Resetting state for mode: {args.mode}")
    print(f"Data directory: {DATA_DIR}")
    print()
    
    if args.mode == "BTC_1H_ONLY":
        print("Deleting whale/copy/parity state files...")
        
        # Delete main state files
        for f in STATE_FILES:
            fp = os.path.join(DATA_DIR, f)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    print(f"  Deleted: {fp}")
                except Exception as e:
                    print(f"  Error deleting {fp}: {e}")
        
        # Delete backup files
        print("\nDeleting backup files...")
        for pattern in BACKUP_PATTERNS:
            delete_files(pattern)
        
        # Delete snapshot directories
        print("\nDeleting snapshot directories...")
        for d in SNAPSHOT_DIRS:
            dp = os.path.join(DATA_DIR, d)
            if os.path.exists(dp):
                try:
                    shutil.rmtree(dp)
                    print(f"  Deleted dir: {dp}")
                except Exception as e:
                    print(f"  Error deleting {dp}: {e}")
        
        print("\n✓ State reset complete for BTC_1H_ONLY mode")
        print("  - No whale state loaded")
        print("  - No copy trade history") 
        print("  - No parity state")
        print("  - No snapshots")
        
    else:
        # FULL mode - just delete paper state and snapshots
        print("FULL mode: Only deleting paper state and snapshots...")
        
        for f in ["paper_state.json"]:
            fp = os.path.join(DATA_DIR, f)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    print(f"  Deleted: {fp}")
                except Exception as e:
                    print(f"  Error deleting {fp}: {e}")
        
        for d in SNAPSHOT_DIRS:
            dp = os.path.join(DATA_DIR, d)
            if os.path.exists(dp):
                try:
                    shutil.rmtree(dp)
                    print(f"  Deleted dir: {dp}")
                except Exception as e:
                    print(f"  Error deleting {dp}: {e}")
        
        print("\n✓ Paper state reset complete for FULL mode")


if __name__ == "__main__":
    main()

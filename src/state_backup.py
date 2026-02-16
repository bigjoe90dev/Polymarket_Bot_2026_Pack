"""Shared state backup and recovery utilities (v14).

Provides:
- Backup rotation (keep last N generations)
- Schema validation on load
- Automatic rollback to last good backup on corruption
- Atomic writes (os.replace pattern)

Usage:
    from src.state_backup import save_state_with_backup, load_state_with_recovery

    # Save
    save_state_with_backup(filepath, data_dict, generations=5)

    # Load
    data = load_state_with_recovery(filepath, required_keys=["version", "data"])
"""

import os
import json
import shutil


def save_state_with_backup(filepath, data, generations=5):
    """Save state file with backup rotation.

    Args:
        filepath: Path to state file
        data: Dict to save as JSON
        generations: Number of backup generations to keep (default 5)

    Process:
        1. Rotate existing backups (.bak1 -> .bak2, etc.)
        2. Backup current file to .bak1
        3. Write new file atomically (tmp -> replace)
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Rotate existing backups
        for i in range(generations - 1, 0, -1):
            old_backup = f"{filepath}.bak{i}"
            new_backup = f"{filepath}.bak{i+1}"

            if os.path.exists(old_backup):
                if os.path.exists(new_backup):
                    os.remove(new_backup)
                shutil.move(old_backup, new_backup)

        # Backup current file (if it exists)
        if os.path.exists(filepath):
            backup = f"{filepath}.bak1"
            if os.path.exists(backup):
                os.remove(backup)
            shutil.copy(filepath, backup)

        # Write new file atomically
        tmp = filepath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, filepath)

        return True

    except Exception as e:
        print(f"[BACKUP] Save error for {filepath}: {e}")
        return False


def load_state_with_recovery(filepath, required_keys=None, schema_validator=None):
    """Load state file with automatic recovery from backups on corruption.

    Args:
        filepath: Path to state file
        required_keys: List of keys that must exist in loaded data
        schema_validator: Optional callable(data) -> bool for custom validation

    Returns:
        dict or None: Loaded data, or None if all attempts failed

    Process:
        1. Try to load current file
        2. Validate schema/required keys
        3. If invalid, try backups (.bak1, .bak2, ...)
        4. Return first valid file found
        5. Return None if all fail
    """
    # Try current file + up to 5 backups
    attempts = [filepath]
    for i in range(1, 6):
        backup = f"{filepath}.bak{i}"
        if os.path.exists(backup):
            attempts.append(backup)

    for attempt_path in attempts:
        if not os.path.exists(attempt_path):
            continue

        try:
            with open(attempt_path, "r") as f:
                data = json.load(f)

            # Validate schema
            if required_keys:
                missing = [k for k in required_keys if k not in data]
                if missing:
                    print(f"[BACKUP] {attempt_path} missing keys: {missing}")
                    continue

            # Custom validation
            if schema_validator and not schema_validator(data):
                print(f"[BACKUP] {attempt_path} failed schema validation")
                continue

            # Valid file found
            if attempt_path != filepath:
                print(f"[BACKUP] ✅ Recovered from {attempt_path}")

            return data

        except json.JSONDecodeError as e:
            print(f"[BACKUP] {attempt_path} corrupted (JSON error): {e}")
            continue
        except Exception as e:
            print(f"[BACKUP] {attempt_path} load error: {e}")
            continue

    # All attempts failed
    print(f"[BACKUP] ❌ All recovery attempts failed for {filepath}")
    return None


def create_fresh_state(template):
    """Create a fresh state dict from a template.

    Args:
        template: Dict with default values for a fresh state

    Returns:
        dict: Fresh state initialized from template
    """
    return dict(template)

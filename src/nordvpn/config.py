"""Configuration management for NordVPN CLI."""

from __future__ import annotations

import json
import os
from typing import Any

CONFIG_FILE = os.path.expanduser("~/.nordvpn-config")

DEFAULT_CONFIG: dict[str, Any] = {
    "tray_enabled": False,
    "notify_enabled": False,
    "daemon_enabled": True,
    "last_rotate_country": "US",
    "last_rotate_hostname": "",
    "last_rotate_protocol": "openvpn_udp",
    "obfuscate_enabled": False,
}


def load_config() -> dict[str, Any]:
    """Load settings from JSON file, returning defaults if missing."""
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r") as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG.copy()


def save_config(config: dict[str, Any]) -> None:
    """Save settings to JSON file."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
    except OSError as e:
        print(f"⚠️  Could not save config: {e}")


def set_setting(key: str, value: Any) -> None:
    """Update a single setting."""
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def get_setting(key: str) -> Any:
    """Get a single setting."""
    return load_config().get(key, DEFAULT_CONFIG.get(key))

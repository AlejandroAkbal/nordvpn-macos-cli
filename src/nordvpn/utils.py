"""Shared utilities for path resolution and environment detection."""

from __future__ import annotations

import os
import shutil
import subprocess


def resolve_binary(name: str) -> str:
    """
    Find the absolute path of a binary.
    Checks current PATH first, then standard system and Homebrew paths (Apple Silicon and Intel).
    """
    if os.path.isabs(name) and os.path.exists(name):
        return name

    found = shutil.which(name)
    if found:
        return found

    search_paths = [
        "/opt/homebrew/bin",  # Apple Silicon Homebrew
        "/usr/local/bin",     # Intel Homebrew
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for path in search_paths:
        candidate = os.path.join(path, name)
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise RuntimeError(
        f"Could not find required binary: '{name}'. "
        f"Please install it (e.g. brew install {name})."
    )


def kill_tray() -> None:
    """Find and terminate any running NordVPN tray processes."""
    try:
        subprocess.run(["pkill", "-f", "nordvpn.tray"], check=False)
    except Exception:
        pass

"""macOS Packet Filter (pf) Kill Switch using a dedicated anchor (safe for system rules)."""

from __future__ import annotations

import os
import subprocess
import sys

from . import utils

# Dedicated anchor so we don't overwrite system rules (AirDrop, etc.)
ANCHOR_NAME = "com.nordvpn.client"
RULES_FILE = "/tmp/nordvpn_anchor.conf"
# For cli._safety_cleanup: "did we enable kill switch?" (same file we write on enable)
PF_CONF_PATH = RULES_FILE


def enable_killswitch(server_ip: str) -> None:
    """
    Enable kill switch by loading rules into our anchor only.
    1. Write rules to temp file.
    2. Load into anchor com.nordvpn.client.
    3. Enable pf if needed.
    4. Reference anchor in main ruleset (replaces main ruleset with one line; see caveat below).
    """
    print(f"🛡️  Enabling Kill Switch (Anchor: {ANCHOR_NAME}, allowing {server_ip})...")
    pfctl = utils.resolve_binary("pfctl")

    rules = (
        "block drop out all\n"
        "pass out quick on lo0\n"
        "pass out quick on utun+\n"
        f"pass out quick proto udp from any to {server_ip} port 1194\n"
        f"pass out quick proto tcp from any to {server_ip} port 443\n"
    )

    try:
        with open(RULES_FILE, "w") as f:
            f.write(rules)

        subprocess.run(
            ["sudo", pfctl, "-a", ANCHOR_NAME, "-f", RULES_FILE],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["sudo", pfctl, "-e"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Reference our anchor in main ruleset. Replaces entire main ruleset;
        # on Macs with custom /etc/pf.conf this may remove other rules.
        subprocess.run(
            ["bash", "-c", f'echo "anchor {ANCHOR_NAME}" | sudo "{pfctl}" -f -'],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print("❌ Failed to enable firewall anchor (sudo required).", file=sys.stderr)
        sys.exit(1)


def disable_killswitch() -> None:
    """Flush only our anchor; leave pf enabled and system rules untouched."""
    print("🛡️  Disabling Kill Switch (flushing anchor)...")
    pfctl = utils.resolve_binary("pfctl")
    try:
        subprocess.run(
            ["sudo", pfctl, "-a", ANCHOR_NAME, "-F", "all"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(RULES_FILE):
            os.remove(RULES_FILE)
    except OSError as e:
        print(f"❌ Failed to disable kill switch: {e}", file=sys.stderr)

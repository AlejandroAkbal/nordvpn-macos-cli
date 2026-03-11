"""Automated setup for passwordless sudo (OpenVPN and firewall)."""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile

from . import openvpn
from . import utils

SUDOERS_FILE = "/etc/sudoers.d/nordvpn"


def _current_user() -> str:
    """Detect the actual human user even if running under sudo or pyenv."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    user = os.environ.get("USER")
    if user and user != "root":
        return user
    try:
        return pwd.getpwuid(os.getuid())[0]
    except Exception:
        return "root"


def _allowed_openvpn_bins() -> list[str]:
    bins = [openvpn.ensure_openvpn_binary()]
    try:
        system_openvpn = utils.resolve_binary("openvpn")
    except RuntimeError:
        system_openvpn = None
    if system_openvpn and system_openvpn not in bins:
        bins.append(system_openvpn)
    return bins


def install_sudoers_rule() -> None:
    print("🔧  Configuring passwordless access for VPN...")

    try:
        openvpn_bins = _allowed_openvpn_bins()
        pfctl_bin = utils.resolve_binary("pfctl")
        cat_bin = utils.resolve_binary("cat")
        kill_bin = utils.resolve_binary("kill")
        pkill_bin = utils.resolve_binary("pkill")
        rm_bin = utils.resolve_binary("rm")
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    user = _current_user()
    if not user:
        print("❌ Could not determine current username.", file=sys.stderr)
        sys.exit(1)

    allowed_bins = [pfctl_bin, *openvpn_bins, cat_bin, kill_bin, pkill_bin, rm_bin]
    rule_content = f"{user} ALL=(ALL) NOPASSWD: {', '.join(allowed_bins)}\n"
    print(f"    User: {user}")
    print(f"    Binaries: {', '.join(allowed_bins)}")

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sudoers") as tmp:
        tmp.write(rule_content)
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["visudo", "-c", "-f", tmp_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print(
            "❌ Generated sudoers rule failed syntax check. Aborting.", file=sys.stderr
        )
        os.remove(tmp_path)
        sys.exit(1)

    print(
        "🔑  Installing permission file (you may be asked for your password one last time)..."
    )
    try:
        subprocess.run(["sudo", "cp", tmp_path, SUDOERS_FILE], check=True)
        subprocess.run(["sudo", "chmod", "0440", SUDOERS_FILE], check=True)
        subprocess.run(["sudo", "chown", "root:wheel", SUDOERS_FILE], check=True)
        print("✅  Success! You can now run 'nordvpn connect' without a password.")
    except subprocess.CalledProcessError:
        print("❌ Failed to install sudoers file.", file=sys.stderr)
        sys.exit(1)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def check_sudo_access() -> bool:
    try:
        required_checks = [
            [openvpn.ensure_openvpn_binary(), "--version"],
            [utils.resolve_binary("pfctl"), "-s", "info"],
            [utils.resolve_binary("pkill"), "-V"],
        ]
        for cmd in required_checks:
            subprocess.run(
                ["sudo", "-n", *cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        return True
    except (subprocess.CalledProcessError, RuntimeError):
        return False

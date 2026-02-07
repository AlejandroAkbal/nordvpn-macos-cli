"""Automated setup for passwordless sudo (OpenVPN and firewall)."""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import tempfile

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


def install_sudoers_rule() -> None:
    """
    Create a sudoers file so openvpn and pfctl can run without a password.
    1. Resolve absolute paths for openvpn and pfctl.
    2. Write a temporary sudoers fragment.
    3. Validate with visudo -c -f.
    4. Install to /etc/sudoers.d/nordvpn with correct permissions.
    """
    print("🔧  Configuring passwordless access for VPN...")

    try:
        openvpn_bin = utils.resolve_binary("openvpn")
        pfctl_bin = utils.resolve_binary("pfctl")
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    user = _current_user()
    if not user:
        print("❌ Could not determine current username.", file=sys.stderr)
        sys.exit(1)

    rule_content = f"{user} ALL=(ALL) NOPASSWD: {pfctl_bin}, {openvpn_bin}\n"
    print(f"    User: {user}")
    print(f"    Binaries: {pfctl_bin}, {openvpn_bin}")

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
        print("❌ Generated sudoers rule failed syntax check. Aborting.", file=sys.stderr)
        os.remove(tmp_path)
        sys.exit(1)

    print("🔑  Installing permission file (you may be asked for your password one last time)...")
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
    """Return True if openvpn can be run with sudo without a password."""
    try:
        openvpn_bin = utils.resolve_binary("openvpn")
        subprocess.run(
            ["sudo", "-n", openvpn_bin, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, RuntimeError):
        return False

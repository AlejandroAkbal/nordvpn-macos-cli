"""OpenVPN: download Nord configs, run openvpn with auth (env or file)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

import requests

from .api import CONFIG_BASE_URL, get_recommendation
from . import utils

DOWNLOAD_DIR = "/tmp"
AUTH_FILE = os.path.expanduser("~/.nord-auth")
PID_FILE = "/tmp/nordvpn-cli.pid"
LOG_FILE = os.path.expanduser("~/.nordvpn.log")
DEFAULT_TIMEOUT = 10


def download_config(hostname: str, protocol: str = "openvpn_udp") -> str:
    """Download .ovpn config with fallback: try standard path first, then legacy."""
    local_path = os.path.join(DOWNLOAD_DIR, f"{hostname}.ovpn")
    proto_std = "udp" if "udp" in protocol else "tcp"
    proto_leg = "udp1194" if "udp" in protocol else "tcp443"
    urls = [
        f"{CONFIG_BASE_URL}/{protocol}/servers/{hostname}.{proto_std}.ovpn",
        f"{CONFIG_BASE_URL}/ovpn_legacy/servers/{hostname}.{proto_leg}.ovpn",
    ]
    for url in urls:
        try:
            resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                return local_path
        except requests.RequestException:
            continue
    raise RuntimeError(f"Could not download config for {hostname} (tried {len(urls)} URL paths)")


def get_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return (user, pass) from env NORD_USER/NORD_PASS, or (None, None)."""
    return (os.environ.get("NORD_USER"), os.environ.get("NORD_PASS"))


def has_auth() -> bool:
    """True if we can authenticate (file or env)."""
    if os.path.exists(AUTH_FILE):
        return True
    u, p = get_credentials()
    return bool(u and p)


def _ensure_auth_file() -> bool:
    """
    If NORD_USER and NORD_PASS are set and ~/.nord-auth does not exist,
    create it and return True. Otherwise return False (caller uses pipe or existing file).
    """
    if os.path.exists(AUTH_FILE):
        return True
    user, password = get_credentials()
    if not user or not password:
        return False
    with open(AUTH_FILE, "w") as f:
        f.write(f"{user}\n{password}\n")
    os.chmod(AUTH_FILE, 0o600)
    return True


def _normalize_hostname(hostname: str) -> str:
    """Ensure hostname is FQDN for Nord CDN (e.g. us9364 -> us9364.nordvpn.com)."""
    hostname = hostname.lower().strip()
    if not hostname.endswith(".nordvpn.com"):
        return f"{hostname}.nordvpn.com"
    return hostname


def connect(
    country_code: str = "US",
    protocol: str = "openvpn_udp",
    daemon: bool = False,
    server_hostname: Optional[str] = None,
) -> None:
    """
    Connect to VPN: either best server in country or a specific server.
    Uses ~/.nord-auth if present, else NORD_USER/NORD_PASS env vars.
    Exits the process on failure; on success openvpn runs (foreground or daemon).
    """
    if server_hostname:
        hostname = _normalize_hostname(server_hostname)
    else:
        server = get_recommendation(country_code, protocol)
        hostname = _normalize_hostname(server["hostname"])
    config_path = download_config(hostname, protocol)
    openvpn_bin = utils.resolve_binary("openvpn")

    cmd = ["sudo", openvpn_bin, "--config", config_path, "--writepid", PID_FILE]
    if daemon:
        cmd.append("--daemon")
        cmd.extend(["--log", LOG_FILE])

    if not _ensure_auth_file():
        print("❌ No credentials. Set NORD_USER and NORD_PASS in your shell.", file=sys.stderr)
        sys.exit(1)
    cmd.extend(["--auth-user-pass", AUTH_FILE])

    if daemon:
        # Redirect OpenVPN stdout/stderr to log file so CLI output stays clean (no cursor race).
        # Use user-writable path to avoid PermissionError when /tmp/nordvpn.log is root-owned.
        with open(LOG_FILE, "ab") as log_file:
            subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return
    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        disconnect()


def get_pid() -> Optional[str]:
    """Read PID from our PID file (written by OpenVPN). Returns None if missing or unreadable."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        r = subprocess.run(
            ["sudo", "cat", PID_FILE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout:
            pid = r.stdout.strip()
            if pid and pid.isdigit():
                return pid
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def disconnect() -> None:
    """Terminate the VPN and wait for the process to exit completely (synchronous)."""
    pid = get_pid()
    if not pid:
        print(
            "⚠️  No PID file found (connection wasn't started by this CLI, or file was removed). "
            "Killing all OpenVPN processes.",
            file=sys.stderr,
        )
        subprocess.run(["sudo", "pkill", "-x", "openvpn"], check=False)
    else:
        subprocess.run(["sudo", "kill", pid], check=False)

    # Deterministic wait loop: poll until process is gone (max 5 seconds)
    for _ in range(50):
        if not is_connected():
            subprocess.run(["sudo", "rm", "-f", PID_FILE], check=False)
            return
        time.sleep(0.1)

    # Escalation: still running after 5s — force kill (SIGKILL)
    subprocess.run(["sudo", "pkill", "-9", "-x", "openvpn"], check=False)
    subprocess.run(["sudo", "rm", "-f", PID_FILE], check=False)


def is_connected() -> bool:
    """True if an openvpn process is running."""
    result = subprocess.run(
        ["pgrep", "-x", "openvpn"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

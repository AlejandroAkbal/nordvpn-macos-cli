"""OpenVPN: download Nord configs, run openvpn with auth (env or file)."""

from __future__ import annotations

import io
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Optional

import requests

from .api import CONFIG_BASE_URL, get_recommendation
from . import utils

DOWNLOAD_DIR = "/tmp"
AUTH_FILE = os.path.expanduser("~/.nord-auth")
PID_FILE = "/tmp/nordvpn-cli.pid"
LOG_FILE = os.path.expanduser("~/.nordvpn.log")
DEFAULT_TIMEOUT = 10
XOR_CONFIG_ZIP_URL = (
    "https://downloads.nordcdn.com/configs/archives/servers/ovpn_xor.zip"
)
XOR_PROTOCOLS = frozenset({"openvpn_xor_udp", "openvpn_xor_tcp"})

# Tunnelblick DMG — latest stable release (v8.0)
TUNNELBLICK_DMG_URL = (
    "https://github.com/Tunnelblick/Tunnelblick/releases/download/v8.0/"
    "Tunnelblick_8.0_build_6300.dmg"
)
MANAGED_OPENVPN_DIR = os.path.expanduser("~/.local/share/nordvpn")
MANAGED_OPENVPN_PATH = os.path.join(MANAGED_OPENVPN_DIR, "openvpn")
LEGACY_XOR_OPENVPN_PATH = os.path.join(MANAGED_OPENVPN_DIR, "openvpn-xor")
OPENVPN_PROCESS_NAMES = tuple(
    dict.fromkeys(
        (
            "openvpn",
            os.path.basename(MANAGED_OPENVPN_PATH),
            os.path.basename(LEGACY_XOR_OPENVPN_PATH),
        )
    )
)


def check_scramble_support(openvpn_bin: str) -> bool:
    """Return True if the openvpn binary supports the scramble (XOR) option."""
    probe_config = """client
dev tun
proto udp
remote 127.0.0.1 1194
nobind
ca /dev/null
cert /dev/null
key /dev/null
verb 0
scramble xorptrpos
"""
    cfg_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ovpn", delete=False
        ) as tmp_cfg:
            tmp_cfg.write(probe_config)
            cfg_path = tmp_cfg.name
        result = subprocess.run(
            [openvpn_bin, "--config", cfg_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if cfg_path:
            try:
                os.remove(cfg_path)
            except OSError:
                pass

    output = f"{result.stdout}\n{result.stderr}".lower()
    unsupported_markers = (
        "unrecognized option or missing or extra parameter(s)",
        "options error",
    )
    if "scramble" in output and any(marker in output for marker in unsupported_markers):
        return False
    return True


def _openvpn_version_key(name: str) -> tuple[list[int], str]:
    return ([int(part) for part in re.findall(r"\d+", name)], name)


def _select_tunnelblick_openvpn(mount_point: str) -> str:
    ovpn_dir = os.path.join(
        mount_point,
        "Tunnelblick.app",
        "Contents",
        "Resources",
        "openvpn",
    )
    if not os.path.isdir(ovpn_dir):
        raise RuntimeError(f"Tunnelblick bundle missing OpenVPN directory: {ovpn_dir}")

    default_path = os.path.join(ovpn_dir, "default")
    default_realpath = os.path.realpath(default_path)
    if os.path.isfile(default_realpath):
        return default_realpath

    versions = sorted(
        (d for d in os.listdir(ovpn_dir) if os.path.isdir(os.path.join(ovpn_dir, d))),
        key=_openvpn_version_key,
        reverse=True,
    )
    for version in versions:
        candidate = os.path.join(ovpn_dir, version, "openvpn")
        if os.path.isfile(candidate):
            return candidate

    raise RuntimeError("No OpenVPN binary found inside Tunnelblick bundle")


def _install_managed_openvpn(src_binary: str) -> str:
    os.makedirs(MANAGED_OPENVPN_DIR, exist_ok=True)
    tmp_target = f"{MANAGED_OPENVPN_PATH}.tmp"
    shutil.copy2(src_binary, tmp_target)
    os.chmod(tmp_target, 0o755)
    subprocess.run(
        ["xattr", "-d", "com.apple.quarantine", tmp_target],
        capture_output=True,
    )
    os.replace(tmp_target, MANAGED_OPENVPN_PATH)
    return MANAGED_OPENVPN_PATH


def ensure_openvpn_binary(require_scramble: bool = False) -> str:
    """
    Return path to the managed Tunnelblick OpenVPN binary, installing it automatically if needed.

    The binary is extracted from the Tunnelblick DMG (no GUI install required):
      1. Download Tunnelblick DMG to a temp file.
      2. Mount it headlessly with hdiutil (no Finder window).
      3. Follow Tunnelblick's default OpenVPN selection (fallback: latest bundled version).
      4. Copy the binary to MANAGED_OPENVPN_PATH and strip macOS quarantine.
      5. Unmount and clean up.
    """
    for existing_path in (MANAGED_OPENVPN_PATH, LEGACY_XOR_OPENVPN_PATH):
        if os.path.isfile(existing_path) and os.access(existing_path, os.X_OK):
            if require_scramble and not check_scramble_support(existing_path):
                continue
            if existing_path != MANAGED_OPENVPN_PATH:
                return _install_managed_openvpn(existing_path)
            return existing_path

    print(
        "🔍  Managed OpenVPN binary not found. Downloading from Tunnelblick…",
        file=sys.stderr,
    )

    # Download DMG
    with tempfile.NamedTemporaryFile(suffix=".dmg", delete=False) as tmp_dmg:
        dmg_path = tmp_dmg.name
    try:
        resp = requests.get(TUNNELBLICK_DMG_URL, stream=True, timeout=60)
        resp.raise_for_status()
        with open(dmg_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    except requests.RequestException as exc:
        os.remove(dmg_path)
        raise RuntimeError(f"Failed to download Tunnelblick DMG: {exc}") from exc

    mount_point: Optional[str] = None
    try:
        # Mount headlessly and parse the plist output for the real mount point.
        result = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-plist", dmg_path],
            capture_output=True,
            check=True,
        )
        mount_data = plistlib.loads(result.stdout)
        for entity in mount_data.get("system-entities", []):
            candidate = entity.get("mount-point")
            if candidate:
                mount_point = candidate
                break
        if not mount_point:
            raise RuntimeError(
                "Could not determine DMG mount point from hdiutil output"
            )

        src_binary = _select_tunnelblick_openvpn(mount_point)
        installed_path = _install_managed_openvpn(src_binary)

        if require_scramble and not check_scramble_support(installed_path):
            raise RuntimeError(
                "Extracted binary does not support scramble — Tunnelblick bundle may be incompatible"
            )

        print(f"✅  Managed OpenVPN installed: {installed_path}", file=sys.stderr)
        return installed_path

    finally:
        if mount_point:
            subprocess.run(["hdiutil", "detach", mount_point, "-quiet"], check=False)
        try:
            os.remove(dmg_path)
        except OSError:
            pass


def ensure_xor_openvpn() -> str:
    """Backward-compatible wrapper for callers that specifically need scramble support."""
    return ensure_openvpn_binary(require_scramble=True)


def download_xor_config(hostname: str, protocol: str) -> str:
    """Download XOR obfuscated .ovpn config from Nord's zip archive."""
    local_path = os.path.join(DOWNLOAD_DIR, f"{hostname}.xor.ovpn")
    inner_path = (
        f"ovpn_udp/{hostname}.udp.ovpn"
        if "udp" in protocol
        else f"ovpn_tcp/{hostname}.tcp.ovpn"
    )

    try:
        resp = requests.get(XOR_CONFIG_ZIP_URL, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError("Could not download XOR config archive") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not download XOR config archive (status {resp.status_code})"
        )

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            data = zf.read(inner_path)
    except KeyError as exc:
        raise RuntimeError(
            f"Could not find XOR config in archive: {inner_path}"
        ) from exc
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Downloaded XOR config archive is invalid") from exc

    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def download_config(hostname: str, protocol: str = "openvpn_udp") -> str:
    """Download .ovpn config with fallback: try standard path first, then legacy."""
    if protocol in XOR_PROTOCOLS:
        return download_xor_config(hostname, protocol)

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
    raise RuntimeError(
        f"Could not download config for {hostname} (tried {len(urls)} URL paths)"
    )


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

    try:
        openvpn_bin = ensure_openvpn_binary(require_scramble=protocol in XOR_PROTOCOLS)
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    cmd = ["sudo", openvpn_bin, "--config", config_path, "--writepid", PID_FILE]
    if daemon:
        cmd.append("--daemon")
        cmd.extend(["--log", LOG_FILE])

    if not _ensure_auth_file():
        print(
            "❌ No credentials. Set NORD_USER and NORD_PASS in your shell.",
            file=sys.stderr,
        )
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
        for name in OPENVPN_PROCESS_NAMES:
            subprocess.run(["sudo", "pkill", "-x", name], check=False)
    else:
        subprocess.run(["sudo", "kill", pid], check=False)

    # Deterministic wait loop: poll until process is gone (max 5 seconds)
    for _ in range(50):
        if not is_connected():
            subprocess.run(["sudo", "rm", "-f", PID_FILE], check=False)
            return
        time.sleep(0.1)

    # Escalation: still running after 5s — force kill (SIGKILL)
    for name in OPENVPN_PROCESS_NAMES:
        subprocess.run(["sudo", "pkill", "-9", "-x", name], check=False)
    subprocess.run(["sudo", "rm", "-f", PID_FILE], check=False)


def is_connected() -> bool:
    """True if an openvpn process is running."""
    for name in OPENVPN_PROCESS_NAMES:
        result = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
    return False

"""CLI entry point: nordvpn connect, disconnect, status, list, list-countries."""

from __future__ import annotations

import argparse
import os
import random
import signal
import socket
import subprocess
import sys
import time

try:
    from pync import Notifier
except ImportError:
    Notifier = None  # pync not installed (e.g. headless); notifications no-op

from . import __version__
from . import api
from . import config
from . import firewall
from . import openvpn
from . import setup
from . import utils


def _notify(
    message: str,
    title: str = "NordVPN",
    sound: str = "Default",
    force: bool = False,
) -> None:
    """Send a macOS desktop notification. If force=True, bypasses the notify_enabled setting (for critical alerts)."""
    if not force and not config.get_setting("notify_enabled"):
        return
    if Notifier is None:
        return
    try:
        Notifier.notify(message, title=title, sound=sound)
    except Exception:
        pass


def _safety_cleanup() -> None:
    """
    Disable kill switch (used only on connect failure before VPN is up).
    Not registered on normal run: kill switch is hard (fail-closed). If the
    script crashes, firewall stays on; restore internet with: sudo pfctl -d
    """
    if os.path.exists(firewall.PF_CONF_PATH):
        msg = "Unexpected exit. Kill switch disabled to restore internet."
        print(f"\n⚠️  {msg}", file=sys.stderr)
        _notify(msg, sound="Basso", force=True)
        firewall.disable_killswitch()


def _hostname_to_fqdn(h: str) -> str:
    """Normalize to Nord FQDN for DNS resolution."""
    h = h.lower().strip()
    return h if h.endswith(".nordvpn.com") else f"{h}.nordvpn.com"


def _resolve_server_ip(server: dict | None, hostname_override: str | None) -> str:
    """Resolve VPN server IP (for kill switch). Prefer API 'station' when available."""
    if hostname_override:
        return socket.gethostbyname(_hostname_to_fqdn(hostname_override))
    if server and server.get("station"):
        return str(server["station"])
    if server:
        return socket.gethostbyname(_hostname_to_fqdn(server["hostname"]))
    raise ValueError("Need server or hostname to resolve IP")


def _ensure_tray_running() -> None:
    """Launch tray in background if enabled in settings and not already running."""
    if not config.get_setting("tray_enabled"):
        return
    try:
        subprocess.check_call(
            ["pgrep", "-f", "nordvpn.tray"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except subprocess.CalledProcessError:
        pass
    print("🖥️  Launching Menu Bar Icon...")
    subprocess.Popen(
        [sys.executable, "-m", "nordvpn.tray"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)


def _resolve_daemon(args: argparse.Namespace) -> bool:
    """Use --daemon/--no-daemon if set, otherwise config daemon_enabled (default True)."""
    if getattr(args, "daemon", None) is None:
        return bool(config.get_setting("daemon_enabled"))
    return bool(args.daemon)


def _resolve_protocol(args: argparse.Namespace) -> str:
    """Resolve effective protocol string, mapping --obfuscated to XOR variants."""
    obfuscated = getattr(args, "obfuscated", False) or False
    proto = getattr(args, "proto", "openvpn_udp") or "openvpn_udp"
    if obfuscated:
        return "openvpn_xor_udp" if "udp" in proto else "openvpn_xor_tcp"
    return proto


def _cmd_connect(args: argparse.Namespace) -> None:
    if openvpn.is_connected():
        print(
            "⚠️  OpenVPN is already running. Disconnect first (nordvpn disconnect).",
            file=sys.stderr,
        )
        sys.exit(1)
    if not openvpn.has_auth():
        print(
            "❌ No credentials. Set NORD_USER and NORD_PASS in your shell, or create ~/.nord-auth",
            file=sys.stderr,
        )
        sys.exit(1)

    _ensure_tray_running()

    daemon = _resolve_daemon(args)
    effective_proto = _resolve_protocol(args)

    server: dict | None = None
    if args.server:
        print(f"🔎  Connecting to server: {args.server}")
    else:
        try:
            server = api.get_recommendation(args.country, effective_proto)
        except (ValueError, Exception) as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"🔎  Best server in {args.country}: {server['hostname']} (load {server['load']}%)"
        )

    def _sigint_handler(sig: int, frame: object) -> None:
        if args.killswitch:
            firewall.disable_killswitch()
        utils.kill_tray()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    if args.killswitch:
        # Hard kill switch: we do NOT register atexit cleanup. If the script
        # crashes or is killed, the firewall stays on (fail-closed). Restore
        # internet manually with: sudo pfctl -d
        try:
            server_ip = _resolve_server_ip(server, args.server)
            firewall.enable_killswitch(server_ip)
        except Exception as e:
            print(f"❌ Kill switch error: {e}", file=sys.stderr)
            _safety_cleanup()
            sys.exit(1)

    print("⬇️  Downloading config...")
    print("🚀  Connecting (sudo password may be required)...", flush=True)
    server_name = (server["hostname"] if server else args.server) or "VPN"
    try:
        openvpn.connect(
            country_code=args.country,
            protocol=effective_proto,
            daemon=daemon,
            server_hostname=args.server,
        )
        if daemon:
            _notify(f"Connected to {server_name} ({args.country})", sound="Ping")
    except (RuntimeError, KeyboardInterrupt) as e:
        if args.killswitch:
            firewall.disable_killswitch()
        utils.kill_tray()
        if isinstance(e, RuntimeError):
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if daemon and args.killswitch:
        print(
            "🛡️  Kill switch is ACTIVE in background. Run 'nordvpn disconnect' to disable."
        )

    # Persist for rotate: next time "rotate" will use this country, hostname, and protocol
    config.set_setting("last_rotate_country", args.country)
    config.set_setting("last_rotate_protocol", effective_proto)
    config.set_setting(
        "last_rotate_hostname",
        (server["hostname"] if server else (args.server or "")),
    )


def _cmd_rotate(args: argparse.Namespace) -> None:
    """Rotate to a randomized, low-load server without lifting the firewall barrier (fail-closed)."""
    if openvpn.is_connected():
        # Do NOT call firewall.disable_killswitch() — keep "block all" active during transition
        utils.kill_tray()
        openvpn.disconnect()  # Synchronous: waits for process to exit
        print("🛑  Tunnel closed; waiting for network stack to settle...")
        time.sleep(1.0)  # Kernel releases utun device

    last_country = config.get_setting("last_rotate_country") or "US"
    last_protocol = config.get_setting("last_rotate_protocol") or "openvpn_udp"

    # --obfuscated on rotate overrides the stored protocol
    obfuscated = getattr(args, "obfuscated", False) or False
    if obfuscated:
        last_protocol = (
            "openvpn_xor_udp" if "udp" in last_protocol else "openvpn_xor_tcp"
        )

    try:
        # Fetch a large pool to ensure we have enough options after filtering
        servers = api.get_servers(last_country, protocol=last_protocol, limit=500)
    except (ValueError, Exception) as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if not servers:
        print(f"❌ No servers found for {last_country}.", file=sys.stderr)
        sys.exit(1)

    # 1. Load Filtering: Remove servers above threshold (default 70% capacity)
    valid_servers = [s for s in servers if s.get("load", 100) <= args.max_load]

    if not valid_servers:
        print(
            f"⚠️  All servers in {last_country} are above {args.max_load}% load. Using full list."
        )
        valid_servers = servers

    # 2. Randomized Shuffle: Pick a random server from the valid pool
    next_server = random.choice(valid_servers)
    next_hostname = next_server["hostname"]

    # Resolve the new IP before touching firewall rules (atomic swap uses this)
    next_server_ip = _resolve_server_ip(next_server, next_hostname)

    if args.killswitch:
        # Atomic swap: overwrite anchor with new pass rule; "block all" stays in place (no leak)
        firewall.enable_killswitch(next_server_ip)
        print(f"🔄  Atomic swap to: {next_hostname} ({next_server_ip})")
    else:
        print(
            f"🔄  Rotating to: {next_hostname} (Load: {next_server.get('load')}% | Pool: {len(valid_servers)})"
        )

    daemon = _resolve_daemon(args)
    rotate_args = argparse.Namespace(
        country=last_country,
        server=next_hostname,
        proto=last_protocol,
        obfuscated=False,  # protocol already resolved above; don't double-apply
        daemon=daemon,
        killswitch=args.killswitch,
    )
    _cmd_connect(rotate_args)


def _cmd_settings(args: argparse.Namespace) -> None:
    """Show or update CLI settings (tray, notifications, daemon, obfuscate)."""
    changes_made = False
    if args.tray is not None:
        state = args.tray.lower() == "enable"
        config.set_setting("tray_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Tray Icon auto-launch is now: {status}")
        if not state:
            print("   (Note: You may need to Quit the existing tray icon manually)")
        changes_made = True
    if args.notify is not None:
        state = args.notify.lower() == "enable"
        config.set_setting("notify_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Desktop Notifications are now: {status}")
        changes_made = True
    if args.daemon is not None:
        state = args.daemon.lower() == "enable"
        config.set_setting("daemon_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Daemon mode (background connect) is now: {status}")
        changes_made = True
    if args.obfuscated is not None:
        state = args.obfuscated.lower() == "enable"
        config.set_setting("obfuscate_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Obfuscated (XOR) mode is now: {status}")
        if state:
            print(
                "   (CLI auto-downloads the required Tunnelblick-patched OpenVPN binary on first use)"
            )
        changes_made = True
    cfg = config.load_config()
    print("Current Settings:")
    print(f"  Tray Icon:     {'ENABLED' if cfg['tray_enabled'] else 'DISABLED'}")
    print(f"  Notifications: {'ENABLED' if cfg['notify_enabled'] else 'DISABLED'}")
    print(f"  Daemon Mode:   {'ENABLED' if cfg['daemon_enabled'] else 'DISABLED'}")
    print(f"  Obfuscated:    {'ENABLED' if cfg['obfuscate_enabled'] else 'DISABLED'}")


def _cmd_disconnect(_args: argparse.Namespace) -> None:
    firewall.disable_killswitch()
    utils.kill_tray()
    if not openvpn.is_connected():
        print("No OpenVPN process running.")
        return
    print("🛑  Stopping OpenVPN...")
    openvpn.disconnect()
    _notify("VPN Disconnected", sound="Pop")
    print("✅  Disconnected.")


def _cmd_status(_args: argparse.Namespace) -> None:
    if openvpn.is_connected():
        print("🔒  VPN: connected")
    else:
        print("🔓  VPN: disconnected")
    print("🌍  Public IP info:")
    try:
        curl_bin = utils.resolve_binary("curl")
        r = subprocess.run(
            [curl_bin, "-s", "ipinfo.io/json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout:
            print(r.stdout)
        else:
            print("  (could not fetch)")
    except (RuntimeError, FileNotFoundError):
        print("  (install curl to see IP details)")
    except subprocess.TimeoutExpired:
        print("  (timeout)")


def _cmd_list(args: argparse.Namespace) -> None:
    effective_proto = _resolve_protocol(args)
    try:
        servers = api.get_servers(args.country, effective_proto, limit=args.limit)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    # Prefer online only if API provides status
    if servers and "status" in servers[0]:
        servers = [s for s in servers if s.get("status") == "online"]
    print(
        f"Servers in {args.country} ({effective_proto}) — showing up to {args.limit}\n"
    )
    for s in servers[: args.limit]:
        loc = s.get("locations") or []
        city = loc[0].get("city", "") if loc and isinstance(loc[0], dict) else ""
        extra = f" — {city}" if city else ""
        print(f"  {s['hostname']}  load {s.get('load', '?')}%{extra}")


def _cmd_setup(_args: argparse.Namespace) -> None:
    """Configure passwordless sudo for the managed OpenVPN binary and pfctl."""
    setup.install_sudoers_rule()


def _cmd_list_countries(_args: argparse.Namespace) -> None:
    try:
        countries = api.get_countries()
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    print("Country codes (use with: nordvpn connect <code>):\n")
    for c in sorted(countries, key=lambda x: x.get("name", "")):
        print(f"  {c.get('code', '?'):2}  {c.get('name', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nordvpn",
        description="NordVPN CLI for macOS (OpenVPN). Connect, disconnect, status, list servers.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, help="command")

    # setup
    p_setup = sub.add_parser(
        "setup", help="Configure passwordless sudo for VPN (recommended)"
    )
    p_setup.set_defaults(func=_cmd_setup)

    # settings
    p_settings = sub.add_parser("settings", help="Configure CLI behavior")
    p_settings.add_argument(
        "--tray",
        choices=["enable", "disable"],
        help="Auto-launch menu bar icon on connect",
    )
    p_settings.add_argument(
        "--notify",
        choices=["enable", "disable"],
        help="Desktop notifications (daemon connect, disconnect, kill switch)",
    )
    p_settings.add_argument(
        "--daemon",
        choices=["enable", "disable"],
        help="Run OpenVPN in background by default (enable); use --no-daemon on connect/rotate to override",
    )
    p_settings.add_argument(
        "--obfuscated",
        choices=["enable", "disable"],
        help="Use XOR obfuscated servers by default (requires Tunnelblick-patched openvpn)",
    )
    p_settings.set_defaults(func=_cmd_settings)

    # connect
    p_connect = sub.add_parser(
        "connect", help="Connect to VPN (default: best in US, UDP)"
    )
    p_connect.add_argument(
        "country",
        nargs="?",
        default="US",
        help="2-letter country code when not using --server",
    )
    p_connect.add_argument(
        "--server",
        "-s",
        metavar="HOSTNAME",
        help="Specific server (e.g. us9364 or us9364.nordvpn.com)",
    )
    p_connect.add_argument(
        "--proto",
        default="openvpn_udp",
        choices=["openvpn_udp", "openvpn_tcp"],
        help="Base protocol (UDP or TCP); combine with --obfuscated for XOR variants",
    )
    p_connect.add_argument(
        "--obfuscated",
        action="store_true",
        default=False,
        help="Use XOR obfuscated servers (requires Tunnelblick-patched openvpn)",
    )
    p_connect.add_argument(
        "--daemon",
        dest="daemon",
        action="store_true",
        help="Run OpenVPN in background (overrides settings)",
    )
    p_connect.add_argument(
        "--no-daemon",
        dest="daemon",
        action="store_false",
        help="Run in foreground (overrides settings)",
    )
    p_connect.set_defaults(daemon=None)
    p_connect.add_argument(
        "--killswitch",
        "-k",
        action="store_true",
        help="Enable pf firewall: block all traffic except VPN (macOS)",
    )
    p_connect.set_defaults(func=_cmd_connect)

    # disconnect
    p_disconnect = sub.add_parser("disconnect", help="Disconnect VPN")
    p_disconnect.set_defaults(func=_cmd_disconnect)

    # rotate
    p_rotate = sub.add_parser(
        "rotate",
        help="Connect to a random low-load server in the same country; disconnects first if connected",
    )
    p_rotate.add_argument(
        "--daemon",
        dest="daemon",
        action="store_true",
        help="Run OpenVPN in background (overrides settings)",
    )
    p_rotate.add_argument(
        "--no-daemon",
        dest="daemon",
        action="store_false",
        help="Run in foreground (overrides settings)",
    )
    p_rotate.set_defaults(daemon=None)
    p_rotate.add_argument(
        "--killswitch",
        "-k",
        action="store_true",
        help="Enable pf firewall (block all except VPN)",
    )
    p_rotate.add_argument(
        "--max-load",
        type=int,
        default=70,
        metavar="PCT",
        help="Exclude servers above this load %% (default: 70); use full list if none pass",
    )
    p_rotate.add_argument(
        "--obfuscated",
        action="store_true",
        default=False,
        help="Override stored protocol to use XOR obfuscated servers for this rotation",
    )
    p_rotate.set_defaults(func=_cmd_rotate)

    # status
    p_status = sub.add_parser("status", help="Show connection and IP info")
    p_status.set_defaults(func=_cmd_status)

    # list
    p_list = sub.add_parser("list", help="List servers for a country")
    p_list.add_argument(
        "country", nargs="?", default="US", help="2-letter country code"
    )
    p_list.add_argument(
        "--proto", default="openvpn_udp", choices=["openvpn_udp", "openvpn_tcp"]
    )
    p_list.add_argument(
        "--obfuscated",
        action="store_true",
        default=False,
        help="List XOR obfuscated servers instead",
    )
    p_list.add_argument("--limit", type=int, default=20, help="Max servers to show")
    p_list.set_defaults(func=_cmd_list)

    # list-countries
    p_countries = sub.add_parser("list-countries", help="List country codes")
    p_countries.set_defaults(func=_cmd_list_countries)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

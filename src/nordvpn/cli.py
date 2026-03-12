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
from . import verify


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


def _resolve_server_ip(
    server: dict[str, object] | None, hostname_override: str | None
) -> str:
    """Resolve VPN server IP (for kill switch). Prefer API 'station' when available."""
    if hostname_override:
        return socket.gethostbyname(_hostname_to_fqdn(hostname_override))
    if server and server.get("station"):
        return str(server["station"])
    if server:
        hostname = server.get("hostname")
        if isinstance(hostname, str):
            return socket.gethostbyname(_hostname_to_fqdn(hostname))
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


def _save_last_connection_state(country: str, hostname: str) -> None:
    config.set_setting("last_rotate_country", country)
    config.set_setting("last_rotate_hostname", hostname)


def _server_hostname(
    server: dict[str, object] | None, fallback: str | None = None
) -> str:
    if server:
        hostname = server.get("hostname")
        if isinstance(hostname, str) and hostname:
            return hostname
    if fallback:
        return fallback
    return "VPN"


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

    server: dict[str, object] | None = None
    expected_country = ""

    if args.server:
        print(f"🔎  Connecting to server: {args.server}")
    else:
        try:
            server = api.get_recommendation(args.country, args.proto)
        except (ValueError, Exception) as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        expected_country = args.country.upper()
        print(
            f"🔎  Best server in {args.country}: {server['hostname']} (load {server['load']}%)"
        )

    # Snapshot the local IP *before* the kill switch raises (which blocks outbound).
    # This is the baseline used to detect when the tunnel actually routes traffic.
    pre_ip = verify.fetch_ip_info()
    if pre_ip:
        print(f"📍  Local IP: {pre_ip.ip} ({pre_ip.country})")

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
    server_name = _server_hostname(server, args.server)
    try:
        openvpn.connect(
            country_code=args.country,
            protocol=args.proto,
            daemon=daemon,
            server_hostname=args.server,
        )
    except (RuntimeError, KeyboardInterrupt) as e:
        if args.killswitch:
            firewall.disable_killswitch()
        utils.kill_tray()
        if isinstance(e, RuntimeError):
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if daemon:
        # Verify the tunnel is actually routing traffic before declaring success.
        # When --server is given without a country argument, skip country check
        # (the positional default "US" should not be used as an assertion).
        print("⏳  Verifying tunnel", end="", flush=True)
        result = verify.verify_tunnel(
            expected_country=expected_country or None,
            pre_ip=pre_ip,
            progress=lambda: print(".", end="", flush=True),
        )
        print()  # newline after progress dots

        if result.state == verify.TunnelState.VERIFIED:
            print(f"✅  Tunnel verified: {result.message}")
            _notify(f"Connected to {server_name} ({args.country})", sound="Ping")

        elif result.state == verify.TunnelState.COUNTRY_MISMATCH:
            print(f"⚠️  Country mismatch — {result.message}", file=sys.stderr)
            if result.post_ip:
                print(
                    f"   Tunnel active but exit node is in {result.post_ip.country}, not {expected_country}.",
                    file=sys.stderr,
                )
            _notify(
                f"Mismatch: exited via {result.post_ip.country if result.post_ip else '?'}",
                sound="Basso",
                force=True,
            )
            if args.killswitch:
                print(
                    "🛡️  Kill switch ACTIVE. Run 'nordvpn disconnect' to stop.",
                    file=sys.stderr,
                )
            # Save state so 'status' and 'rotate' are aware of the last attempt
            _save_last_connection_state(expected_country or "", server_name)
            sys.exit(2)

        elif result.state == verify.TunnelState.COUNTRY_UNCONFIRMED:
            print(f"⚠️  Country unconfirmed — {result.message}", file=sys.stderr)
            if result.post_ip:
                print(
                    f"   Tunnel active but provider consensus is ambiguous for {result.post_ip.ip}.",
                    file=sys.stderr,
                )
            _notify("VPN country unconfirmed", sound="Basso", force=True)
            if args.killswitch:
                print(
                    "🛡️  Kill switch ACTIVE. Run 'nordvpn disconnect' to stop.",
                    file=sys.stderr,
                )
            _save_last_connection_state(expected_country or "", server_name)
            sys.exit(2)

        elif result.state == verify.TunnelState.LOCAL_ISP:
            print(f"❌  {result.message}", file=sys.stderr)
            print("   Killing broken VPN process.", file=sys.stderr)
            openvpn.disconnect()
            if args.killswitch:
                firewall.disable_killswitch()
            utils.kill_tray()
            _notify("VPN failed — still on local ISP", sound="Basso", force=True)
            sys.exit(1)

        else:  # TIMEOUT
            print(f"⚠️  {result.message}", file=sys.stderr)
            print(
                f"   VPN process is running but tunnel unconfirmed. Log: {openvpn.LOG_FILE}",
                file=sys.stderr,
            )
            _notify("VPN tunnel unconfirmed", sound="Basso", force=True)
            if args.killswitch:
                print(
                    "🛡️  Kill switch ACTIVE. Run 'nordvpn disconnect' to stop.",
                    file=sys.stderr,
                )
            _save_last_connection_state(expected_country or "", server_name)
            sys.exit(1)

        if args.killswitch:
            print(
                "🛡️  Kill switch is ACTIVE in background. Run 'nordvpn disconnect' to disable."
            )

    # Persist for rotate / status (only reached on success or foreground mode)
    _save_last_connection_state(
        expected_country,
        _server_hostname(server, args.server or ""),
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

    try:
        # Fetch a large pool to ensure we have enough options after filtering
        servers = api.get_servers(last_country, protocol="openvpn_udp", limit=500)
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
        proto="openvpn_udp",
        daemon=daemon,
        killswitch=args.killswitch,
    )
    _cmd_connect(rotate_args)


def _cmd_settings(args: argparse.Namespace) -> None:
    """Show or update CLI settings (tray, notifications, daemon)."""
    if args.tray is not None:
        state = args.tray.lower() == "enable"
        config.set_setting("tray_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Tray Icon auto-launch is now: {status}")
        if not state:
            print("   (Note: You may need to Quit the existing tray icon manually)")
    if args.notify is not None:
        state = args.notify.lower() == "enable"
        config.set_setting("notify_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Desktop Notifications are now: {status}")
    if args.daemon is not None:
        state = args.daemon.lower() == "enable"
        config.set_setting("daemon_enabled", state)
        status = "ENABLED" if state else "DISABLED"
        print(f"✅  Daemon mode (background connect) is now: {status}")
    cfg = config.load_config()
    print("Current Settings:")
    print(f"  Tray Icon:     {'ENABLED' if cfg['tray_enabled'] else 'DISABLED'}")
    print(f"  Notifications: {'ENABLED' if cfg['notify_enabled'] else 'DISABLED'}")
    print(f"  Daemon Mode:   {'ENABLED' if cfg['daemon_enabled'] else 'DISABLED'}")


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
    proc_running = openvpn.is_connected()
    if proc_running:
        print("🔒  VPN process: running")
    else:
        print("🔓  VPN: disconnected")

    current = verify.fetch_ip_info()
    if not current:
        print("⚠️  Could not fetch public IP (network may be unreachable)")
        return

    last_country = (config.get_setting("last_rotate_country") or "").upper()

    if proc_running and last_country:
        result = verify.verify_current_ip(expected_country=last_country, ip=current.ip)
        if result.state == verify.TunnelState.VERIFIED:
            print(f"✅  Tunnel verified — {current.summary()}")
            print(f"   {result.message}")
        elif result.state == verify.TunnelState.COUNTRY_MISMATCH:
            print(f"⚠️  Country mismatch — {result.message}")
            print(f"   Current IP: {current.summary()}")
        else:
            print(f"⚠️  Country unconfirmed — {result.message}")
            print(f"   Current IP: {current.summary()}")
    elif proc_running:
        # No saved country (e.g. connected via --server without country)
        print(f"🌍  Current IP: {current.summary()} (country check unavailable)")
    else:
        print(f"🌍  Local IP: {current.summary()}")


def _cmd_list(args: argparse.Namespace) -> None:
    try:
        servers = api.get_servers(args.country, args.proto, limit=args.limit)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    # Prefer online only if API provides status
    if servers and "status" in servers[0]:
        servers = [s for s in servers if s.get("status") == "online"]
    print(f"Servers in {args.country} ({args.proto}) — showing up to {args.limit}\n")
    for s in servers[: args.limit]:
        loc = s.get("locations") or []
        city = loc[0].get("city", "") if loc and isinstance(loc[0], dict) else ""
        extra = f" — {city}" if city else ""
        print(f"  {s['hostname']}  load {s.get('load', '?')}%{extra}")


def _cmd_setup(_args: argparse.Namespace) -> None:
    """Configure passwordless sudo for openvpn and pfctl (recommended)."""
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
        help="Protocol",
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
    p_list.add_argument("--limit", type=int, default=20, help="Max servers to show")
    p_list.set_defaults(func=_cmd_list)

    # list-countries
    p_countries = sub.add_parser("list-countries", help="List country codes")
    p_countries.set_defaults(func=_cmd_list_countries)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

# TODO

Future improvements and gaps to address.

---

## DNS handling

**Current behavior:** DNS is whatever the downloaded `.ovpn` config pushes (typically `redirect-gateway` and/or `dhcp-option DNS`), so traffic—including DNS—is sent through the VPN tunnel by default.

**TODO:**

- Document how DNS is handled in the README (e.g. a short “DNS” section) so privacy-conscious users know they’re covered.
- Clarify that `redirect-gateway` in the Nord config sends all traffic, including DNS, through the tunnel, and what to check if they want to verify (e.g. `scutil --dns`, or a leak test site).

---

## Kill switch + local access

**Current behavior:** Kill switch is strict: only traffic to the VPN tunnel and the Nord server IP is allowed. No access to LAN devices (e.g. SSH, NAS) while kill switch is on.

**TODO:**

- Add optional allow-list so users can reach local devices with kill switch enabled, e.g.:
  - `--allow-local 22 80` (allow outbound to LAN on specific ports), or
  - `--allow-range 192.168.1.0/24` (allow a CIDR range for LAN).
- Keep default behavior strict (VPN + Nord server only); allow-list is opt-in.

---

## IPv6 and kill switch

**Current behavior:** pf rules are focused on IPv4 (Nord server IP, UDP 1194, TCP 443). IPv6 behavior under kill switch is not explicitly defined and may leak.

**TODO:**

- Check whether current pf rules (and default routing) allow IPv6 to leak when kill switch is enabled.
- If so, either:
  - Document the limitation in the README and/or this doc, or
  - Add an explicit IPv6 block when kill switch is enabled (e.g. block default IPv6 outbound in the Nord anchor).

---

## Specialty server filters (P2P, Netflix, etc.)

**Current behavior:** Server selection is by country + protocol (UDP/TCP) + optional specific `--server` hostname. No filters for specialty types.

**TODO:**

- Check if Nord’s public API exposes server attributes for P2P, Netflix, dedicated IP, double VPN, Tor over VPN, anti-DDoS, etc.
- If the API supports it, add optional flags (e.g. `--p2p`, `--netflix`) that filter or prefer those servers without changing the core connect flow.

---

## Research-backed ideas (from demand / gap analysis)

These items come from external research on macOS NordVPN CLI demand and competitive requirements. They are not yet in the sections above.

**Developer / automation**

- **JSON output for status** — Add a `--json` (or similar) flag to `nordvpn status` so monitoring tools and scripts can parse connection state and IP info without scraping stdout.
- **Environment variables for zero-touch** — Expand env support beyond `NORD_USER` / `NORD_PASS` (e.g. default country, default daemon, default protocol) so CI/CD or Docker can run without config file or flags.

**Logging and privacy**

- **Log sanitation / minimal-log mode** — Offer a way to clear or avoid persisting local session logs (e.g. a command to scrub logs, or a “minimal log” mode). Privacy-focused users have asked for this in response to the official app’s mandatory local logging.

**Protocols**

- **NordLynx / WireGuard** — Nord does not currently expose NordLynx configs for download; tools are limited to OpenVPN. If Nord ever provides WireGuard/NordLynx configs or an API for them, consider adding support so CLI users can get speed/UX parity with the official app.

**Rotate and automation**

- **Rotate: configurable intervals** — Support automated rotation (e.g. “rotate every N minutes” or a mode that can be driven by cron) for users who need frequent IP changes (e.g. scraping, testing).

**Out of scope for now (noted for context)**

- **Meshnet management** — Users want terminal control for Meshnet (e.g. exit nodes, routing). That would require Nord APIs or protocols we don’t currently use; leave as future exploration only if demand and API availability justify it.
- **Token-based login** — The report cites the Linux client’s non-expiring auth token. We use service credentials. If Nord exposes a similar token for manual/CLI use on macOS, consider adding it as an alternative to username/password.

---

## First-class CLI and library support (requirements)

**Goal:** Keep the CLI as the primary interface and add a clean, safe Python API (e.g. `import nordvpn; nordvpn.connect(country="US", killswitch=True)`). Errors surface as exceptions when used as a library; no process exit.

**Current state:** CLI works; library use is partial. Several code paths call `sys.exit()`, and `__init__.py` only exposes `__version__`. Full connect/disconnect orchestration lives in private CLI handlers.

**Requirements (for implementation):**

1. **Exception types** — Add a small exception hierarchy (e.g. `nordvpn/exceptions.py`): base `NordVPNError`, and subclasses such as `NordVPNCredentialsError`, `NordVPNConnectionError`, `NordVPNFirewallError`, `NordVPNConfigError`. Document which public functions raise which.

2. **Refactor: raise instead of `sys.exit()`** — **openvpn.connect()**: on missing credentials, raise (e.g. `NordVPNCredentialsError`) instead of `sys.exit(1)`; on `KeyboardInterrupt`, disconnect and re-raise so callers can handle. **firewall.enable_killswitch()** / **disable_killswitch()**: on failure, raise instead of `sys.exit(1)`. **setup.install_sudoers_rule()**: on any failure, raise instead of `sys.exit(1)`. **config.save_config()**: on `OSError`, raise instead of printing.

3. **Core / high-level API** — Add a module (e.g. `nordvpn/core.py`) used by both CLI and library: **connect(country, server, protocol, daemon, killswitch, …)** (resolve server, optional kill switch, tray, openvpn.connect, persist state, notify; return on success, raise on failure), **disconnect()**, **status()** (return structured dict: connected, ip_info), **list_servers(country, protocol, limit)**, **list_countries()**, **rotate(max_load, killswitch, daemon, …)**. All use refactored modules that raise; no `sys.exit`.

4. **Public API surface** — In **`nordvpn/__init__.py`**: export core functions and exception types; define `__all__`. CLI stays entry point via `nordvpn.cli:main`.

5. **CLI unchanged** — CLI parses args, calls core API; on exception, catches, prints message, then `sys.exit(1)`. Behavior and output stay the same.

6. **Optional** — Quiet / no_print mode for library use so core and submodules do not print when invoked as a library; CLI keeps current messages.
- Optionally add a “quiet” or “no_print” mode for library callers who don’t want stdout/stderr output.

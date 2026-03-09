# nordvpn-macos-cli

**NordVPN CLI for macOS** using OpenVPN. NordVPN doesn’t ship an official Mac CLI—this gives you a `nordvpn` command to find servers, download configs, and run OpenVPN.

## Requirements

- **macOS** (uses OpenVPN and pf; not tested on Linux/Windows)
- **Python 3.8+**
- **OpenVPN** (e.g. `brew install openvpn`)
- **NordVPN service credentials** from [Manual setup](https://my.nordaccount.com/dashboard/) (not your NordAccount email/password)

## Install

```bash
# From GitHub:
pip install git+https://github.com/lukerbs/nordvpn-macos-cli.git

# Or clone this repo and run:
pip install -e .
```

After install, the `nordvpn` command is available. You can also run without installing: `python -m nordvpn`.

### Local project setup with uv

For this local checkout in `/Users/lume/Projects/nordvpn-operator`, use the project virtual environment created with `uv`:

```bash
uv venv .venv
uv pip install -e .
```

Then either activate it:

```bash
source .venv/bin/activate
python -m nordvpn status
```

Or run the CLI directly through the virtualenv without activation:

```bash
./.venv/bin/python -m nordvpn status
./.venv/bin/python -m nordvpn list-countries
```

For this local project, prefer `./.venv/bin/python -m nordvpn ...` in docs, scripts, and assistant-operated workflows unless the virtualenv is already activated.

**Optional:** Run `nordvpn setup` once to configure passwordless sudo for OpenVPN and the firewall so `nordvpn connect` and kill switch work without repeated password prompts.

## Credentials

Set these in your shell (e.g. in `~/.zshrc`). The CLI uses them on first connect and stores credentials in `~/.nord-auth` with secure permissions. You only need to set the env vars once.

```bash
export NORD_USER="your_service_username"
export NORD_PASS="your_service_password"
```

Then run `source ~/.zshrc` (or open a new terminal).

For this local setup, you can also create `~/.nord-auth` directly if you already have the Nord manual setup service credentials. The file format is:

```text
service_username
service_password
```

The file should be readable only by your user:

```bash
chmod 600 ~/.nord-auth
```

## Usage

| Command | Description |
|--------|-------------|
| `nordvpn setup` | Configure passwordless sudo for OpenVPN and pfctl (one-time). |
| `nordvpn connect [COUNTRY]` | Connect to best server in country (default: US). |
| `nordvpn connect --server HOSTNAME` | Connect to a **specific server** (e.g. `us9364` or `us9364.nordvpn.com`). |
| `nordvpn connect --proto openvpn_tcp` | Use TCP instead of UDP (if UDP is blocked). |
| `nordvpn connect --daemon` | Run OpenVPN in background (log: `~/.nordvpn.log`). |
| `nordvpn connect --no-daemon` | Run in foreground (overrides default/settings). |
| `nordvpn connect --killswitch` | Enable macOS firewall: block all traffic except VPN (kill switch). |
| `nordvpn disconnect` | Stop OpenVPN (and disable kill switch if it was enabled). |
| `nordvpn rotate` | Connect to a random low-load server in the same country; disconnects first if connected. |
| `nordvpn rotate --killswitch` | Same as rotate, with kill switch (atomic server swap). |
| `nordvpn rotate --max-load N` | Exclude servers above N% load (default: 70). |
| `nordvpn status` | Show connected/disconnected and public IP info. |
| `nordvpn list [COUNTRY]` | List servers for country (`--limit N`, `--proto openvpn_udp` or `openvpn_tcp`). |
| `nordvpn list-countries` | List country codes. |
| `nordvpn settings` | Show current settings (tray, notifications, daemon). |
| `nordvpn settings --tray enable` | Auto-launch menu bar icon when you connect. |
| `nordvpn settings --tray disable` | Do not launch menu bar icon. |
| `nordvpn settings --notify enable` | Desktop notifications (daemon connect, disconnect, kill switch). |
| `nordvpn settings --notify disable` | No desktop notifications (default). |
| `nordvpn settings --daemon enable` | Run OpenVPN in background by default. |
| `nordvpn settings --daemon disable` | Run OpenVPN in foreground by default. |

Settings are stored in `~/.nordvpn-config`. Daemon mode is on by default (background connect); use `--no-daemon` on `connect` or `rotate` to run in the foreground. With the tray enabled, the menu bar icon shows 🔒/🔓 and lets you connect (US) or disconnect without a terminal. Notifications are off by default; enable with `nordvpn settings --notify enable`.

### Examples

```bash
nordvpn connect                    # Best US server (UDP, background by default)
nordvpn connect JP                 # Best server in Japan
nordvpn connect --server us9364    # Specific server (see nordvpn list to get hostnames)
nordvpn connect -s us1234.nordvpn.com --proto openvpn_tcp
nordvpn connect DE --proto openvpn_tcp   # Germany, TCP (e.g. strict networks)
nordvpn connect --killswitch             # Block all traffic except VPN (macOS pf)
nordvpn connect --no-daemon              # Run in foreground (see log in terminal)
nordvpn rotate                      # Switch to a random low-load server (same country)
nordvpn rotate --killswitch         # Rotate with kill switch (no leak during swap)
nordvpn rotate --max-load 50        # Only consider servers under 50% load
nordvpn list US --limit 10 --proto openvpn_tcp
nordvpn settings                    # View current settings (tray, notifications, daemon)
nordvpn settings --tray enable       # Show lock icon in menu bar when connected
nordvpn settings --notify enable    # Desktop notifications (daemon, disconnect, etc.)
nordvpn settings --daemon disable   # Prefer foreground connect
nordvpn connect                     # Tray icon launches automatically (if enabled)
nordvpn status
nordvpn disconnect
```

## Kill switch (macOS)

Use `--killswitch` (or `-k`) to enable a firewall that blocks all traffic except the VPN tunnel. This uses macOS’s built-in **Packet Filter (pf)**.

The kill switch is fail-closed: if the script crashes, the terminal closes, or the VPN drops, the firewall stays on and your internet stays blocked, so your real IP is not exposed.

- **Enable:** `nordvpn connect --killswitch` (or add `-k` to any connect command).
- **Disable:** `nordvpn disconnect` turns off the firewall and stops OpenVPN.
- **Stuck offline** (e.g. after a crash): restore connectivity with `sudo pfctl -d` (Packet Filter disable). To flush only the NordVPN rules: `sudo pfctl -a com.nordvpn.client -F all`. A reboot also clears pf rules.

## License

MIT.

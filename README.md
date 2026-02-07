# nordvpn-macos-cli

**NordVPN CLI for macOS** using OpenVPN. Since NordVPN doesn’t ship an official Mac CLI, this package gives you a `nordvpn` command that finds servers, downloads configs, and runs OpenVPN.

## Requirements

- **macOS** (uses OpenVPN and pf; not tested on Linux/Windows)
- **Python 3.8+**
- **OpenVPN** (e.g. `brew install openvpn`)
- **NordVPN “Service credentials”** from [Manual setup](https://my.nordaccount.com/dashboard/) (not your email/password)

## Install

```bash
# From this repo (editable)
pip install -e .

# Or from PyPI (when published)
pip install nordvpn-cli
```

After install, the `nordvpn` command is available. You can also run from the repo without installing: `python -m nordvpn`.

**Optional:** Run `nordvpn setup` once to configure passwordless sudo for OpenVPN and the firewall, so `nordvpn connect` and kill switch work without repeated password prompts.

## Credentials

Set these in your shell (e.g. in `~/.zshrc`). The library uses them automatically and will create `~/.nord-auth` from them on first connect (with secure permissions), so you only need to set the env vars once.

```bash
export NORD_USER="your_service_username"
export NORD_PASS="your_service_password"
```

Then run `source ~/.zshrc` (or open a new terminal).

## Usage

| Command | Description |
|--------|-------------|
| `nordvpn setup` | Configure passwordless sudo for openvpn and pfctl (one-time). |
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

Settings are stored in `~/.nordvpn-config`. Daemon mode is **on by default** (background connect); use `--no-daemon` on `connect` or `rotate` to run in foreground. The menu bar icon (when enabled) shows 🔒/🔓 and lets you Connect (US) or Disconnect without opening a terminal. Notifications are off by default; enable with `nordvpn settings --notify enable`.

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

On macOS you can use `--killswitch` (or `-k`) to enable a firewall that blocks all traffic except the VPN tunnel and the VPN server. This uses the built-in **Packet Filter (pf)**.

The kill switch is **fail-closed**: if the script crashes, the terminal is closed, or the VPN drops, the firewall **stays on** and your internet stays blocked. That avoids leaking your real IP when the tunnel goes down.

- **Enable:** `nordvpn connect --killswitch` (or add `-k` to any connect command).
- **Disable:** Run `nordvpn disconnect` to turn off the firewall and stop OpenVPN.
- **If you get stuck offline** (script crashed, etc.), restore internet with:
  ```bash
  sudo pfctl -d
  ```
  Memorize: **P**acket **F**ilter **D**isable. Alternatively, flush only the NordVPN anchor (keeps PF enabled): `sudo pfctl -a com.nordvpn.client -F all`. A reboot also clears pf rules.

## License

MIT.


# REPO TO CHECK OUT: https://github.com/jotyGill/openpyn-nordvpn (could be good for inspiration / stealing some of their know-how)
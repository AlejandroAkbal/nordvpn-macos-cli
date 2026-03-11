"""NordVPN public API: countries, technologies, servers, recommendations."""

from __future__ import annotations

import requests
from typing import Any, Optional

NORD_API_BASE = "https://api.nordvpn.com/v1"
CONFIG_BASE_URL = "https://downloads.nordcdn.com/configs/files"

DEFAULT_TIMEOUT = 10
SERVERS_TIMEOUT = 30  # /servers can return large payloads on slow connections
XOR_PROTOCOLS = frozenset({"openvpn_xor_udp", "openvpn_xor_tcp"})


def _get(
    endpoint: str,
    params: Optional[dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> Any:
    t = timeout if timeout is not None else DEFAULT_TIMEOUT
    resp = requests.get(f"{NORD_API_BASE}/{endpoint}", params=params, timeout=t)
    resp.raise_for_status()
    return resp.json()


def get_countries() -> list[dict[str, Any]]:
    """Return list of country objects (id, code, name, etc.)."""
    return _get("servers/countries")


def get_technologies() -> list[dict[str, Any]]:
    """Return list of technology objects (id, identifier, name, etc.)."""
    return _get("technologies")


def get_id_by_identifier(
    endpoint: str,
    identifier: str,
    key_name: str = "code",
) -> Optional[int]:
    """Resolve country code or protocol identifier to Nord API id."""
    data = _get(endpoint)
    for item in data:
        if (
            str(item.get(key_name, "")).lower() == identifier.lower()
            or str(item.get("identifier", "")).lower() == identifier.lower()
        ):
            return item["id"]
    return None


def get_recommendation(
    country_code: str = "US",
    protocol: str = "openvpn_udp",
) -> dict[str, Any]:
    """Get recommended server for country + protocol. Trust API sort order (no client-side load sorting). Raises on error."""
    country_id = get_id_by_identifier(
        "servers/countries", country_code, key_name="code"
    )
    if not country_id:
        raise ValueError(f"Country code '{country_code}' not found")
    tech_id = get_id_by_identifier("technologies", protocol, key_name="identifier")
    if not tech_id:
        raise ValueError(f"Protocol '{protocol}' not found")

    if protocol in XOR_PROTOCOLS:
        params = {
            "filters[country_id]": country_id,
            "filters[servers_technologies][id]": tech_id,
            "limit": 100,
        }
        data = _get("servers", params=params, timeout=SERVERS_TIMEOUT)
        data.sort(key=lambda server: server.get("load", float("inf")))
    else:
        params = {
            "filters[country_id]": country_id,
            "filters[servers_technologies][id]": tech_id,
            "limit": 1,
        }
        data = _get("servers/recommendations", params=params)
    if not data:
        raise ValueError("No servers found matching criteria")
    return data[0]


def get_servers(
    country_code: str = "US",
    protocol: str = "openvpn_udp",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Get list of servers for country + protocol (for 'list' command)."""
    country_id = get_id_by_identifier(
        "servers/countries", country_code, key_name="code"
    )
    if not country_id:
        raise ValueError(f"Country code '{country_code}' not found")
    tech_id = get_id_by_identifier("technologies", protocol, key_name="identifier")
    if not tech_id:
        raise ValueError(f"Protocol '{protocol}' not found")

    params = {
        "filters[country_id]": country_id,
        "filters[servers_technologies][id]": tech_id,
        "limit": min(limit, 5000),
    }
    return _get("servers", params=params, timeout=SERVERS_TIMEOUT)

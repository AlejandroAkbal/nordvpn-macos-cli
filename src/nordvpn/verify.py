"""Post-connect tunnel verification: poll public IP until the tunnel is confirmed."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import requests

IPINFO_URL = "https://ipinfo.io/json"
IPWHOIS_URL = "https://ipwho.is/{ip}"
IPAPIIS_URL = "https://api.ipapi.is?q={ip}"
DEFAULT_VERIFY_TIMEOUT = 45  # seconds to wait for IP to change
DEFAULT_POLL_INTERVAL = 2  # seconds between polls
REQUEST_TIMEOUT = 8  # per-request timeout for ipinfo
PROVIDER_TIMEOUT = 5
PROVIDER_WORKERS = 3


class TunnelState(Enum):
    VERIFIED = "verified"  # IP changed; country matches (or no check)
    COUNTRY_MISMATCH = "country_mismatch"  # IP changed but not in requested country
    COUNTRY_UNCONFIRMED = "country_unconfirmed"  # providers disagree / no consensus
    LOCAL_ISP = "local_isp"  # IP did not change — still on local ISP
    TIMEOUT = "timeout"  # Never got a changed IP within the deadline


@dataclass
class IPInfo:
    ip: str
    country: str
    org: str = ""
    city: str = ""
    region: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [p for p in [self.ip, self.country, self.city, self.org] if p]
        return " | ".join(parts)


@dataclass
class TunnelResult:
    state: TunnelState
    pre_ip: IPInfo | None
    post_ip: IPInfo | None
    expected_country: str | None
    elapsed: float
    message: str

    @property
    def ok(self) -> bool:
        return self.state == TunnelState.VERIFIED


@dataclass
class GeoObservation:
    provider: str
    ip: str
    country: str = ""
    org: str = ""
    city: str = ""
    region: str = ""
    error: str = ""


@dataclass
class GeoConsensus:
    ip: str
    expected_country: str = ""
    provider_results: list[GeoObservation] = field(default_factory=list)
    countries: dict[str, list[str]] = field(default_factory=dict)
    majority_country: str = ""
    majority_count: int = 0

    @property
    def provider_count(self) -> int:
        return len(self.provider_results)

    @property
    def matching_providers(self) -> list[str]:
        return list(self.countries.get(self.expected_country, []))

    @property
    def has_majority(self) -> bool:
        return self.majority_count >= 2

    @property
    def matched_expected(self) -> bool:
        return (
            bool(self.expected_country)
            and self.has_majority
            and self.majority_country == self.expected_country
        )

    @property
    def mismatched_expected(self) -> bool:
        return (
            bool(self.expected_country)
            and self.has_majority
            and self.majority_country != self.expected_country
        )

    @property
    def ambiguous(self) -> bool:
        return bool(self.expected_country) and not self.has_majority

    def summary(self) -> str:
        votes = []
        for country, providers in sorted(
            self.countries.items(), key=lambda item: (-len(item[1]), item[0])
        ):
            votes.append(f"{country} via {', '.join(providers)}")
        if not votes:
            return "no provider returned country data"
        return "; ".join(votes)


def fetch_ip_info(
    timeout: int = REQUEST_TIMEOUT, ip: str | None = None
) -> IPInfo | None:
    """Fetch public IP metadata from ipinfo.io. Returns None on any error."""
    try:
        url = IPINFO_URL if not ip else f"https://ipinfo.io/{ip}/json"
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            d: dict[str, Any] = r.json()
            return IPInfo(
                ip=d.get("ip", ""),
                country=d.get("country", ""),
                org=d.get("org", ""),
                city=d.get("city", ""),
                region=d.get("region", ""),
                raw=d,
            )
    except Exception:
        pass
    return None


def _provider_ipinfo(ip: str) -> GeoObservation | None:
    info = fetch_ip_info(timeout=PROVIDER_TIMEOUT, ip=ip)
    if info is None or not info.country:
        return None
    return GeoObservation(
        provider="ipinfo",
        ip=info.ip or ip,
        country=info.country.upper(),
        org=info.org,
        city=info.city,
        region=info.region,
    )


def _provider_ipwhois(ip: str) -> GeoObservation | None:
    try:
        response = requests.get(IPWHOIS_URL.format(ip=ip), timeout=PROVIDER_TIMEOUT)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    except Exception:
        return None

    if data.get("success") is False:
        return None

    connection = data.get("connection")
    org = ""
    if isinstance(connection, dict):
        org_value = connection.get("org") or connection.get("isp")
        if isinstance(org_value, str):
            org = org_value

    return GeoObservation(
        provider="ipwho.is",
        ip=str(data.get("ip") or ip),
        country=str(data.get("country_code") or "").upper(),
        org=org,
        city=str(data.get("city") or ""),
        region=str(data.get("region") or ""),
    )


def _provider_ipapiis(ip: str) -> GeoObservation | None:
    try:
        response = requests.get(IPAPIIS_URL.format(ip=ip), timeout=PROVIDER_TIMEOUT)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    except Exception:
        return None

    if "error" in data:
        return None

    location = data.get("location")
    asn = data.get("asn")
    company = data.get("company")

    country = ""
    city = ""
    region = ""
    if isinstance(location, dict):
        raw_country = location.get("country_code")
        raw_city = location.get("city")
        raw_region = location.get("state")
        if isinstance(raw_country, str):
            country = raw_country.upper()
        if isinstance(raw_city, str):
            city = raw_city
        if isinstance(raw_region, str):
            region = raw_region

    org = ""
    if isinstance(asn, dict):
        raw_org = asn.get("org")
        if isinstance(raw_org, str):
            org = raw_org
    if not org and isinstance(company, dict):
        raw_name = company.get("name")
        if isinstance(raw_name, str):
            org = raw_name

    return GeoObservation(
        provider="ipapi.is",
        ip=str(data.get("ip") or ip),
        country=country,
        org=org,
        city=city,
        region=region,
    )


def fetch_geo_consensus(ip: str, expected_country: str | None = None) -> GeoConsensus:
    expected = expected_country.upper() if expected_country else ""
    providers = (_provider_ipinfo, _provider_ipwhois, _provider_ipapiis)
    results: list[GeoObservation] = []

    with ThreadPoolExecutor(max_workers=PROVIDER_WORKERS) as executor:
        futures = [executor.submit(provider, ip) for provider in providers]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                continue
            if result is not None and result.country:
                results.append(result)

    countries: dict[str, list[str]] = {}
    for result in results:
        countries.setdefault(result.country, []).append(result.provider)

    majority_country = max(countries, key=lambda c: len(countries[c]), default="")
    majority_count = len(countries[majority_country]) if majority_country else 0

    return GeoConsensus(
        ip=ip,
        expected_country=expected,
        provider_results=sorted(results, key=lambda item: item.provider),
        countries=countries,
        majority_country=majority_country,
        majority_count=majority_count,
    )


def verify_current_ip(
    expected_country: str | None = None, ip: str | None = None
) -> TunnelResult:
    current = fetch_ip_info(ip=ip)
    if current is None:
        return TunnelResult(
            state=TunnelState.TIMEOUT,
            pre_ip=None,
            post_ip=None,
            expected_country=expected_country,
            elapsed=0,
            message="Could not fetch public IP",
        )

    if not expected_country:
        return TunnelResult(
            state=TunnelState.VERIFIED,
            pre_ip=None,
            post_ip=current,
            expected_country=None,
            elapsed=0,
            message=current.summary(),
        )

    consensus = fetch_geo_consensus(current.ip, expected_country)
    if consensus.provider_count < 2:
        return TunnelResult(
            state=TunnelState.COUNTRY_UNCONFIRMED,
            pre_ip=None,
            post_ip=current,
            expected_country=expected_country,
            elapsed=0,
            message=(
                f"not enough provider data to confirm country: {consensus.summary()}"
            ),
        )

    if consensus.matched_expected:
        return TunnelResult(
            state=TunnelState.VERIFIED,
            pre_ip=None,
            post_ip=current,
            expected_country=expected_country,
            elapsed=0,
            message=(
                f"country {consensus.expected_country} confirmed by majority vote "
                f"({consensus.majority_count}/{consensus.provider_count} providers: "
                f"{', '.join(consensus.matching_providers)})"
            ),
        )

    if consensus.mismatched_expected:
        return TunnelResult(
            state=TunnelState.COUNTRY_MISMATCH,
            pre_ip=None,
            post_ip=current,
            expected_country=expected_country,
            elapsed=0,
            message=(
                f"requested {consensus.expected_country}, majority vote says "
                f"{consensus.majority_country} via "
                f"{', '.join(consensus.countries[consensus.majority_country])}"
            ),
        )

    return TunnelResult(
        state=TunnelState.COUNTRY_UNCONFIRMED,
        pre_ip=None,
        post_ip=current,
        expected_country=expected_country,
        elapsed=0,
        message=f"provider results disagree: {consensus.summary()}",
    )


def verify_tunnel(
    expected_country: str | None = None,
    pre_ip: IPInfo | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    progress: Callable[[], None] | None = None,
) -> TunnelResult:
    """
    Poll public IP until it changes from *pre_ip* or *timeout* expires.

    States returned:
      VERIFIED         – IP changed AND country matches (or no country check)
      COUNTRY_MISMATCH – IP changed but observed country ≠ expected_country
      LOCAL_ISP        – Timed out with IP still equal to pre_ip (broken tunnel)
      TIMEOUT          – Timed out without ever receiving an IP response

    With a kill switch active, ipinfo.io requests are blocked until the tunnel
    is up (traffic routes through the VPN interface). This is intentional: the
    first successful response is the post-tunnel IP.
    """
    t0 = time.monotonic()
    last_post: IPInfo | None = None

    while True:
        elapsed = time.monotonic() - t0

        if elapsed >= timeout:
            # One final check at the deadline before giving up
            final = fetch_ip_info()
            last_post = final or last_post
            if (
                last_post is not None
                and pre_ip is not None
                and last_post.ip == pre_ip.ip
            ):
                return TunnelResult(
                    state=TunnelState.LOCAL_ISP,
                    pre_ip=pre_ip,
                    post_ip=last_post,
                    expected_country=expected_country,
                    elapsed=elapsed,
                    message=f"IP unchanged after {timeout:.0f}s — still {last_post.ip} (local ISP)",
                )
            return TunnelResult(
                state=TunnelState.TIMEOUT,
                pre_ip=pre_ip,
                post_ip=last_post,
                expected_country=expected_country,
                elapsed=elapsed,
                message=f"No IP response within {timeout:.0f}s — tunnel unconfirmed",
            )

        post_ip = fetch_ip_info()
        if post_ip is not None:
            last_post = post_ip
            ip_changed = (pre_ip is None) or (post_ip.ip != pre_ip.ip)
            if ip_changed:
                result = verify_current_ip(
                    expected_country=expected_country, ip=post_ip.ip
                )
                result.pre_ip = pre_ip
                result.post_ip = post_ip
                result.elapsed = elapsed
                return result

        if progress is not None:
            progress()
        time.sleep(poll_interval)

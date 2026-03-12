from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Generator
from urllib.error import URLError
from urllib.request import urlopen

import pytest


IPINFO_URL = "https://ipinfo.io/json"
CONNECT_TIMEOUT = int(os.environ.get("NORDVPN_E2E_CONNECT_TIMEOUT", "180"))
DISCONNECT_TIMEOUT = int(os.environ.get("NORDVPN_E2E_DISCONNECT_TIMEOUT", "60"))
POLL_INTERVAL = 2.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cli_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    real_home = Path(env.get("HOME", str(Path.home())))
    test_home = tmp_path / "home"
    test_home.mkdir()

    env["HOME"] = str(test_home)
    env["PYTHONPATH"] = str(_repo_root() / "src")

    auth_file = real_home / ".nord-auth"
    if not (env.get("NORD_USER") and env.get("NORD_PASS")) and auth_file.exists():
        shutil.copy2(auth_file, test_home / ".nord-auth")

    return env


def _require_real_test_env(env: dict[str, str]) -> None:
    if sys.platform != "darwin":
        pytest.skip("real end-to-end VPN tests are macOS-only")
    if os.environ.get("RUN_NORDVPN_E2E") != "1":
        pytest.skip("set RUN_NORDVPN_E2E=1 to run live VPN tests")
    openvpn_bin = shutil.which("openvpn", path=env.get("PATH"))
    if openvpn_bin is None:
        pytest.skip("openvpn binary not found")
    if not (
        (env.get("NORD_USER") and env.get("NORD_PASS"))
        or Path(env["HOME"], ".nord-auth").exists()
    ):
        pytest.skip("missing NordVPN credentials (NORD_USER/NORD_PASS or ~/.nord-auth)")

    probe = subprocess.run(
        ["sudo", "-n", openvpn_bin, "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("non-interactive sudo for openvpn is required for live tests")


def _run_cli(
    env: dict[str, str], *args: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nordvpn.cli", *args],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fetch_ip_info(timeout: int = 10) -> dict[str, str]:
    try:
        with urlopen(IPINFO_URL, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AssertionError(f"failed to fetch live IP info: {exc}") from exc


def _openvpn_running() -> bool:
    return (
        subprocess.run(
            ["pgrep", "-x", "openvpn"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        ).returncode
        == 0
    )


def _disconnect_best_effort(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run_cli(env, "disconnect", timeout=DISCONNECT_TIMEOUT)


def _wait_until(predicate: Callable[[], bool], timeout: int, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL)
    raise AssertionError(message)


@pytest.fixture
def live_env(tmp_path: Path) -> Generator[dict[str, str], None, None]:
    env = _cli_env(tmp_path)
    _require_real_test_env(env)

    if _openvpn_running() and os.environ.get("NORDVPN_E2E_FORCE_DISCONNECT") != "1":
        pytest.skip(
            "an openvpn process is already running; set NORDVPN_E2E_FORCE_DISCONNECT=1 to let tests tear it down"
        )

    if _openvpn_running():
        _disconnect_best_effort(env)
        _wait_until(
            lambda: not _openvpn_running(),
            DISCONNECT_TIMEOUT,
            "openvpn stayed up before test start",
        )

    yield env

    if _openvpn_running():
        _disconnect_best_effort(env)
        _wait_until(
            lambda: not _openvpn_running(),
            DISCONNECT_TIMEOUT,
            "openvpn stayed up during test cleanup",
        )


@pytest.mark.e2e
def test_status_reports_local_ip_when_disconnected(live_env: dict[str, str]) -> None:
    local = _fetch_ip_info()

    result = _run_cli(live_env, "status")

    assert result.returncode == 0, result.stderr
    assert "VPN: disconnected" in result.stdout
    assert "Local IP:" in result.stdout
    assert local["ip"] in result.stdout


@pytest.mark.e2e
def test_connect_status_disconnect_real_country(live_env: dict[str, str]) -> None:
    country = os.environ.get("NORDVPN_E2E_COUNTRY", "US").upper()
    baseline = _fetch_ip_info()

    connect = _run_cli(
        live_env, "connect", country, "--daemon", timeout=CONNECT_TIMEOUT
    )
    assert connect.returncode in (0, 2), connect.stderr or connect.stdout

    tunnel = _fetch_ip_info()
    assert tunnel["ip"] != baseline["ip"], (
        baseline,
        tunnel,
        connect.stdout,
        connect.stderr,
    )

    if connect.returncode == 0:
        assert "Tunnel verified" in connect.stdout, connect.stdout
    else:
        combined = f"{connect.stdout}\n{connect.stderr}"
        assert "Country unconfirmed" in combined or "Country mismatch" in combined, (
            combined
        )

    assert _openvpn_running(), connect.stdout

    status = _run_cli(live_env, "status")
    assert status.returncode == 0, status.stderr
    assert tunnel["ip"] in status.stdout, status.stdout
    if connect.returncode == 0:
        assert "Tunnel verified" in status.stdout, status.stdout
    else:
        assert (
            "Country unconfirmed" in status.stdout
            or "Country mismatch" in status.stdout
        ), status.stdout

    disconnect = _disconnect_best_effort(live_env)
    assert disconnect.returncode == 0, disconnect.stderr or disconnect.stdout
    _wait_until(
        lambda: not _openvpn_running(),
        DISCONNECT_TIMEOUT,
        "openvpn stayed up after disconnect",
    )

    restored = _fetch_ip_info()
    assert restored["ip"] == baseline["ip"], (
        baseline,
        restored,
        disconnect.stdout,
        disconnect.stderr,
    )


@pytest.mark.e2e
def test_connect_specific_server_status_is_unscoped(live_env: dict[str, str]) -> None:
    server = os.environ.get("NORDVPN_E2E_SERVER")
    if not server:
        pytest.skip(
            "set NORDVPN_E2E_SERVER to run the explicit --server end-to-end test"
        )
    assert server is not None

    connect = _run_cli(
        live_env, "connect", "--server", server, "--daemon", timeout=CONNECT_TIMEOUT
    )
    assert connect.returncode == 0, connect.stderr or connect.stdout
    assert "Tunnel verified" in connect.stdout

    status = _run_cli(live_env, "status")
    assert status.returncode == 0, status.stderr
    assert "country check unavailable" in status.stdout, status.stdout

    disconnect = _disconnect_best_effort(live_env)
    assert disconnect.returncode == 0, disconnect.stderr or disconnect.stdout
    _wait_until(
        lambda: not _openvpn_running(),
        DISCONNECT_TIMEOUT,
        "openvpn stayed up after explicit-server disconnect",
    )

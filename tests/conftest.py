from __future__ import annotations

import ipaddress
import os
import socket
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel.vault import Vault

_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex


@dataclass(frozen=True)
class RecordedBrowserCall:
    url: str
    caller: str
    browser_name: str | None = None
    test_nodeid: str | None = None


RECORDED_BROWSER_CALLS: list[RecordedBrowserCall] = []
webbrowser._recorded_calls = RECORDED_BROWSER_CALLS  # type: ignore[attr-defined]


def get_recorded_browser_calls() -> list[RecordedBrowserCall]:
    return RECORDED_BROWSER_CALLS


def _is_loopback_address(address: object) -> bool:
    if isinstance(address, (str, bytes, os.PathLike)):
        # AF_UNIX socket path
        return True
    if isinstance(address, tuple) and address:
        host = address[0]
        if not isinstance(host, str):
            return False
        if host.lower() == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_loopback
        except ValueError:
            return False
    return False


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if not _is_loopback_address(address):
        raise RuntimeError(f"test tried to reach non-loopback address {address!r}")
    return _REAL_SOCKET_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _is_loopback_address(address):
        raise RuntimeError(f"test tried to reach non-loopback address {address!r}")
    return _REAL_SOCKET_CONNECT_EX(self, address)


class _StubBrowserController:
    def __init__(self, name: str | None = None, test_nodeid: str | None = None) -> None:
        self.name = name
        self._test_nodeid = test_nodeid

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        del new, autoraise
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="controller.open",
                browser_name=self.name,
                test_nodeid=self._test_nodeid,
            )
        )
        return True

    def open_new(self, url: str) -> bool:
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="controller.open_new",
                browser_name=self.name,
                test_nodeid=self._test_nodeid,
            )
        )
        return True

    def open_new_tab(self, url: str) -> bool:
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="controller.open_new_tab",
                browser_name=self.name,
                test_nodeid=self._test_nodeid,
            )
        )
        return True


@pytest.fixture(autouse=True)
def isolated_platform_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    value = Vault(tmp_path / "vault")
    value.initialize(name="Test GSV")
    return value


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    nodeid = request.node.nodeid

    def _stub_open(url: str, new: int = 0, autoraise: bool = True) -> bool:
        del new, autoraise
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="webbrowser.open",
                browser_name=None,
                test_nodeid=nodeid,
            )
        )
        return True

    def _stub_open_new(url: str) -> bool:
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="webbrowser.open_new",
                browser_name=None,
                test_nodeid=nodeid,
            )
        )
        return True

    def _stub_open_new_tab(url: str) -> bool:
        RECORDED_BROWSER_CALLS.append(
            RecordedBrowserCall(
                url=url,
                caller="webbrowser.open_new_tab",
                browser_name=None,
                test_nodeid=nodeid,
            )
        )
        return True

    def _stub_get(using: str | None = None) -> _StubBrowserController:
        return _StubBrowserController(name=using, test_nodeid=nodeid)

    monkeypatch.setattr(webbrowser, "open", _stub_open)
    monkeypatch.setattr(webbrowser, "open_new", _stub_open_new)
    monkeypatch.setattr(webbrowser, "open_new_tab", _stub_open_new_tab)
    monkeypatch.setattr(webbrowser, "get", _stub_get)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    del exitstatus, config
    if RECORDED_BROWSER_CALLS:
        caught_tests: dict[str, list[RecordedBrowserCall]] = {}
        for call in RECORDED_BROWSER_CALLS:
            if call.test_nodeid and not call.test_nodeid.startswith(
                "tests/test_conftest_guards.py"
            ):
                caught_tests.setdefault(call.test_nodeid, []).append(call)
        if caught_tests:
            terminalreporter.write_sep(
                "=",
                f"GUARD REPORT: {len(caught_tests)} tests caught attempting real browser launch",
            )
            for test, calls in sorted(caught_tests.items()):
                terminalreporter.write_line(f"  [CAUGHT] {test} ({len(calls)} call(s))")
                for c in calls:
                    terminalreporter.write_line(f"           via {c.caller}: {c.url}")

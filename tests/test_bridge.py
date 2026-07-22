from __future__ import annotations

import errno
import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from http import HTTPStatus
from importlib.resources import as_file, files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest

from continuity_kernel import __version__, bridge
from continuity_kernel.config import data_dir
from continuity_kernel.errors import SetupError
from continuity_kernel.vault import Vault, doctor_dict

INSTANCE_ID = "a" * 32
ACCESS_TOKEN = "b" * 48


@pytest.fixture
def running_bridge(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[bridge.BridgeHTTPServer, str]]:
    monkeypatch.setattr(
        bridge,
        "codex_status",
        lambda: {
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
        },
    )
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}"
        try:
            yield server, base
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _request(url: str, *, token: str | None = None, origin: str | None = None) -> Request:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return Request(url, headers=headers)


def _state(vault: Vault, *, port: int = 43117, pid: int = 4242) -> dict[str, object]:
    return {
        "format_version": bridge.STATE_VERSION,
        "instance_id": INSTANCE_ID,
        "pid": pid,
        "port": port,
        "token": ACCESS_TOKEN,
        "url": f"http://{bridge.LOOPBACK_HOST}:{port}/",
        "vault": str(vault.root),
        "vault_id": vault.identity()["vault_id"],
    }


def _health_response(payload: dict[str, object]) -> bridge._HealthProbe:
    return bridge._HealthProbe(bridge._HealthOutcome.RESPONSE, payload)


def _health_unavailable() -> bridge._HealthProbe:
    return bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_snapshot_projects_authored_records_and_codex_links(vault: Vault) -> None:
    task = vault.create_task(
        identifier="ship-atlas",
        title="Ship Atlas",
        outcome="The migration is complete.",
        status="doing",
        next_actor="agent",
        next_action="Run the acceptance suite.",
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
        },
    )

    projected = next(item for item in snapshot["tasks"] if item["identifier"] == task.identifier)
    link = urlsplit(projected["codex_url"])
    query = parse_qs(link.query)
    assert link.scheme == "codex"
    assert link.netloc == "new"
    assert query["originUrl"] == [bridge.REPOSITORY_URL]
    assert query["path"] == [str(vault.root)]
    assert "ship-atlas" in query["prompt"][0]
    assert snapshot["codex"]["ready"] is True
    assert snapshot["codex"]["new_mind_url"].startswith("codex://new?")
    assert snapshot["bridge"] == {"local": True, "read_only": True, "version": __version__}


@pytest.mark.parametrize(
    "integration",
    [
        {"available": False, "instructions_installed": False, "plugin_installed": False},
        {"available": True, "instructions_installed": False, "plugin_installed": True},
        {"available": True, "instructions_installed": True, "plugin_installed": False},
    ],
)
def test_snapshot_withholds_every_codex_url_until_integration_is_ready(
    vault: Vault, integration: dict[str, bool]
) -> None:
    vault.create_task(
        identifier="not-actionable",
        title="Not actionable",
        outcome="No deep link is exposed before setup is complete.",
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration=integration,
    )

    assert snapshot["codex"]["ready"] is False
    assert "new_mind_url" not in snapshot["codex"]
    assert all("codex_url" not in task for task in snapshot["tasks"])


def test_snapshot_never_calls_full_status_or_logical_digest(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Bridge snapshots must not hash the full vault")

    monkeypatch.setattr(vault, "status", forbidden)
    monkeypatch.setattr(vault, "logical_digest", forbidden)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "plugin_installed": True},
    )

    assert snapshot["status"]["vault_id"] == vault.identity()["vault_id"]
    assert snapshot["status"]["counts"] == {"tasks": 0, "entities": 0, "threads": 0}
    assert "digest" not in snapshot["status"]


def test_codex_deep_link_round_trips_encoded_prompt_path_and_origin(tmp_path: Path) -> None:
    vault = tmp_path / "GSV vault & proof"
    prompt = "Resume Atlas A&B, then check path / evidence."

    link = urlsplit(bridge.codex_deep_link(vault, prompt))
    query = parse_qs(link.query)

    assert link.scheme == "codex"
    assert link.netloc == "new"
    assert query == {
        "originUrl": [bridge.REPOSITORY_URL],
        "path": [str(vault.resolve())],
        "prompt": [prompt],
    }


def test_packaged_bridge_bundle_contains_ui_assets_and_licenses() -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    expected = {
        "bridge.css",
        "bridge.js",
        "gsv-mark.svg",
        "index.html",
        "licenses/Lucide-ISC.txt",
        "licenses/Thinking-Orbs-MIT.txt",
    }

    with as_file(resource) as root:
        present = {str(path.relative_to(root)) for path in Path(root).rglob("*") if path.is_file()}

    assert expected <= present


def test_bridge_ui_tokens_and_dependency_free_orb_contract() -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as root:
        css = (Path(root) / "bridge.css").read_text(encoding="utf-8")
        html = (Path(root) / "index.html").read_text(encoding="utf-8")
        javascript = (Path(root) / "bridge.js").read_text(encoding="utf-8")

    for token in (
        "--text-xs: 12px",
        "--text-sm: 13px",
        "--text-md: 14px",
        "--text-title: 24px",
        "--ink: #292929",
        "--muted: #5d5d5d",
        "--quiet: #9e9e9e",
        "--radius-nav: 8px",
        "--radius-card: 16px",
        "--radius-pill: 999px",
    ):
        assert token in css
    assert '"SF Pro Text"' in css
    assert '"SF Pro Display"' in css
    assert "@font-face" not in css
    assert "letter-spacing: -0.15px" in css
    assert "letter-spacing: 0" not in css
    assert {match.group(1) for match in re.finditer(r"font-weight:\s*(\d+)", css)} == {
        "400",
        "500",
    }
    assert re.search(r"\.nav-icon,[^{]+\{[^}]*width:\s*14px", css, re.DOTALL)
    assert re.search(r"\.thinking-orb\s*\{[^}]*width:\s*20px[^}]*height:\s*20px", css, re.DOTALL)
    assert "thinking-orbs 0.1.1" in javascript
    assert "382be79c472cd600277f01e14f98f8c0ee18dcb0" in javascript
    assert "prefers-reduced-motion: reduce" in javascript
    assert "IntersectionObserver" in javascript
    assert "Math.min(2, window.devicePixelRatio || 1)" in javascript
    assert "snapshotSignature" in javascript
    assert 'from "react"' not in javascript
    assert 'aria-live="polite"' not in html
    assert ".woff2" not in bridge._MIME_TYPES


def test_http_surface_requires_per_launch_bearer_for_private_data(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    with urlopen(f"{base}/", timeout=2) as response:
        assert response.status == HTTPStatus.OK
        assert b"The agent is not the thread" in response.read()

    with pytest.raises(HTTPError) as missing:
        urlopen(_request(f"{base}/api/v1/snapshot"), timeout=2)
    assert missing.value.code == HTTPStatus.FORBIDDEN

    with pytest.raises(HTTPError) as wrong:
        urlopen(_request(f"{base}/api/v1/snapshot", token="c" * 48), timeout=2)
    assert wrong.value.code == HTTPStatus.FORBIDDEN

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        payload = json.loads(response.read())
    assert payload["status"]["vault_id"] == server.vault.identity()["vault_id"]

    with urlopen(_request(f"{base}/api/v1/health", token=ACCESS_TOKEN), timeout=2) as response:
        health = json.loads(response.read())
    assert health["instance_id"] == INSTANCE_ID
    assert health["port"] == server.server_address[1]


def test_health_endpoint_uses_only_cached_manifest_identity(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, base = running_bridge

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("health must not scan or validate vault content")

    for name in (
        "identity",
        "status",
        "list_tasks",
        "list_entities",
        "list_threads",
        "doctor",
        "logical_digest",
    ):
        monkeypatch.setattr(server.vault, name, forbidden)

    with urlopen(_request(f"{base}/api/v1/health", token=ACCESS_TOKEN), timeout=2) as response:
        health = json.loads(response.read())

    assert health["vault_id"] == server.vault_id
    assert health["instance_id"] == INSTANCE_ID


def test_snapshot_endpoint_never_calls_full_status_or_logical_digest(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, base = running_bridge

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("snapshot endpoint must not hash the full vault")

    monkeypatch.setattr(server.vault, "status", forbidden)
    monkeypatch.setattr(server.vault, "logical_digest", forbidden)

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        snapshot = json.loads(response.read())

    assert snapshot["status"]["vault_id"] == server.vault_id
    assert "digest" not in snapshot["status"]


def test_snapshot_does_not_wait_for_slow_codex_status_and_refreshes_once(
    vault: Vault,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_integration() -> dict[str, object]:
        calls.append("started")
        started.set()
        assert release.wait(timeout=3)
        return {
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
        }

    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            integration_provider=slow_integration,
        )
        try:
            before = time.monotonic()
            first = server.snapshot()
            elapsed = time.monotonic() - before
            second = server.snapshot()

            assert elapsed < 0.5
            assert started.wait(timeout=1)
            assert first["codex"]["checking"] is True
            assert second["codex"]["checking"] is True
            assert calls == ["started"]

            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                refreshed = server.snapshot()
                if refreshed["codex"].get("available") is True:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Codex metadata did not finish its background refresh")

            assert refreshed["codex"]["checking"] is False
            assert refreshed["codex"]["plugin_installed"] is True
            assert calls == ["started"]
        finally:
            release.set()
            server.server_close()


def test_http_surface_rejects_cross_origin_and_writes(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    _, base = running_bridge
    with pytest.raises(HTTPError) as cross_origin:
        urlopen(
            _request(
                f"{base}/api/v1/snapshot",
                token=ACCESS_TOKEN,
                origin="http://127.0.0.1:9",
            ),
            timeout=2,
        )
    assert cross_origin.value.code == HTTPStatus.FORBIDDEN

    request = Request(
        f"{base}/api/v1/snapshot",
        data=b"{}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        method="POST",
    )
    with pytest.raises(HTTPError) as write:
        urlopen(request, timeout=2)
    assert write.value.code == HTTPStatus.METHOD_NOT_ALLOWED


def test_stop_never_signals_reused_pid_when_health_identity_mismatches(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": "c" * 32,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result["stopped"] is False
    assert result["stale_reason"] == "identity_mismatch"
    assert result["stale_receipt_removed"] is True
    assert not bridge._state_path().exists()


def test_stop_signals_only_a_fully_verified_bridge(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault)
    bridge._write_state(state)
    alive = {4242: True}
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": INSTANCE_ID,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )

    def terminate(pid: int) -> None:
        signaled.append(pid)
        alive[pid] = False

    monkeypatch.setattr(bridge, "_terminate_pid", terminate)

    result = bridge.stop_bridge()

    assert signaled == [4242]
    assert result["stopped"] is True
    assert not bridge._state_path().exists()


def test_stop_failure_preserves_verified_receipt(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    clock = iter((0.0, 4.0))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr("continuity_kernel.bridge.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": INSTANCE_ID,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    with pytest.raises(SetupError, match="did not stop"):
        bridge.stop_bridge()

    assert signaled == [4242]
    assert bridge._state_path().is_file()


def test_forged_or_invalid_receipt_is_removed_without_signaling(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault)
    state["url"] = "http://127.0.0.1:9/"
    bridge._write_state(state)
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result["stale_receipt_removed"] is True
    assert not bridge._state_path().exists()


def test_default_ephemeral_port_avoids_an_occupied_port(vault: Vault) -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    with socket.socket() as occupied, as_file(resource) as static_root:
        occupied.bind((bridge.LOOPBACK_HOST, 0))
        occupied_port = occupied.getsockname()[1]
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, bridge.DEFAULT_PORT),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
        )
        try:
            assert server.server_address[1] != occupied_port
            assert server.server_address[1] > 0
        finally:
            server.server_close()


def test_state_receipt_is_private_and_open_token_stays_in_fragment(vault: Vault) -> None:
    state = _state(vault)
    bridge._write_state(state)

    if os.name != "nt":
        assert bridge._state_path().stat().st_mode & 0o777 == 0o600
    open_url = bridge._open_url(state)
    before_fragment, fragment = open_url.split("#", 1)
    assert ACCESS_TOKEN not in before_fragment
    assert fragment == f"token={ACCESS_TOKEN}"
    assert "token" not in bridge.bridge_status()


def test_frozen_bridge_child_requests_an_independent_pyinstaller_runtime(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("continuity_kernel.bridge.sys.frozen", True, raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/synthetic/parent-extraction")

    environment = bridge._bridge_child_environment(vault)

    assert environment["GSV_VAULT"] == str(vault.root)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_frozen_start_accepts_worker_pid_after_exact_launch_identity(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault, pid=5252)
    monkeypatch.setattr(bridge, "_load_state", lambda: state)

    observed = bridge._wait_for_state(
        INSTANCE_ID,
        4242,
        expected_pid=None,
        vault_id=str(vault.identity()["vault_id"]),
        timeout=0.1,
    )

    assert observed == state


def test_source_start_requires_receipt_to_match_launcher_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault, pid=5252)
    monkeypatch.setattr(bridge, "_load_state", lambda: state)
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: False)

    observed = bridge._wait_for_state(
        INSTANCE_ID,
        4242,
        expected_pid=4242,
        vault_id=str(vault.identity()["vault_id"]),
        timeout=0.1,
    )

    assert observed is None


def test_windows_liveness_branch_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[int] = []
    monkeypatch.setattr(bridge, "_IS_WINDOWS", True)

    def windows_probe(pid: int) -> bool:
        observed.append(pid)
        return True

    monkeypatch.setattr(bridge, "_windows_pid_alive", windows_probe)

    def forbidden_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("the Windows liveness branch must not call os.kill")

    monkeypatch.setattr("continuity_kernel.bridge.os.kill", forbidden_kill)

    assert bridge._pid_alive(4242) is True
    assert observed == [4242]


def test_status_preserves_receipt_when_live_health_is_temporarily_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())

    result = bridge.bridge_status()

    assert result["running"] is True
    assert result["identity_verified"] is False
    assert result["health_unavailable"] is True
    assert bridge._state_path().is_file()


def test_open_preserves_receipt_and_starts_nothing_when_live_health_is_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())

    def forbidden_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a replacement Bridge must not be started")

    monkeypatch.setattr("continuity_kernel.bridge.subprocess.Popen", forbidden_popen)

    with pytest.raises(SetupError, match="receipt was preserved"):
        bridge.open_bridge(vault, open_browser=False)

    assert bridge._state_path().is_file()


def test_stop_preserves_receipt_and_signals_nothing_when_live_health_is_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    with pytest.raises(SetupError, match="No process was signalled"):
        bridge.stop_bridge()

    assert signaled == []
    assert bridge._state_path().is_file()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("injected timeout"),
        ConnectionResetError(errno.ECONNRESET, "injected reset"),
        OSError(errno.EHOSTUNREACH, "injected unreachable host"),
    ],
)
def test_health_probe_treats_non_refusal_network_errors_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(bridge, "urlopen", fail_request)

    probe = bridge._probe_health("http://127.0.0.1:43117/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_health_probe_treats_malformed_response_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedResponse:
        status = HTTPStatus.OK

        def __enter__(self) -> MalformedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(bridge, "urlopen", lambda *_args, **_kwargs: MalformedResponse())

    probe = bridge._probe_health("http://127.0.0.1:43117/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_health_probe_never_classifies_remote_refusal_as_local_stale_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = URLError(ConnectionRefusedError(errno.ECONNREFUSED, "injected refusal"))

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise refused

    monkeypatch.setattr(bridge, "urlopen", fail_request)

    probe = bridge._probe_health("http://example.invalid:9/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_stop_cleans_refused_receipt_without_signalling_unverified_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled: list[int] = []
    with socket.socket() as released:
        released.bind((bridge.LOOPBACK_HOST, 0))
        port = int(released.getsockname()[1])
    bridge._write_state(_state(vault, port=port, pid=os.getpid()))
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result == {
        "running": False,
        "stale_reason": "connection_refused",
        "stale_receipt_removed": True,
        "stopped": False,
    }
    assert not bridge._state_path().exists()


def test_open_replaces_refused_receipt_without_signalling_unverified_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled: list[int] = []
    with socket.socket() as released:
        released.bind((bridge.LOOPBACK_HOST, 0))
        port = int(released.getsockname()[1])
    bridge._write_state(_state(vault, port=port, pid=os.getpid()))
    with monkeypatch.context() as guarded:
        guarded.setattr(bridge, "_terminate_pid", signaled.append)
        opened = bridge.open_bridge(vault, open_browser=False)

    try:
        replacement = bridge._load_state()
        assert opened["started"] is True
        assert signaled == []
        assert replacement is not None
        assert replacement["pid"] != os.getpid()
        assert replacement["instance_id"] != INSTANCE_ID
    finally:
        stopped = bridge.stop_bridge()
    assert stopped["stopped"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_bridge_runtime_directory_and_log_are_private_and_reject_symlinks(
    tmp_path: Path,
) -> None:
    root = data_dir()
    root.mkdir(parents=True)
    root.chmod(0o755)

    secured = bridge._private_runtime_dir()
    log_path = secured / "bridge.log"
    with bridge._open_private_log(log_path) as log:
        log.write(b"synthetic diagnostic\n")

    assert secured.stat().st_mode & 0o777 == 0o700
    assert log_path.stat().st_mode & 0o777 == 0o600

    log_path.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text("must remain untouched", encoding="utf-8")
    log_path.symlink_to(outside)
    with pytest.raises(SetupError, match="unsafe Bridge log"):
        bridge._open_private_log(log_path)
    assert outside.read_text(encoding="utf-8") == "must remain untouched"


def test_real_detached_bridge_child_binds_reports_and_stops(vault: Vault) -> None:
    opened = bridge.open_bridge(vault, open_browser=False)
    try:
        status = bridge.bridge_status()
        state = bridge._load_state()
        assert opened["started"] is True
        assert opened["browser_opened"] is False
        assert status["running"] is True
        assert status["identity_verified"] is True
        assert status["port"] > 0
        assert state is not None
        health = bridge._health_payload(str(state["url"]), token=str(state["token"]), timeout=2)
        assert bridge._state_matches_health(state, health)
    finally:
        stopped = bridge.stop_bridge()
    assert stopped["stopped"] is True
    assert not bridge._state_path().exists()

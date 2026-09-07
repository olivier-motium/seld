from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel.app_corpus_notion import (
    CONFIG_SCHEMA,
    RESULT_SCHEMA,
    NotionAppCorpusAdapter,
    notion_app_corpus_adapters,
)
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.errors import ValidationError

_ACCOUNT = "sha256:" + "a" * 64
_WORKSPACE = "sha256:" + "b" * 64
_OTHER_ACCOUNT = "sha256:" + "c" * 64
_OTHER_WORKSPACE = "sha256:" + "d" * 64
_OBJECT = "notion:sha256:" + "e" * 64


@dataclass
class _Bridge:
    response: dict[str, Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, command: list[str] | tuple[str, ...], payload: bytes, timeout_seconds: float
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout_seconds
        self.requests.append(json.loads(payload.decode("utf-8")))
        return subprocess.CompletedProcess(
            list(command), 0, stdout=json.dumps(self.response).encode("utf-8")
        )


def _config(tmp_path: Path) -> Path:
    host = tmp_path / "host"
    host.mkdir(parents=True)
    host.chmod(0o700)
    bridge = tmp_path / "notion-bridge"
    bridge.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bridge.chmod(0o700)
    config = host / "notion-corpus.json"
    config.write_text(
        json.dumps(
            {
                "schema": CONFIG_SCHEMA,
                "backend": {"command": [str(bridge), "sync"]},
                "routes": [
                    {
                        "connection": "notion-alpha",
                        "pin": {
                            "account_label": "Account Alpha",
                            "account_fingerprint": _ACCOUNT,
                            "workspace_label": "Workspace Alpha",
                            "workspace_fingerprint": _WORKSPACE,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)
    return config


def _response(
    *,
    account: str = _ACCOUNT,
    workspace: str = _WORKSPACE,
    complete: bool = False,
    discovery_coverage: str = "live_workspace_root_pages",
    deleted: bool = False,
    gaps: dict[str, int] | None = None,
    recursive: bool = True,
    pagination_complete: bool = True,
    source_event_at: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "account_label": "Account Alpha",
        "workspace_label": "Workspace Alpha",
    }
    if source_event_at is not None:
        metadata["updated_at"] = source_event_at
    return {
        "schema": RESULT_SCHEMA,
        "pin": {
            "account_fingerprint": account,
            "workspace_fingerprint": workspace,
        },
        "documents": [
            {
                "object_id": _OBJECT,
                "revision": "revision-1",
                "fetched_at": "2026-09-07T08:00:00Z",
                "source_ref": _OBJECT,
                "title": "" if deleted else "A fetched page",
                "text": "" if deleted else "Full recursive page body.",
                "metadata": metadata,
                **({"source_event_at": source_event_at} if source_event_at is not None else {}),
                **({"deleted": True} if deleted else {}),
            }
        ],
        "checkpoint": "checkpoint-2",
        "scanned": 1 + sum((gaps or {}).values()),
        "complete": complete,
        "freshness": {
            "cache_coverage": "desktop-cache-subset",
            "discovery_coverage": discovery_coverage,
            "live_page_coverage": "page_bodies",
            "gaps": gaps or {},
            "block_traversal": {
                "recursive": recursive,
                "pagination_complete": pagination_complete,
            },
        },
    }


def _adapter(tmp_path: Path, bridge: _Bridge) -> NotionAppCorpusAdapter:
    return NotionAppCorpusAdapter(
        cast(ConnectorRuntime, object()),
        config_path=_config(tmp_path),
        runner=bridge,
    )


def test_sync_sends_one_explicit_pin_and_returns_live_page_body(tmp_path: Path) -> None:
    bridge = _Bridge(_response())
    adapter = _adapter(tmp_path, bridge)

    result = adapter.sync("notion-alpha", limit=10)

    assert adapter.configured_connections() == ("notion-alpha",)
    assert result.complete is False
    assert result.checkpoint == "checkpoint-2"
    assert result.documents[0].text == "Full recursive page body."
    assert result.documents[0].object_id == _OBJECT
    assert result.freshness == {
        "block_traversal": {"pagination_complete": True, "recursive": True},
        "cache_coverage": "desktop-cache-subset",
        "discovery_coverage": "live_workspace_root_pages",
        "gaps": {},
        "live_page_coverage": "page_bodies",
        "status": "partial",
    }
    assert bridge.requests == [
        {
            "checkpoint": None,
            "connection_id": "notion-alpha",
            "limit": 10,
            "operation": "sync_page_bodies",
            "pin": {
                "account_fingerprint": _ACCOUNT,
                "account_label": "Account Alpha",
                "workspace_fingerprint": _WORKSPACE,
                "workspace_label": "Workspace Alpha",
            },
            "schema": "seld.notion-desktop-session.request.v1",
        }
    ]


def test_complete_scan_with_safe_skips_stays_coverage_partial(tmp_path: Path) -> None:
    bridge = _Bridge(_response(complete=True, gaps={"page_access_denied": 1}))

    result = _adapter(tmp_path, bridge).sync("notion-alpha")

    assert result.complete is True
    assert result.freshness["status"] == "partial"
    assert result.freshness["gaps"] == {"page_access_denied": 1}


def test_sync_retains_reachable_page_discovery_evidence(tmp_path: Path) -> None:
    bridge = _Bridge(_response(discovery_coverage="live_workspace_reachable_pages"))

    result = _adapter(tmp_path, bridge).sync("notion-alpha")

    assert result.freshness["discovery_coverage"] == "live_workspace_reachable_pages"


def test_sync_accepts_an_authoritative_page_tombstone(tmp_path: Path) -> None:
    result = _adapter(tmp_path, _Bridge(_response(deleted=True))).sync("notion-alpha")

    assert result.documents[0].deleted is True
    assert result.documents[0].title == ""


def test_sync_preserves_notion_source_edit_time_without_using_fetch_time(tmp_path: Path) -> None:
    event_at = "2026-09-07T12:34:56.000Z"

    result = _adapter(tmp_path, _Bridge(_response(source_event_at=event_at))).sync("notion-alpha")

    assert result.documents[0].source_event_at == event_at
    assert result.documents[0].metadata["updated_at"] == event_at


def test_launchd_sized_request_is_clamped_to_the_notion_page_bound(tmp_path: Path) -> None:
    bridge = _Bridge(_response())

    _adapter(tmp_path, bridge).sync("notion-alpha", limit=1_000)

    assert bridge.requests[0]["limit"] == 100
    with pytest.raises(ValidationError, match="between 1 and 1000"):
        _adapter(tmp_path / "invalid", _Bridge(_response())).sync("notion-alpha", limit=1_001)


def test_pin_mismatch_and_nonrecursive_block_result_are_refused(tmp_path: Path) -> None:
    mismatch = _adapter(tmp_path / "mismatch", _Bridge(_response(account=_OTHER_ACCOUNT)))
    traversal = _adapter(tmp_path / "traversal", _Bridge(_response(recursive=False)))

    wrong_pin = mismatch.sync("notion-alpha", checkpoint="resume-1")
    incomplete_body = traversal.sync("notion-alpha", checkpoint="resume-1")

    assert wrong_pin.documents == ()
    assert wrong_pin.checkpoint == "resume-1"
    assert wrong_pin.freshness["status"] == "refused"
    assert incomplete_body.documents == ()
    assert incomplete_body.freshness["status"] == "refused"


def test_config_requires_owner_only_file_and_directory_modes(tmp_path: Path) -> None:
    bridge = _Bridge(_response())
    config = _config(tmp_path)
    adapter = NotionAppCorpusAdapter(
        cast(ConnectorRuntime, object()), config_path=config, runner=bridge
    )

    config.chmod(0o644)
    with pytest.raises(ValidationError, match="mode 0600"):
        adapter.configured_connections()

    config.chmod(0o600)
    config.parent.chmod(0o755)
    with pytest.raises(ValidationError, match="mode 0700"):
        adapter.configured_connections()
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o600


def test_factory_has_no_unpinned_default_and_only_notion_aliases(tmp_path: Path) -> None:
    config = _config(tmp_path)

    adapters = notion_app_corpus_adapters(cast(ConnectorRuntime, object()), config_path=config)

    assert set(adapters) == {"notion", "notion_desktop_session"}
    assert adapters["notion"].configured_connections() == ("notion-alpha",)

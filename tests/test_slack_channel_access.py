from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuity_kernel import slack_channel_access
from continuity_kernel.app_corpus import AppCorpusCompanion, AppCorpusDocument, AppCorpusSyncResult
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


def _configure_policy(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    connection_id: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    "T1": {
                        "channels": {"CABC123": "general"},
                        "connection_ids": [connection_id],
                        "pending_channel_names": ["proj-medi-market"],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(slack_channel_access, "_config_path", lambda: path)


def test_policy_keeps_unapproved_cached_slack_documents_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection_id = "con-restricted"
    _configure_policy(
        monkeypatch,
        tmp_path / "slack-channel-access.json",
        connection_id=connection_id,
    )
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        def sync(
            self, current_connection_id: str, *, checkpoint: str | None = None, limit: int = 100
        ) -> AppCorpusSyncResult:
            assert current_connection_id == connection_id
            assert checkpoint is None
            assert limit == 100
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="slack",
                        object_id="allowed",
                        revision="1",
                        fetched_at="2026-09-08T00:00:00Z",
                        source_ref="slack:message:CABC123:1",
                        title="Allowed",
                        text="allowed marker",
                        metadata={"channel_id": "CABC123"},
                        freshness={"status": "complete"},
                    ),
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="slack",
                        object_id="denied",
                        revision="1",
                        fetched_at="2026-09-08T00:00:00Z",
                        source_ref="slack:message:Cdenied:1",
                        title="Denied",
                        text="private marker",
                        metadata={"channel_id": "Cdenied"},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint="complete",
                scanned=2,
                complete=True,
                freshness={"status": "complete"},
            )

    companion.sync(Adapter(), connection_id)

    assert companion.status().document_count == 1
    assert companion.read(connection_id, "allowed") is not None
    assert companion.read(connection_id, "denied") is None
    assert companion.search("allowed marker").hits[0].object_id == "allowed"
    assert companion.search("private marker").hits == ()
    # The policy changes access, not stored history.  The old content leaves
    # remain until normal corpus retention changes them.
    assert len(tuple((companion.documents_root).glob("*.md"))) == 2


@pytest.mark.parametrize(
    ("channel_id", "channel_name", "pending_name"),
    (
        ("DABC123", "general", "project"),
        ("Cabc123", "general", "project"),
        ("CABC123", "Not a channel", "project"),
        ("CABC123", "general", "Not a channel"),
    ),
)
def test_policy_rejects_non_channel_ids_and_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel_id: str,
    channel_name: str,
    pending_name: str,
) -> None:
    path = tmp_path / "slack-channel-access.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    "T1": {
                        "channels": {channel_id: channel_name},
                        "connection_ids": ["con-restricted"],
                        "pending_channel_names": [pending_name],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(slack_channel_access, "_config_path", lambda: path)

    with pytest.raises(ValidationError, match="configuration is invalid"):
        slack_channel_access.load_slack_channel_access("con-restricted")

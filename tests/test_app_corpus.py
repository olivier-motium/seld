from __future__ import annotations

import argparse
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from continuity_kernel import app_corpus, cli
from continuity_kernel import recall as recall_module
from continuity_kernel.app_corpus import (
    AppCorpusCompanion,
    AppCorpusDocument,
    AppCorpusSyncResult,
)
from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


@dataclass
class _PagedAdapter:
    calls: list[str | None]

    def sync(
        self, connection_id: str, *, checkpoint: str | None = None, limit: int = 100
    ) -> AppCorpusSyncResult:
        self.calls.append(checkpoint)
        document = AppCorpusDocument(
            connection_id=connection_id,
            provider="gmail",
            object_id="message-1",
            revision="r1",
            fetched_at="2026-09-07T08:00:00Z",
            source_ref="gmail:message-1",
            title="Project decision",
            text="The launch decision is ready for review.",
            metadata={"thread_id": "thread-1"},
            freshness={"status": "complete"},
        )
        if checkpoint is None:
            return AppCorpusSyncResult(
                documents=(document,),
                checkpoint="page-2",
                scanned=1,
                complete=False,
                freshness={"status": "partial", "detail": "page limit"},
            )
        return AppCorpusSyncResult(
            documents=(document,),
            checkpoint="watermark-1",
            scanned=1,
            complete=True,
            freshness={"status": "complete"},
        )


def test_partial_page_resumes_without_claiming_current(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    adapter = _PagedAdapter([])

    first = companion.sync(adapter, "con_abc", limit=10)
    first_status = companion.status()
    second = companion.sync(adapter, "con_abc", limit=10)
    document = companion.read("con_abc", "message-1")
    search = companion.search("launch decision")

    assert first.complete is False
    assert first.freshness["sync_kind"] == "backfill"
    assert first_status.complete is False
    assert second.complete is True
    assert adapter.calls == [None, "page-2"]
    assert document is not None
    assert document.metadata == {"thread_id": "thread-1"}
    assert document.text == "The launch decision is ready for review."
    assert search.hits[0].object_id == "message-1"


def test_two_mebibyte_provider_checkpoint_is_preserved_without_truncation(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    checkpoint = "x" * app_corpus.MAX_CHECKPOINT_BYTES

    class Adapter:
        def __init__(self, token: str) -> None:
            self.calls: list[str | None] = []
            self.token = token

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            self.calls.append(checkpoint)
            return AppCorpusSyncResult(
                documents=(),
                checkpoint=checkpoint or self.token,
                scanned=0,
                complete=True,
                freshness={"status": "complete"},
            )

    adapter = Adapter(checkpoint)
    companion.sync(adapter, "con_abc")
    companion.sync(adapter, "con_abc")

    assert adapter.calls == [None, checkpoint]
    with pytest.raises(ValidationError, match="invalid checkpoint"):
        app_corpus._validate_sync_result(
            AppCorpusSyncResult(
                documents=(),
                checkpoint=checkpoint + "x",
                scanned=0,
                complete=True,
                freshness={"status": "complete"},
            ),
            "con_abc",
        )


def test_sync_all_only_uses_explicit_scope(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    adapter = _PagedAdapter([])
    companion.configure("con_abc", adapter="gmail")

    results = companion.sync_all({"gmail": adapter}, limit=10)

    assert len(results) == 2
    assert results[0]["connection_id"] == "con_abc"
    assert adapter.calls == [None]


def test_read_only_status_and_scopes_do_not_wait_for_the_corpus_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    companion.configure("con_abc", adapter="gmail")

    def writer_lock_is_unavailable(*_args, **_kwargs):
        raise AssertionError("read-only corpus snapshot took the writer lock")

    monkeypatch.setattr(PinnedPathRoot, "exclusive_file_lock", writer_lock_is_unavailable)

    assert companion.scopes()[0].connection_id == "con_abc"
    assert companion.status().document_count == 0


def test_apps_sync_passes_whatsapp_existing_row_recheck_to_its_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    monkeypatch.setattr(app_corpus, "data_dir", lambda: tmp_path / "data")
    adapter = _PagedAdapter([])
    received: list[bool] = []

    class Runtime:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def close(self) -> None:
            pass

    def adapters(_runtime, _corpus, *, recheck_existing: bool):
        received.append(recheck_existing)
        return {"whatsapp": adapter}

    monkeypatch.setattr(cli, "ConnectorRuntime", Runtime)
    monkeypatch.setattr(cli, "default_connector_adapters", lambda: {})
    monkeypatch.setattr(cli, "_app_corpus_adapters", adapters)
    args = cli._parser().parse_args(
        [
            "apps",
            "sync",
            "--connection-id",
            "whatsapp-local",
            "--adapter",
            "whatsapp",
            "--recheck-existing",
        ]
    )

    result = cli._apps(vault, args)

    assert received == [True]
    assert result["document_count"] == 1


def test_tombstone_removes_the_pinned_content_addressed_document(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        deleted = False

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="message-1",
                        revision="deleted" if self.deleted else "r1",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:message-1",
                        title="Project decision",
                        text="",
                        metadata={},
                        freshness={"status": "complete"},
                        deleted=self.deleted,
                    ),
                ),
                checkpoint="watermark-1",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    adapter = Adapter()
    companion.sync(adapter, "con_abc")
    adapter.deleted = True

    result = companion.sync(adapter, "con_abc")

    assert result.document_count == 0
    assert companion.read("con_abc", "message-1") is None
    companion.refresh()
    assert tuple(companion.documents_root.glob("*.md")) == ()


def test_configured_whatsapp_capabilities_are_visible_but_apps_call_rejects_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    monkeypatch.setattr(app_corpus, "data_dir", lambda: tmp_path / "data")
    companion = AppCorpusCompanion(vault.root)

    assert not any(
        operation["name"].startswith("whatsapp.")
        for operation in cli._app_capabilities(vault)["operations"]
    )

    companion.configure(
        "whatsapp-local",
        adapter="whatsapp",
        settings={"store_root": str(tmp_path / "local-wacli")},
    )
    operations = cli._app_capabilities(vault)["operations"]
    whatsapp_operations = [
        operation for operation in operations if operation["name"].startswith("whatsapp.")
    ]

    assert {operation["name"] for operation in whatsapp_operations} == {
        "whatsapp.messages.context",
        "whatsapp.messages.delete",
        "whatsapp.messages.edit_sent_text",
        "whatsapp.messages.export",
        "whatsapp.messages.list",
        "whatsapp.messages.purge_tombstone",
        "whatsapp.messages.revoke",
        "whatsapp.messages.search",
        "whatsapp.messages.send_text",
        "whatsapp.messages.show",
    }
    assert all(operation["connection_id"] == "whatsapp-local" for operation in whatsapp_operations)
    assert all(operation["apps_call_supported"] is False for operation in whatsapp_operations)
    with pytest.raises(
        ValidationError, match="app calls must use one listed closed connector tool"
    ):
        cli._apps(
            vault,
            argparse.Namespace(
                apps_command="call",
                confirmation_token=None,
                connection_id="whatsapp-local",
                cursor=None,
                executable="qmd",
                input="{}",
                operation="whatsapp.messages.list",
                tool="whatsapp.messages.list",
            ),
        )


def test_apps_call_executes_a_confirmed_write_inside_one_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    monkeypatch.setattr(app_corpus, "data_dir", lambda: tmp_path / "data")

    class Runtime:
        instances: ClassVar[list[Runtime]] = []

        def __init__(self, _vault, *, adapters, require_confirmation_for_safe_mutations):
            assert adapters == {"adapter": "registry"}
            assert require_confirmation_for_safe_mutations is True
            self.calls: list[dict[str, object]] = []
            self.closed = False
            self.instances.append(self)

        def call_tool(self, name, values):
            assert name == "gsv_gmail_write"
            current = dict(values)
            self.calls.append(current)
            if "confirmation_token" not in current:
                return {
                    "confirmation_token": "one-process-token",
                    "effect": "outward",
                    "operation": "messages.send",
                    "preview": {"to": ["person@example.test"]},
                    "provider": "gmail",
                    "status": "confirmation_required",
                }
            assert current["confirmation_token"] == "one-process-token"
            return {
                "effect": "outward",
                "operation": "messages.send",
                "provider": "gmail",
                "result": {"id": "sent-message"},
                "status": "ok",
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(cli, "ConnectorRuntime", Runtime)
    monkeypatch.setattr(cli, "default_connector_adapters", lambda: {"adapter": "registry"})

    def call_args(*, execute_confirmed: bool) -> argparse.Namespace:
        return argparse.Namespace(
            apps_command="call",
            confirmation_token=None,
            connection_id="con_abc",
            cursor=None,
            executable="qmd",
            execute_confirmed=execute_confirmed,
            input='{"to":["person@example.test"]}',
            operation="messages.send",
            tool="gsv_gmail_write",
        )

    preview = cli._apps(vault, call_args(execute_confirmed=False))
    executed = cli._apps(vault, call_args(execute_confirmed=True))

    assert preview["status"] == "confirmation_required"
    assert "confirmation_token" not in preview
    assert "--execute-confirmed" in preview["next_action"]
    assert executed["status"] == "executed"
    assert "confirmation_token" not in executed["preview"]
    assert executed["execution"]["result"] == {"id": "sent-message"}
    assert len(Runtime.instances) == 2
    assert len(Runtime.instances[1].calls) == 2
    assert Runtime.instances[1].closed is True


def test_current_qmd_search_maps_hits_back_to_exact_source(tmp_path: Path, monkeypatch) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root,
        executable=executable,
        index_root=tmp_path / "corpus",
    )
    adapter = _PagedAdapter([])

    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda *_args, **_kwargs: recall_module._CommandResult(0, b"", b""),
    )
    companion.sync(adapter, "con_abc", refresh=True)
    # The object path uses a connection/object hash. Read it from local state through the
    # companion result rather than treating a provider ID as a filesystem name.
    key = hashlib.sha256(b"con_abc\0message-1").hexdigest()
    digest = hashlib.sha256(
        b"# Project decision\n\nThe launch decision is ready for review."
    ).hexdigest()[:16]
    state = json.loads(companion.state_path.read_text(encoding="utf-8"))
    collection = state["qmd_bindings"]["con_abc"]["collection"]
    documents_root = state["qmd_bindings"]["con_abc"]["documents_root"]

    def query(command, **_kwargs):
        if command[3:5] == ("collection", "show"):
            return recall_module._CommandResult(
                0,
                (
                    f"Collection: {collection}\n"
                    f"  Path:     {documents_root}\n"
                    "  Pattern:  **/*.md\n"
                ).encode(),
                b"",
            )
        if "query" in command:
            return recall_module._CommandResult(
                0,
                (
                    '{"results":[{"file":"qmd://'
                    + collection
                    + "/"
                    + key
                    + "-"
                    + digest
                    + '.md","score":0.75}]}'
                ).encode(),
                b"",
            )
        return recall_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(
        recall_module,
        "_run_command",
        query,
    )

    result = companion.search("launch decision", connection_id="con_abc")

    assert result.backend == "qmd"
    assert result.hits[0].source_ref == "gmail:message-1"


def test_rebound_scoped_qmd_collection_uses_local_lexical_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root,
        executable=executable,
        index_root=tmp_path / "corpus",
    )
    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda *_args, **_kwargs: recall_module._CommandResult(0, b"", b""),
    )
    companion.sync(_PagedAdapter([]), "con_abc", refresh=True)
    state = json.loads(companion.state_path.read_text(encoding="utf-8"))
    collection = state["qmd_bindings"]["con_abc"]["collection"]

    def rebound(command, **_kwargs):
        if command[3:5] == ("collection", "show"):
            return recall_module._CommandResult(
                0,
                (
                    f"Collection: {collection}\n"
                    f"  Path:     {tmp_path / 'foreign'}\n"
                    "  Pattern:  **/*.md\n"
                ).encode(),
                b"",
            )
        if "query" in command:
            pytest.fail("a rebound scoped collection must not be queried")
        return recall_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(recall_module, "_run_command", rebound)

    result = companion.search("launch decision", connection_id="con_abc")

    assert result.backend == "local"
    assert result.reason == "QMD app search failed; local lexical app search was used"
    assert result.hits[0].source_ref == "gmail:message-1"


def test_sync_removes_replaced_content_addressed_documents(tmp_path: Path, monkeypatch) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )

    class Adapter:
        text = "first"

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="one",
                        revision=self.text,
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:one",
                        title="One",
                        text=self.text,
                        metadata={},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint=self.text,
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda *_args, **_kwargs: recall_module._CommandResult(0, b"", b""),
    )
    adapter = Adapter()
    companion.sync(adapter, "con_abc", refresh=True)
    adapter.text = "second"
    companion.sync(adapter, "con_abc")
    assert len(tuple(companion.documents_root.glob("*.md"))) == 2

    companion.refresh()

    assert len(tuple(companion.documents_root.glob("*.md"))) == 1


def test_refresh_reuses_a_bound_collection_for_changed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )

    class Adapter:
        text = "first"

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="message-1",
                        revision=self.text,
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:message-1",
                        title="Project decision",
                        text=self.text,
                        metadata={},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint=self.text,
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    commands: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[3:5] == ("collection", "add"):
            collection = command[command.index("--name") + 1]
            (companion.index_root / "config").mkdir(parents=True, exist_ok=True)
            (companion.index_root / "config" / f"{command[2]}.yml").write_text(
                f'collections:\n  {collection}:\n    path: {command[5]}\n    pattern: "**/*.md"\n',
                encoding="utf-8",
            )
        return recall_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(recall_module, "_run_command", run)
    adapter = Adapter()
    companion.sync(adapter, "con_abc", refresh=True)
    first_refresh = list(commands)
    adapter.text = "second"
    companion.sync(adapter, "con_abc", refresh=True)
    incremental_refresh = commands[len(first_refresh) :]

    assert [command[3] for command in first_refresh] == ["collection", "update", "embed"]
    assert first_refresh[0][5] != str(companion.documents_root)
    assert "/qmd-scopes/" in first_refresh[0][5]
    assert [command[3:] for command in incremental_refresh] == [("update",), ("embed",)]


def test_refresh_ignores_legacy_global_qmd_config_and_builds_a_physical_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )
    adapter = _PagedAdapter([])
    companion.sync(adapter, "con_abc")
    config = companion.index_root / "config" / f"{companion.index}.yml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "collections:\n"
        f"  {companion.collection}:\n"
        f"    path: {companion.documents_root}\n"
        '    pattern: "**/*.md"\n',
        encoding="utf-8",
    )
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda command, **_kwargs: (
            commands.append(command) or recall_module._CommandResult(0, b"", b"")
        ),
    )

    companion.refresh()

    assert [command[3] for command in commands] == ["collection", "update", "embed"]
    assert commands[0][5] != str(companion.documents_root)
    assert "/qmd-scopes/" in commands[0][5]


def test_refresh_publishes_scoped_binding_when_legacy_state_has_no_binding_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )
    adapter = _PagedAdapter([])
    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda *_args, **_kwargs: recall_module._CommandResult(0, b"", b""),
    )
    companion.sync(adapter, "con_abc")
    companion.refresh()
    legacy_state = json.loads(companion.state_path.read_text(encoding="utf-8"))
    legacy_state.pop("qmd_bindings")
    companion.state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

    refreshed = companion.refresh()
    state = json.loads(companion.state_path.read_text(encoding="utf-8"))

    assert refreshed.indexed is True
    assert "con_abc" in state["qmd_bindings"]


def test_refresh_continues_smallest_scope_first_after_a_quick_scope_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )

    class Adapter:
        def __init__(self, object_ids: tuple[str, ...]) -> None:
            self.object_ids = object_ids

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=tuple(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id=object_id,
                        revision="r1",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref=f"gmail:{object_id}",
                        title=object_id,
                        text=object_id,
                        metadata={},
                        freshness={"status": "complete"},
                    )
                    for object_id in self.object_ids
                ),
                checkpoint="watermark-1",
                scanned=len(self.object_ids),
                complete=True,
                freshness={"status": "complete"},
            )

    companion.sync(Adapter(("large-1", "large-2")), "con_large")
    companion.sync(Adapter(("small-1",)), "con_small")
    calls: list[tuple[str, ...]] = []
    small_index = hashlib.sha256(b"con_small").hexdigest()[:32]

    def run(command, **_kwargs):
        calls.append(command)
        if command[2].endswith(small_index):
            return recall_module._CommandResult(1, b"", b"failure")
        return recall_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(recall_module, "_run_command", run)

    result = companion.refresh()

    assert result.indexed is False
    assert result.reason is not None
    assert calls[0][2].endswith(small_index)
    assert [command[3] for command in calls[1:]] == ["collection", "update", "embed"]
    state = json.loads(companion.state_path.read_text(encoding="utf-8"))
    assert "con_large" in state["qmd_bindings"]
    assert "con_small" not in state["qmd_bindings"]


def test_legacy_flat_document_paths_stay_readable_and_are_not_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )
    adapter = _PagedAdapter([])
    companion.sync(adapter, "con_abc")
    state = json.loads(companion.state_path.read_text(encoding="utf-8"))
    key = hashlib.sha256(b"con_abc\0message-1").hexdigest()
    record = state["documents"][key]
    prior_path = companion.index_root / record["path"]
    legacy_path = companion.documents_root / f"{key}.md"
    prior_path.replace(legacy_path)
    record["path"] = f"documents/{key}.md"
    companion.state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        recall_module,
        "_run_command",
        lambda *_args, **_kwargs: recall_module._CommandResult(0, b"", b""),
    )

    companion.sync(adapter, "con_abc")
    companion.refresh()

    assert companion.read("con_abc", "message-1") is not None
    persisted = json.loads(companion.state_path.read_text(encoding="utf-8"))
    assert persisted["documents"][key]["path"] == f"documents/{key}.md"
    assert legacy_path.is_file()


def test_search_uses_committed_content_while_a_provider_read_is_in_flight(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        update = False
        started = threading.Event()
        release = threading.Event()

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            if self.update:
                self.started.set()
                assert self.release.wait(2)
            text = "newly fetched content" if self.update else "committed immutable content"
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="message-1",
                        revision="two" if self.update else "one",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:message-1",
                        title="One",
                        text=text,
                        metadata={},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint="two" if self.update else "one",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    adapter = Adapter()
    companion.sync(adapter, "con_abc")
    adapter.update = True
    errors: list[BaseException] = []

    def background_sync() -> None:
        try:
            companion.sync(adapter, "con_abc", timeout_seconds=5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=background_sync)
    worker.start()
    assert adapter.started.wait(1)

    result = companion.search("committed immutable", timeout_seconds=1)

    adapter.release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert result.backend == "local"
    assert result.hits[0].source_ref == "gmail:message-1"
    assert companion.read("con_abc", "message-1").text == "newly fetched content"


def test_search_degrades_to_committed_exact_text_during_qmd_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    executable = tmp_path / "qmd"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    companion = AppCorpusCompanion(
        vault.root, executable=executable, index_root=tmp_path / "corpus"
    )

    class Adapter:
        text = "first committed text"

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="message-1",
                        revision=self.text,
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:message-1",
                        title="One",
                        text=self.text,
                        metadata={},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint=self.text,
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    block = threading.Event()
    started = threading.Event()
    release = threading.Event()

    def run(command, **_kwargs):
        if block.is_set() and command[-1] == "update":
            started.set()
            release.wait(2)
        return recall_module._CommandResult(0, b"", b"")

    monkeypatch.setattr(recall_module, "_run_command", run)
    adapter = Adapter()
    companion.sync(adapter, "con_abc", refresh=True)
    adapter.text = "second committed text"
    companion.sync(adapter, "con_abc")
    block.set()
    worker = threading.Thread(target=lambda: companion.refresh(timeout_seconds=5))
    worker.start()
    assert started.wait(1)

    result = companion.search("second committed", timeout_seconds=1)

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result.backend == "local"
    assert result.reason is not None
    assert result.hits[0].source_ref == "gmail:message-1"


def test_status_surfaces_extraction_gaps_without_changing_walk_completion(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="gmail",
                        object_id="message-1",
                        revision="one",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="gmail:message-1",
                        title="Attachment",
                        text="Provider text is unavailable",
                        metadata={
                            "extraction_status": "gap",
                            "sent_at": "2024-04-01T12:00:00Z",
                        },
                        freshness={"status": "partial"},
                    ),
                ),
                checkpoint="one",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    companion.configure("con_abc", adapter="gmail")
    companion.sync(Adapter(), "con_abc")

    status = companion.status()
    result = companion.search("unavailable")
    document = companion.read("con_abc", "message-1")

    assert status.complete is True
    assert status.connections[0]["extraction_status"] == {"gap": 1, "partial": 0}
    assert status.connections[0]["has_completed_snapshot"] is True
    assert status.connections[0]["has_resume"] is False
    assert "complete_checkpoint" not in status.connections[0]
    assert "resume_checkpoint" not in status.connections[0]
    assert document is not None
    assert document.fetched_at == "2026-09-07T08:00:00Z"
    assert document.source_event_at == "2024-04-01T12:00:00Z"
    assert result.hits[0].source_event_at == "2024-04-01T12:00:00Z"


def test_legacy_gmail_message_gap_recovery_starts_once_per_connection(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    running_checkpoint = json.dumps(
        {
            "gmail": {"continuation": "provider-next"},
            "provider": "google",
            "round": "running",
            "v": 1,
        }
    )
    recovery_checkpoint = json.dumps(
        {
            "gmail": {
                "continuation": "provider-after-recovery",
                "legacy_message_gap_recovery_epoch": 1,
                "legacy_message_gap_recovery_started": True,
            },
            "provider": "google",
            "round": "running",
            "v": 1,
        }
    )
    completed_checkpoint = json.dumps(
        {
            "gmail_history_anchor": True,
            "gmail_history_id": "99",
            "provider": "google",
            "round": "complete",
            "v": 1,
        }
    )

    def document(object_id: str, source_ref: str) -> AppCorpusDocument:
        return AppCorpusDocument(
            connection_id="con_abc",
            provider="gmail",
            object_id=object_id,
            revision="one",
            fetched_at="2026-09-07T08:00:00Z",
            source_ref=source_ref,
            title="Unavailable Gmail content",
            text="Provider text is unavailable",
            metadata={"extraction_status": "gap"},
            freshness={"status": "partial"},
        )

    assert not app_corpus._is_legacy_gmail_message_gap(
        {
            "connection_id": "con_abc",
            "metadata": {"extraction_status": "gap"},
            "object_id": "gmail-attachment:m-1:a-1",
            "provider": "gmail",
            "source_ref": "gmail:attachment:m-1:a-1",
        },
        "con_abc",
    )

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            del connection_id, limit
            self.calls.append(checkpoint)
            call = len(self.calls)
            if call == 1:
                return AppCorpusSyncResult(
                    documents=(
                        document("gmail:m-1", "gmail:message:m-1"),
                        document("gmail-attachment:m-1:a-1", "gmail:attachment:m-1:a-1"),
                    ),
                    checkpoint=running_checkpoint,
                    scanned=2,
                    complete=True,
                    freshness={"status": "complete"},
                )
            if call == 2:
                assert checkpoint is not None
                assert json.loads(checkpoint)["gmail_legacy_message_gap_recovery_epoch"] == 1
                return AppCorpusSyncResult(
                    documents=(),
                    checkpoint=recovery_checkpoint,
                    scanned=1,
                    complete=False,
                    freshness={"status": "partial"},
                )
            if call == 3:
                assert checkpoint == recovery_checkpoint
                return AppCorpusSyncResult(
                    documents=(),
                    checkpoint=completed_checkpoint,
                    scanned=1,
                    complete=True,
                    freshness={"status": "complete"},
                )
            assert checkpoint == completed_checkpoint
            return AppCorpusSyncResult(
                documents=(),
                checkpoint=completed_checkpoint,
                scanned=0,
                complete=True,
                freshness={"status": "complete"},
            )

    adapter = Adapter()
    for _ in range(4):
        companion.sync(adapter, "con_abc")

    assert json.loads(adapter.calls[1] or "{}")["gmail_legacy_message_gap_recovery_epoch"] == 1
    assert (
        json.loads(adapter.calls[3] or "{}").get("gmail_legacy_message_gap_recovery_epoch") is None
    )


def test_outlook_detail_failure_recovery_rewinds_once_per_connection(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    delta_checkpoint = json.dumps(
        {
            "candidates": {},
            "coverage_gaps": [],
            "folder_delta_link": "folders-delta",
            "folder_index": 2,
            "folder_order": ["folder-a", "folder-b", "folder-c"],
            "folders": {
                "folder-a": {"delta_link": "messages-a"},
                "folder-b": {"delta_link": "messages-b"},
                "folder-c": {"continuation": {"path": "page-c"}, "delta_link": "messages-c"},
            },
            "phase": "messages",
            "v": 1,
        }
    )
    failed_checkpoint = json.dumps(
        {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "mail": {"delta_checkpoint": delta_checkpoint},
            "provider": "microsoft",
            "round": "running",
            "v": 1,
        }
    )

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            del connection_id, limit
            self.calls.append(checkpoint)
            return AppCorpusSyncResult(
                documents=(),
                checkpoint=failed_checkpoint if checkpoint is None else checkpoint,
                scanned=0,
                complete=False,
                freshness={"status": "error", "detail": "connector provider read failed"},
            )

    adapter = Adapter()
    for _ in range(3):
        companion.sync(adapter, "outlook-connection")

    recovered = json.loads(adapter.calls[1] or "{}")
    recovered_delta = json.loads(recovered["mail"]["delta_checkpoint"])
    assert recovered["outlook_delta_materialization_recovery_epoch"] == 1
    assert recovered_delta["phase"] == "messages"
    assert recovered_delta["folder_index"] == 1
    assert recovered_delta["folders"]["folder-a"]["delta_link"] == "messages-a"
    assert recovered_delta["folders"]["folder-b"] == {}
    assert recovered_delta["folders"]["folder-c"] == {}
    assert adapter.calls[2] == adapter.calls[1]


def test_whatsapp_status_derives_legacy_media_coverage_from_metadata(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        def sync(self, connection_id, *, checkpoint=None, limit=100):
            documents = (
                ("audio-relay", {"media_type": "audio", "transcript_source": "local_relay"}),
                ("audio-missing", {"media_type": "audio"}),
                (
                    "audio-explicit-gap",
                    {
                        "media_type": "audio",
                        "extraction_status": "gap",
                        "media_extraction_status": "transcript_unavailable",
                    },
                ),
                ("image", {"media_type": "image"}),
                (
                    "video",
                    {
                        "media_type": "video/mp4",
                        "media_extraction_status": "not_extracted",
                    },
                ),
            )
            return AppCorpusSyncResult(
                documents=tuple(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="whatsapp",
                        object_id=object_id,
                        revision="one",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref=f"whatsapp:{object_id}",
                        title=object_id,
                        text="caption",
                        metadata=metadata,
                        freshness={"status": "complete"},
                    )
                    for object_id, metadata in documents
                ),
                checkpoint="one",
                scanned=len(documents),
                complete=True,
                freshness={"status": "complete"},
            )

    companion.configure("whatsapp-local", adapter="whatsapp")
    companion.sync(Adapter(), "whatsapp-local")

    connection = companion.status().connections[0]

    assert connection["extraction_status"] == {"gap": 1, "partial": 0}
    assert connection["media_coverage"] == {
        "audio": {
            "total": 3,
            "local_relay_transcripts": 1,
            "transcript_unavailable": 2,
        },
        "image": {"total": 1, "not_extracted": 1},
        "video": {"total": 1, "not_extracted": 1},
    }


def test_source_event_time_uses_drive_modification_when_no_occurrence_time_exists() -> None:
    document = AppCorpusDocument(
        connection_id="con_abc",
        provider="google_drive",
        object_id="drive-file-1",
        revision="one",
        fetched_at="2026-09-07T08:00:00Z",
        source_ref="google-drive:file:file-1",
        title="Roadmap",
        text="A document",
        metadata={"modified_at": "2026-09-06T10:00:00Z"},
        freshness={"status": "complete"},
    )

    assert app_corpus._source_event_at(document) == "2026-09-06T10:00:00Z"


def test_lexical_search_keeps_scoped_indexes_and_updates_changed_bodies(tmp_path, monkeypatch):
    from dataclasses import replace

    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Lexical")
    companion = AppCorpusCompanion(
        vault.root, executable=tmp_path / "missing-qmd", index_root=tmp_path / "corpus"
    )
    adapter = _PagedAdapter([])
    companion.sync(adapter, "con_a")
    companion.sync(adapter, "con_b")
    reads = []
    original = PinnedPathRoot.read_regular_file

    def observe(self, path, **kwargs):
        if kwargs.get("label") == "app corpus lexical document":
            reads.append(path)
        return original(self, path, **kwargs)

    monkeypatch.setattr(PinnedPathRoot, "read_regular_file", observe)
    assert companion.search("launch", connection_id="con_a").hits[0].connection_id == "con_a"
    assert companion.search("launch", connection_id="con_b").hits[0].connection_id == "con_b"
    assert len(reads) == 2
    assert companion.search("launch", connection_id="con_a").hits
    assert len(reads) == 2

    class Refetched:
        def sync(self, connection_id, *, checkpoint=None, limit=100):
            document = adapter.sync(connection_id).documents[0]
            return AppCorpusSyncResult(
                documents=(replace(document, fetched_at="2026-09-07T12:00:00Z"),),
                checkpoint="refetched",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    companion.sync(Refetched(), "con_a")
    hit = companion.search("launch", connection_id="con_a").hits[0]
    assert hit.fetched_at == "2026-09-07T12:00:00Z"
    assert len(reads) == 2

    class Updated:
        def sync(self, connection_id, *, checkpoint=None, limit=100):
            document = adapter.sync(connection_id).documents[0]
            return AppCorpusSyncResult(
                documents=(replace(document, revision="r2", text="The correction is decisive."),),
                checkpoint="r2",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    companion.sync(Updated(), "con_a")
    assert companion.search("correction", connection_id="con_a").hits
    assert len(reads) == 3
    assert not companion.search("launch", connection_id="con_a").hits
    assert companion.search("launch", connection_id="con_b").hits
    assert len(reads) == 3

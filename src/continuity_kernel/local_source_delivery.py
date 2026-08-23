"""Crash-safe delivery of bounded host-local message evidence.

The adapters return transient content.  This module owns only the host-local
checkpoint and the ordering rule around the portable, content-free source
receipt: verify evidence, CAS-record the receipt, then advance the checkpoint.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from continuity_kernel import apple_messages, config, whatsapp
from continuity_kernel.atomic import PinnedPathRoot, open_regular_file, sha256_bytes
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.records import format_time, parse_time
from continuity_kernel.source_recipes import get_recipe
from continuity_kernel.source_state import (
    SourceCompleteness,
    SourceSnapshot,
    source_fingerprint,
)
from continuity_kernel.source_state import (
    record_source_observation as project_source_observation,
)
from continuity_kernel.vault import Vault

SUPPORTED_LOCAL_SOURCES: Final = ("apple_messages", "whatsapp")
LOCAL_SOURCE_TOOL_BINDINGS: Final = {
    "apple_messages": "seld.local.apple-messages.read.v1",
    "whatsapp": "seld.local.whatsapp-wacli.read.v1",
}

STATE_VERSION: Final = 3
LEGACY_TOKEN_VERSION: Final = 1
TOKEN_VERSION: Final = 2
MAX_STATE_BYTES: Final = 96 * 1024
MAX_TOKEN_BYTES: Final = 48 * 1024
MAX_CURSOR_CHARS: Final = 8_192
MAX_RESULT_REFS: Final = 20
MAX_RESULT_REF_CHARS: Final = 512
MAX_COMMIT_MARKER_BYTES: Final = 16 * 1024
MAX_JOURNAL_EVENT_BYTES: Final = 64 * 1024
MAX_ADAPTER_BINDING_BYTES: Final = 16 * 1024
MAX_STORE_ROOT_CHARACTERS: Final = 4_096
ADAPTER_BINDING_VERSION: Final = 1
FORWARD_ONLY_RESET: Final = "forward_only_reset"
DISCARD_STALE_DELIVERY: Final = "discard_stale_delivery"
RESET_DISPOSITIONS: Final = (FORWARD_ONLY_RESET, DISCARD_STALE_DELIVERY)
VERIFIED_PREFIX_ADOPTION: Final = "adopt_verified_prefix"
_AUTHORITY_RELATIVE: Final = Path("local-source-delivery")
_CHECKPOINTS_RELATIVE: Final = _AUTHORITY_RELATIVE / "checkpoints"
_LOCKS_RELATIVE: Final = _AUTHORITY_RELATIVE / "locks"
_HISTORY_RELATIVE: Final = _CHECKPOINTS_RELATIVE / "history"
_COMMITS_RELATIVE: Final = _CHECKPOINTS_RELATIVE / "commits"
_ADAPTER_BINDINGS_RELATIVE: Final = _CHECKPOINTS_RELATIVE / "adapter-bindings"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:absent|[0-9a-f]{64})$")
_RESULT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@|=+-]{0,511}$")


@dataclass(frozen=True)
class _Binding:
    source: str
    vault_id: str
    vault_root_digest: str
    host_digest: str
    key: str
    authority: Path
    checkpoint_relative: Path
    lock_relative: Path
    adapter_relative: Path

    @property
    def path(self) -> Path:
        return self.authority / self.checkpoint_relative

    @property
    def lock_path(self) -> Path:
        return self.authority / self.lock_relative


@dataclass(frozen=True)
class _DeliveryToken:
    source: str
    binding_key: str
    sequence: int
    checkpoint_digest: str
    source_revision: str
    target_cursor: str
    target_cursor_digest: str
    observed_at: str
    covered_through: str
    completeness: str
    items_observed: int
    limit: int
    delivery_digest: str
    adapter_token: str | None
    account_digest: str | None


@dataclass(frozen=True)
class _StoreRootSnapshot:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class _LastReceipt:
    token_digest: str
    disposition: str
    result_refs: tuple[str, ...]
    actor_digest: str
    account_digest: str
    source_revision: str
    acknowledged_at: str


@dataclass(frozen=True)
class _ResetReceipt:
    previous_checkpoint_digest: str
    previous_sequence: int
    previous_store_identity: str
    replacement_store_identity: str
    pending_delivery_discarded: bool
    archive_digest: str
    disposition: str
    reset_at: str


@dataclass(frozen=True)
class _CommitMarker:
    source: str
    binding_key: str
    token_digest: str
    before_revision: str
    after_revision: str
    observed_at: str
    receipt_binding_digest: str | None


@dataclass(frozen=True)
class _AdoptionReceipt:
    imported_checkpoint_digest: str
    disposition: str
    adopted_at: str


@dataclass(frozen=True)
class _Checkpoint:
    source: str
    vault_id: str
    vault_root_digest: str
    host_digest: str
    sequence: int
    cursor: str
    checkpoint_digest: str
    pending_token: str | None
    last_receipt: _LastReceipt | None
    updated_at: str
    last_reset: _ResetReceipt | None = None
    adoption: _AdoptionReceipt | None = None


class LocalSourceDelivery:
    """One host-local delivery authority bound to a single initialized vault."""

    def __init__(
        self,
        vault: Vault,
        *,
        store_root: Path | None = None,
        whatsapp_runtime: Path = whatsapp.DEFAULT_RUNTIME,
        whatsapp_service_label: str | None = None,
        whatsapp_runner: whatsapp.Runner | None = None,
        after_source_commit: Callable[[dict[str, Any]], None] | None = None,
        after_rebaseline_history: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.vault = vault
        self.store_root = store_root.expanduser().resolve() if store_root is not None else None
        self.whatsapp_runtime = whatsapp_runtime.expanduser()
        self.whatsapp_service_label = whatsapp.resolve_service_label(whatsapp_service_label)
        self.whatsapp_runner = whatsapp_runner
        self._after_source_commit = after_source_commit
        self._after_rebaseline_history = after_rebaseline_history

    def status(self, source: str) -> dict[str, Any]:
        """Return only host checkpoint metadata; never the cursor or pending token."""

        source = _source(source)
        binding = self._binding(source, create_host=False)
        if binding is None:
            return {
                "source": source,
                "initialized": False,
                "pending": False,
                "sequence": None,
                "checkpoint_digest": None,
                "pending_token_digest": None,
                "source_health": "uninitialized",
                "last_receipt": None,
                "last_reset": None,
                "adoption": None,
            }
        with self._locked_storage(binding, create=False) as store:
            if store is None:
                return {
                    "source": source,
                    "initialized": False,
                    "pending": False,
                    "sequence": None,
                    "checkpoint_digest": None,
                    "pending_token_digest": None,
                    "source_health": "uninitialized",
                    "last_receipt": None,
                    "last_reset": None,
                    "adoption": None,
                }
            store_root, _pending_binding = self._adapter_root(store, binding)
            state, _encoded = self._read_state(store, binding, required=False)
        if state is None:
            return {
                "source": source,
                "initialized": False,
                "pending": False,
                "sequence": None,
                "checkpoint_digest": None,
                "pending_token_digest": None,
                "source_health": "uninitialized",
                "last_receipt": None,
                "last_reset": None,
                "adoption": None,
            }
        result = self._status_dict(state)
        if source in SUPPORTED_LOCAL_SOURCES:
            error_code = self._checkpoint_identity_error(
                source,
                state,
                binding=binding,
                store_root=store_root,
            )
            if error_code is not None:
                result["source_health"] = "blocked"
                result["error_code"] = error_code
        return result

    def baseline(self, source: str) -> dict[str, Any]:
        """Store the current aggregate cursor as a forward-only baseline."""

        source = _source(source)
        snapshot = self._selected_snapshot(source)
        binding = self._required_binding(source)
        with self._locked_storage(binding, create=True) as store:
            assert store is not None
            store_root, pending_binding = self._adapter_root(store, binding)
            existing, existing_bytes = self._read_state(store, binding, required=False)
            if existing is not None:
                raise ConflictError("local source is already baselined; poll its checkpoint")
            status = self._inspect(source, store_root=store_root)
            if not status.available:
                raise ContinuityError(status.error or "local source is unavailable")
            cursor = status.cursor()
            if cursor is None:
                raise ContinuityError("local source did not produce an aggregate baseline")
            now = format_time(datetime.now(UTC))
            state = _Checkpoint(
                source=source,
                vault_id=binding.vault_id,
                vault_root_digest=binding.vault_root_digest,
                host_digest=binding.host_digest,
                sequence=0,
                cursor=cursor,
                checkpoint_digest=_digest(cursor),
                pending_token=None,
                last_receipt=None,
                updated_at=now,
            )
            if pending_binding is not None:
                self._persist_adapter_binding(store, binding, pending_binding)
            self._write_state(store, binding, state, expected=existing_bytes)
        return {
            "source": source,
            "baseline_scope": "forward-only",
            "initialized": True,
            "sequence": 0,
            "checkpoint_digest": state.checkpoint_digest,
            "source_revision": snapshot.revision,
            "observed_at": status.observed_at,
            "messages": None,
        }

    def adopt_checkpoint(
        self,
        source: str,
        *,
        prior_cursor: str,
        expected_source_revision: str,
        disposition: str,
    ) -> dict[str, Any]:
        """Adopt one verified aggregate prefix without replaying already-covered content."""

        source = _source(source)
        if disposition != VERIFIED_PREFIX_ADOPTION:
            raise ValidationError(
                "local source checkpoint adoption requires disposition adopt_verified_prefix"
            )
        if _REVISION.fullmatch(expected_source_revision) is None:
            raise ValidationError("expected source-state revision is invalid")
        snapshot = self._selected_snapshot(source)
        if snapshot.revision != expected_source_revision:
            raise ConflictError("source state changed before local checkpoint adoption")
        binding = self._required_binding(source)
        with self._locked_storage(binding, create=True) as store:
            assert store is not None
            store_root, pending_binding = self._adapter_root(store, binding)
            existing, existing_bytes = self._read_state(store, binding, required=False)
            if existing is not None:
                adoption = existing.adoption
                expected_digest = _digest(prior_cursor)
                if (
                    adoption is None
                    or adoption.imported_checkpoint_digest != expected_digest
                    or adoption.disposition != disposition
                ):
                    raise ConflictError("local source already has a different host checkpoint")
                if existing.last_reset is not None and parse_time(
                    existing.last_reset.reset_at
                ) >= parse_time(adoption.adopted_at):
                    raise ConflictError(
                        "local source migrated checkpoint was superseded by a store reset"
                    )
                _verified_cursor, observed_at = self._verify_prior_checkpoint(
                    source,
                    prior_cursor,
                    store_root=store_root,
                )
                current = self._selected_snapshot(source)
                if current.revision != expected_source_revision:
                    raise ConflictError("source state changed during local checkpoint adoption")
                if pending_binding is not None:
                    self._persist_adapter_binding(store, binding, pending_binding)
                return {
                    "source": source,
                    "adopted": True,
                    "already_adopted": True,
                    "adopted_at": adoption.adopted_at,
                    "baseline_scope": "verified-prefix",
                    "sequence": existing.sequence,
                    "checkpoint_digest": adoption.imported_checkpoint_digest,
                    "source_revision": expected_source_revision,
                    "observed_at": observed_at,
                    "disposition": disposition,
                    "messages": None,
                }
            verified_cursor, observed_at = self._verify_prior_checkpoint(
                source,
                prior_cursor,
                store_root=store_root,
            )
            current = self._selected_snapshot(source)
            if current.revision != expected_source_revision:
                raise ConflictError("source state changed during local checkpoint adoption")
            adopted_at = format_time(datetime.now(UTC))
            state = _Checkpoint(
                source=source,
                vault_id=binding.vault_id,
                vault_root_digest=binding.vault_root_digest,
                host_digest=binding.host_digest,
                sequence=0,
                cursor=verified_cursor,
                checkpoint_digest=_digest(verified_cursor),
                pending_token=None,
                last_receipt=None,
                updated_at=adopted_at,
                adoption=_AdoptionReceipt(
                    imported_checkpoint_digest=_digest(prior_cursor),
                    disposition=disposition,
                    adopted_at=adopted_at,
                ),
            )
            if pending_binding is not None:
                self._persist_adapter_binding(store, binding, pending_binding)
            self._write_state(store, binding, state, expected=existing_bytes)
        return {
            "source": source,
            "adopted": True,
            "already_adopted": False,
            "adopted_at": adopted_at,
            "baseline_scope": "verified-prefix",
            "sequence": 0,
            "checkpoint_digest": state.checkpoint_digest,
            "source_revision": expected_source_revision,
            "observed_at": observed_at,
            "disposition": disposition,
            "messages": None,
        }

    def poll(self, source: str, *, limit: int = 100) -> dict[str, Any]:
        """Replay one bounded transient delta without advancing its checkpoint."""

        source = _source(source)
        if not 1 <= limit <= 100:
            raise ValidationError("local source poll limit must be between 1 and 100")
        binding = self._required_binding(source)
        with self._locked_storage(binding, create=True) as store:
            assert store is not None
            store_root, pending_binding = self._adapter_root(store, binding)
            state, state_bytes = self._read_state(store, binding, required=True)
            assert state is not None
            if pending_binding is not None:
                self._persist_adapter_binding(store, binding, pending_binding)
            if state.pending_token is not None:
                prepared = _decode_token(state.pending_token, binding)
                self._validate_prepared_state(state, prepared)
                delta = self._replay_delta(
                    source,
                    cursor=state.cursor,
                    target_cursor=prepared.target_cursor,
                    complete=prepared.completeness == "complete",
                    limit=prepared.limit,
                    store_root=store_root,
                )
                self._verify_replayed_delta(prepared, delta)
                if prepared.adapter_token is not None:
                    self._verify_prepared_delivery(
                        prepared,
                        state.cursor,
                        store_root=store_root,
                    )
                token = state.pending_token
            else:
                snapshot = self._selected_snapshot(source)
                effective_limit = min(limit, get_recipe(source).read_limit)
                delta = self._read_delta(
                    source,
                    cursor=state.cursor,
                    limit=effective_limit,
                    store_root=store_root,
                )
                prepared = self._prepare_token(
                    binding=binding,
                    state=state,
                    source_revision=snapshot.revision,
                    delta=delta,
                    limit=effective_limit,
                )
                token = _encode_token(prepared)
                state = _Checkpoint(
                    source=state.source,
                    vault_id=state.vault_id,
                    vault_root_digest=state.vault_root_digest,
                    host_digest=state.host_digest,
                    sequence=state.sequence,
                    cursor=state.cursor,
                    checkpoint_digest=state.checkpoint_digest,
                    pending_token=token,
                    last_receipt=state.last_receipt,
                    updated_at=format_time(datetime.now(UTC)),
                    last_reset=state.last_reset,
                    adoption=state.adoption,
                )
                self._write_state(store, binding, state, expected=state_bytes)
        messages = cast(list[dict[str, Any]], delta.to_dict()["messages"])
        return {
            "source": source,
            "sequence": state.sequence,
            "observed_at": prepared.observed_at,
            "complete": prepared.completeness == "complete",
            "messages": messages,
            "untrusted_evidence": True,
            "persisted": False,
            "delivery": {
                "ack_required": True,
                "token": token,
                "token_digest": _digest(token),
                "source_revision": prepared.source_revision,
                "items_observed": prepared.items_observed,
            },
        }

    def rebaseline(
        self,
        source: str,
        *,
        expected_checkpoint_digest: str,
        expected_sequence: int,
        disposition: str,
        expected_source_revision: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly accept a replaced store or discard a stale pending delivery."""

        source = _source(source)
        if _SHA256.fullmatch(expected_checkpoint_digest) is None:
            raise ValidationError("expected local source checkpoint digest is invalid")
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ValidationError("expected local source checkpoint sequence is invalid")
        if disposition not in RESET_DISPOSITIONS:
            raise ValidationError(
                f"local source replacement requires disposition {FORWARD_ONLY_RESET} "
                f"or {DISCARD_STALE_DELIVERY}"
            )
        if disposition == DISCARD_STALE_DELIVERY and (
            expected_source_revision is None
            or _REVISION.fullmatch(expected_source_revision) is None
        ):
            raise ValidationError(
                "discard_stale_delivery requires a valid expected_source_revision"
            )
        self._selected_snapshot(source)
        binding = self._required_binding(source)
        with self._locked_storage(binding, create=True) as store:
            assert store is not None
            store_root, pending_binding = self._adapter_root(store, binding)
            state, state_bytes = self._read_state(store, binding, required=True)
            assert state is not None and state_bytes is not None
            if pending_binding is not None:
                self._persist_adapter_binding(store, binding, pending_binding)
            previous_reset = state.last_reset
            if (
                previous_reset is not None
                and state.sequence == expected_sequence + 1
                and previous_reset.previous_sequence == expected_sequence
                and previous_reset.previous_checkpoint_digest == expected_checkpoint_digest
                and previous_reset.disposition == disposition
            ):
                current_store = self._inspect(source, store_root=store_root)
                current_cursor = current_store.cursor() if current_store.available else None
                if (
                    current_cursor is None
                    or _cursor_store_identity(current_cursor)
                    != previous_reset.replacement_store_identity
                ):
                    raise ConflictError("local source store changed again after replacement repair")
                return self._rebaseline_result(
                    state,
                    already_rebaselined=True,
                )
            if (
                state.sequence != expected_sequence
                or state.checkpoint_digest != expected_checkpoint_digest
            ):
                raise ConflictError(
                    "local source checkpoint changed; reload status before replacement repair"
                )
            replacement = self._inspect(source, store_root=store_root)
            if not replacement.available:
                raise ContinuityError(
                    replacement.error or "replacement local source is unavailable"
                )
            cursor = replacement.cursor()
            if cursor is None:
                raise ContinuityError("replacement local source has no aggregate baseline")
            replacement_digest = _digest(cursor)
            previous_store_identity = _cursor_store_identity(state.cursor)
            replacement_store_identity = _cursor_store_identity(cursor)
            if (
                disposition == FORWARD_ONLY_RESET
                and previous_store_identity == replacement_store_identity
            ):
                raise ConflictError("local source store identity has not changed")
            if disposition == DISCARD_STALE_DELIVERY:
                if state.pending_token is None:
                    raise ConflictError("no local source delivery is pending to discard")
                snapshot = self.vault.get_source_snapshot()
                if expected_source_revision != snapshot.revision:
                    raise ConflictError(
                        "source-state revision does not match expected recovery revision"
                    )
                observation = snapshot.observation(source)
                if observation is None:
                    raise ConflictError("source state has no recorded observation for gap recovery")
                if observation.completeness is not SourceCompleteness.PARTIAL:
                    raise ConflictError(
                        "source observation must record partial completeness for gap recovery"
                    )
                token_digest = _digest(state.pending_token)
                valid_digests = {
                    source_fingerprint(
                        f"seld-local-source-gap:{token_digest}", "source evidence reference"
                    ),
                    source_fingerprint(
                        f"seld-local-source-token:{token_digest}", "source evidence reference"
                    ),
                    source_fingerprint(
                        f"seld-local-source-delivery:{token_digest}", "source evidence reference"
                    ),
                    source_fingerprint(
                        f"seld-local-source-checkpoint:{expected_checkpoint_digest}",
                        "source evidence reference",
                    ),
                    source_fingerprint(token_digest, "source evidence reference"),
                    token_digest,
                }
                if not any(
                    d in observation.evidence_digests for d in valid_digests if d is not None
                ):
                    raise ConflictError(
                        "source observation does not verify the exact stale delivery "
                        "evidence digest"
                    )

            archive_relative = self._checkpoint_archive_relative(binding, state)
            store.ensure_directory(_HISTORY_RELATIVE)
            store.ensure_directory(archive_relative.parent)
            archived = store.read_regular_file(
                archive_relative,
                label="local source checkpoint history",
                max_bytes=MAX_STATE_BYTES,
                missing_ok=True,
            )
            if archived is None:
                store.compare_and_swap_regular_file(
                    archive_relative,
                    expected=None,
                    replacement=state_bytes,
                    label="local source checkpoint history",
                    max_bytes=MAX_STATE_BYTES,
                    mode=0o600,
                )
            elif archived != state_bytes:
                raise ConflictError("local source checkpoint history path already contains data")
            archive_digest = _digest_bytes(state_bytes)
            if self._after_rebaseline_history is not None:
                self._after_rebaseline_history(
                    {
                        "archive_digest": archive_digest,
                        "checkpoint_digest": state.checkpoint_digest,
                        "pending_delivery_discarded": state.pending_token is not None,
                        "previous_store_identity": previous_store_identity,
                        "replacement_store_identity": replacement_store_identity,
                        "sequence": state.sequence,
                        "source": source,
                    }
                )
            now = format_time(datetime.now(UTC))
            completed = _Checkpoint(
                source=state.source,
                vault_id=state.vault_id,
                vault_root_digest=state.vault_root_digest,
                host_digest=state.host_digest,
                sequence=state.sequence + 1,
                cursor=cursor,
                checkpoint_digest=replacement_digest,
                pending_token=None,
                last_receipt=state.last_receipt,
                updated_at=now,
                last_reset=_ResetReceipt(
                    previous_checkpoint_digest=state.checkpoint_digest,
                    previous_sequence=state.sequence,
                    previous_store_identity=previous_store_identity,
                    replacement_store_identity=replacement_store_identity,
                    pending_delivery_discarded=state.pending_token is not None,
                    archive_digest=archive_digest,
                    disposition=disposition,
                    reset_at=now,
                ),
                adoption=state.adoption,
            )
            self._write_state(store, binding, completed, expected=state_bytes)
        return self._rebaseline_result(
            completed,
            already_rebaselined=False,
        )

    def acknowledge(
        self,
        source: str,
        *,
        token: str,
        expected_source_revision: str,
        disposition: str,
        result_refs: tuple[str, ...],
        actor_ref: str,
    ) -> dict[str, Any]:
        """Record the semantic receipt before advancing the host checkpoint."""

        source = _source(source)
        clean_disposition = _disposition(disposition)
        clean_refs = _result_refs(result_refs)
        actor_digest = _required_fingerprint(actor_ref, "actor reference")
        binding = self._required_binding(source)
        prepared = _decode_token(token, binding)
        if expected_source_revision != prepared.source_revision:
            raise ConflictError("source-state revision does not match the prepared delivery")
        with self._locked_storage(binding, create=True) as store:
            assert store is not None
            store_root, pending_binding = self._adapter_root(store, binding)
            state, state_bytes = self._read_state(store, binding, required=True)
            assert state is not None
            if pending_binding is not None:
                self._persist_adapter_binding(store, binding, pending_binding)
            token_digest = _digest(token)
            if state.pending_token is None:
                receipt = state.last_receipt
                if receipt is None:
                    raise ConflictError("local source delivery is no longer pending")
                account_digest = receipt.account_digest
                return self._already_acknowledged(
                    state,
                    token_digest=token_digest,
                    disposition=clean_disposition,
                    result_refs=clean_refs,
                    actor_digest=actor_digest,
                    account_digest=account_digest,
                )
            if state.pending_token != token:
                raise ConflictError("another local-source delivery is pending")
            self._validate_prepared_state(state, prepared)
            account_digest = self._account_digest(
                source,
                store_root=store_root,
            )
            if prepared.account_digest is None:
                prior = self.vault.get_source_snapshot().observation(source)
                if prior is not None and prior.account_fingerprint != account_digest:
                    raise ConflictError(
                        f"legacy {_source_label(source)} delivery lacks a verified local identity; "
                        "deselect and explicitly select it again"
                    )
            elif prepared.account_digest != account_digest:
                raise ConflictError("local source account binding changed after polling")
            self._verify_prepared_delivery(
                prepared,
                state.cursor,
                store_root=store_root,
            )
            current = self.vault.get_source_snapshot()
            receipt_binding_digest = _receipt_binding_digest(
                token_digest=token_digest,
                disposition=clean_disposition,
                result_refs=clean_refs,
                actor_digest=actor_digest,
                account_digest=account_digest,
            )
            completed: _Checkpoint | None = None

            def complete_checkpoint(
                committed_revision: str,
                receipt: dict[str, Any] | None = None,
            ) -> None:
                nonlocal completed
                if receipt is not None and self._after_source_commit is not None:
                    self._after_source_commit(receipt)
                acknowledged_at = format_time(datetime.now(UTC))
                completed = _Checkpoint(
                    source=state.source,
                    vault_id=state.vault_id,
                    vault_root_digest=state.vault_root_digest,
                    host_digest=state.host_digest,
                    sequence=state.sequence + 1,
                    cursor=prepared.target_cursor,
                    checkpoint_digest=prepared.target_cursor_digest,
                    pending_token=None,
                    last_receipt=_LastReceipt(
                        token_digest=token_digest,
                        disposition=clean_disposition,
                        result_refs=clean_refs,
                        actor_digest=actor_digest,
                        account_digest=account_digest,
                        source_revision=committed_revision,
                        acknowledged_at=acknowledged_at,
                    ),
                    updated_at=acknowledged_at,
                    last_reset=state.last_reset,
                    adoption=state.adoption,
                )
                self._write_state(store, binding, completed, expected=state_bytes)

            if current.revision != prepared.source_revision:
                committed_revision = self._recover_committed_receipt(
                    store,
                    binding,
                    current,
                    prepared=prepared,
                    token_digest=token_digest,
                    receipt_binding_digest=receipt_binding_digest,
                )
                if committed_revision is not None:
                    complete_checkpoint(committed_revision)
                    assert completed is not None
                    return self._ack_result(completed, already_acknowledged=False)
                # The provider delta and host checkpoint are still exact. Rebind
                # only the source-state CAS to the fresh snapshot; the source
                # model itself will reject account, selection, or coverage drift.
                prepared = replace(prepared, source_revision=current.revision)

            clean_refs = tuple(
                self.vault.resolve_canonical_result_ref(value) for value in clean_refs
            )
            receipt_binding_digest = _receipt_binding_digest(
                token_digest=token_digest,
                disposition=clean_disposition,
                result_refs=clean_refs,
                actor_digest=actor_digest,
                account_digest=account_digest,
            )
            evidence_refs = _evidence_refs(token_digest, clean_disposition, clean_refs)
            marker: _CommitMarker | None = None

            def prepare_source_commit(snapshot: SourceSnapshot) -> datetime:
                nonlocal marker
                marker = self._prepare_commit_marker(
                    store,
                    binding,
                    snapshot=snapshot,
                    prepared=prepared,
                    token_digest=token_digest,
                    receipt_binding_digest=receipt_binding_digest,
                    actor_ref=actor_ref,
                    account_digest=account_digest,
                    evidence_refs=evidence_refs,
                )
                return parse_time(marker.observed_at)

            def finish_source_commit(receipt: dict[str, Any]) -> None:
                assert marker is not None
                committed_revision = cast(str, receipt["revision"])
                if committed_revision != marker.after_revision:
                    raise ContinuityError(
                        "source receipt revision differs from its prepared commit marker"
                    )
                complete_checkpoint(committed_revision, receipt)

            self.vault.record_source_observation(
                expected_revision=prepared.source_revision,
                source_id=source,
                actor_ref=actor_ref,
                result=("explicit_empty" if prepared.items_observed == 0 else "success"),
                covered_through=prepared.covered_through,
                completeness=prepared.completeness,
                tool_binding=LOCAL_SOURCE_TOOL_BINDINGS[source],
                cursor=prepared.target_cursor,
                evidence_refs=evidence_refs,
                canonical_result_refs=clean_refs,
                _account_fingerprint=account_digest,
                _before_commit=prepare_source_commit,
                _after_commit=finish_source_commit,
            )
            assert completed is not None
        return self._ack_result(completed, already_acknowledged=False)

    def _binding(self, source: str, *, create_host: bool) -> _Binding | None:
        identity = self.vault.identity()
        host_id = config.local_host_id(create=create_host)
        if host_id is None:
            return None
        root = str(self.vault.root.resolve())
        vault_id = str(identity["vault_id"])
        vault_root_digest = _digest(root)
        host_digest = _digest(host_id)
        key = sha256_bytes(f"{vault_id}\0{root}\0{host_id}\0{source}".encode())[:40]
        authority = config.data_dir()
        return _Binding(
            source=source,
            vault_id=vault_id,
            vault_root_digest=vault_root_digest,
            host_digest=host_digest,
            key=key,
            authority=authority,
            checkpoint_relative=_CHECKPOINTS_RELATIVE / f"{source}-{key}.json",
            lock_relative=_LOCKS_RELATIVE / f"{key}.lock",
            adapter_relative=_ADAPTER_BINDINGS_RELATIVE / f"{source}-{key}.json",
        )

    def _required_binding(self, source: str) -> _Binding:
        binding = self._binding(source, create_host=True)
        assert binding is not None
        return binding

    @contextmanager
    def _locked_storage(
        self,
        binding: _Binding,
        *,
        create: bool,
    ) -> Iterator[PinnedPathRoot | None]:
        """Pin authority, checkpoint directory, and lock for one complete operation."""

        try:
            store = PinnedPathRoot(binding.authority)
        except ValidationError as exc:
            raise ValidationError(
                f"local source checkpoint authority is unavailable: {exc}"
            ) from exc
        try:
            if create:
                store.ensure_directory(_AUTHORITY_RELATIVE)
                store.ensure_directory(_CHECKPOINTS_RELATIVE)
                store.ensure_directory(_LOCKS_RELATIVE)
            elif not store.directory_exists(_CHECKPOINTS_RELATIVE):
                yield None
                return
            elif not store.directory_exists(_LOCKS_RELATIVE):
                raise ValidationError("local source checkpoint lock directory is missing")
            with (
                store.bind_directory(_CHECKPOINTS_RELATIVE),
                store.exclusive_file_lock(binding.lock_relative),
            ):
                yield store
        finally:
            store.close()

    def _adapter_root(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
    ) -> tuple[Path | None, _StoreRootSnapshot | None]:
        """Resolve one host-local adapter root without exposing it in public status."""

        encoded = None
        if store.directory_exists(_ADAPTER_BINDINGS_RELATIVE):
            encoded = store.read_regular_file(
                binding.adapter_relative,
                label="local source adapter binding",
                max_bytes=MAX_ADAPTER_BINDING_BYTES,
                missing_ok=True,
            )
        if encoded is not None:
            recorded = _parse_adapter_binding(encoded, binding)
            if self.store_root is not None and self.store_root != recorded.path:
                raise ConflictError("local source is bound to a different host-local store root")
            current = _store_root_snapshot(recorded.path)
            if not _same_store_root_identity(recorded, current):
                raise ConflictError("host-local source store root identity changed")
            return recorded.path, None
        if self.store_root is None:
            return None, None
        snapshot = _store_root_snapshot(self.store_root)
        return snapshot.path, snapshot

    def _persist_adapter_binding(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
        snapshot: _StoreRootSnapshot,
    ) -> None:
        current = _store_root_snapshot(snapshot.path)
        if (current.device, current.inode) != (snapshot.device, snapshot.inode):
            raise ConflictError("host-local source store root changed before binding")
        store.ensure_directory(_ADAPTER_BINDINGS_RELATIVE)
        encoded = _adapter_binding_bytes(binding, snapshot)
        store.compare_and_swap_regular_file(
            binding.adapter_relative,
            expected=None,
            replacement=encoded,
            label="local source adapter binding",
            max_bytes=MAX_ADAPTER_BINDING_BYTES,
            mode=0o600,
        )
        observed = store.read_regular_file(
            binding.adapter_relative,
            label="local source adapter binding",
            max_bytes=MAX_ADAPTER_BINDING_BYTES,
        )
        if observed != encoded:
            raise ContinuityError("host-local source adapter binding durability is unknown")

    def _read_state(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
        *,
        required: bool,
    ) -> tuple[_Checkpoint | None, bytes | None]:
        encoded = store.read_regular_file(
            binding.checkpoint_relative,
            label="local source checkpoint",
            max_bytes=MAX_STATE_BYTES,
            missing_ok=True,
        )
        if encoded is None:
            if required:
                raise ConflictError("local source is not baselined")
            return None, None
        state = _parse_state(encoded)
        if (
            state.source != binding.source
            or state.vault_id != binding.vault_id
            or state.vault_root_digest != binding.vault_root_digest
            or state.host_digest != binding.host_digest
        ):
            raise ConflictError("local source checkpoint belongs to another vault or host")
        if state.checkpoint_digest != _digest(state.cursor):
            raise ValidationError("local source checkpoint cursor digest is invalid")
        if state.pending_token is not None:
            prepared = _decode_token(state.pending_token, binding)
            self._validate_prepared_state(state, prepared)
        return state, encoded

    def _write_state(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
        state: _Checkpoint,
        *,
        expected: bytes | None,
    ) -> None:
        encoded = _state_bytes(state)
        store.compare_and_swap_regular_file(
            binding.checkpoint_relative,
            expected=expected,
            replacement=encoded,
            label="local source checkpoint",
            max_bytes=MAX_STATE_BYTES,
            mode=0o600,
        )

    def _selected_snapshot(self, source: str) -> SourceSnapshot:
        snapshot = self.vault.get_source_snapshot()
        if source not in snapshot.selected_sources:
            raise ConflictError("local source is not selected")
        return snapshot

    def _inspect(
        self,
        source: str,
        *,
        store_root: Path | None,
    ) -> apple_messages.AppleMessagesStatus | whatsapp.WhatsAppStatus:
        if source == "apple_messages":
            return apple_messages.inspect_apple_messages(
                store_root=store_root,
                account_key=self._local_account_key(),
            )
        return whatsapp.inspect_whatsapp(
            store_root=store_root,
            runtime=self.whatsapp_runtime,
            service_label=self.whatsapp_service_label,
            runner=self.whatsapp_runner,
        )

    def _account_digest(
        self,
        source: str,
        *,
        store_root: Path | None,
    ) -> str:
        status = self._inspect(source, store_root=store_root)
        if not status.available or status.account_fingerprint is None:
            raise ContinuityError(
                status.error or f"{_source_label(source)} identity is unavailable"
            )
        return status.account_fingerprint

    def _checkpoint_identity_error(
        self,
        source: str,
        state: _Checkpoint,
        *,
        binding: _Binding,
        store_root: Path | None,
    ) -> str | None:
        status = self._inspect(source, store_root=store_root)
        if not status.available or status.account_fingerprint is None:
            return "adapter_unavailable"
        expected = state.last_receipt.account_digest if state.last_receipt is not None else None
        if state.pending_token is not None:
            prepared = _decode_token(
                state.pending_token,
                binding,
            )
            if prepared.account_digest is not None:
                expected = prepared.account_digest
            elif expected is None:
                return "identity_unverified"
        if expected is not None and expected != status.account_fingerprint:
            return "identity_mismatch"
        return None

    @staticmethod
    def _local_account_key() -> bytes:
        host_id = config.local_host_id(create=True)
        assert host_id is not None
        return bytes.fromhex(
            sha256_bytes(f"seld-local-source-account-key-v1\0{host_id}".encode("ascii"))
        )

    def _verify_prior_checkpoint(
        self,
        source: str,
        cursor: str,
        *,
        store_root: Path | None,
    ) -> tuple[str, str]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS:
            raise ValidationError("prior local source cursor is invalid")
        if source == "apple_messages":
            return apple_messages.verify_apple_messages_checkpoint(
                cursor=cursor,
                store_root=store_root,
            )
        return whatsapp.verify_whatsapp_checkpoint(
            cursor=cursor,
            store_root=store_root,
            runtime=self.whatsapp_runtime,
            service_label=self.whatsapp_service_label,
            runner=self.whatsapp_runner,
        )

    def _read_delta(
        self,
        source: str,
        *,
        cursor: str,
        limit: int,
        store_root: Path | None,
    ) -> apple_messages.AppleMessagesDelta | whatsapp.WhatsAppDelta:
        if source == "apple_messages":
            return apple_messages.read_apple_messages_delta(
                cursor=cursor,
                store_root=store_root,
                limit=limit,
                account_key=self._local_account_key(),
            )
        return whatsapp.read_whatsapp_delta(
            cursor=cursor,
            store_root=store_root,
            runtime=self.whatsapp_runtime,
            service_label=self.whatsapp_service_label,
            runner=self.whatsapp_runner,
            limit=limit,
        )

    def _replay_delta(
        self,
        source: str,
        *,
        cursor: str,
        target_cursor: str,
        complete: bool,
        limit: int,
        store_root: Path | None,
    ) -> apple_messages.AppleMessagesDelta | whatsapp.WhatsAppDelta:
        if source == "apple_messages":
            return apple_messages.replay_apple_messages_delta(
                cursor=cursor,
                target_cursor=target_cursor,
                complete=complete,
                store_root=store_root,
                limit=limit,
                account_key=self._local_account_key(),
            )
        return whatsapp.replay_whatsapp_delta(
            cursor=cursor,
            target_cursor=target_cursor,
            complete=complete,
            store_root=store_root,
            runtime=self.whatsapp_runtime,
            service_label=self.whatsapp_service_label,
            runner=self.whatsapp_runner,
            limit=limit,
        )

    def _prepare_token(
        self,
        *,
        binding: _Binding,
        state: _Checkpoint,
        source_revision: str,
        delta: apple_messages.AppleMessagesDelta | whatsapp.WhatsAppDelta,
        limit: int,
    ) -> _DeliveryToken:
        messages = delta.messages
        adapter_token: str | None = None
        if messages:
            if binding.source == "apple_messages":
                assert isinstance(delta, apple_messages.AppleMessagesDelta)
                adapter_token = apple_messages.create_apple_messages_ack_token(
                    project_root=self.vault.root,
                    previous_cursor=state.cursor,
                    delta=delta,
                )
            else:
                assert isinstance(delta, whatsapp.WhatsAppDelta)
                adapter_token = whatsapp.create_whatsapp_ack_token(
                    project_root=self.vault.root,
                    previous_cursor=state.cursor,
                    delta=delta,
                )
        return _DeliveryToken(
            source=binding.source,
            binding_key=binding.key,
            sequence=state.sequence,
            checkpoint_digest=state.checkpoint_digest,
            source_revision=source_revision,
            target_cursor=delta.cursor,
            target_cursor_digest=_digest(delta.cursor),
            observed_at=delta.observed_at,
            covered_through=delta.covered_through,
            completeness="complete" if delta.complete else "partial",
            items_observed=len(messages),
            limit=limit,
            delivery_digest=_delta_digest(delta),
            adapter_token=adapter_token,
            account_digest=delta.account_fingerprint,
        )

    def _validate_prepared_state(self, state: _Checkpoint, prepared: _DeliveryToken) -> None:
        if (
            prepared.source != state.source
            or prepared.sequence != state.sequence
            or prepared.checkpoint_digest != state.checkpoint_digest
        ):
            raise ConflictError("local source checkpoint changed after polling")

    def _verify_replayed_delta(
        self,
        prepared: _DeliveryToken,
        delta: apple_messages.AppleMessagesDelta | whatsapp.WhatsAppDelta,
    ) -> None:
        if (
            prepared.account_digest is not None
            and delta.account_fingerprint != prepared.account_digest
        ):
            raise ConflictError("local source account binding changed after polling")
        if delta.cursor != prepared.target_cursor or len(delta.messages) != prepared.items_observed:
            raise ContinuityError("local source content changed after polling")
        if prepared.adapter_token is None and _delta_digest(delta) != prepared.delivery_digest:
            raise ContinuityError("local source content changed after polling")

    def _verify_prepared_delivery(
        self,
        prepared: _DeliveryToken,
        cursor: str,
        *,
        store_root: Path | None,
    ) -> None:
        if prepared.adapter_token is None:
            delta = self._replay_delta(
                prepared.source,
                cursor=cursor,
                target_cursor=prepared.target_cursor,
                complete=prepared.completeness == "complete",
                limit=prepared.limit,
                store_root=store_root,
            )
            self._verify_replayed_delta(prepared, delta)
            return
        if prepared.source == "apple_messages":
            acknowledgement, covered = apple_messages.verify_apple_messages_ack_token(
                token=prepared.adapter_token,
                project_root=self.vault.root,
                current_cursor=cursor,
                store_root=store_root,
            )
            acknowledged_cursor = acknowledgement.cursor
        else:
            whatsapp_acknowledgement, covered = whatsapp.verify_whatsapp_ack_token(
                token=prepared.adapter_token,
                project_root=self.vault.root,
                current_cursor=cursor,
                store_root=store_root,
                runtime=self.whatsapp_runtime,
                service_label=self.whatsapp_service_label,
                runner=self.whatsapp_runner,
            )
            acknowledged_cursor = whatsapp_acknowledgement.cursor
        if covered or acknowledged_cursor != prepared.target_cursor:
            raise ConflictError("local source checkpoint already changed")

    def _recover_committed_receipt(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
        snapshot: SourceSnapshot,
        *,
        prepared: _DeliveryToken,
        token_digest: str,
        receipt_binding_digest: str,
    ) -> str | None:
        marker = self._read_commit_marker(store, binding)
        if marker is None or (
            marker.source,
            marker.binding_key,
            marker.token_digest,
        ) != (prepared.source, binding.key, token_digest):
            return None
        if marker.receipt_binding_digest != receipt_binding_digest:
            raise ConflictError("source state changed without the exact prepared receipt")
        if snapshot.revision == marker.before_revision or not _has_exact_source_event(
            self.vault,
            source=prepared.source,
            before_revision=marker.before_revision,
            after_revision=marker.after_revision,
        ):
            return None
        return marker.after_revision

    def _prepare_commit_marker(
        self,
        store: PinnedPathRoot,
        binding: _Binding,
        *,
        snapshot: SourceSnapshot,
        prepared: _DeliveryToken,
        token_digest: str,
        receipt_binding_digest: str,
        actor_ref: str,
        account_digest: str,
        evidence_refs: tuple[str, ...],
    ) -> _CommitMarker:
        relative = self._commit_marker_relative(binding)
        store.ensure_directory(_COMMITS_RELATIVE)
        existing_bytes = store.read_regular_file(
            relative,
            label="local source commit marker",
            max_bytes=MAX_COMMIT_MARKER_BYTES,
            missing_ok=True,
        )
        if existing_bytes is not None:
            existing = _parse_commit_marker(existing_bytes)
            if (
                existing.source == prepared.source
                and existing.binding_key == binding.key
                and existing.token_digest == token_digest
                and existing.before_revision == prepared.source_revision
            ):
                if existing.receipt_binding_digest != receipt_binding_digest:
                    raise ConflictError(
                        "local source delivery already prepared a different semantic receipt"
                    )
                expected = self._project_committed_snapshot(
                    snapshot,
                    prepared=prepared,
                    actor_ref=actor_ref,
                    account_digest=account_digest,
                    evidence_refs=evidence_refs,
                    observed_at=parse_time(existing.observed_at),
                )
                if expected.revision != existing.after_revision:
                    raise ValidationError("local source commit marker revision is invalid")
                return existing

        observed_at = datetime.now(UTC)
        projected = self._project_committed_snapshot(
            snapshot,
            prepared=prepared,
            actor_ref=actor_ref,
            account_digest=account_digest,
            evidence_refs=evidence_refs,
            observed_at=observed_at,
        )
        marker = _CommitMarker(
            source=prepared.source,
            binding_key=binding.key,
            token_digest=token_digest,
            before_revision=prepared.source_revision,
            after_revision=projected.revision,
            observed_at=format_time(observed_at),
            receipt_binding_digest=receipt_binding_digest,
        )
        store.compare_and_swap_regular_file(
            relative,
            expected=existing_bytes,
            replacement=_commit_marker_bytes(marker),
            label="local source commit marker",
            max_bytes=MAX_COMMIT_MARKER_BYTES,
            mode=0o600,
        )
        return marker

    def _read_commit_marker(self, store: PinnedPathRoot, binding: _Binding) -> _CommitMarker | None:
        encoded = store.read_regular_file(
            self._commit_marker_relative(binding),
            label="local source commit marker",
            max_bytes=MAX_COMMIT_MARKER_BYTES,
            missing_ok=True,
        )
        return _parse_commit_marker(encoded) if encoded is not None else None

    @staticmethod
    def _project_committed_snapshot(
        snapshot: SourceSnapshot,
        *,
        prepared: _DeliveryToken,
        actor_ref: str,
        account_digest: str,
        evidence_refs: tuple[str, ...],
        observed_at: datetime,
    ) -> SourceSnapshot:
        host_id = config.local_host_id(create=True)
        assert host_id is not None
        return project_source_observation(
            snapshot,
            source_id=prepared.source,
            actor_ref=actor_ref,
            result=("explicit_empty" if prepared.items_observed == 0 else "success"),
            covered_through=prepared.covered_through,
            completeness=prepared.completeness,
            account_fingerprint=account_digest,
            host_fingerprint=source_fingerprint(host_id, "host identity"),
            tool_fingerprint=source_fingerprint(
                LOCAL_SOURCE_TOOL_BINDINGS[prepared.source], "tool binding"
            ),
            cursor_digest=source_fingerprint(prepared.target_cursor, "source cursor"),
            evidence_digests=tuple(
                source_fingerprint(item, "source evidence reference") for item in evidence_refs
            ),
            observed_at=observed_at,
        )

    @staticmethod
    def _commit_marker_relative(binding: _Binding) -> Path:
        return _COMMITS_RELATIVE / f"{binding.source}-{binding.key}.json"

    def _already_acknowledged(
        self,
        state: _Checkpoint,
        *,
        token_digest: str,
        disposition: str,
        result_refs: tuple[str, ...],
        actor_digest: str,
        account_digest: str,
    ) -> dict[str, Any]:
        receipt = state.last_receipt
        if receipt is None or receipt.token_digest != token_digest:
            raise ConflictError("local source delivery is no longer pending")
        if (
            receipt.disposition != disposition
            or receipt.result_refs != result_refs
            or receipt.actor_digest != actor_digest
            or receipt.account_digest != account_digest
        ):
            raise ConflictError("local source delivery already has a different disposition")
        return self._ack_result(state, already_acknowledged=True)

    def _status_dict(self, state: _Checkpoint) -> dict[str, Any]:
        receipt = state.last_receipt
        reset = state.last_reset
        adoption = state.adoption
        needs_reproof = reset is not None and (
            receipt is None or parse_time(receipt.acknowledged_at) <= parse_time(reset.reset_at)
        )
        return {
            "source": state.source,
            "initialized": True,
            "pending": state.pending_token is not None,
            "sequence": state.sequence,
            "checkpoint_digest": state.checkpoint_digest,
            "pending_token_digest": (
                _digest(state.pending_token) if state.pending_token is not None else None
            ),
            "source_health": "needs_reproof" if needs_reproof else "current",
            "last_receipt": (
                {
                    "token_digest": receipt.token_digest,
                    "disposition": receipt.disposition,
                    "result_refs": list(receipt.result_refs),
                    "source_revision": receipt.source_revision,
                    "acknowledged_at": receipt.acknowledged_at,
                }
                if receipt is not None
                else None
            ),
            "last_reset": (
                {
                    "previous_checkpoint_digest": reset.previous_checkpoint_digest,
                    "previous_sequence": reset.previous_sequence,
                    "previous_store_identity": reset.previous_store_identity,
                    "replacement_store_identity": reset.replacement_store_identity,
                    "pending_delivery_discarded": reset.pending_delivery_discarded,
                    "archive_digest": reset.archive_digest,
                    "disposition": reset.disposition,
                    "reset_at": reset.reset_at,
                }
                if reset is not None
                else None
            ),
            "adoption": (
                {
                    "imported_checkpoint_digest": adoption.imported_checkpoint_digest,
                    "disposition": adoption.disposition,
                    "adopted_at": adoption.adopted_at,
                }
                if adoption is not None
                else None
            ),
        }

    def _ack_result(self, state: _Checkpoint, *, already_acknowledged: bool) -> dict[str, Any]:
        receipt = state.last_receipt
        assert receipt is not None
        return {
            "source": state.source,
            "sequence": state.sequence,
            "checkpoint_digest": state.checkpoint_digest,
            "source_revision": receipt.source_revision,
            "disposition": receipt.disposition,
            "result_refs": list(receipt.result_refs),
            "acknowledged_at": receipt.acknowledged_at,
            "already_acknowledged": already_acknowledged,
        }

    @staticmethod
    def _checkpoint_archive_relative(binding: _Binding, state: _Checkpoint) -> Path:
        digest = state.checkpoint_digest.removeprefix("sha256:")
        return (
            _HISTORY_RELATIVE
            / f"{binding.source}-{binding.key}"
            / f"checkpoint-{state.sequence}-{digest}.json"
        )

    @staticmethod
    def _rebaseline_result(
        state: _Checkpoint,
        *,
        already_rebaselined: bool,
    ) -> dict[str, Any]:
        reset = state.last_reset
        assert reset is not None
        return {
            "source": state.source,
            "sequence": state.sequence,
            "checkpoint_digest": state.checkpoint_digest,
            "source_health": "needs_reproof",
            "pending_delivery_discarded": reset.pending_delivery_discarded,
            "disposition": reset.disposition,
            "archive_digest": reset.archive_digest,
            "reset_at": reset.reset_at,
            "already_rebaselined": already_rebaselined,
            "baseline_scope": "forward-only",
            "messages": None,
        }


def _store_root_snapshot(path: Path) -> _StoreRootSnapshot:
    if not path.is_absolute() or len(str(path)) > MAX_STORE_ROOT_CHARACTERS:
        raise ValidationError("host-local source store root is invalid")
    try:
        canonical = path.resolve(strict=True)
        metadata = os.lstat(canonical)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("host-local source store root is unavailable") from exc
    if canonical != path or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("host-local source store root must be one ordinary directory")
    return _StoreRootSnapshot(canonical, int(metadata.st_dev), int(metadata.st_ino))


def _same_store_root_identity(
    recorded: _StoreRootSnapshot,
    current: _StoreRootSnapshot,
    *,
    platform: str | None = None,
) -> bool:
    """Keep a directory binding stable across a macOS APFS device renumbering."""

    if recorded.path != current.path or recorded.inode != current.inode:
        return False
    if recorded.device == current.device:
        return True
    return (platform or sys.platform) == "darwin"


def _adapter_binding_bytes(binding: _Binding, snapshot: _StoreRootSnapshot) -> bytes:
    payload = {
        "binding_key": binding.key,
        "format_version": ADAPTER_BINDING_VERSION,
        "host_digest": binding.host_digest,
        "source": binding.source,
        "store_device": snapshot.device,
        "store_inode": snapshot.inode,
        "store_root": str(snapshot.path),
        "store_root_digest": _digest(str(snapshot.path)),
        "vault_id": binding.vault_id,
        "vault_root_digest": binding.vault_root_digest,
    }
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    if len(encoded) > MAX_ADAPTER_BINDING_BYTES:
        raise ValidationError("local source adapter binding exceeds its size bound")
    return encoded


def _parse_adapter_binding(encoded: bytes, binding: _Binding) -> _StoreRootSnapshot:
    try:
        payload = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("local source adapter binding is invalid") from exc
    expected = {
        "binding_key",
        "format_version",
        "host_digest",
        "source",
        "store_device",
        "store_inode",
        "store_root",
        "store_root_digest",
        "vault_id",
        "vault_root_digest",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValidationError("local source adapter binding is invalid")
    root = payload.get("store_root")
    device = payload.get("store_device")
    inode = payload.get("store_inode")
    if (
        payload.get("format_version") != ADAPTER_BINDING_VERSION
        or payload.get("source") != binding.source
        or payload.get("binding_key") != binding.key
        or payload.get("vault_id") != binding.vault_id
        or payload.get("vault_root_digest") != binding.vault_root_digest
        or payload.get("host_digest") != binding.host_digest
        or not isinstance(root, str)
        or not root
        or "\x00" in root
        or len(root) > MAX_STORE_ROOT_CHARACTERS
        or not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode < 0
        or payload.get("store_root_digest") != _digest(root)
    ):
        raise ValidationError("local source adapter binding is invalid")
    path = Path(root)
    if not path.is_absolute():
        raise ValidationError("local source adapter binding is invalid")
    return _StoreRootSnapshot(path, device, inode)


def _source(value: str) -> str:
    if value not in SUPPORTED_LOCAL_SOURCES:
        raise ValidationError("local source must be apple_messages or whatsapp")
    return value


def _source_label(source: str) -> str:
    return "Apple Messages" if source == "apple_messages" else "WhatsApp"


def _digest(value: str) -> str:
    return f"sha256:{sha256_bytes(value.encode('utf-8'))}"


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{sha256_bytes(value)}"


def _receipt_binding_digest(
    *,
    token_digest: str,
    disposition: str,
    result_refs: tuple[str, ...],
    actor_digest: str,
    account_digest: str,
) -> str:
    payload = {
        "account_digest": account_digest,
        "actor_digest": actor_digest,
        "disposition": disposition,
        "result_refs": list(result_refs),
        "token_digest": token_digest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _digest_bytes(encoded)


def _cursor_store_identity(cursor: str) -> str:
    try:
        payload = json.loads(cursor)
    except json.JSONDecodeError as exc:
        raise ValidationError("local source checkpoint cursor is invalid") from exc
    schema = payload.get("schema") if isinstance(payload, dict) else None
    generation = payload.get("generation") if isinstance(payload, dict) else None
    if (
        not isinstance(schema, str)
        or not schema
        or not isinstance(generation, str)
        or not generation
    ):
        raise ValidationError("local source checkpoint cursor has no store identity")
    return _digest(f"{schema}\0{generation}")


def _required_fingerprint(value: str, label: str) -> str:
    result = source_fingerprint(value, label)
    if result is None:  # pragma: no cover - the input is required text
        raise ValidationError(f"{label} is invalid")
    return result


def _disposition(value: str) -> str:
    if value not in {"accepted", "rejected"}:
        raise ValidationError("local source disposition must be accepted or rejected")
    return value


def _result_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValidationError("local source acknowledgement requires a durable result reference")
    if len(values) > MAX_RESULT_REFS:
        raise ValidationError(f"local source accepts at most {MAX_RESULT_REFS} result references")
    if len(set(values)) != len(values):
        raise ValidationError("local source result references contain a duplicate")
    if any(_RESULT_REF.fullmatch(value) is None for value in values):
        raise ValidationError("local source result reference is invalid")
    if any(value.startswith("seld-local-source-") for value in values):
        raise ValidationError("local source result reference uses a reserved prefix")
    return values


def _evidence_refs(
    token_digest: str, disposition: str, result_refs: tuple[str, ...]
) -> tuple[str, ...]:
    return (
        f"seld-local-source-token:{token_digest}",
        f"seld-local-source-disposition:{disposition}",
        *result_refs,
    )


def _delta_digest(
    delta: apple_messages.AppleMessagesDelta | whatsapp.WhatsAppDelta,
) -> str:
    payload = delta.to_dict()
    # Observation time changes on a replay; delivery identity does not. The
    # prepared token separately preserves the original bounded coverage time.
    payload.pop("observed_at", None)
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
    return f"sha256:{sha256_bytes(encoded)}"


def _encode_token(value: _DeliveryToken) -> str:
    payload = {"version": TOKEN_VERSION, **asdict(value)}
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    envelope = {
        "digest": sha256_bytes(("seld-local-source-delivery-v1\0" + canonical).encode()),
        "payload": payload,
    }
    encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
    if len(encoded) > MAX_TOKEN_BYTES:
        raise ValidationError("local source delivery token exceeds its size bound")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_token(token: str, binding: _Binding) -> _DeliveryToken:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES * 2:
        raise ValidationError("local source delivery token is invalid")
    try:
        encoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        envelope = json.loads(encoded.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("local source delivery token is invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"digest", "payload"}:
        raise ValidationError("local source delivery token is invalid")
    payload = envelope.get("payload")
    expected = {
        "version",
        "source",
        "binding_key",
        "sequence",
        "checkpoint_digest",
        "source_revision",
        "target_cursor",
        "target_cursor_digest",
        "observed_at",
        "covered_through",
        "completeness",
        "items_observed",
        "limit",
        "delivery_digest",
        "adapter_token",
    }
    version = payload.get("version") if isinstance(payload, dict) else None
    if version == TOKEN_VERSION:
        expected.add("account_digest")
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValidationError("local source delivery token is invalid")
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = envelope.get("digest")
    if not isinstance(digest, str) or digest != sha256_bytes(
        ("seld-local-source-delivery-v1\0" + canonical).encode()
    ):
        raise ValidationError("local source delivery token is invalid")
    value = _token_payload(payload)
    if value.source != binding.source or value.binding_key != binding.key:
        raise ConflictError("local source delivery token belongs to another vault or host")
    return value


def _token_payload(payload: dict[str, Any]) -> _DeliveryToken:
    version = payload.get("version")
    if version not in {LEGACY_TOKEN_VERSION, TOKEN_VERSION}:
        raise ValidationError("local source delivery token is invalid")
    strings = (
        "source",
        "binding_key",
        "checkpoint_digest",
        "source_revision",
        "target_cursor",
        "target_cursor_digest",
        "observed_at",
        "covered_through",
        "completeness",
        "delivery_digest",
    )
    if any(not isinstance(payload.get(name), str) for name in strings):
        raise ValidationError("local source delivery token is invalid")
    source = _source(cast(str, payload["source"]))
    sequence = payload.get("sequence")
    items = payload.get("items_observed")
    limit = payload.get("limit")
    adapter_token = payload.get("adapter_token")
    account_digest = payload.get("account_digest") if version == TOKEN_VERSION else None
    target_cursor = cast(str, payload["target_cursor"])
    if (
        type(sequence) is not int
        or sequence < 0
        or type(items) is not int
        or not 0 <= items <= 100
        or type(limit) is not int
        or not 1 <= limit <= 100
        or len(target_cursor) > MAX_CURSOR_CHARS
        or (adapter_token is not None and not isinstance(adapter_token, str))
        or (account_digest is not None and _SHA256.fullmatch(account_digest) is None)
        or (items > 0) != (adapter_token is not None)
        or cast(str, payload["completeness"]) not in {"complete", "partial"}
        or _REVISION.fullmatch(cast(str, payload["source_revision"])) is None
        or any(
            _SHA256.fullmatch(cast(str, payload[name])) is None
            for name in ("checkpoint_digest", "target_cursor_digest", "delivery_digest")
        )
    ):
        raise ValidationError("local source delivery token is invalid")
    if version == TOKEN_VERSION and account_digest is None:
        raise ValidationError("local source delivery token is invalid")
    parse_time(cast(str, payload["observed_at"]))
    parse_time(cast(str, payload["covered_through"]))
    return _DeliveryToken(
        source=source,
        binding_key=cast(str, payload["binding_key"]),
        sequence=sequence,
        checkpoint_digest=cast(str, payload["checkpoint_digest"]),
        source_revision=cast(str, payload["source_revision"]),
        target_cursor=target_cursor,
        target_cursor_digest=cast(str, payload["target_cursor_digest"]),
        observed_at=cast(str, payload["observed_at"]),
        covered_through=cast(str, payload["covered_through"]),
        completeness=cast(str, payload["completeness"]),
        items_observed=items,
        limit=limit,
        delivery_digest=cast(str, payload["delivery_digest"]),
        adapter_token=adapter_token,
        account_digest=cast(str | None, account_digest),
    )


def _state_bytes(state: _Checkpoint) -> bytes:
    payload = asdict(state)
    payload["format_version"] = STATE_VERSION
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    if len(encoded) > MAX_STATE_BYTES:
        raise ValidationError("local source checkpoint exceeds its size bound")
    return encoded


def _parse_state(encoded: bytes) -> _Checkpoint:
    try:
        payload = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("local source checkpoint is invalid") from exc
    version = payload.get("format_version") if isinstance(payload, dict) else None
    expected = {
        "format_version",
        "source",
        "vault_id",
        "vault_root_digest",
        "host_digest",
        "sequence",
        "cursor",
        "checkpoint_digest",
        "pending_token",
        "last_receipt",
        "updated_at",
    }
    if isinstance(version, int) and version >= 2:
        expected.add("last_reset")
    if version == STATE_VERSION:
        expected.add("adoption")
    if (
        not isinstance(payload, dict)
        or type(version) is not int
        or version not in {1, 2, STATE_VERSION}
        or set(payload) != expected
    ):
        raise ValidationError("local source checkpoint is invalid")
    raw_source = payload.get("source")
    if not isinstance(raw_source, str):
        raise ValidationError("local source checkpoint is invalid")
    source = _source(raw_source)
    sequence = payload.get("sequence")
    cursor = payload.get("cursor")
    pending = payload.get("pending_token")
    strings = ("vault_id", "vault_root_digest", "host_digest", "checkpoint_digest", "updated_at")
    if (
        type(sequence) is not int
        or sequence < 0
        or not isinstance(cursor, str)
        or len(cursor) > MAX_CURSOR_CHARS
        or (pending is not None and not isinstance(pending, str))
        or any(not isinstance(payload.get(name), str) for name in strings)
        or any(
            _SHA256.fullmatch(cast(str, payload[name])) is None
            for name in ("vault_root_digest", "host_digest", "checkpoint_digest")
        )
    ):
        raise ValidationError("local source checkpoint is invalid")
    parse_time(cast(str, payload["updated_at"]))
    last = _parse_last_receipt(payload.get("last_receipt"))
    last_reset = (
        _parse_reset_receipt(payload.get("last_reset"))
        if isinstance(version, int) and version >= 2
        else None
    )
    adoption = (
        _parse_adoption_receipt(payload.get("adoption")) if version == STATE_VERSION else None
    )
    return _Checkpoint(
        source=source,
        vault_id=cast(str, payload["vault_id"]),
        vault_root_digest=cast(str, payload["vault_root_digest"]),
        host_digest=cast(str, payload["host_digest"]),
        sequence=sequence,
        cursor=cursor,
        checkpoint_digest=cast(str, payload["checkpoint_digest"]),
        pending_token=pending,
        last_receipt=last,
        updated_at=cast(str, payload["updated_at"]),
        last_reset=last_reset,
        adoption=adoption,
    )


def _parse_last_receipt(value: object) -> _LastReceipt | None:
    if value is None:
        return None
    expected = {
        "token_digest",
        "disposition",
        "result_refs",
        "actor_digest",
        "account_digest",
        "source_revision",
        "acknowledged_at",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("local source checkpoint receipt is invalid")
    refs = value.get("result_refs")
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        raise ValidationError("local source checkpoint receipt is invalid")
    clean_refs = _result_refs(tuple(refs))
    for name in ("token_digest", "actor_digest", "account_digest"):
        if not isinstance(value.get(name), str) or _SHA256.fullmatch(value[name]) is None:
            raise ValidationError("local source checkpoint receipt is invalid")
    disposition = _disposition(cast(str, value.get("disposition")))
    revision = value.get("source_revision")
    acknowledged_at = value.get("acknowledged_at")
    if (
        not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or not isinstance(acknowledged_at, str)
    ):
        raise ValidationError("local source checkpoint receipt is invalid")
    parse_time(acknowledged_at)
    return _LastReceipt(
        token_digest=cast(str, value["token_digest"]),
        disposition=disposition,
        result_refs=clean_refs,
        actor_digest=cast(str, value["actor_digest"]),
        account_digest=cast(str, value["account_digest"]),
        source_revision=revision,
        acknowledged_at=acknowledged_at,
    )


def _parse_reset_receipt(value: object) -> _ResetReceipt | None:
    if value is None:
        return None
    expected = {
        "previous_checkpoint_digest",
        "previous_sequence",
        "previous_store_identity",
        "replacement_store_identity",
        "pending_delivery_discarded",
        "archive_digest",
        "disposition",
        "reset_at",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("local source checkpoint reset receipt is invalid")
    previous_digest = value.get("previous_checkpoint_digest")
    previous_store_identity = value.get("previous_store_identity")
    replacement_store_identity = value.get("replacement_store_identity")
    pending_delivery_discarded = value.get("pending_delivery_discarded")
    archive_digest = value.get("archive_digest")
    previous_sequence = value.get("previous_sequence")
    disposition = value.get("disposition")
    reset_at = value.get("reset_at")
    if (
        not isinstance(previous_digest, str)
        or _SHA256.fullmatch(previous_digest) is None
        or not isinstance(previous_store_identity, str)
        or _SHA256.fullmatch(previous_store_identity) is None
        or not isinstance(replacement_store_identity, str)
        or _SHA256.fullmatch(replacement_store_identity) is None
        or (
            disposition == FORWARD_ONLY_RESET
            and previous_store_identity == replacement_store_identity
        )
        or type(pending_delivery_discarded) is not bool
        or not isinstance(archive_digest, str)
        or _SHA256.fullmatch(archive_digest) is None
        or type(previous_sequence) is not int
        or previous_sequence < 0
        or disposition not in RESET_DISPOSITIONS
        or not isinstance(reset_at, str)
    ):
        raise ValidationError("local source checkpoint reset receipt is invalid")
    parse_time(reset_at)
    return _ResetReceipt(
        previous_checkpoint_digest=previous_digest,
        previous_sequence=previous_sequence,
        previous_store_identity=previous_store_identity,
        replacement_store_identity=replacement_store_identity,
        pending_delivery_discarded=pending_delivery_discarded,
        archive_digest=archive_digest,
        disposition=disposition,
        reset_at=reset_at,
    )


def _parse_adoption_receipt(value: object) -> _AdoptionReceipt | None:
    if value is None:
        return None
    expected = {"imported_checkpoint_digest", "disposition", "adopted_at"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("local source checkpoint adoption receipt is invalid")
    imported = value.get("imported_checkpoint_digest")
    disposition = value.get("disposition")
    adopted_at = value.get("adopted_at")
    if (
        not isinstance(imported, str)
        or _SHA256.fullmatch(imported) is None
        or disposition != VERIFIED_PREFIX_ADOPTION
        or not isinstance(adopted_at, str)
    ):
        raise ValidationError("local source checkpoint adoption receipt is invalid")
    parse_time(adopted_at)
    return _AdoptionReceipt(
        imported_checkpoint_digest=imported,
        disposition=disposition,
        adopted_at=adopted_at,
    )


def _commit_marker_bytes(marker: _CommitMarker) -> bytes:
    payload = asdict(marker)
    if marker.receipt_binding_digest is None:
        payload.pop("receipt_binding_digest")
        payload["format_version"] = 1
    else:
        payload["format_version"] = 2
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    if len(encoded) > MAX_COMMIT_MARKER_BYTES:  # pragma: no cover - fixed-size fields
        raise ValidationError("local source commit marker exceeds its size bound")
    return encoded


def _parse_commit_marker(encoded: bytes) -> _CommitMarker:
    try:
        payload = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("local source commit marker is invalid") from exc
    common = {
        "format_version",
        "source",
        "binding_key",
        "token_digest",
        "before_revision",
        "after_revision",
        "observed_at",
    }
    version = payload.get("format_version") if isinstance(payload, dict) else None
    expected = common | ({"receipt_binding_digest"} if version == 2 else set())
    if not isinstance(payload, dict) or set(payload) != expected or version not in {1, 2}:
        raise ValidationError("local source commit marker is invalid")
    source = payload.get("source")
    binding_key = payload.get("binding_key")
    token_digest = payload.get("token_digest")
    before_revision = payload.get("before_revision")
    after_revision = payload.get("after_revision")
    observed_at = payload.get("observed_at")
    receipt_binding_digest = payload.get("receipt_binding_digest")
    if (
        not isinstance(source, str)
        or _source(source) != source
        or not isinstance(binding_key, str)
        or not binding_key
        or not isinstance(token_digest, str)
        or _SHA256.fullmatch(token_digest) is None
        or not isinstance(before_revision, str)
        or _REVISION.fullmatch(before_revision) is None
        or not isinstance(after_revision, str)
        or _REVISION.fullmatch(after_revision) is None
        or after_revision == "absent"
        or not isinstance(observed_at, str)
        or (
            version == 2
            and (
                not isinstance(receipt_binding_digest, str)
                or _SHA256.fullmatch(receipt_binding_digest) is None
            )
        )
    ):
        raise ValidationError("local source commit marker is invalid")
    parse_time(observed_at)
    marker = _CommitMarker(
        source=source,
        binding_key=binding_key,
        token_digest=token_digest,
        before_revision=before_revision,
        after_revision=after_revision,
        observed_at=observed_at,
        receipt_binding_digest=(cast(str, receipt_binding_digest) if version == 2 else None),
    )
    if _commit_marker_bytes(marker) != encoded:
        raise ValidationError("local source commit marker is not canonical")
    return marker


def _has_exact_source_event(
    vault: Vault,
    *,
    source: str,
    before_revision: str,
    after_revision: str,
) -> bool:
    path = vault.root / "journal/events.jsonl"
    found = False
    try:
        with (
            open_regular_file(path, label="vault journal") as (descriptor, _snapshot),
            os.fdopen(os.dup(descriptor), "rb") as stream,
        ):
            while raw := stream.readline(MAX_JOURNAL_EVENT_BYTES + 1):
                if len(raw) > MAX_JOURNAL_EVENT_BYTES or not raw.endswith(b"\n"):
                    return False
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    return False
                if not isinstance(value, dict):
                    return False
                if (
                    value.get("before_revision") == before_revision
                    and value.get("after_revision") == after_revision
                    and value.get("operation") == "source.observe"
                    and value.get("identifier") == source
                ):
                    found = True
    except ValidationError:
        return False
    return found


__all__ = [
    "DISCARD_STALE_DELIVERY",
    "FORWARD_ONLY_RESET",
    "LOCAL_SOURCE_TOOL_BINDINGS",
    "RESET_DISPOSITIONS",
    "SUPPORTED_LOCAL_SOURCES",
    "VERIFIED_PREFIX_ADOPTION",
    "LocalSourceDelivery",
]

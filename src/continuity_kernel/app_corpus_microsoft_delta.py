"""Bounded, resumable Microsoft Graph mail delta synchronization.

This module owns only Graph delta state.  It returns immutable-ID message changes
for the corpus provider to materialize.  A removal from one mail folder is not
reported as a permanent deletion: the provider must confirm that the immutable
message ID no longer resolves before it removes corpus content.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from continuity_kernel.connector_runtime import AppCorpusReader
from continuity_kernel.errors import ValidationError

MAX_FOLDERS: Final = 128
MAX_CHECKPOINT_CHARS: Final = 2 * 1024 * 1024
_MAX_REMOVAL_CANDIDATES: Final = 1_024
_MAX_PAGE_SIZE: Final = 1_000
_VERSION: Final = 1


@dataclass(frozen=True)
class MicrosoftMailDeltaChange:
    """A Graph mail delta item with an immutable message identifier."""

    folder_id: str
    message_id: str
    value: Mapping[str, Any]


@dataclass(frozen=True)
class MicrosoftMailDeltaRemoval:
    """A folder-level removal that still needs permanent-delete confirmation."""

    folder_id: str
    message_id: str
    revision: str


@dataclass(frozen=True)
class MicrosoftMailDeltaResult:
    changes: tuple[MicrosoftMailDeltaChange, ...]
    checkpoint: str
    complete: bool
    removals: tuple[MicrosoftMailDeltaRemoval, ...]
    scanned: int
    coverage_gaps: tuple[str, ...]


class MicrosoftMailDeltaSync:
    """Synchronize every readable mailbox folder through closed runtime reads."""

    def __init__(self, runtime: AppCorpusReader) -> None:
        self._runtime = runtime

    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> MicrosoftMailDeltaResult:
        if not isinstance(connection_id, str) or not connection_id:
            raise ValidationError("Microsoft mail delta connection ID is invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise ValidationError("Microsoft mail delta limit is invalid")
        state = _decode(checkpoint) if checkpoint is not None else _initial_state()
        if state["phase"] == "complete":
            state = _next_round(state)

        if state["phase"] == "folders":
            page = self._read(
                connection_id,
                "folders.delta",
                _folder_input(state, limit),
                continuation=state.get("folder_continuation"),
            )
            scanned = len(_items(page.payload))
            folders = _folders(state)
            for value in _items(page.payload):
                folder_id = _identifier(value.get("id"), "Outlook folder")
                if _removed(value):
                    if folders.pop(folder_id, None) is not None:
                        _gap(
                            state,
                            "Removed Outlook folders do not prove that their messages "
                            "were permanently deleted",
                        )
                    continue
                if folder_id not in folders and len(folders) >= MAX_FOLDERS:
                    state["folder_overflow"] = True
                    _gap(
                        state,
                        "Outlook mail folder limit reached; folders after the first 128 "
                        "are not covered",
                    )
                    continue
                folders.setdefault(folder_id, {"delta_link": None})
            if page.continuation is not None:
                state["folder_continuation"] = page.continuation
                return _result(state, scanned=scanned)
            state.pop("folder_continuation", None)
            state["folder_delta_link"] = _delta_link(page.payload, "Outlook folder delta")
            state["folder_order"] = sorted(folders)
            state["folder_index"] = 0
            state["phase"] = "messages"
            return _result(state, scanned=scanned)

        if state["phase"] != "messages":
            raise ValidationError("Microsoft mail delta checkpoint is invalid")
        folders = _folders(state)
        order = _folder_order(state, folders)
        index = _nonnegative(state.get("folder_index"))
        if index >= len(order):
            state["phase"] = "complete"
            return _result(state, scanned=0, complete=True)
        folder_id = order[index]
        folder = folders.get(folder_id)
        if folder is None:
            state["folder_index"] = index + 1
            return _result(state, scanned=0)
        page = self._read(
            connection_id,
            "messages.delta",
            _message_input(folder_id, folder, limit),
            continuation=folder.get("continuation"),
        )
        scanned = len(_items(page.payload))
        changes: list[MicrosoftMailDeltaChange] = []
        for value in _items(page.payload):
            message_id = _identifier(value.get("id"), "Outlook message")
            if _removed(value):
                _candidate(state, folder_id, message_id, _revision(value, message_id))
            else:
                changes.append(MicrosoftMailDeltaChange(folder_id, message_id, value))
        if page.continuation is not None:
            folder["continuation"] = page.continuation
            return _result(state, changes=changes, scanned=scanned)
        folder.pop("continuation", None)
        folder["delta_link"] = _delta_link(page.payload, "Outlook message delta")
        state["folder_index"] = index + 1
        if index + 1 >= len(order):
            state["phase"] = "complete"
            return _result(state, changes=changes, scanned=scanned, complete=True)
        return _result(state, changes=changes, scanned=scanned)

    def _read(
        self,
        connection_id: str,
        operation: str,
        input_value: Mapping[str, object],
        *,
        continuation: object | None,
    ) -> _Page:
        response = self._runtime.call_app_corpus_read(
            "gsv_outlook_mail_read",
            {
                "connection_id": connection_id,
                "input": dict(input_value),
                "operation": operation,
            },
            continuation=continuation,
        )
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise ValidationError("Microsoft mail delta read did not complete")
        payload = response.get("result")
        if not isinstance(payload, Mapping):
            raise ValidationError("Microsoft mail delta response is invalid")
        return _Page(
            {str(key): value for key, value in payload.items() if isinstance(key, str)},
            response.get("continuation"),
        )


def clear_continuations(checkpoint: str) -> str:
    """Restart only in-progress Graph pages after a rejected continuation."""

    state = _decode(checkpoint)
    state.pop("folder_continuation", None)
    for folder in _folders(state).values():
        folder.pop("continuation", None)
    return _encode(state)


def rewind_message_page_for_materialization_retry(checkpoint: str) -> str:
    """Replay the affected Graph message page after a failed detail read.

    Delta pages carry only identifiers.  If materializing one of those identifiers
    fails after the delta checkpoint is produced, replaying the current folder and
    its predecessor protects both a continued page and a page that ended a folder.
    """

    state = _decode(checkpoint)
    order = _folder_order(state, _folders(state))
    if state["phase"] not in {"messages", "complete"} or not order:
        return _encode(state)
    if state["phase"] == "complete":
        current_index = len(order) - 1
    else:
        current_index = min(_nonnegative(state.get("folder_index")), len(order) - 1)
    restart_index = max(current_index - 1, 0)
    folders = _folders(state)
    for folder_id in order[restart_index : current_index + 1]:
        folder = folders[folder_id]
        folder.pop("continuation", None)
        folder.pop("delta_link", None)
    state["folder_index"] = restart_index
    state["phase"] = "messages"
    return _encode(state)


@dataclass(frozen=True)
class _Page:
    payload: Mapping[str, Any]
    continuation: object | None


def _initial_state() -> dict[str, Any]:
    return {
        "candidates": {},
        "coverage_gaps": [],
        "folder_delta_link": None,
        "folder_index": 0,
        "folder_order": [],
        "folders": {},
        "phase": "folders",
        "v": _VERSION,
    }


def _next_round(prior: Mapping[str, Any]) -> dict[str, Any]:
    state = _initial_state()
    state["folder_delta_link"] = _optional_text(prior.get("folder_delta_link"))
    state["folders"] = {
        folder_id: {"delta_link": _optional_text(folder.get("delta_link"))}
        for folder_id, folder in _folders_copy(prior).items()
    }
    return state


def _folder_input(state: Mapping[str, Any], limit: int) -> dict[str, object]:
    delta_link = _optional_text(state.get("folder_delta_link"))
    return (
        {"delta_link": delta_link, "page_size": limit}
        if delta_link is not None
        else {"page_size": limit}
    )


def _message_input(folder_id: str, folder: Mapping[str, Any], limit: int) -> dict[str, object]:
    delta_link = _optional_text(folder.get("delta_link"))
    if delta_link is not None:
        return {"delta_link": delta_link, "folder_id": folder_id, "page_size": limit}
    return {"folder_id": folder_id, "page_size": limit}


def _result(
    state: Mapping[str, Any],
    *,
    changes: list[MicrosoftMailDeltaChange] | None = None,
    scanned: int,
    complete: bool = False,
) -> MicrosoftMailDeltaResult:
    removals: tuple[MicrosoftMailDeltaRemoval, ...] = ()
    if complete and state.get("removal_overflow") is not True:
        candidates = _candidates(state)
        removals = tuple(
            MicrosoftMailDeltaRemoval(
                folder_id=_optional_text(value.get("folder_id")) or "unknown",
                message_id=message_id,
                revision=_optional_text(value.get("revision")) or message_id,
            )
            for message_id, value in sorted(candidates.items())
        )
    return MicrosoftMailDeltaResult(
        changes=tuple(changes or ()),
        checkpoint=_encode(state),
        complete=complete,
        removals=removals,
        scanned=scanned,
        coverage_gaps=tuple(_gaps(state)),
    )


def _decode(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > MAX_CHECKPOINT_CHARS:
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    try:
        state = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("Microsoft mail delta checkpoint is invalid") from exc
    if (
        not isinstance(state, dict)
        or state.get("v") != _VERSION
        or state.get("phase")
        not in {
            "folders",
            "messages",
            "complete",
        }
    ):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    _folders(state)
    _candidates(state)
    _gaps(state)
    _folder_order(state, _folders(state))
    _nonnegative(state.get("folder_index"))
    return state


def _encode(state: Mapping[str, Any]) -> str:
    encoded = json.dumps(state, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_CHECKPOINT_CHARS:
        raise ValidationError("Microsoft mail delta checkpoint exceeds its local bound")
    return encoded


def _folders(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    current = state.get("folders")
    if not isinstance(current, dict):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    result: dict[str, dict[str, Any]] = {}
    for key, value in current.items():
        if not isinstance(key, str) or not key or len(key) > 1_024 or not isinstance(value, dict):
            raise ValidationError("Microsoft mail delta checkpoint is invalid")
        result[key] = value
    if len(result) > MAX_FOLDERS:
        raise ValidationError("Microsoft mail delta folder limit is invalid")
    if result is not current:
        state["folders"] = result
    return result


def _folders_copy(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    current = state.get("folders")
    if not isinstance(current, Mapping):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for key, value in current.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            raise ValidationError("Microsoft mail delta checkpoint is invalid")
        result[key] = value
    if len(result) > MAX_FOLDERS:
        raise ValidationError("Microsoft mail delta folder limit is invalid")
    return result


def _folder_order(state: Mapping[str, Any], folders: Mapping[str, object]) -> list[str]:
    value = state.get("folder_order")
    if not isinstance(value, list) or len(value) > MAX_FOLDERS:
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    if any(not isinstance(item, str) or item not in folders for item in value):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    if len(set(value)) != len(value):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    return value


def _candidates(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    value = state.get("candidates")
    if not isinstance(value, Mapping) or len(value) > _MAX_REMOVAL_CANDIDATES:
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 1_024 or not isinstance(item, Mapping):
            raise ValidationError("Microsoft mail delta checkpoint is invalid")
        result[key] = item
    return result


def _candidate(state: dict[str, Any], folder_id: str, message_id: str, revision: str) -> None:
    candidates = state["candidates"]
    if not isinstance(candidates, dict):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    if message_id in candidates:
        return
    if len(candidates) >= _MAX_REMOVAL_CANDIDATES:
        state["removal_overflow"] = True
        _gap(state, "Outlook removal candidate limit reached; permanent deletions were retained")
        return
    candidates[message_id] = {"folder_id": folder_id, "revision": revision}


def _gaps(state: Mapping[str, Any]) -> list[str]:
    value = state.get("coverage_gaps")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    return value


def _gap(state: dict[str, Any], detail: str) -> None:
    gaps = state["coverage_gaps"]
    if not isinstance(gaps, list):
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    if detail not in gaps and len(gaps) < 8:
        gaps.append(detail)


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("value")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("Microsoft mail delta response is invalid")
    result: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationError("Microsoft mail delta response is invalid")
        result.append({str(key): value for key, value in item.items() if isinstance(key, str)})
    return result


def _delta_link(payload: Mapping[str, Any], label: str) -> str:
    value = _optional_text(payload.get("@odata.deltaLink"))
    if value is None:
        raise ValidationError(f"{label} ended without a delta link")
    return value


def _removed(value: Mapping[str, Any]) -> bool:
    return isinstance(value.get("@removed"), Mapping)


def _identifier(value: object, label: str) -> str:
    result = _optional_text(value)
    if result is None or len(result) > 1_024:
        raise ValidationError(f"{label} has no identifier")
    return result


def _revision(value: Mapping[str, Any], fallback: str) -> str:
    for key in ("changeKey", "lastModifiedDateTime"):
        result = _optional_text(value.get(key))
        if result is not None:
            return result
    return fallback


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _nonnegative(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError("Microsoft mail delta checkpoint is invalid")
    return value

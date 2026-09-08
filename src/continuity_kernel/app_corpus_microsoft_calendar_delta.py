"""Bounded, resumable Microsoft Graph primary-calendar delta synchronization.

Graph v1 exposes event delta only for a fixed ``calendarView`` of the primary
calendar.  This module keeps that exact time window and its opaque cursor
together.  Callers must confirm a reported removal before deleting a corpus
record because a calendar-view delta can also report events that moved outside
the configured window.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from continuity_kernel.connector_runtime import AppCorpusReader
from continuity_kernel.errors import ValidationError

MAX_CHECKPOINT_CHARS: Final = 64 * 1024
_MAX_PAGE_SIZE: Final = 1_000
_VERSION: Final = 1


@dataclass(frozen=True)
class MicrosoftCalendarDeltaWindow:
    """One explicit primary-calendar time range retained with its delta cursor."""

    end: str
    start: str


@dataclass(frozen=True)
class MicrosoftCalendarDeltaChange:
    """One primary-calendar event change, including an unconfirmed removal."""

    event_id: str
    removed: bool
    value: Mapping[str, Any]


@dataclass(frozen=True)
class MicrosoftCalendarDeltaResult:
    changes: tuple[MicrosoftCalendarDeltaChange, ...]
    checkpoint: str
    complete: bool
    scanned: int


class MicrosoftCalendarDeltaSync:
    """Read one bounded primary-calendar delta page through the closed runtime."""

    def __init__(self, runtime: AppCorpusReader, *, window: MicrosoftCalendarDeltaWindow) -> None:
        self._runtime = runtime
        self._window = validate_calendar_delta_window(window.start, window.end)

    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> MicrosoftCalendarDeltaResult:
        if not isinstance(connection_id, str) or not connection_id:
            raise ValidationError("Microsoft calendar delta connection ID is invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise ValidationError("Microsoft calendar delta limit is invalid")
        state = _decode(checkpoint) if checkpoint is not None else _initial_state(self._window)
        if _window_from_state(state) != self._window:
            raise ValidationError("Microsoft calendar delta checkpoint scope changed")

        delta_link = _optional_text(state.get("delta_link"))
        input_value: dict[str, object]
        if delta_link is None:
            input_value = {
                "end": self._window.end,
                "page_size": limit,
                "start": self._window.start,
            }
        else:
            input_value = {"delta_link": delta_link, "page_size": limit}
        page = self._read(
            connection_id,
            input_value,
            continuation=state.get("continuation"),
        )
        values = _items(page.payload)
        changes = tuple(
            MicrosoftCalendarDeltaChange(
                event_id=_identifier(value.get("id"), "Outlook calendar event"),
                removed=_removed(value),
                value=value,
            )
            for value in values
        )
        if page.continuation is not None:
            state["continuation"] = page.continuation
            return MicrosoftCalendarDeltaResult(
                changes=changes,
                checkpoint=_encode(state),
                complete=False,
                scanned=len(values),
            )
        state.pop("continuation", None)
        state["delta_link"] = _delta_link(page.payload)
        return MicrosoftCalendarDeltaResult(
            changes=changes,
            checkpoint=_encode(state),
            complete=True,
            scanned=len(values),
        )

    def _read(
        self,
        connection_id: str,
        input_value: Mapping[str, object],
        *,
        continuation: object | None,
    ) -> _Page:
        response = self._runtime.call_app_corpus_read(
            "gsv_outlook_calendar_read",
            {
                "connection_id": connection_id,
                "input": dict(input_value),
                "operation": "events.delta",
            },
            continuation=continuation,
        )
        if not isinstance(response, Mapping) or response.get("status") != "ok":
            raise ValidationError("Microsoft calendar delta read did not complete")
        payload = response.get("result")
        if not isinstance(payload, Mapping):
            raise ValidationError("Microsoft calendar delta response is invalid")
        return _Page(
            {str(key): value for key, value in payload.items() if isinstance(key, str)},
            response.get("continuation"),
        )


@dataclass(frozen=True)
class _Page:
    payload: Mapping[str, Any]
    continuation: object | None


def validate_calendar_delta_window(start: object, end: object) -> MicrosoftCalendarDeltaWindow:
    """Validate the finite primary-calendar window retained in local scope state."""

    start_text = _timestamp(start)
    end_text = _timestamp(end)
    if _parse_timestamp(start_text) >= _parse_timestamp(end_text):
        raise ValidationError("Microsoft calendar delta window end must follow its start")
    return MicrosoftCalendarDeltaWindow(start=start_text, end=end_text)


def calendar_delta_checkpoint_matches_window(
    checkpoint: str | None, window: MicrosoftCalendarDeltaWindow
) -> bool:
    """Return whether a retained checkpoint is safe to replay for this exact window."""

    if checkpoint is None:
        return False
    try:
        return _window_from_state(_decode(checkpoint)) == validate_calendar_delta_window(
            window.start, window.end
        )
    except ValidationError:
        return False


def clear_calendar_delta_continuation(checkpoint: str) -> str:
    """Retry the current Graph page while retaining its fixed window and delta token."""

    state = _decode(checkpoint)
    state.pop("continuation", None)
    return _encode(state)


def _initial_state(window: MicrosoftCalendarDeltaWindow) -> dict[str, object]:
    return {"end": window.end, "start": window.start, "v": _VERSION}


def _decode(value: str) -> dict[str, object]:
    if not isinstance(value, str) or not value or len(value) > MAX_CHECKPOINT_CHARS:
        raise ValidationError("Microsoft calendar delta checkpoint is invalid")
    try:
        state = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("Microsoft calendar delta checkpoint is invalid") from exc
    if not isinstance(state, dict) or state.get("v") != _VERSION:
        raise ValidationError("Microsoft calendar delta checkpoint is invalid")
    _window_from_state(state)
    delta_link = state.get("delta_link")
    if delta_link is not None and _optional_text(delta_link) is None:
        raise ValidationError("Microsoft calendar delta checkpoint is invalid")
    return state


def _encode(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_CHECKPOINT_CHARS:
        raise ValidationError("Microsoft calendar delta checkpoint exceeds its local bound")
    return encoded


def _window_from_state(value: Mapping[str, object]) -> MicrosoftCalendarDeltaWindow:
    return validate_calendar_delta_window(value.get("start"), value.get("end"))


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValidationError("Microsoft calendar delta window timestamp is invalid")
    _parse_timestamp(value)
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Microsoft calendar delta window timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError("Microsoft calendar delta window timestamp needs a time zone")
    return parsed


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("value")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("Microsoft calendar delta collection is invalid")
    values: list[Mapping[str, Any]] = []
    for value in raw:
        if not isinstance(value, Mapping):
            raise ValidationError("Microsoft calendar delta item is invalid")
        values.append({str(key): item for key, item in value.items() if isinstance(key, str)})
    return values


def _identifier(value: object, label: str) -> str:
    identifier = _optional_text(value)
    if identifier is None:
        raise ValidationError(f"{label} identifier is invalid")
    return identifier


def _removed(value: Mapping[str, Any]) -> bool:
    return "@removed" in value


def _delta_link(payload: Mapping[str, Any]) -> str:
    value = _optional_text(payload.get("@odata.deltaLink"))
    if value is None:
        raise ValidationError("Microsoft calendar delta response has no final cursor")
    return value


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean if clean else None

"""Fail-closed, transient prepared decisions for Bridge Portfolio review.

The exact Codex hand authors this bounded envelope in its final answer.  GSV
validates and renders it, but never persists the provider body or a preparation
cache.  Durable authority remains the review-session subject refs, canonical
Task records, the append-only control queue, and per-row CAS/readback.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Final

from continuity_kernel.errors import ValidationError
from continuity_kernel.records import Task, stored_time

MAX_REVIEW_SHEET_BYTES: Final = 64 * 1024
MAX_REVIEW_SHEET_ENTRIES: Final = 25
MAX_REVIEW_SHEET_PROSE_CHARACTERS: Final = 600
MAX_REVIEW_LINE_CHARACTERS: Final = 200
MIN_REVIEW_CHOICES: Final = 2
MAX_REVIEW_CHOICES: Final = 5

_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_REQUIRED_ENTRY_KEYS: Final = frozenset(
    {"task", "anchor", "question", "recommendation", "reasoning", "choices"}
)
_OPTIONAL_ENTRY_KEYS: Final = frozenset({"dissent", "group"})
# The legacy fence is recognized only so mixed or stale model formats fail
# closed instead of leaving a second machine-looking payload in visible prose.
_ENVELOPE_FENCES: Final = frozenset({"```bridge-choices", "```bridge-sheet"})


@dataclass(frozen=True)
class ReviewChoice:
    """One complete, visible answer authored for an exact prepared outcome."""

    answer: str
    effect: str = ""
    recommended: bool = False


@dataclass(frozen=True)
class ReviewSheetEntry:
    """One transient judgment, bound to one exact Task observation."""

    task: str
    anchor: str
    question: str
    recommendation: str
    reasoning: str
    choices: tuple[ReviewChoice, ...]
    dissent: str = ""
    group: str = ""
    stale: bool = False
    current_anchor: str = ""

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["choices"] = [asdict(choice) for choice in self.choices]
        return payload


@dataclass(frozen=True)
class ReviewReply:
    """Visible prose plus an optional validated machine-readable sheet."""

    message: str | None
    sheet: tuple[ReviewSheetEntry, ...]


def parse_review_reply(answer: str | None) -> ReviewReply:
    """Strip exactly one terminal envelope and validate a prepared sheet.

    Multiple, mixed, incomplete, or nonterminal envelopes never grant Bridge
    authority.  Their visible prose remains ordinary model output and the
    machine-readable sheet is empty.
    """

    if not isinstance(answer, str) or not answer.strip():
        return ReviewReply(message=None, sheet=())
    lines = answer.splitlines()
    visible: list[str] = []
    envelopes: list[tuple[str, str]] = []
    envelope_is_terminal = False
    index = 0
    while index < len(lines):
        fence = lines[index].strip()
        if fence not in _ENVELOPE_FENCES:
            visible.append(lines[index])
            index += 1
            continue
        closing = next(
            (
                candidate
                for candidate in range(index + 1, len(lines))
                if lines[candidate].strip() == "```"
            ),
            None,
        )
        if closing is None:
            # An output cap can bisect the terminal envelope. It grants no
            # authority, and the partial JSON is not useful visible prose.
            break
        envelopes.append((fence.removeprefix("```"), "\n".join(lines[index + 1 : closing])))
        envelope_is_terminal = not any(line.strip() for line in lines[closing + 1 :])
        index = closing + 1

    message = "\n".join(visible).strip() or None
    if len(envelopes) != 1 or not envelope_is_terminal:
        return ReviewReply(message=message, sheet=())
    kind, payload = envelopes[0]
    if kind != "bridge-sheet":
        return ReviewReply(message=message, sheet=())
    return ReviewReply(message=message, sheet=_decode_review_sheet(payload))


def bind_review_sheet(
    sheet: tuple[ReviewSheetEntry, ...],
    *,
    subject_task_ids: tuple[str, ...],
    task_by_id: Mapping[str, Task],
) -> tuple[ReviewSheetEntry, ...]:
    """Bind the whole transient sheet to authored subjects and current truth."""

    if not sheet or tuple(sorted(entry.task for entry in sheet)) != tuple(sorted(subject_task_ids)):
        return ()
    bound: list[ReviewSheetEntry] = []
    for entry in sheet:
        current = task_by_id.get(entry.task)
        bound.append(
            replace(
                entry,
                stale=current is None or current.updated_at != entry.anchor,
                current_anchor=current.updated_at if current is not None else "",
            )
        )
    return tuple(bound)


def _decode_review_sheet(payload_text: str) -> tuple[ReviewSheetEntry, ...]:
    if len(payload_text.encode("utf-8")) > MAX_REVIEW_SHEET_BYTES:
        return ()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict) or set(payload) != {"v", "entries"}:
        return ()
    if isinstance(payload["v"], bool) or payload["v"] != 1:
        return ()
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= MAX_REVIEW_SHEET_ENTRIES:
        return ()
    entries: list[ReviewSheetEntry] = []
    seen: set[str] = set()
    for raw_entry in raw_entries:
        entry = _decode_review_sheet_entry(raw_entry)
        if entry is None or entry.task in seen:
            return ()
        seen.add(entry.task)
        entries.append(entry)
    return tuple(entries)


def _decode_review_sheet_entry(raw_entry: object) -> ReviewSheetEntry | None:
    if not isinstance(raw_entry, dict):
        return None
    keys = set(raw_entry)
    if not keys >= _REQUIRED_ENTRY_KEYS or not keys <= (
        _REQUIRED_ENTRY_KEYS | _OPTIONAL_ENTRY_KEYS
    ):
        return None
    task = raw_entry["task"]
    if not isinstance(task, str) or _TASK_ID.fullmatch(task) is None:
        return None
    anchor = _visible_line(raw_entry["anchor"], 100)
    question = _visible_line(raw_entry["question"])
    recommendation = _visible_line(raw_entry["recommendation"])
    reasoning = _visible_line(raw_entry["reasoning"], MAX_REVIEW_SHEET_PROSE_CHARACTERS)
    if anchor is None or question is None or recommendation is None or reasoning is None:
        return None
    try:
        anchor = stored_time(anchor, "review sheet anchor")
    except (TypeError, ValueError, ValidationError):
        return None
    dissent = _optional_visible_line(
        raw_entry.get("dissent", ""), MAX_REVIEW_SHEET_PROSE_CHARACTERS
    )
    group = _optional_visible_line(raw_entry.get("group", ""))
    choices = _decode_choices(raw_entry["choices"])
    if dissent is None or group is None or not choices:
        return None
    return ReviewSheetEntry(
        task=task,
        anchor=anchor,
        question=question,
        recommendation=recommendation,
        reasoning=reasoning,
        choices=choices,
        dissent=dissent,
        group=group,
    )


def _decode_choices(raw_choices: object) -> tuple[ReviewChoice, ...]:
    if (
        not isinstance(raw_choices, list)
        or not MIN_REVIEW_CHOICES <= len(raw_choices) <= MAX_REVIEW_CHOICES
    ):
        return ()
    choices: list[ReviewChoice] = []
    seen: set[str] = set()
    for raw_choice in raw_choices:
        choice = _decode_choice(raw_choice)
        if choice is None or choice.answer.casefold() in seen:
            return ()
        seen.add(choice.answer.casefold())
        choices.append(choice)
    if sum(choice.recommended for choice in choices) > 1:
        return ()
    return tuple(choices)


def _decode_choice(raw_choice: object) -> ReviewChoice | None:
    if isinstance(raw_choice, str):
        answer = _visible_line(raw_choice)
        return ReviewChoice(answer=answer) if answer is not None else None
    if (
        not isinstance(raw_choice, dict)
        or not set(raw_choice)
        <= {
            "answer",
            "effect",
            "recommended",
        }
        or "answer" not in raw_choice
    ):
        return None
    answer = _visible_line(raw_choice["answer"])
    effect = _optional_visible_line(raw_choice.get("effect", ""))
    recommended = raw_choice.get("recommended", False)
    if answer is None or effect is None or not isinstance(recommended, bool):
        return None
    return ReviewChoice(answer=answer, effect=effect, recommended=recommended)


def _optional_visible_line(value: object, limit: int = MAX_REVIEW_LINE_CHARACTERS) -> str | None:
    if not isinstance(value, str):
        return None
    return _visible_line(value, limit) if value.strip() else ""


def _visible_line(value: object, limit: int = MAX_REVIEW_LINE_CHARACTERS) -> str | None:
    if not isinstance(value, str):
        return None
    line = value.strip()
    if (
        not line
        or len(line) > limit
        or len(line.splitlines()) != 1
        or any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in line)
    ):
        return None
    return line

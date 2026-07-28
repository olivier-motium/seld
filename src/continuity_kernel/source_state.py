"""Content-free, portable source coverage authored by the resident AI.

Provider tools remain owned by ChatGPT apps, custom MCP servers, or audited
local readers.  This module records only the bounded delivery facts needed to
resume safely: which sources the user selected, when a read was attempted,
the horizon it covered, compact fingerprints, and a bounded failure code.
It never stores provider bodies and never decides what source content means.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, overload

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.records import format_time, parse_time, stored_time
from continuity_kernel.source_recipes import get_recipe

SOURCE_STATE_FORMAT_VERSION: Final = 1
ABSENT_SOURCE_REVISION: Final = "absent"
MAX_SOURCE_STATE_BYTES: Final = 256 * 1024
MAX_SELECTED_SOURCES: Final = 32
MAX_EVIDENCE_DIGESTS: Final = 25
MAX_TRANSIENT_BINDING_BYTES: Final = 2_048
MAX_FUTURE_SKEW: Final = timedelta(minutes=1)
SOURCE_ERROR_CODES: Final = (
    "auth_expired",
    "auth_required",
    "cloud_placeholder",
    "identity_mismatch",
    "local_path_rejected",
    "permission_denied",
    "policy_blocked",
    "provider_unavailable",
    "rate_limited",
    "read_failed",
    "secret_quarantined",
    "timeout",
    "tool_absent",
    "tool_changed",
    "tool_error",
    "unsupported_tool_shape",
)
_SOURCE_ERROR_CODE_SET: Final = frozenset(SOURCE_ERROR_CODES)

_META = re.compile(r"^<!-- seld-sources:(\{.*\}) -->$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECIPE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")


class SourceResult(StrEnum):
    SUCCESS = "success"
    EXPLICIT_EMPTY = "explicit_empty"
    FAILURE = "failure"


class SourceCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True)
class SourceObservation:
    source_id: str
    recipe_version: str
    actor_ref: str
    result: SourceResult
    attempted_at: str
    last_success_at: str | None
    covered_through: str | None
    completeness: SourceCompleteness | None
    account_fingerprint: str | None
    host_fingerprint: str | None
    tool_fingerprint: str | None
    attempted_tool_fingerprint: str | None
    cursor_digest: str | None
    evidence_digests: tuple[str, ...]
    error_code: str | None


@dataclass(frozen=True)
class SourceSnapshot:
    format_version: int
    selected_sources: tuple[str, ...]
    observations: tuple[SourceObservation, ...]
    updated_at: str | None
    revision: str

    def observation(self, source_id: str) -> SourceObservation | None:
        return next(
            (item for item in self.observations if item.source_id == source_id),
            None,
        )


def empty_source_snapshot() -> SourceSnapshot:
    return SourceSnapshot(
        format_version=SOURCE_STATE_FORMAT_VERSION,
        selected_sources=(),
        observations=(),
        updated_at=None,
        revision=ABSENT_SOURCE_REVISION,
    )


@overload
def source_fingerprint(value: str, label: str) -> str: ...


@overload
def source_fingerprint(value: None, label: str) -> None: ...


def source_fingerprint(value: str | None, label: str) -> str | None:
    """Hash one transient binding so its raw value never enters the vault."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    encoded = value.strip().encode("utf-8")
    if not encoded or len(encoded) > MAX_TRANSIENT_BINDING_BYTES or b"\x00" in encoded:
        raise ValidationError(f"{label} is empty, too large, or contains a null byte")
    return f"sha256:{sha256_bytes(encoded)}"


def select_sources(
    snapshot: SourceSnapshot,
    sources: tuple[str, ...],
    *,
    observed_at: datetime | None = None,
) -> SourceSnapshot:
    now = observed_at or datetime.now(UTC)
    selected = _source_ids(sources)
    retained = tuple(
        observation for observation in snapshot.observations if observation.source_id in selected
    )
    return _round_trip(
        SourceSnapshot(
            format_version=SOURCE_STATE_FORMAT_VERSION,
            selected_sources=selected,
            observations=retained,
            updated_at=format_time(now),
            revision="",
        ),
        observed_at=now,
    )


def record_source_observation(
    snapshot: SourceSnapshot,
    *,
    source_id: str,
    actor_ref: str,
    result: str,
    covered_through: str | None = None,
    completeness: str | None = None,
    account_fingerprint: str | None = None,
    host_fingerprint: str | None = None,
    tool_fingerprint: str | None = None,
    cursor_digest: str | None = None,
    evidence_digests: tuple[str, ...] = (),
    error_code: str | None = None,
    observed_at: datetime | None = None,
) -> SourceSnapshot:
    get_recipe(source_id)
    if source_id not in snapshot.selected_sources:
        raise ConflictError("source is not selected; reload source state before recording a read")
    actor_fingerprint = source_fingerprint(actor_ref, "actor reference")
    if actor_fingerprint is None:  # pragma: no cover - actor_ref is required text
        raise ValidationError("source observation actor_ref is invalid")
    try:
        parsed_result = SourceResult(result)
    except ValueError as exc:
        raise ValidationError("source observation result is invalid") from exc
    now = observed_at or datetime.now(UTC)
    attempted_at = format_time(now)
    prior = snapshot.observation(source_id)

    parsed_account = _optional_digest(account_fingerprint, "account fingerprint")
    parsed_host = _optional_digest(host_fingerprint, "host fingerprint")
    parsed_tool = _optional_digest(tool_fingerprint, "tool fingerprint")
    parsed_cursor = _optional_digest(cursor_digest, "cursor digest")
    parsed_evidence = _digests(evidence_digests)
    parsed_error = _optional_error_code(error_code)

    if parsed_result is SourceResult.FAILURE:
        if parsed_error is None:
            raise ValidationError("failed source observation requires error_code")
        if (
            any(value is not None for value in (covered_through, completeness, cursor_digest))
            or parsed_evidence
        ):
            raise ValidationError(
                "failed source observation cannot claim coverage, a cursor, or evidence"
            )
        if (
            prior
            and prior.account_fingerprint
            and parsed_account
            and parsed_account != prior.account_fingerprint
        ):
            raise ConflictError(
                "source account binding changed; deselect and explicitly select it again"
            )
        observation = SourceObservation(
            source_id=source_id,
            recipe_version=(
                prior.recipe_version if prior else get_recipe(source_id).recipe_version
            ),
            actor_ref=actor_fingerprint,
            result=parsed_result,
            attempted_at=attempted_at,
            last_success_at=prior.last_success_at if prior else None,
            covered_through=prior.covered_through if prior else None,
            completeness=prior.completeness if prior else None,
            account_fingerprint=(prior.account_fingerprint if prior else None),
            host_fingerprint=(prior.host_fingerprint if prior else None),
            tool_fingerprint=(prior.tool_fingerprint if prior else None),
            attempted_tool_fingerprint=parsed_tool,
            cursor_digest=prior.cursor_digest if prior else None,
            evidence_digests=prior.evidence_digests if prior else (),
            error_code=parsed_error,
        )
    else:
        recipe = get_recipe(source_id)
        if parsed_host is None:
            raise ValidationError("successful source observation requires host_fingerprint")
        if parsed_tool is None:
            raise ValidationError("successful source observation requires tool_fingerprint")
        if recipe.identity_capability is not None and parsed_account is None:
            raise ValidationError("successful source observation requires account_fingerprint")
        if covered_through is None:
            raise ValidationError("successful source observation requires covered_through")
        parsed_horizon = stored_time(covered_through, "source covered-through time")
        if parse_time(parsed_horizon) > now.astimezone(UTC) + recipe.max_future_coverage:
            raise ValidationError("source covered-through time is implausibly in the future")
        try:
            parsed_completeness = SourceCompleteness(str(completeness))
        except ValueError as exc:
            raise ValidationError("successful source observation requires completeness") from exc
        if prior and prior.account_fingerprint and parsed_account != prior.account_fingerprint:
            raise ConflictError(
                "source account binding changed; deselect and explicitly select it again"
            )
        if prior and prior.covered_through:
            prior_horizon = parse_time(prior.covered_through)
            next_horizon = parse_time(parsed_horizon)
            if prior_horizon > next_horizon:
                raise ConflictError(
                    "source coverage would regress; reload source state before recording the read"
                )
            if (
                prior_horizon == next_horizon
                and prior.completeness is SourceCompleteness.COMPLETE
                and parsed_completeness is SourceCompleteness.PARTIAL
            ):
                raise ConflictError(
                    "source coverage completeness would regress; reload before recording the read"
                )
        observation = SourceObservation(
            source_id=source_id,
            recipe_version=recipe.recipe_version,
            actor_ref=actor_fingerprint,
            result=parsed_result,
            attempted_at=attempted_at,
            last_success_at=attempted_at,
            covered_through=parsed_horizon,
            completeness=parsed_completeness,
            account_fingerprint=parsed_account,
            host_fingerprint=parsed_host,
            tool_fingerprint=parsed_tool,
            attempted_tool_fingerprint=parsed_tool,
            cursor_digest=parsed_cursor,
            evidence_digests=parsed_evidence,
            error_code=None,
        )

    observations = {
        item.source_id: item
        for item in snapshot.observations
        if item.source_id in snapshot.selected_sources
    }
    observations[source_id] = observation
    return _round_trip(
        SourceSnapshot(
            format_version=SOURCE_STATE_FORMAT_VERSION,
            selected_sources=snapshot.selected_sources,
            observations=tuple(observations[source] for source in sorted(observations)),
            updated_at=attempted_at,
            revision="",
        ),
        observed_at=now,
    )


def source_snapshot_dict(
    snapshot: SourceSnapshot,
    *,
    current_host_fingerprint: str | None = None,
    current_tool_fingerprints: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    now = observed_at or datetime.now(UTC)
    by_source = {item.source_id: item for item in snapshot.observations}
    sources: list[dict[str, Any]] = []
    for source_id in snapshot.selected_sources:
        observation = by_source.get(source_id)
        current_tool_fingerprint = (current_tool_fingerprints or {}).get(source_id)
        freshness = "unread"
        if observation and observation.last_success_at:
            recipe = get_recipe(source_id)
            if (
                observation.recipe_version != recipe.recipe_version
                or current_host_fingerprint is None
                or observation.host_fingerprint != current_host_fingerprint
                or (
                    current_tool_fingerprint is not None
                    and observation.tool_fingerprint != current_tool_fingerprint
                )
            ):
                freshness = "needs_revalidation"
            else:
                try:
                    # A future-looking calendar window describes what was read; it
                    # does not make that read fresh until the end of the window.
                    # Freshness always expires from the successful observation.
                    expires_at = parse_time(observation.last_success_at) + recipe.proof_ttl
                except OverflowError:
                    freshness = "needs_revalidation"
                else:
                    freshness = "current" if now.astimezone(UTC) <= expires_at else "stale"
        sources.append(
            {
                "freshness": freshness,
                "observation": _observation_dict(observation) if observation else None,
                "recipe": get_recipe(source_id).to_dict(),
                "selected": True,
            }
        )
    return {
        "format_version": snapshot.format_version,
        "revision": snapshot.revision,
        "selected_count": len(snapshot.selected_sources),
        "sources": sources,
        "updated_at": snapshot.updated_at,
    }


def render_source_snapshot(snapshot: SourceSnapshot) -> str:
    payload = {
        "format_version": snapshot.format_version,
        "observations": [_observation_dict(item) for item in snapshot.observations],
        "selected_sources": list(snapshot.selected_sources),
        "updated_at": snapshot.updated_at,
    }
    meta = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    lines = [f"<!-- seld-sources:{meta} -->", "# Sources", ""]
    if not snapshot.selected_sources:
        lines.append("No sources selected.")
    for source_id in snapshot.selected_sources:
        recipe = get_recipe(source_id)
        observation = snapshot.observation(source_id)
        lines.extend([f"## {recipe.label}", "", f"- Source: `{source_id}`"])
        if observation is None:
            lines.append("- Coverage: not read yet")
        else:
            lines.extend(
                [
                    f"- Last attempt: {observation.attempted_at}",
                    f"- Result: {observation.result.value}",
                    f"- Recipe version: {observation.recipe_version}",
                    f"- Covered through: {observation.covered_through or 'not available'}",
                    f"- Completeness: "
                    f"{observation.completeness.value if observation.completeness else 'unknown'}",
                    f"- Last error: {observation.error_code or 'none'}",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_source_snapshot(
    encoded: bytes,
    *,
    observed_at: datetime | None = None,
) -> SourceSnapshot:
    if len(encoded) > MAX_SOURCE_STATE_BYTES:
        raise ValidationError("source state exceeds its size bound")
    try:
        text = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("source state is not UTF-8") from exc
    first_line = text.splitlines()[0] if text.splitlines() else ""
    matched = _META.fullmatch(first_line)
    if matched is None:
        raise ValidationError("source state metadata is missing or malformed")
    try:
        payload = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("source state metadata is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "observations",
        "selected_sources",
        "updated_at",
    }:
        raise ValidationError("source state has an unsupported shape")
    if payload["format_version"] != SOURCE_STATE_FORMAT_VERSION:
        raise ValidationError("source state has an unsupported version")
    selected_raw = payload["selected_sources"]
    if not isinstance(selected_raw, list) or not all(
        isinstance(item, str) for item in selected_raw
    ):
        raise ValidationError("source state selected_sources must be strings")
    selected = _source_ids(tuple(selected_raw))
    updated_at = payload["updated_at"]
    if not isinstance(updated_at, str):
        raise ValidationError("source state updated_at is invalid")
    updated_at = stored_time(updated_at, "source state updated-at time")
    raw_observations = payload["observations"]
    if not isinstance(raw_observations, list):
        raise ValidationError("source state observations must be an array")
    observations = tuple(_parse_observation(item) for item in raw_observations)
    if tuple(sorted(item.source_id for item in observations)) != tuple(
        item.source_id for item in observations
    ):
        raise ValidationError("source observations must be sorted")
    if len({item.source_id for item in observations}) != len(observations):
        raise ValidationError("source observations contain a duplicate")
    if any(item.source_id not in selected for item in observations):
        raise ValidationError("source observation exists for an unselected source")
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    if parse_time(updated_at) > now + MAX_FUTURE_SKEW:
        raise ValidationError("source state updated_at is implausibly in the future")
    for observation in observations:
        _validate_observation_timeline(observation, updated_at=updated_at, now=now)
    snapshot = SourceSnapshot(
        format_version=SOURCE_STATE_FORMAT_VERSION,
        selected_sources=selected,
        observations=observations,
        updated_at=updated_at,
        revision=sha256_bytes(encoded),
    )
    if render_source_snapshot(snapshot).encode("utf-8") != encoded:
        raise ValidationError("source state is not in canonical form")
    return snapshot


def _round_trip(
    snapshot: SourceSnapshot,
    *,
    observed_at: datetime | None = None,
) -> SourceSnapshot:
    return parse_source_snapshot(
        render_source_snapshot(snapshot).encode("utf-8"),
        observed_at=observed_at,
    )


def _source_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > MAX_SELECTED_SOURCES:
        raise ValidationError("too many selected sources")
    selected = tuple(sorted(set(values)))
    if len(selected) != len(values):
        raise ValidationError("selected sources contain a duplicate")
    for source_id in selected:
        get_recipe(source_id)
    return selected


def _parse_observation(value: object) -> SourceObservation:
    if not isinstance(value, dict) or set(value) != {
        "account_fingerprint",
        "actor_ref",
        "attempted_tool_fingerprint",
        "attempted_at",
        "completeness",
        "covered_through",
        "cursor_digest",
        "error_code",
        "evidence_digests",
        "host_fingerprint",
        "last_success_at",
        "recipe_version",
        "result",
        "source_id",
        "tool_fingerprint",
    }:
        raise ValidationError("source observation has an unsupported shape")
    source_id = value["source_id"]
    recipe_version = value["recipe_version"]
    actor_ref = value["actor_ref"]
    if not isinstance(source_id, str):
        raise ValidationError("source observation source_id is invalid")
    get_recipe(source_id)
    if not isinstance(recipe_version, str) or _RECIPE_VERSION.fullmatch(recipe_version) is None:
        raise ValidationError("source observation recipe_version is invalid")
    actor_fingerprint = _optional_digest(actor_ref, "actor fingerprint")
    if actor_fingerprint is None:
        raise ValidationError("source observation actor_ref is invalid")
    try:
        result = SourceResult(value["result"])
    except (ValueError, TypeError) as exc:
        raise ValidationError("source observation result is invalid") from exc
    attempted_at = _stored_optional_time(value["attempted_at"], "source attempt", required=True)
    last_success_at = _stored_optional_time(value["last_success_at"], "source success")
    covered_through = _stored_optional_time(value["covered_through"], "source coverage")
    raw_completeness = value["completeness"]
    try:
        completeness = (
            SourceCompleteness(raw_completeness) if raw_completeness is not None else None
        )
    except (ValueError, TypeError) as exc:
        raise ValidationError("source observation completeness is invalid") from exc
    digests = value["evidence_digests"]
    if not isinstance(digests, list) or not all(isinstance(item, str) for item in digests):
        raise ValidationError("source observation evidence_digests are invalid")
    observation = SourceObservation(
        source_id=source_id,
        recipe_version=recipe_version,
        actor_ref=actor_fingerprint,
        result=result,
        attempted_at=attempted_at,
        last_success_at=last_success_at,
        covered_through=covered_through,
        completeness=completeness,
        account_fingerprint=_optional_digest(value["account_fingerprint"], "account fingerprint"),
        host_fingerprint=_optional_digest(value["host_fingerprint"], "host fingerprint"),
        tool_fingerprint=_optional_digest(value["tool_fingerprint"], "tool fingerprint"),
        attempted_tool_fingerprint=_optional_digest(
            value["attempted_tool_fingerprint"], "attempted tool fingerprint"
        ),
        cursor_digest=_optional_digest(value["cursor_digest"], "cursor digest"),
        evidence_digests=_digests(tuple(digests)),
        error_code=_optional_error_code(value["error_code"]),
    )
    _validate_observation_state(observation)
    return observation


def _validate_observation_state(observation: SourceObservation) -> None:
    if observation.result is SourceResult.FAILURE:
        if observation.error_code is None:
            raise ValidationError("failed source observation is missing error_code")
        recipe = get_recipe(observation.source_id)
        success_fields = (
            observation.covered_through,
            observation.completeness,
            observation.host_fingerprint,
            observation.tool_fingerprint,
        )
        if observation.last_success_at is None:
            if (
                any(value is not None for value in success_fields)
                or observation.account_fingerprint is not None
                or observation.evidence_digests
            ):
                raise ValidationError("failed source observation invents prior success state")
            if observation.cursor_digest is not None:
                raise ValidationError("failed source observation invents a prior cursor")
        else:
            if any(value is None for value in success_fields):
                raise ValidationError(
                    "failed source observation has incomplete prior success state"
                )
            if recipe.identity_capability is not None and observation.account_fingerprint is None:
                raise ValidationError("failed source observation lost prior account binding")
        return
    recipe = get_recipe(observation.source_id)
    if (
        observation.last_success_at is None
        or observation.covered_through is None
        or observation.completeness is None
        or observation.host_fingerprint is None
        or observation.tool_fingerprint is None
        or observation.attempted_tool_fingerprint != observation.tool_fingerprint
        or observation.error_code is not None
    ):
        raise ValidationError("successful source observation is incomplete")
    if recipe.identity_capability is not None and observation.account_fingerprint is None:
        raise ValidationError("successful source observation is missing account binding")


def _validate_observation_timeline(
    observation: SourceObservation,
    *,
    updated_at: str,
    now: datetime,
) -> None:
    attempted = parse_time(observation.attempted_at)
    if attempted > now + MAX_FUTURE_SKEW:
        raise ValidationError("source attempt time is implausibly in the future")
    if attempted > parse_time(updated_at):
        raise ValidationError("source state updated_at precedes a source attempt")
    if observation.last_success_at is None:
        return
    succeeded = parse_time(observation.last_success_at)
    if succeeded > attempted:
        raise ValidationError("source success time follows the latest attempt")
    if observation.result is not SourceResult.FAILURE and succeeded != attempted:
        raise ValidationError("successful source observation has mismatched attempt time")
    if observation.covered_through is None:
        raise ValidationError("source success is missing a coverage horizon")
    coverage = parse_time(observation.covered_through)
    if coverage > succeeded + get_recipe(observation.source_id).max_future_coverage:
        raise ValidationError("source coverage horizon is implausibly in the future")


def _observation_dict(observation: SourceObservation | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    payload = asdict(observation)
    payload["result"] = observation.result.value
    payload["completeness"] = observation.completeness.value if observation.completeness else None
    payload["evidence_digests"] = list(observation.evidence_digests)
    return payload


def _optional_digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a sha256 digest")
    return value


def _digests(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > MAX_EVIDENCE_DIGESTS:
        raise ValidationError("too many source evidence digests")
    if len(set(values)) != len(values):
        raise ValidationError("source evidence digests contain a duplicate")
    for value in values:
        _optional_digest(value, "source evidence digest")
    return tuple(sorted(values))


def _optional_error_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _SOURCE_ERROR_CODE_SET:
        raise ValidationError("source error_code is not a supported classification")
    return value


@overload
def _stored_optional_time(value: object, label: str, *, required: Literal[True]) -> str: ...


@overload
def _stored_optional_time(
    value: object, label: str, *, required: Literal[False] = False
) -> str | None: ...


def _stored_optional_time(value: object, label: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} time is invalid")
    return stored_time(value, f"{label} time")

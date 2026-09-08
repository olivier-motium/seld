"""Durable, bounded source reports awaiting Pulse judgment.

Reports retain one derived source claim and its coverage evidence.  They are
not Resident Signals: deciding or delivering a report never acknowledges a
signal or changes a task.  The caller can acknowledge a signal only after it
has durably recorded the result that justified that acknowledgement.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.records import format_time, stored_time

PULSE_REPORT_FORMAT_VERSION: Final = 1
MAX_PULSE_REPORT_BYTES: Final = 32 * 1024
MAX_PULSE_REPORTS: Final = 10_000
MAX_PULSE_REPORT_RESULTS: Final = 1_000
MAX_REPORT_TEXT: Final = 2_000
MAX_REASON_TEXT: Final = 1_000
MAX_REPORT_REFS: Final = 20

_META = re.compile(r"^<!-- gsv-pulse-report:(\{.*\}) -->$")
_REPORT_FILE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.md$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_KEY = re.compile(r"^pulse-report:[0-9a-f]{64}$")
_CAUSAL_KEY = re.compile(r"^[a-z][a-z0-9-]{0,63}:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NATIVE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}@[0-9a-f]{64}$")

PulseDecision = Literal["discard", "retain", "investigate", "wake"]
PulsePendingStage = Literal["relevance", "delivery", "investigation"]
PulseReportListStage = Literal["relevance", "delivery", "investigation", "recent"]
_DECISIONS: Final = frozenset({"discard", "retain", "investigate", "wake"})
_COMPLETENESS: Final = frozenset({"complete", "partial", "unavailable"})


@dataclass(frozen=True)
class RelevanceDecision:
    """One explicit Pulse judgment about the relevance of a source report."""

    decision: PulseDecision
    reason: str
    target_refs: tuple[str, ...]
    decided_at: str


@dataclass(frozen=True)
class DeliveryDisposition:
    """The durable result of a wake after Pulse made a wake decision."""

    result_ref: str
    delivered_at: str


@dataclass(frozen=True)
class PulseReportHistory:
    """A small, append-only record of the report's meaningful transitions."""

    kind: Literal["created", "decided", "delivered"]
    recorded_at: str


@dataclass(frozen=True)
class PulseReport:
    """A Markdown-canonical, durable derived report with explicit disposition."""

    identifier: str
    event_key: str
    claim: str
    uncertainty: str
    source_id: str
    observed_at: str
    coverage_ref: str
    coverage_revision: str
    completeness: str
    evidence_refs: tuple[str, ...]
    causal_key: str | None
    created_at: str
    decision: RelevanceDecision | None
    delivery: DeliveryDisposition | None
    history: tuple[PulseReportHistory, ...]
    revision: str

    @property
    def report_ref(self) -> str:
        """The safe native reference callers may use as a result pointer."""

        return f"source-report:{self.identifier}@{self.revision}"

    @property
    def report_id(self) -> str:
        """Compatibility name for native record resolvers that use report_id."""

        return self.identifier


@dataclass(frozen=True)
class PulseReportList:
    reports: tuple[PulseReport, ...]
    stage: PulseReportListStage
    remaining: int


class PulseReportStore:
    """Store Pulse reports inside one vault with one report-level CAS boundary."""

    def __init__(self, vault_root: Path | str):
        expanded = Path(vault_root).expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        # Preserve the lexical root until PinnedPathRoot can reject a replaced
        # or symlinked entry instead of resolving it to an uncontrolled target.
        self.vault_root = Path(os.path.abspath(expanded))
        self.root = self.vault_root / ".gsv/pulse-reports"

    @property
    def lock_path(self) -> Path:
        return self.vault_root / ".gsv/locks/pulse-reports.lock"

    def append(
        self,
        *,
        event_key: str,
        claim: str,
        uncertainty: str,
        source_id: str,
        observed_at: datetime | str,
        coverage_ref: str,
        coverage_revision: str,
        completeness: str,
        evidence_refs: Sequence[str] = (),
        causal_key: str | None = None,
        created_at: datetime | None = None,
    ) -> PulseReport:
        """Create one report or return its exact event-key replay.

        An event key is opaque, content-derived, and never a provider object
        identifier.  Replaying it with any different source report rejects the
        write rather than replacing its meaning.
        """

        candidate = _new_report(
            event_key=event_key,
            claim=claim,
            uncertainty=uncertainty,
            source_id=source_id,
            observed_at=observed_at,
            coverage_ref=coverage_ref,
            coverage_revision=coverage_revision,
            completeness=completeness,
            evidence_refs=evidence_refs,
            causal_key=causal_key,
            created_at=created_at,
        )
        with self._transaction() as store:
            reports = self._all_reports(store)
            replay = next((item for item in reports if item.event_key == candidate.event_key), None)
            if replay is not None:
                if _immutable_report_fields(replay) != _immutable_report_fields(candidate):
                    raise ConflictError(
                        "pulse report event key already has different source content"
                    )
                return replay

            for _ in range(3):
                identifier = str(uuid.uuid4())
                report = replace(candidate, identifier=identifier)
                encoded = render_pulse_report(report).encode("utf-8")
                path = self._report_path(identifier)
                if self._read_optional(store, path, label="pulse report") is not None:
                    continue
                self._write_exact(
                    store,
                    path,
                    expected=None,
                    replacement=encoded,
                    label="pulse report",
                )
                return parse_pulse_report(encoded)
            raise PersistenceError("could not allocate a unique pulse report identifier")

    def show(self, report_id: str) -> PulseReport:
        """Return one exact report revision from its UUID."""

        identifier = _uuid(report_id, "pulse report ID")
        with self._transaction() as store:
            report = self._read_report(store, identifier)
            if report is None:
                raise NotFoundError(f"pulse report does not exist: {identifier}")
            return report

    def list_pending(
        self,
        *,
        stage: PulsePendingStage = "relevance",
        limit: int = 100,
    ) -> PulseReportList:
        """List the bounded relevance, wake-delivery, or investigation work pending."""

        if stage not in {"relevance", "delivery", "investigation"}:
            raise ValidationError(
                "pulse report pending stage must be relevance, delivery, or investigation"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PULSE_REPORT_RESULTS
        ):
            raise ValidationError("pulse report limit must be between 1 and 1000")
        with self._transaction() as store:
            reports = self._all_reports(store)
        pending = tuple(
            report
            for report in reports
            if (
                report.decision is None
                if stage == "relevance"
                else report.decision is not None
                and report.delivery is None
                and (
                    report.decision.decision == "wake"
                    if stage == "delivery"
                    else report.decision.decision == "investigate"
                )
            )
        )
        return PulseReportList(
            reports=pending[:limit], stage=stage, remaining=len(pending) - len(pending[:limit])
        )

    def recent(
        self,
        *,
        source_id: str | None = None,
        limit: int = 5,
    ) -> PulseReportList:
        """Return only a small newest-first report context, optionally for one source."""

        if source_id is not None:
            source_id = _source_id(source_id)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PULSE_REPORT_RESULTS
        ):
            raise ValidationError("pulse report limit must be between 1 and 1000")
        with self._transaction() as store:
            reports = self._all_reports(store)
        matching = tuple(
            sorted(
                (item for item in reports if source_id is None or item.source_id == source_id),
                key=lambda item: (item.created_at, item.identifier),
                reverse=True,
            )
        )
        return PulseReportList(
            reports=matching[:limit],
            stage="recent",
            remaining=len(matching) - len(matching[:limit]),
        )

    def decide(
        self,
        report_id: str,
        *,
        expected_revision: str,
        decision: PulseDecision,
        reason: str,
        target_refs: Sequence[str] = (),
        decided_at: datetime | None = None,
    ) -> PulseReport:
        """Record one relevance decision without affecting tasks or signals."""

        identifier = _uuid(report_id, "pulse report ID")
        expected = _revision(expected_revision, "expected pulse report revision")
        recorded = RelevanceDecision(
            decision=_decision(decision),
            reason=_text(reason, "pulse report decision reason", MAX_REASON_TEXT),
            target_refs=_refs(target_refs, "pulse report target reference"),
            decided_at=format_time(decided_at or datetime.now(UTC)),
        )
        with self._transaction() as store:
            report, before = self._required_report_bytes(store, identifier, retain=True)
            if report.revision != expected:
                raise ConflictError("pulse report changed; reload it before deciding")
            if report.delivery is not None:
                raise ConflictError("pulse report was already delivered")
            if report.decision is not None:
                if report.decision == recorded:
                    return report
                raise ConflictError("pulse report already has a different relevance decision")
            after = replace(
                report,
                decision=recorded,
                history=(*report.history, PulseReportHistory("decided", recorded.decided_at)),
                revision="",
            )
            encoded = render_pulse_report(after).encode("utf-8")
            self._write_exact(
                store,
                self._report_path(identifier),
                expected=before,
                replacement=encoded,
                label="pulse report",
            )
            return parse_pulse_report(encoded)

    def record_delivery(
        self,
        report_id: str,
        *,
        expected_revision: str,
        result_ref: str,
        delivered_at: datetime | None = None,
    ) -> PulseReport:
        """Record a completed wake result before any caller acknowledges input."""

        identifier = _uuid(report_id, "pulse report ID")
        expected = _revision(expected_revision, "expected pulse report revision")
        delivery = DeliveryDisposition(
            result_ref=_native_ref(result_ref, "pulse report delivery result reference"),
            delivered_at=format_time(delivered_at or datetime.now(UTC)),
        )
        with self._transaction() as store:
            report, before = self._required_report_bytes(store, identifier, retain=True)
            if report.revision != expected:
                raise ConflictError("pulse report changed; reload it before recording delivery")
            if report.decision is None or report.decision.decision not in {"wake", "investigate"}:
                raise ValidationError(
                    "pulse report delivery requires an existing wake or investigate decision"
                )
            if report.decision.decision == "investigate" and not delivery.result_ref.startswith(
                "source-report:"
            ):
                raise ValidationError(
                    "pulse report investigation delivery requires a child source-report reference"
                )
            if report.delivery is not None:
                if report.delivery == delivery:
                    return report
                raise ConflictError("pulse report already has a different delivery result")
            after = replace(
                report,
                delivery=delivery,
                history=(*report.history, PulseReportHistory("delivered", delivery.delivered_at)),
                revision="",
            )
            encoded = render_pulse_report(after).encode("utf-8")
            self._write_exact(
                store,
                self._report_path(identifier),
                expected=before,
                replacement=encoded,
                label="pulse report",
            )
            return parse_pulse_report(encoded)

    @contextmanager
    def _transaction(self) -> Iterator[PinnedPathRoot | None]:
        """Hold a rooted lock while reading or changing report records."""

        store: PinnedPathRoot | None = None
        try:
            if not PINNED_PATH_ROOT_SUPPORTED:
                self.root.mkdir(parents=True, exist_ok=True)
                with exclusive_lock(self.lock_path):
                    yield None
                return
            store = PinnedPathRoot(self.vault_root)
            with (
                store.watch_directory(".gsv", create=True),
                store.watch_directory(".gsv/locks", create=True),
                store.bind_directory(".gsv/pulse-reports", create=True),
                store.exclusive_file_lock(".gsv/locks/pulse-reports.lock"),
            ):
                yield store
        finally:
            if store is not None:
                store.close()

    def _all_reports(self, store: PinnedPathRoot | None) -> tuple[PulseReport, ...]:
        reports: list[PulseReport] = []
        for identifier in self._report_ids(store):
            report = self._read_report(store, identifier)
            if report is None:
                raise ConflictError("pulse report disappeared while the store was locked")
            reports.append(report)
        return tuple(sorted(reports, key=lambda item: (item.created_at, item.identifier)))

    def _report_ids(self, store: PinnedPathRoot | None) -> tuple[str, ...]:
        if store is not None:
            names = store.list_directory_entry_names(
                ".gsv/pulse-reports",
                max_entries=MAX_PULSE_REPORTS + 100,
            )
        else:
            try:
                names = tuple(entry.name for entry in self.root.iterdir())
            except OSError as exc:
                raise PersistenceError(f"could not list pulse reports: {exc}") from exc
            if len(names) > MAX_PULSE_REPORTS + 100:
                raise ValidationError("pulse report directory exceeds its supported bound")
        identifiers: list[str] = []
        for name in names:
            if name.startswith("."):
                continue
            matched = _REPORT_FILE.fullmatch(name)
            if matched is None:
                raise ValidationError("pulse report directory contains an unsupported record")
            identifiers.append(matched.group(1))
        if len(identifiers) > MAX_PULSE_REPORTS:
            raise ValidationError("pulse report store reached its bounded record limit")
        return tuple(sorted(identifiers))

    def _report_path(self, identifier: str) -> Path:
        return self.root / f"{identifier}.md"

    def _read_report(
        self,
        store: PinnedPathRoot | None,
        identifier: str,
        *,
        retain: bool = False,
    ) -> PulseReport | None:
        encoded = self._read_optional(
            store,
            self._report_path(identifier),
            label="pulse report",
            retain=retain,
        )
        if encoded is None:
            return None
        report = parse_pulse_report(encoded)
        if report.identifier != identifier:
            raise ValidationError("pulse report file name does not match its record ID")
        return report

    def _required_report_bytes(
        self,
        store: PinnedPathRoot | None,
        identifier: str,
        *,
        retain: bool,
    ) -> tuple[PulseReport, bytes]:
        path = self._report_path(identifier)
        encoded = self._read_optional(store, path, label="pulse report", retain=retain)
        if encoded is None:
            raise NotFoundError(f"pulse report does not exist: {identifier}")
        report = parse_pulse_report(encoded)
        if report.identifier != identifier:
            raise ValidationError("pulse report file name does not match its record ID")
        return report, encoded

    def _read_optional(
        self,
        store: PinnedPathRoot | None,
        path: Path,
        *,
        label: str,
        retain: bool = False,
    ) -> bytes | None:
        if store is not None:
            return store.read_regular_file(
                path.relative_to(self.vault_root),
                label=label,
                max_bytes=MAX_PULSE_REPORT_BYTES,
                missing_ok=True,
                retain=retain,
            )
        if not os.path.lexists(path):
            return None
        return read_regular_file(path, label=label, max_bytes=MAX_PULSE_REPORT_BYTES)

    def _write_exact(
        self,
        store: PinnedPathRoot | None,
        path: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
    ) -> None:
        if len(replacement) > MAX_PULSE_REPORT_BYTES:
            raise ValidationError("pulse report exceeds its size bound")
        if store is None:
            current = self._read_optional(None, path, label=label)
            if current != expected:
                raise ConflictError("pulse report changed before publication")
            atomic_write(path, replacement)
            return
        try:
            store.exchange_regular_file_if_exact(
                path.relative_to(self.vault_root),
                expected=expected,
                replacement=replacement,
                label=label,
                max_bytes=MAX_PULSE_REPORT_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is PublishOutcome.UNPUBLISHED:
                raise PersistenceError("pulse report change was not published") from exc
            if exc.outcome is PublishOutcome.COMMITTED:
                raise MutationCommittedError(
                    "pulse report change committed, but cleanup is unconfirmed"
                ) from exc
            raise DegradedIntegrityError(
                "pulse report storage has an unknown publication state; run gsv doctor"
            ) from exc


def pulse_report_dict(value: PulseReport | PulseReportList) -> dict[str, Any]:
    """Return one API-ready representation without leaking any external payload."""

    return asdict(value)


def render_pulse_report(report: PulseReport) -> str:
    """Render the one canonical Markdown record for a Pulse report."""

    _validate_report(report)
    metadata: dict[str, object] = {
        "causal_key": report.causal_key,
        "claim_sha256": sha256_bytes(report.claim.encode("utf-8")),
        "completeness": report.completeness,
        "coverage_ref": report.coverage_ref,
        "coverage_revision": report.coverage_revision,
        "created_at": report.created_at,
        "decision": _decision_dict(report.decision),
        "delivery": _delivery_dict(report.delivery),
        "evidence_refs": list(report.evidence_refs),
        "event_key": report.event_key,
        "history": [_history_dict(item) for item in report.history],
        "id": report.identifier,
        "observed_at": report.observed_at,
        "source_id": report.source_id,
        "uncertainty_sha256": sha256_bytes(report.uncertainty.encode("utf-8")),
        "version": PULSE_REPORT_FORMAT_VERSION,
    }
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    rendered = (
        f"<!-- gsv-pulse-report:{header} -->\n\n"
        "# Pulse report\n\n"
        f"## Claim\n{report.claim}\n\n"
        f"## Uncertainty\n{report.uncertainty}\n"
    )
    if len(rendered.encode("utf-8")) > MAX_PULSE_REPORT_BYTES:
        raise ValidationError("pulse report exceeds its size bound")
    return rendered


def parse_pulse_report(encoded: bytes) -> PulseReport:
    """Parse and canonicalize a report from its Markdown source of truth."""

    if len(encoded) > MAX_PULSE_REPORT_BYTES:
        raise ValidationError("pulse report exceeds its size bound")
    try:
        markdown = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("pulse report is not UTF-8") from exc
    lines = markdown.splitlines()
    if not lines:
        raise ValidationError("pulse report is empty")
    matched = _META.fullmatch(lines[0])
    if matched is None:
        raise ValidationError("pulse report metadata is missing or malformed")
    try:
        metadata = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("pulse report metadata is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise ValidationError("pulse report metadata must be an object")
    expected = {
        "causal_key",
        "claim_sha256",
        "completeness",
        "coverage_ref",
        "coverage_revision",
        "created_at",
        "decision",
        "delivery",
        "evidence_refs",
        "event_key",
        "history",
        "id",
        "observed_at",
        "source_id",
        "uncertainty_sha256",
        "version",
    }
    if set(metadata) != expected or metadata.get("version") != PULSE_REPORT_FORMAT_VERSION:
        raise ValidationError("pulse report metadata has an unsupported shape")
    prefix = markdown.split("\n", 1)[0] + "\n\n# Pulse report\n\n## Claim\n"
    if not markdown.startswith(prefix):
        raise ValidationError("pulse report sections are malformed")
    claim_and_tail = markdown[len(prefix) :]
    claim, separator, uncertainty_tail = claim_and_tail.partition("\n\n## Uncertainty\n")
    if not separator or not uncertainty_tail.endswith("\n"):
        raise ValidationError("pulse report sections are malformed")
    uncertainty = uncertainty_tail[:-1]
    report = PulseReport(
        identifier=_uuid(metadata.get("id"), "pulse report ID"),
        event_key=_event_key(metadata.get("event_key")),
        claim=_text(claim, "pulse report claim", MAX_REPORT_TEXT),
        uncertainty=_text(uncertainty, "pulse report uncertainty", MAX_REPORT_TEXT),
        source_id=_source_id(metadata.get("source_id")),
        observed_at=_stored_time(metadata.get("observed_at"), "pulse report observation time"),
        coverage_ref=_coverage_ref(metadata.get("coverage_ref"), metadata.get("source_id")),
        coverage_revision=_revision(
            metadata.get("coverage_revision"), "pulse report coverage revision"
        ),
        completeness=_completeness(metadata.get("completeness")),
        evidence_refs=_refs(
            _string_sequence(metadata.get("evidence_refs"), "pulse report evidence references"),
            "pulse report evidence reference",
        ),
        causal_key=_optional_causal_key(metadata.get("causal_key")),
        created_at=_stored_time(metadata.get("created_at"), "pulse report creation time"),
        decision=_parse_decision(metadata.get("decision")),
        delivery=_parse_delivery(metadata.get("delivery")),
        history=_parse_history(metadata.get("history")),
        revision=sha256_bytes(encoded),
    )
    _validate_report(report)
    if metadata.get("claim_sha256") != sha256_bytes(report.claim.encode("utf-8")):
        raise ValidationError("pulse report claim digest is invalid")
    if metadata.get("uncertainty_sha256") != sha256_bytes(report.uncertainty.encode("utf-8")):
        raise ValidationError("pulse report uncertainty digest is invalid")
    if render_pulse_report(report).encode("utf-8") != encoded:
        raise ValidationError("pulse report is not in canonical form")
    return report


def _new_report(
    *,
    event_key: str,
    claim: str,
    uncertainty: str,
    source_id: str,
    observed_at: datetime | str,
    coverage_ref: str,
    coverage_revision: str,
    completeness: str,
    evidence_refs: Sequence[str],
    causal_key: str | None,
    created_at: datetime | None,
) -> PulseReport:
    recorded_at = format_time(created_at or datetime.now(UTC))
    report = PulseReport(
        identifier=str(uuid.uuid4()),
        event_key=_event_key(event_key),
        claim=_text(claim, "pulse report claim", MAX_REPORT_TEXT),
        uncertainty=_text(uncertainty, "pulse report uncertainty", MAX_REPORT_TEXT),
        source_id=_source_id(source_id),
        observed_at=_time_or_datetime(observed_at, "pulse report observation time"),
        coverage_ref=_coverage_ref(coverage_ref, source_id),
        coverage_revision=_revision(coverage_revision, "pulse report coverage revision"),
        completeness=_completeness(completeness),
        evidence_refs=_refs(evidence_refs, "pulse report evidence reference"),
        causal_key=_optional_causal_key(causal_key),
        created_at=recorded_at,
        decision=None,
        delivery=None,
        history=(PulseReportHistory("created", recorded_at),),
        revision="",
    )
    _validate_report(report)
    return report


def _immutable_report_fields(report: PulseReport) -> tuple[object, ...]:
    return (
        report.event_key,
        report.claim,
        report.uncertainty,
        report.source_id,
        report.observed_at,
        report.coverage_ref,
        report.coverage_revision,
        report.completeness,
        report.evidence_refs,
        report.causal_key,
    )


def _validate_report(report: PulseReport) -> None:
    _uuid(report.identifier, "pulse report ID")
    _event_key(report.event_key)
    _text(report.claim, "pulse report claim", MAX_REPORT_TEXT)
    _text(report.uncertainty, "pulse report uncertainty", MAX_REPORT_TEXT)
    source_id = _source_id(report.source_id)
    _stored_time(report.observed_at, "pulse report observation time")
    _coverage_ref(report.coverage_ref, source_id)
    _revision(report.coverage_revision, "pulse report coverage revision")
    _completeness(report.completeness)
    _refs(report.evidence_refs, "pulse report evidence reference")
    _optional_causal_key(report.causal_key)
    _stored_time(report.created_at, "pulse report creation time")
    if report.decision is not None:
        _decision(report.decision.decision)
        _text(report.decision.reason, "pulse report decision reason", MAX_REASON_TEXT)
        _refs(report.decision.target_refs, "pulse report target reference")
        _stored_time(report.decision.decided_at, "pulse report decision time")
    if report.delivery is not None:
        if report.decision is None or report.decision.decision not in {"wake", "investigate"}:
            raise ValidationError("pulse report delivery requires a wake or investigate decision")
        if report.decision.decision == "investigate" and not report.delivery.result_ref.startswith(
            "source-report:"
        ):
            raise ValidationError(
                "pulse report investigation delivery requires a child source-report reference"
            )
        _native_ref(report.delivery.result_ref, "pulse report delivery result reference")
        _stored_time(report.delivery.delivered_at, "pulse report delivery time")
    expected_history = [PulseReportHistory("created", report.created_at)]
    if report.decision is not None:
        expected_history.append(PulseReportHistory("decided", report.decision.decided_at))
    if report.delivery is not None:
        expected_history.append(PulseReportHistory("delivered", report.delivery.delivered_at))
    if report.history != tuple(expected_history):
        raise ValidationError("pulse report history must preserve its recorded transitions")


def _parse_decision(value: object) -> RelevanceDecision | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "decision",
        "decided_at",
        "reason",
        "target_refs",
    }:
        raise ValidationError("pulse report decision has an unsupported shape")
    return RelevanceDecision(
        decision=_decision(value.get("decision")),
        reason=_text(value.get("reason"), "pulse report decision reason", MAX_REASON_TEXT),
        target_refs=_refs(
            _string_sequence(value.get("target_refs"), "pulse report target references"),
            "pulse report target reference",
        ),
        decided_at=_stored_time(value.get("decided_at"), "pulse report decision time"),
    )


def _parse_delivery(value: object) -> DeliveryDisposition | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"delivered_at", "result_ref"}:
        raise ValidationError("pulse report delivery has an unsupported shape")
    return DeliveryDisposition(
        result_ref=_native_ref(value.get("result_ref"), "pulse report delivery result reference"),
        delivered_at=_stored_time(value.get("delivered_at"), "pulse report delivery time"),
    )


def _parse_history(value: object) -> tuple[PulseReportHistory, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValidationError("pulse report history is invalid")
    history: list[PulseReportHistory] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "recorded_at"}:
            raise ValidationError("pulse report history has an unsupported shape")
        kind = item.get("kind")
        if kind not in {"created", "decided", "delivered"}:
            raise ValidationError("pulse report history kind is invalid")
        history.append(
            PulseReportHistory(
                kind=kind,
                recorded_at=_stored_time(item.get("recorded_at"), "pulse report history time"),
            )
        )
    return tuple(history)


def _decision_dict(value: RelevanceDecision | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "decision": value.decision,
        "decided_at": value.decided_at,
        "reason": value.reason,
        "target_refs": list(value.target_refs),
    }


def _delivery_dict(value: DeliveryDisposition | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"delivered_at": value.delivered_at, "result_ref": value.result_ref}


def _history_dict(value: PulseReportHistory) -> dict[str, str]:
    return {"kind": value.kind, "recorded_at": value.recorded_at}


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc
    if canonical != value.casefold():
        raise ValidationError(f"{label} must be canonical")
    return canonical


def _event_key(value: object) -> str:
    if not isinstance(value, str) or _EVENT_KEY.fullmatch(value) is None:
        raise ValidationError("pulse report event key must be an opaque pulse-report SHA-256 key")
    return value


def _optional_causal_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CAUSAL_KEY.fullmatch(value) is None:
        raise ValidationError("pulse report causal key must be an opaque SHA-256 key or null")
    return value


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID.fullmatch(value) is None:
        raise ValidationError("pulse report source ID is invalid")
    return value


def _coverage_ref(value: object, source_id: object) -> str:
    clean_source = _source_id(source_id)
    expected = f"source:{clean_source}"
    if value != expected:
        raise ValidationError("pulse report coverage reference must match its source ID")
    return expected


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _completeness(value: object) -> str:
    if value not in _COMPLETENESS:
        raise ValidationError("pulse report completeness must be complete, partial, or unavailable")
    return str(value)


def _native_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or _NATIVE_REF.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a safe native revision reference")
    return value


def _refs(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError(f"{label}s must be a sequence")
    if len(values) > MAX_REPORT_REFS:
        raise ValidationError(f"{label}s exceed the supported bound")
    clean = tuple(_native_ref(value, label) for value in values)
    if len(set(clean)) != len(clean):
        raise ValidationError(f"{label}s must be unique")
    return clean


def _string_sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be a list of strings")
    return tuple(value)


def _decision(value: object) -> PulseDecision:
    if value not in _DECISIONS:
        raise ValidationError("pulse report decision is invalid")
    return str(value)  # type: ignore[return-value]


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValidationError(f"{label} must be bounded derived text")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum:
        raise ValidationError(f"{label} must be one non-empty bounded line")
    return clean


def _stored_time(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    return stored_time(value, label)


def _time_or_datetime(value: datetime | str, label: str) -> str:
    if isinstance(value, datetime):
        return format_time(value)
    return _stored_time(value, label)

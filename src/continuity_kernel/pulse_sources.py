"""One-source-at-a-time, transient Pulse acquisition with report-before-checkpoint commit.

This boundary deliberately keeps provider payloads in the process.  It creates
only content-free source receipts after a durable Pulse report has been read
back.  A failed report therefore cannot advance a connector receipt or a local
checkpoint.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, cast

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.connector_auth import ConnectionHealth
from continuity_kernel.connector_sources import SUPPORTED_SOURCE_IDS, read_connector_source
from continuity_kernel.discord_source import DiscordSourceBridge
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    NotFoundError,
    SetupError,
    ValidationError,
)
from continuity_kernel.local_source_delivery import SUPPORTED_LOCAL_SOURCES, LocalSourceDelivery
from continuity_kernel.records import format_time, parse_time
from continuity_kernel.slack_tasks import SlackTaskReader
from continuity_kernel.source_state import SourceCompleteness, SourceObservation, source_fingerprint
from continuity_kernel.vault import Vault

SUPPORTED_PULSE_SOURCES = SUPPORTED_SOURCE_IDS | frozenset(SUPPORTED_LOCAL_SOURCES) | {"discord"}
MAX_PULSE_SOURCE_LIMIT = 100
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_REF = re.compile(
    r"^source-report:(?P<identifier>[0-9a-f-]{36})@(?P<revision>[0-9a-f]{64})$"
)


class _PulseReport(Protocol):
    @property
    def identifier(self) -> str: ...

    @property
    def event_key(self) -> str: ...

    @property
    def source_id(self) -> str: ...

    @property
    def observed_at(self) -> str: ...

    @property
    def coverage_ref(self) -> str: ...

    @property
    def coverage_revision(self) -> str: ...

    @property
    def completeness(self) -> str: ...

    @property
    def revision(self) -> str: ...


class PulseReportReader(Protocol):
    """The small readback surface required before a source receipt can commit."""

    def show(self, report_id: str) -> _PulseReport: ...


ConnectorReader = Callable[..., Mapping[str, object]]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class AcquisitionReceipt:
    """Private, process-local commit facts for one acquired source window."""

    identifier: str
    kind: str
    source_revision: str
    prior_observation: SourceObservation | None = field(repr=False)
    record: Mapping[str, object] | None = field(repr=False)
    local_token: str | None = field(default=None, repr=False)
    discord_ack_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AcquiredSourceWindow:
    """One bounded payload and its content-free durable coverage envelope."""

    source_id: str
    items: tuple[Mapping[str, object], ...] = field(repr=False)
    fingerprint: str
    coverage: Mapping[str, object]
    completeness: str
    observed_at: str
    status: str
    result: str
    changed: bool
    empty: bool
    error_code: str | None
    source_revision: str
    receipt: AcquisitionReceipt = field(repr=False)

    @property
    def coverage_ref(self) -> str:
        return f"source:{self.source_id}"

    @property
    def report_event_key(self) -> str:
        return f"pulse-report:{self.fingerprint}"


class PulseSourceAdapter:
    """Acquire one selected native source, then commit it after report readback.

    ``acquire`` never writes a portable source receipt.  ``commit`` first
    proves that the exact in-memory payload has a matching durable report, then
    records the content-free receipt and advances a native local checkpoint when
    one exists.
    """

    def __init__(
        self,
        vault: Vault,
        *,
        report_store: PulseReportReader | None = None,
        connector_reader: ConnectorReader = read_connector_source,
        local_delivery: LocalSourceDelivery | None = None,
        discord_bridge: DiscordSourceBridge | None = None,
        connection_ids: Mapping[str, str] | None = None,
        actor_ref: str = "system-role:resident-pulse",
        now: Clock | None = None,
    ) -> None:
        if not isinstance(actor_ref, str) or not actor_ref.strip():
            raise ValidationError("Pulse source actor reference is required")
        self.vault = vault
        self.report_store = report_store
        self._connector_reader = connector_reader
        self._local_delivery = local_delivery
        self._discord_bridge = discord_bridge
        self._connection_ids = dict(connection_ids or {})
        self._actor_ref = actor_ref
        self._now = now or (lambda: datetime.now(UTC))
        self._issued: dict[str, AcquiredSourceWindow] = {}
        self._committed: dict[str, Mapping[str, object]] = {}
        self._committed_fingerprints: dict[str, str] = {}
        self._discord_receipts_recorded: set[str] = set()

    def acquire(self, source_id: str, limit: int | None = None) -> AcquiredSourceWindow:
        """Read one currently selected source without advancing its receipt."""

        clean_source = _source_id(source_id)
        if limit is None:
            # Local delivery already supports bounded batches of 100. Keep
            # related backlog updates together instead of asking Luna to judge
            # four separate pages; connector reads retain their smaller window.
            limit = 100 if clean_source in SUPPORTED_LOCAL_SOURCES else 25
        _limit(limit)
        snapshot = self._selected_snapshot(clean_source)
        observed_at = _format_now(self._now())

        if clean_source in SUPPORTED_SOURCE_IDS:
            return self._acquire_connector(clean_source, limit, snapshot, observed_at)
        if clean_source in SUPPORTED_LOCAL_SOURCES:
            return self._acquire_local(clean_source, limit, snapshot, observed_at)
        if clean_source == "discord":
            return self._acquire_discord(limit, snapshot, observed_at)
        return self._failure_window(
            source_id=clean_source,
            snapshot=snapshot,
            observed_at=observed_at,
            error_code="tool_absent",
            status="unsupported",
            kind="unsupported",
        )

    def commit(
        self,
        window: AcquiredSourceWindow,
        *,
        report_ref: str,
        report_revision: str,
        replay: bool = False,
    ) -> Mapping[str, object]:
        """Commit one window only after strict durable report readback.

        The report event key is derived from the transient payload fingerprint.
        The report store is therefore a durable semantic receipt for this exact
        acquisition, while the source ledger receives no provider body.
        """

        issued = self._issued.get(window.receipt.identifier)
        if issued is not window:
            raise ConflictError("Pulse source window is not an active acquisition")
        committed = self._committed.get(window.receipt.identifier)
        if committed is not None:
            return committed
        self._validate_report(
            window,
            report_ref=report_ref,
            report_revision=report_revision,
            replay=replay,
        )

        current = self._selected_snapshot(window.source_id)
        expected_revision = self._current_source_lease(window, current)

        if window.receipt.kind == "local":
            result = self._commit_local(window, report_ref=report_ref)
        elif window.receipt.kind == "discord":
            result = self._commit_discord(
                window,
                report_ref=report_ref,
                expected_revision=expected_revision,
            )
        else:
            result = self._commit_source_receipt(
                window,
                report_ref=report_ref,
                expected_revision=expected_revision,
            )

        frozen = _freeze_mapping(result)
        self._committed[window.receipt.identifier] = frozen
        self._committed_fingerprints[window.source_id] = window.fingerprint
        return frozen

    def release(self, window: AcquiredSourceWindow) -> None:
        """Forget one transient payload after its report/commit path is finished."""

        issued = self._issued.get(window.receipt.identifier)
        if issued is not window:
            raise ConflictError("Pulse source window is not an active acquisition")
        self._issued.pop(window.receipt.identifier)
        self._committed.pop(window.receipt.identifier, None)
        self._discord_receipts_recorded.discard(window.receipt.identifier)

    def slack_search(
        self,
        query: str,
        *,
        max_pages: int = 1,
        max_results: int = 100,
        snippet_chars: int = 320,
    ) -> Mapping[str, object]:
        """Run bounded Slack search through the same portable Slack reader."""

        self._selected_snapshot("slack")
        reader = self._slack_reader()
        return _freeze_mapping(
            reader.search(
                query,
                max_pages=max_pages,
                max_results=max_results,
                snippet_chars=snippet_chars,
            )
        )

    def slack_context(
        self,
        reference: str,
        *,
        before: int = 5,
        after: int = 5,
        include_thread: bool = True,
        snippet_chars: int = 1_000,
    ) -> Mapping[str, object]:
        """Expand one short-lived Slack reference through ``SlackTaskReader`` only."""

        self._selected_snapshot("slack")
        return _freeze_mapping(
            self._slack_reader().context(
                reference,
                before=before,
                after=after,
                include_thread=include_thread,
                snippet_chars=snippet_chars,
            )
        )

    def _acquire_connector(
        self,
        source_id: str,
        limit: int,
        snapshot: object,
        observed_at: str,
    ) -> AcquiredSourceWindow:
        try:
            connection_id = self._connection_id(source_id)
        except (NotFoundError, SetupError):
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="auth_required",
                status="failure",
                kind="connector",
            )
        except ValidationError:
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="unsupported_tool_shape",
                status="failure",
                kind="connector",
            )

        try:
            delivery = self._connector_reader(
                self.vault,
                connection_id=connection_id,
                source_id=source_id,
                limit=limit,
                observed_at=parse_time(observed_at),
            )
            return self._window_from_delivery(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                delivery=delivery,
                kind="connector",
            )
        except ConflictError:
            raise
        except (NotFoundError, SetupError):
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="auth_required",
                status="failure",
                kind="connector",
            )
        except (ValidationError, OSError, TimeoutError):
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="read_failed",
                status="failure",
                kind="connector",
            )
        except ContinuityError:
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="read_failed",
                status="failure",
                kind="connector",
            )

    def _acquire_local(
        self,
        source_id: str,
        limit: int,
        snapshot: object,
        observed_at: str,
    ) -> AcquiredSourceWindow:
        try:
            delivery = self._local().poll(source_id, limit=limit)
            payload = _items(delivery.get("messages"), limit=limit)
            receipt_value = delivery.get("delivery")
            if not isinstance(receipt_value, Mapping):
                raise ValidationError("local Pulse source delivery is invalid")
            token = _required_text(receipt_value.get("token"), "local Pulse source token")
            source_revision = _required_text(
                receipt_value.get("source_revision"), "local Pulse source revision"
            )
            if _SHA256.fullmatch(source_revision) is None:
                raise ValidationError("local Pulse source revision is invalid")
            # A pending local token remains bound to its prepared revision.
            # LocalSourceDelivery acknowledges it only after replaying and
            # validating the original account, store, and provider delta.
            complete = delivery.get("complete")
            if not isinstance(complete, bool):
                raise ValidationError("local Pulse source completeness is invalid")
            local_observed = _time_or(observed_at, delivery.get("observed_at"))
            result = "success" if payload else "explicit_empty"
            return self._issue_window(
                source_id=source_id,
                snapshot=snapshot,
                items=payload,
                observed_at=local_observed,
                result=result,
                completeness="complete" if complete else "partial",
                status="success" if payload else "empty",
                error_code=None,
                kind="local",
                record=None,
                local_token=token,
                receipt_source_revision=source_revision,
            )
        except ConflictError:
            raise
        except (NotFoundError, SetupError, ValidationError, OSError, TimeoutError):
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="tool_absent",
                status="failure",
                kind="local_failure",
            )
        except ContinuityError:
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="read_failed",
                status="failure",
                kind="local_failure",
            )

    def _acquire_discord(
        self,
        limit: int,
        snapshot: object,
        observed_at: str,
    ) -> AcquiredSourceWindow:
        try:
            delivery = self._discord().poll(limit=limit)
            return self._window_from_delivery(
                source_id="discord",
                snapshot=snapshot,
                observed_at=_time_or(observed_at, delivery.get("attemptedAt")),
                delivery=delivery,
                kind="discord",
                discord_ack_token=_optional_text(delivery.get("ackToken")),
            )
        except ConflictError:
            raise
        except (NotFoundError, SetupError, ValidationError, OSError, TimeoutError):
            return self._failure_window(
                source_id="discord",
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="tool_absent",
                status="failure",
                kind="discord",
            )
        except ContinuityError:
            return self._failure_window(
                source_id="discord",
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="tool_error",
                status="failure",
                kind="discord",
            )

    def _window_from_delivery(
        self,
        *,
        source_id: str,
        snapshot: object,
        observed_at: str,
        delivery: Mapping[str, object],
        kind: str,
        discord_ack_token: str | None = None,
    ) -> AcquiredSourceWindow:
        record_value = delivery.get("record")
        if not isinstance(record_value, Mapping):
            raise ValidationError("Pulse source receipt is invalid")
        record = _freeze_mapping(record_value)
        result = _required_text(record.get("result"), "Pulse source result")
        if result not in {"success", "explicit_empty", "failure"}:
            raise ValidationError("Pulse source result is invalid")
        source_revision = _required_text(delivery.get("sourceRevision"), "Pulse source revision")
        if source_revision != _snapshot_revision(snapshot):
            raise ConflictError("source state changed during Pulse acquisition")
        record_source = _required_text(record.get("source"), "Pulse receipt source")
        if record_source != source_id:
            raise ValidationError("Pulse source receipt names another source")
        payload = _items(delivery.get("items"), limit=MAX_PULSE_SOURCE_LIMIT)
        if result == "failure":
            error_code = _required_text(record.get("errorCode"), "Pulse source error")
            return self._issue_window(
                source_id=source_id,
                snapshot=snapshot,
                items=(),
                observed_at=observed_at,
                result=result,
                completeness="unavailable",
                status="failure",
                error_code=error_code,
                kind=kind,
                record=record,
                discord_ack_token=discord_ack_token,
            )
        completeness = _required_text(record.get("completeness"), "Pulse source completeness")
        if completeness not in {item.value for item in SourceCompleteness}:
            raise ValidationError("Pulse source completeness is invalid")
        if result == "explicit_empty" and payload:
            raise ValidationError("empty Pulse source receipt has provider items")
        if result == "success" and not payload:
            raise ValidationError("successful Pulse source receipt has no provider items")
        if not self._account_lease_matches(snapshot, source_id=source_id, record=record):
            return self._failure_window(
                source_id=source_id,
                snapshot=snapshot,
                observed_at=observed_at,
                error_code="identity_mismatch",
                status="failure",
                kind=kind,
            )
        return self._issue_window(
            source_id=source_id,
            snapshot=snapshot,
            items=payload,
            observed_at=observed_at,
            result=result,
            completeness=completeness,
            status="success" if result == "success" else "empty",
            error_code=None,
            kind=kind,
            record=record,
            discord_ack_token=discord_ack_token,
        )

    @staticmethod
    def _account_lease_matches(
        snapshot: object,
        *,
        source_id: str,
        record: Mapping[str, object],
    ) -> bool:
        """Check whether an acquired provider receipt matches the selected account.

        A mismatch becomes a content-free failure window.  The normal source
        commit then preserves the prior binding while recording a stable
        ``identity_mismatch`` incident, without retaining the provider items.
        """

        prior = cast(Any, snapshot).observation(source_id)
        expected_account = getattr(prior, "account_fingerprint", None)
        if not isinstance(expected_account, str):
            return True
        account_binding = _optional_text(record.get("accountBinding"))
        actual_account = source_fingerprint(account_binding, "Pulse source account binding")
        return actual_account == expected_account

    def _failure_window(
        self,
        *,
        source_id: str,
        snapshot: object,
        observed_at: str,
        error_code: str,
        status: str,
        kind: str,
    ) -> AcquiredSourceWindow:
        record = _freeze_mapping(
            {
                "source": source_id,
                "result": "failure",
                "errorCode": error_code,
            }
        )
        return self._issue_window(
            source_id=source_id,
            snapshot=snapshot,
            items=(),
            observed_at=observed_at,
            result="failure",
            completeness="unavailable",
            status=status,
            error_code=error_code,
            kind=kind,
            record=record,
        )

    def _issue_window(
        self,
        *,
        source_id: str,
        snapshot: object,
        items: tuple[Mapping[str, object], ...],
        observed_at: str,
        result: str,
        completeness: str,
        status: str,
        error_code: str | None,
        kind: str,
        record: Mapping[str, object] | None,
        local_token: str | None = None,
        discord_ack_token: str | None = None,
        receipt_source_revision: str | None = None,
    ) -> AcquiredSourceWindow:
        fingerprint = _window_fingerprint(
            source_id=source_id,
            items=items,
            result=result,
            completeness=completeness,
            error_code=error_code,
            record=record,
        )
        receipt = AcquisitionReceipt(
            identifier=secrets.token_hex(16),
            kind=kind,
            source_revision=receipt_source_revision or _snapshot_revision(snapshot),
            prior_observation=cast(Any, snapshot).observation(source_id),
            record=record,
            local_token=local_token,
            discord_ack_token=discord_ack_token,
        )
        window = AcquiredSourceWindow(
            source_id=source_id,
            items=items,
            fingerprint=fingerprint,
            coverage=_coverage(
                snapshot,
                source_id,
                result=result,
                completeness=completeness,
                record=record,
                observed_at=observed_at,
                returned=len(items),
            ),
            completeness=completeness,
            observed_at=observed_at,
            status=status,
            result=result,
            changed=self._committed_fingerprints.get(source_id) != fingerprint,
            empty=result == "explicit_empty",
            error_code=error_code,
            source_revision=receipt.source_revision,
            receipt=receipt,
        )
        self._issued[receipt.identifier] = window
        return window

    def _commit_source_receipt(
        self,
        window: AcquiredSourceWindow,
        *,
        report_ref: str,
        expected_revision: str,
    ) -> Mapping[str, object]:
        record = window.receipt.record
        if record is None:
            raise ConflictError("Pulse source receipt is unavailable")
        return _freeze_mapping(
            self.vault.record_source_observation(
                expected_revision=expected_revision,
                source_id=window.source_id,
                actor_ref=self._actor_ref,
                result=_required_text(record.get("result"), "Pulse source result"),
                covered_through=_optional_text(record.get("coveredThrough")),
                completeness=(
                    _optional_text(record.get("completeness"))
                    if _required_text(record.get("result"), "Pulse source result") != "failure"
                    else None
                ),
                account_binding=_optional_text(record.get("accountBinding")),
                tool_binding=_optional_text(record.get("toolBinding")),
                cursor=_optional_text(record.get("cursor")),
                evidence_refs=_text_tuple(record.get("evidenceRefs")),
                canonical_result_refs=(report_ref,),
                error_code=_optional_text(record.get("errorCode")),
                observed_at=parse_time(window.observed_at),
            )
        )

    def _commit_local(
        self, window: AcquiredSourceWindow, *, report_ref: str
    ) -> Mapping[str, object]:
        token = window.receipt.local_token
        if token is None:
            raise ConflictError("local Pulse source checkpoint receipt is unavailable")
        return _freeze_mapping(
            self._local().acknowledge(
                window.source_id,
                token=token,
                expected_source_revision=window.source_revision,
                disposition="accepted",
                result_refs=(report_ref,),
                actor_ref=self._actor_ref,
            )
        )

    def _commit_discord(
        self,
        window: AcquiredSourceWindow,
        *,
        report_ref: str,
        expected_revision: str,
    ) -> Mapping[str, object]:
        receipt_id = window.receipt.identifier
        if receipt_id not in self._discord_receipts_recorded:
            source_receipt = self._commit_source_receipt(
                window,
                report_ref=report_ref,
                expected_revision=expected_revision,
            )
            self._discord_receipts_recorded.add(receipt_id)
        else:
            source_receipt = _freeze_mapping(
                {"revision": self.vault.get_source_snapshot().revision}
            )
        ack_token = window.receipt.discord_ack_token
        if ack_token is None:
            return source_receipt
        acknowledgement = self._discord().acknowledge(
            ack_token=ack_token,
            expected_source_revision=_required_text(
                source_receipt.get("revision"), "Discord committed source revision"
            ),
        )
        return _freeze_mapping(
            {
                "acknowledgement": acknowledgement,
                "receipt": source_receipt,
            }
        )

    def _validate_report(
        self,
        window: AcquiredSourceWindow,
        *,
        report_ref: str,
        report_revision: str,
        replay: bool,
    ) -> None:
        if self.report_store is None:
            raise ValidationError("Pulse source commit requires a durable Pulse report store")
        match = _REPORT_REF.fullmatch(report_ref)
        if match is None or not _SHA256.fullmatch(report_revision):
            raise ValidationError("Pulse report receipt is invalid")
        identifier = match.group("identifier")
        if match.group("revision") != report_revision:
            raise ConflictError("Pulse report revision does not match its report reference")
        report = self.report_store.show(identifier)
        if (
            report.identifier != identifier
            or report.revision != report_revision
            or report.event_key != window.report_event_key
            or report.source_id != window.source_id
            or report.coverage_ref != window.coverage_ref
            or report.completeness != window.completeness
        ):
            raise ConflictError("Pulse report does not attest the acquired source window")
        if not replay and (
            report.observed_at != window.observed_at
            or report.coverage_revision != window.source_revision
        ):
            raise ConflictError("Pulse report does not attest the acquired source window")
        if replay:
            try:
                report_observed = parse_time(report.observed_at)
                window_observed = parse_time(window.observed_at)
            except ValidationError as exc:
                raise ConflictError("Pulse replay report has an invalid observation time") from exc
            if report_observed > window_observed or not _SHA256.fullmatch(report.coverage_revision):
                raise ConflictError("Pulse replay report does not precede the acquired window")

    def _selected_snapshot(self, source_id: str) -> object:
        snapshot = self.vault.get_source_snapshot()
        if source_id not in snapshot.selected_sources:
            raise ConflictError("Pulse source is not selected")
        return snapshot

    def _current_source_lease(self, window: AcquiredSourceWindow, current: object) -> str:
        current_revision = _snapshot_revision(current)
        if current_revision == window.source_revision:
            return current_revision
        current_observation = cast(Any, current).observation(window.source_id)
        if current_observation != window.receipt.prior_observation:
            raise ConflictError("source changed after acquisition; acquire a new Pulse window")
        return current_revision

    def _connection_id(self, source_id: str) -> str:
        configured = self._connection_ids.get(source_id)
        snapshot = self.vault.get_connection_snapshot()
        if configured is not None:
            connection = snapshot.connection(configured)
            if connection is None:
                raise NotFoundError("Pulse connector connection was not found")
            if source_id not in connection.source_ids:
                raise ValidationError("Pulse connector connection does not authorize source")
            return configured
        candidates = [
            connection
            for connection in snapshot.connections
            if source_id in connection.source_ids
            and connection.health in {ConnectionHealth.READY, ConnectionHealth.DEGRADED}
        ]
        if not candidates:
            raise SetupError("Pulse connector source has no ready connection")
        if len(candidates) != 1:
            raise ValidationError("Pulse connector source has multiple ready connections")
        return str(candidates[0].connection_id)

    def _local(self) -> LocalSourceDelivery:
        if self._local_delivery is None:
            self._local_delivery = LocalSourceDelivery(self.vault)
        return self._local_delivery

    def _discord(self) -> DiscordSourceBridge:
        if self._discord_bridge is None:
            self._discord_bridge = DiscordSourceBridge(self.vault)
        return self._discord_bridge

    def _slack_reader(self) -> SlackTaskReader:
        return SlackTaskReader(self.vault, connection_id=self._connection_id("slack"))


def _coverage(
    snapshot: object,
    source_id: str,
    *,
    result: str,
    completeness: str,
    record: Mapping[str, object] | None,
    observed_at: str,
    returned: int,
) -> Mapping[str, object]:
    observation = cast(Any, snapshot).observation(source_id)
    prior = cast(SourceObservation | None, observation)
    current_coverage = (
        _optional_text(record.get("coveredThrough"))
        if result != "failure" and record is not None
        else None
    )
    current_completeness = completeness if result != "failure" else None
    return _freeze_mapping(
        {
            "covered_through": current_coverage or (prior.covered_through if prior else None),
            "coverage_ref": f"source:{source_id}",
            "last_success_at": observed_at
            if result != "failure"
            else (prior.last_success_at if prior else None),
            "prior_completeness": (
                current_completeness
                or (prior.completeness.value if prior and prior.completeness else None)
            ),
            "returned": returned,
            "source_revision": _snapshot_revision(snapshot),
        }
    )


def _window_fingerprint(
    *,
    source_id: str,
    items: tuple[Mapping[str, object], ...],
    result: str,
    completeness: str,
    error_code: str | None,
    record: Mapping[str, object] | None,
) -> str:
    durable_record = {
        key: value
        for key, value in (record or {}).items()
        if key
        not in {
            "attemptedAt",
            "coveredThrough",
            "cursor",
            "observedAt",
            "sourceRevision",
        }
    }
    payload = {
        "completeness": completeness,
        "error_code": error_code,
        "items": _thaw(items),
        "receipt": _thaw(durable_record),
        "result": result,
        "source": source_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _items(value: object, *, limit: int) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValidationError("Pulse source payload is invalid or exceeds its bound")
    frozen: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValidationError("Pulse source item is invalid")
        frozen.append(_freeze_mapping(item))
    return tuple(frozen)


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValidationError("Pulse source mapping has an invalid key")
        frozen[key] = _freeze(item)
    return MappingProxyType(frozen)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(cast(Mapping[str, object], value))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError("Pulse source payload has a nonportable value")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _snapshot_revision(snapshot: object) -> str:
    revision = getattr(snapshot, "revision", None)
    if not isinstance(revision, str) or not _SHA256.fullmatch(revision):
        raise ValidationError("Pulse source revision is invalid")
    return revision


def _source_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 96 or value.strip() != value:
        raise ValidationError("Pulse source identifier is invalid")
    return value


def _limit(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PULSE_SOURCE_LIMIT
    ):
        raise ValidationError("Pulse source limit must be between 1 and 25")


def _format_now(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Pulse source clock must be timezone-aware")
    return format_time(value.astimezone(UTC))


def _time_or(fallback: str, value: object) -> str:
    if not isinstance(value, str):
        return fallback
    try:
        return format_time(parse_time(value))
    except ValidationError:
        return fallback


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValidationError("Pulse source evidence references are invalid")
    return tuple(cast(str, item) for item in value)


__all__ = [
    "MAX_PULSE_SOURCE_LIMIT",
    "AcquiredSourceWindow",
    "AcquisitionReceipt",
    "PulseReportReader",
    "PulseSourceAdapter",
]

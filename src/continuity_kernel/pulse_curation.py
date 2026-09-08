"""Deliver accepted derived changes through the signed local desktop bridge.

Python never reads the service credential. The native client owns Keychain
access, endpoint authentication, event-key dedupe, and Diane turn ownership.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from continuity_kernel.atomic import atomic_write
from continuity_kernel.errors import ValidationError
from continuity_kernel.pulse_delivery import CurationState, PulseDelivery


class PulseCurationBridge:
    def __init__(self, delivery: PulseDelivery, *, executable: Path | None = None):
        self._retry_after: dict[str, float] = {}
        self.delivery = delivery
        self.executable = executable or (
            Path.home() / "Library/Application Support/SeldDesktop/bin/seld-service"
        )

    def drain(self) -> int:
        checked = 0
        for item in self.delivery.pending_curation(limit=20).outbox:
            if self._retry_after.get(item.identifier, 0) > time.monotonic():
                continue
            checked += 1
            try:
                try:
                    result = self._call("--pulse-curation-status", item.event_key)
                except (OSError, subprocess.SubprocessError, ValidationError):
                    # Retrying this exact event is safe: native ingress rejects
                    # changed content and never starts a second turn for its key.
                    observed = datetime.fromisoformat(item.observed_at.replace("Z", "+00:00"))
                    event = {
                        "eventKey": item.event_key,
                        "acceptedChange": item.summary,
                        "canonicalResultRefs": list(item.result_refs),
                        "observedAt": observed.astimezone(UTC)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                    }
                    if item.target_desktop_work_id is not None:
                        event["targetDesktopWorkID"] = item.target_desktop_work_id
                    with tempfile.TemporaryDirectory(prefix="seld-pulse-curation-") as temporary:
                        path = Path(temporary) / "request.json"
                        atomic_write(path, json.dumps(event).encode())
                        result = self._call("--pulse-curation", str(path))
                if result.get("eventKey") != item.event_key:
                    raise ValidationError("Desktop curation returned another event")
                native_state = result.get("state")
                states: dict[str, CurationState] = {
                    "completed": "completed",
                    "noOp": "noop",
                    "blocked": "blocked",
                    "uncertain": "uncertain",
                    "queued": "queued",
                    "running": "queued",
                }
                state = states.get(str(native_state))
                if state is None:
                    raise ValidationError("Desktop curation returned an unknown state")
                receipt = json.dumps(
                    {
                        "request_id": result.get("id"),
                        "state": native_state,
                        "turn_id": result.get("turnID"),
                        "presentation_revision": result.get("appliedPresentationRevision"),
                        "completed_at": result.get("completedAt"),
                        "observed_at": item.observed_at,
                        "observation_to_curation_seconds": _elapsed(
                            item.observed_at, result.get("completedAt")
                        ),
                    },
                    separators=(",", ":"),
                )
                self._retry_after[item.identifier] = time.monotonic() + (
                    60 if state in {"blocked", "uncertain"} else 3
                )
                if item.state == state and item.receipt == receipt:
                    continue
                self.delivery.record_curation(
                    item.identifier,
                    expected_revision=item.revision,
                    state=state,
                    receipt=receipt,
                )
            except (OSError, subprocess.SubprocessError, ValidationError):
                self._retry_after[item.identifier] = time.monotonic() + 60
                if item.state == "uncertain":
                    continue
                self.delivery.record_curation(
                    item.identifier,
                    expected_revision=item.revision,
                    state="uncertain",
                    receipt="Desktop curation readback is unavailable.",
                )
        return checked

    def _call(self, operation: str, value: str) -> dict[str, Any]:
        response = subprocess.run(
            [str(self.executable), operation, value],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if response.returncode != 0 or len(response.stdout) > 256 * 1024:
            raise ValidationError("Native desktop bridge is unavailable")
        try:
            result = json.loads(response.stdout)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Native desktop bridge returned an invalid response") from exc
        if not isinstance(result, dict):
            raise ValidationError("Native desktop bridge returned an invalid response")
        return result


def _elapsed(observed_at: str, completed_at: object) -> float | None:
    if not isinstance(completed_at, str):
        return None
    try:
        start = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        elapsed = (end - start).total_seconds()
    except (ValueError, TypeError):
        return None
    return round(elapsed, 3) if elapsed >= 0 else None

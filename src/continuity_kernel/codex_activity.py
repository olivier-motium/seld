"""Opt-in structural session observations; no message or reasoning bodies leave this reader."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from continuity_kernel import config
from continuity_kernel.atomic import atomic_write, read_regular_file, sha256_bytes
from continuity_kernel.errors import ConflictError, ValidationError

# An explicit file list is the authorization boundary. No recursive discovery.
EVENTS = frozenset({"task_started", "task_complete", "turn_aborted"})
MAX_SCAN_BYTES = 1024 * 1024


class CodexActivity:
    def __init__(self, vault_root: Path):
        key = sha256_bytes(str(vault_root.resolve()).encode())
        self.root = config.data_dir() / "codex-activity" / key
        self.manifest = self.root / "selected.json"
        self.checkpoint = self.root / "cursor.json"
        self.pending: dict[str, Any] | None = None

    def acquire(self, *, limit: int, observed_at: str) -> dict[str, Any]:
        raw = read_regular_file(self.manifest, label="selected session files", max_bytes=16384)
        try:
            selection = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("Session selection is invalid") from exc
        entries = selection.get("files") if isinstance(selection, dict) else None
        if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
            raise ValidationError("Select between one and sixteen explicit session files")
        saved = (
            read_regular_file(self.checkpoint, label="session cursor", max_bytes=16384)
            if self.checkpoint.exists()
            else b"{}"
        )
        try:
            cursors = json.loads(saved)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("Session cursor is invalid") from exc
        if not isinstance(cursors, dict):
            raise ValidationError("Session cursor is invalid")
        for prior in cursors.values():
            if (
                not isinstance(prior, dict)
                or type(prior.get("offset")) is not int
                or prior["offset"] < 0
                or not isinstance(prior.get("binding"), list)
                or len(prior["binding"]) != 2
                or not all(type(value) is int for value in prior["binding"])
            ):
                raise ValidationError("Session cursor is invalid")
        next_cursors = dict(cursors)
        items: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "start_offset"}:
                raise ValidationError("Session selection requires path and start_offset")
            if not isinstance(entry["path"], str):
                raise ValidationError("Session path is invalid")
            path = Path(entry["path"])
            start = entry["start_offset"]
            if not path.is_absolute() or path.is_symlink() or type(start) is not int or start < 0:
                raise ValidationError("Session selection is invalid")
            identity = sha256_bytes(str(path).encode())
            prior = cursors.get(identity)
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValidationError("Session source must be a regular file")
                binding = [info.st_dev, info.st_ino]
                offset = prior["offset"] if prior else start
                if (prior and prior["binding"] != binding) or offset > info.st_size:
                    raise ConflictError(
                        "Session source was replaced or truncated; cursor preserved"
                    )
                stream.seek(offset)
                scanned = 0
                while scanned < MAX_SCAN_BYTES and len(items) < limit:
                    at = stream.tell()
                    line = stream.readline(MAX_SCAN_BYTES - scanned + 1)
                    if len(line) > MAX_SCAN_BYTES:
                        raise ValidationError("Session event exceeds the bounded read size")
                    if not line or not line.endswith(b"\n") or len(line) + scanned > MAX_SCAN_BYTES:
                        stream.seek(at)
                        break
                    scanned += len(line)
                    offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") not in EVENTS:
                        continue
                    # Only enumerated mechanical facts. Never copy payload text,
                    # user messages, assistant messages, tool output, or reasoning.
                    items.append(
                        {"event": payload["type"], "reference": f"codex-event:{identity}:{at}"}
                    )
                next_cursors[identity] = {"binding": binding, "offset": offset}
            if len(items) >= limit:
                break
        token = sha256_bytes(json.dumps(next_cursors, sort_keys=True).encode())
        self.pending = {"before": saved, "after": next_cursors, "token": token}
        return {
            "sourceRevision": "",
            "items": items,
            "record": {
                "source": "codex_activity",
                "result": "success" if items else "explicit_empty",
                "coveredThrough": observed_at,
                "completeness": "partial",
                "accountBinding": "host-local-explicit-session-selection",
                "toolBinding": "seld.codex.structural-events.v1",
                "cursor": token,
                "evidenceRefs": [item["reference"] for item in items],
            },
        }

    def acknowledge(self, token: str) -> None:
        pending = self.pending
        if not pending or pending["token"] != token:
            raise ConflictError("Session acquisition is no longer active")
        saved = (
            read_regular_file(self.checkpoint, label="session cursor", max_bytes=16384)
            if self.checkpoint.exists()
            else b"{}"
        )
        if saved != pending["before"]:
            raise ConflictError("Session cursor changed before acknowledgement")
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write(self.checkpoint, json.dumps(pending["after"], sort_keys=True).encode())
        self.pending = None

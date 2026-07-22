"""Small dependency-free MCP stdio server for Codex."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import IO, Any, Final

from continuity_kernel import __version__
from continuity_kernel.config import resolve_vault
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.records import record_dict
from continuity_kernel.vault import Vault, doctor_dict

PROTOCOL_VERSION: Final = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: Final = frozenset({PROTOCOL_VERSION})
MAX_REQUEST_BYTES: Final = 1024 * 1024


def serve() -> int:
    """Serve line-delimited MCP JSON-RPC until stdin closes."""

    for raw_line in _bounded_lines(sys.stdin.buffer):
        if raw_line is None:
            _write(_error(None, -32600, "JSON-RPC request exceeds its size bound"))
            continue
        if not raw_line.strip():
            continue
        request_id: object = None
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValidationError("JSON-RPC message must be an object")
            request_id = message.get("id")
            response = _handle(message)
            if response is not None:
                _write(response)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(_error(request_id, -32700, "invalid JSON"))
        except ContinuityError as exc:
            _write(_error(request_id, -32000, str(exc)))
        except Exception as exc:  # pragma: no cover - final protocol safety net
            _write(_error(request_id, -32603, f"internal error: {type(exc).__name__}"))
    return 0


def _bounded_lines(stream: IO[bytes]) -> Iterator[bytes | None]:
    while True:
        raw = stream.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return
        if len(raw) <= MAX_REQUEST_BYTES:
            yield raw
            continue
        while raw and not raw.endswith(b"\n"):
            raw = stream.readline(MAX_REQUEST_BYTES + 1)
        yield None


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return _result(
            request_id,
            {
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": (
                    "GSV is a private local vault. Read exact records before writes, "
                    "use compare-and-swap revisions, and never store secrets or raw provider "
                    "payloads."
                ),
                "protocolVersion": protocol_version,
                "serverInfo": {"name": "gsv", "version": __version__},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "tool arguments must be an object")
        try:
            payload = _call(params["name"], arguments)
            return _result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                payload, ensure_ascii=False, indent=2, sort_keys=True
                            ),
                        }
                    ],
                    "isError": False,
                    "structuredContent": payload,
                },
            )
        except ContinuityError as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def _call(name: str, values: dict[str, Any]) -> dict[str, Any]:
    vault = Vault(resolve_vault())
    if name == "gsv_status":
        return vault.status()
    if name == "gsv_context":
        return {
            "context": vault.context_pack(max_characters=_integer(values, "max_characters", 48_000))
        }
    if name == "gsv_doctor":
        return doctor_dict(vault.doctor(repair=False))
    if name == "gsv_task_list":
        status = _optional_string(values, "status")
        return {"tasks": [record_dict(item) for item in vault.list_tasks(status=status)]}
    if name == "gsv_task_show":
        return record_dict(vault.get_task(_string(values, "id")))
    if name == "gsv_task_create":
        return record_dict(
            vault.create_task(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                outcome=_string(values, "outcome"),
                status=_optional_string(values, "status") or "captured",
                next_actor=_optional_string(values, "next_actor"),
                next_action=_optional_string(values, "next_action"),
                waiting_on=_optional_string(values, "waiting_on"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_task_update":
        return record_dict(
            vault.update_task(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                outcome=_optional_string(values, "outcome"),
                status=_optional_string(values, "status"),
                next_actor=_optional_string(values, "next_actor"),
                next_action=_optional_string(values, "next_action"),
                waiting_on=_optional_string(values, "waiting_on"),
                clear_next_actor=_boolean(values, "clear_next_actor"),
                clear_next_action=_boolean(values, "clear_next_action"),
                clear_waiting_on=_boolean(values, "clear_waiting_on"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_entity_list":
        return {"entities": [record_dict(item) for item in vault.list_entities()]}
    if name == "gsv_entity_show":
        return record_dict(vault.get_entity(_string(values, "id")))
    if name == "gsv_entity_create":
        return record_dict(
            vault.create_entity(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                entity_type=_string(values, "entity_type"),
                summary=_string(values, "summary"),
                aliases=_strings(values, "aliases"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_entity_update":
        return record_dict(
            vault.update_entity(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                summary=_optional_string(values, "summary"),
                aliases=_optional_strings(values, "aliases"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_thread_list":
        status = _optional_string(values, "status")
        return {"threads": [record_dict(item) for item in vault.list_threads(status=status)]}
    if name == "gsv_thread_show":
        return record_dict(vault.get_thread(_string(values, "id")))
    if name == "gsv_thread_create":
        return record_dict(
            vault.create_thread(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                purpose=_string(values, "purpose"),
                summary=_string(values, "summary"),
                status=_optional_string(values, "status") or "active",
                next_move=_optional_string(values, "next_move"),
                task_ids=_strings(values, "task_ids"),
                entity_ids=_strings(values, "entity_ids"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_thread_update":
        return record_dict(
            vault.update_thread(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                purpose=_optional_string(values, "purpose"),
                summary=_optional_string(values, "summary"),
                status=_optional_string(values, "status"),
                next_move=_optional_string(values, "next_move"),
                clear_next_move=_boolean(values, "clear_next_move"),
                task_ids=_optional_strings(values, "task_ids"),
                entity_ids=_optional_strings(values, "entity_ids"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_document_show":
        return vault.read_document(_string(values, "name"))
    if name == "gsv_document_update":
        return vault.write_document(
            _string(values, "name"),
            _string(values, "content"),
            expected_revision=_string(values, "expected_revision"),
        )
    if name == "gsv_backup_create":
        return vault.create_backup()
    raise ValidationError(f"unknown tool: {name}")


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    read_only: bool,
) -> dict[str, Any]:
    return {
        "annotations": {
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": False,
            "readOnlyHint": read_only,
        },
        "description": description,
        "inputSchema": {
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
            "type": "object",
        },
        "name": name,
    }


TEXT = {"type": "string"}
TEXTS = {"items": {"type": "string"}, "type": "array"}
BOOLEAN = {"type": "boolean"}

TOOLS: Final = [
    _tool("gsv_status", "Read vault identity, counts, and digest.", {}, read_only=True),
    _tool(
        "gsv_context",
        "Read the bounded current context pack at the start of substantive work.",
        {"max_characters": {"maximum": 256000, "minimum": 4000, "type": "integer"}},
        read_only=True,
    ),
    _tool(
        "gsv_doctor",
        "Validate vault structure and references without mutation.",
        {},
        read_only=True,
    ),
    _tool("gsv_task_list", "List durable tasks.", {"status": TEXT}, read_only=True),
    _tool(
        "gsv_task_show",
        "Read one exact task and its revision.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_task_create",
        "Create one explicit durable outcome. Do not infer task meaning from source text.",
        {
            "id": TEXT,
            "next_action": TEXT,
            "next_actor": {"enum": ["agent", "human", "external"], "type": "string"},
            "outcome": TEXT,
            "refs": TEXTS,
            "status": TEXT,
            "title": TEXT,
            "waiting_on": TEXT,
        },
        ("id", "title", "outcome"),
        read_only=False,
    ),
    _tool(
        "gsv_task_update",
        "Update an exact task using its latest compare-and-swap revision.",
        {
            "add_refs": TEXTS,
            "clear_next_action": BOOLEAN,
            "clear_next_actor": BOOLEAN,
            "clear_waiting_on": BOOLEAN,
            "expected_revision": TEXT,
            "id": TEXT,
            "next_action": TEXT,
            "next_actor": {"enum": ["agent", "human", "external"], "type": "string"},
            "outcome": TEXT,
            "remove_refs": TEXTS,
            "status": TEXT,
            "title": TEXT,
            "waiting_on": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool("gsv_entity_list", "List canonical entities.", {}, read_only=True),
    _tool(
        "gsv_entity_show",
        "Read one exact canonical entity.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_entity_create",
        "Create a canonical entity only after deliberate identity resolution.",
        {
            "aliases": TEXTS,
            "entity_type": TEXT,
            "id": TEXT,
            "refs": TEXTS,
            "summary": TEXT,
            "title": TEXT,
        },
        ("id", "title", "entity_type", "summary"),
        read_only=False,
    ),
    _tool(
        "gsv_entity_update",
        "Update an exact canonical entity using its latest compare-and-swap revision.",
        {
            "add_refs": TEXTS,
            "aliases": TEXTS,
            "expected_revision": TEXT,
            "id": TEXT,
            "remove_refs": TEXTS,
            "summary": TEXT,
            "title": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool("gsv_thread_list", "List durable work threads.", {"status": TEXT}, read_only=True),
    _tool(
        "gsv_thread_show",
        "Read one exact work thread.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_thread_create",
        "Create an explicitly authored work thread with exact relationships.",
        {
            "entity_ids": TEXTS,
            "id": TEXT,
            "next_move": TEXT,
            "purpose": TEXT,
            "refs": TEXTS,
            "status": TEXT,
            "summary": TEXT,
            "task_ids": TEXTS,
            "title": TEXT,
        },
        ("id", "title", "purpose", "summary"),
        read_only=False,
    ),
    _tool(
        "gsv_thread_update",
        "Update an exact work thread using its latest compare-and-swap revision.",
        {
            "add_refs": TEXTS,
            "clear_next_move": BOOLEAN,
            "entity_ids": TEXTS,
            "expected_revision": TEXT,
            "id": TEXT,
            "next_move": TEXT,
            "purpose": TEXT,
            "remove_refs": TEXTS,
            "status": TEXT,
            "summary": TEXT,
            "task_ids": TEXTS,
            "title": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool(
        "gsv_document_show",
        "Read MIND.md or NOW.md and its revision.",
        {"name": {"enum": ["MIND.md", "NOW.md"], "type": "string"}},
        ("name",),
        read_only=True,
    ),
    _tool(
        "gsv_document_update",
        "Update MIND.md or NOW.md using its latest compare-and-swap revision.",
        {
            "content": TEXT,
            "expected_revision": TEXT,
            "name": {"enum": ["MIND.md", "NOW.md"], "type": "string"},
        },
        ("name", "content", "expected_revision"),
        read_only=False,
    ),
    _tool(
        "gsv_backup_create",
        "Create and verify a portable local vault backup.",
        {},
        read_only=False,
    ),
]


def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "jsonrpc": "2.0", "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}, "id": request_id, "jsonrpc": "2.0"}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _string(values: dict[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _optional_string(values: dict[str, Any], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _strings(values: dict[str, Any], name: str) -> tuple[str, ...]:
    value = values.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be a string list")
    return tuple(value)


def _optional_strings(values: dict[str, Any], name: str) -> tuple[str, ...] | None:
    if name not in values:
        return None
    return _strings(values, name)


def _boolean(values: dict[str, Any], name: str) -> bool:
    value = values.get(name, False)
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _integer(values: dict[str, Any], name: str, default: int) -> int:
    value = values.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value

"""Closed, typed contracts for interactive connector operations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

from continuity_kernel.errors import ValidationError

MAX_JSON_DEPTH: Final = 16
MAX_COLLECTION_ITEMS: Final = 128
MAX_JSON_ARRAY_ITEMS: Final = 1_000
MAX_STRING_LENGTH: Final = 256_000
MAX_SCHEMA_BRANCHES: Final = 64
MAX_SEALED_TOKEN_LENGTH: Final = 8_192

_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_CONNECTION_ID_PATTERN = r"^con-[A-Za-z0-9_-]{32}$"
_TOKEN_PATTERN = r"^v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "const",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)
_FORBIDDEN_TRANSPORT_FIELDS = frozenset(
    {
        "authorization",
        "cursor",
        "header",
        "headers",
        "host",
        "method",
        "nextlink",
        "token",
        "tokens",
        "uploadurl",
        "url",
        "urls",
    }
)


class ConnectorMode(StrEnum):
    READ = "read"
    WRITE = "write"


class ConnectorEffect(StrEnum):
    READ = "read"
    SAFE_MUTATION = "safe_mutation"
    OUTWARD = "outward"
    DESTRUCTIVE = "destructive"
    PERMANENT = "permanent"


_CALENDAR_ATTACHMENT_OPERATIONS: Final = frozenset(
    {
        ("google_calendar", ConnectorMode.WRITE, "events.create"),
        ("google_calendar", ConnectorMode.WRITE, "events.update"),
    }
)
_CALENDAR_ATTACHMENT_FILE_URL_PATH: Final = ("attachments", "[]", "file_url")


class _ToolInputSchema(dict[str, object]):
    """A rendered tool schema retaining its catalog-owned field exception."""

    def __init__(
        self,
        value: Mapping[str, object],
        *,
        allowed_transport_field_paths: frozenset[tuple[str, ...]],
    ) -> None:
        super().__init__(value)
        self.allowed_transport_field_paths = allowed_transport_field_paths


@dataclass(frozen=True)
class OperationSpec:
    """One named provider operation with an exact input boundary."""

    provider: str
    mode: ConnectorMode
    name: str
    effect: ConnectorEffect
    endpoint: str
    required_scopes: tuple[frozenset[str], ...]
    input_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        _operation_name(self.provider, "provider")
        _operation_name(self.name, "operation")
        _operation_name(self.endpoint, "endpoint")
        if not isinstance(self.mode, ConnectorMode):
            raise ValidationError("connector mode is invalid")
        if not isinstance(self.effect, ConnectorEffect):
            raise ValidationError("connector effect is invalid")
        if self.mode is ConnectorMode.READ and self.effect is not ConnectorEffect.READ:
            raise ValidationError("read operations must have a read effect")
        if self.mode is ConnectorMode.WRITE and self.effect is ConnectorEffect.READ:
            raise ValidationError("write operations cannot have a read effect")
        object.__setattr__(
            self,
            "required_scopes",
            _validate_scope_alternatives(self.required_scopes),
        )
        normalized = _normalize_schema(
            self.input_schema,
            depth=0,
            require_closed_object=True,
            allow_control_fields=False,
            allowed_transport_field_paths=_operation_transport_field_paths(
                self.provider,
                self.mode,
                self.name,
            ),
        )
        object.__setattr__(self, "input_schema", _freeze_json(normalized))

    @property
    def key(self) -> tuple[str, ConnectorMode, str]:
        return (self.provider, self.mode, self.name)

    def scope_grant_satisfies(self, granted_scopes: Sequence[str] | set[str]) -> bool:
        granted = frozenset(_scope_text(scope) for scope in granted_scopes)
        return any(alternative <= granted for alternative in self.required_scopes)

    def validate_input(self, value: object) -> object:
        return _validate_value(canonicalize_json(value), self.input_schema, depth=0)


class OperationCatalog:
    """Immutable lookup built only from an explicit operation tuple."""

    def __init__(self, operations: tuple[OperationSpec, ...]) -> None:
        by_key: dict[tuple[str, ConnectorMode, str], OperationSpec] = {}
        for operation in operations:
            if not isinstance(operation, OperationSpec):
                raise ValidationError("operation catalog contains an invalid specification")
            if operation.key in by_key:
                raise ValidationError("operation catalog contains duplicate keys")
            by_key[operation.key] = operation
        self._operations = operations
        self._by_key = MappingProxyType(by_key)

    @property
    def operations(self) -> tuple[OperationSpec, ...]:
        return self._operations

    def lookup(self, provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
        _operation_name(provider, "provider")
        _operation_name(name, "operation")
        if not isinstance(mode, ConnectorMode):
            raise ValidationError("connector mode is invalid")
        operation = self._by_key.get((provider, mode, name))
        if operation is None:
            raise ValidationError("connector operation is not in the catalog")
        return operation

    def list_operations(
        self,
        *,
        provider: str | None = None,
        mode: ConnectorMode | None = None,
    ) -> tuple[OperationSpec, ...]:
        if provider is not None:
            _operation_name(provider, "provider")
        if mode is not None and not isinstance(mode, ConnectorMode):
            raise ValidationError("connector mode is invalid")
        return tuple(
            operation
            for operation in self._operations
            if (provider is None or operation.provider == provider)
            and (mode is None or operation.mode is mode)
        )

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({operation.provider for operation in self._operations}))

    def validate_input(
        self,
        provider: str,
        mode: ConnectorMode,
        name: str,
        value: object,
    ) -> object:
        return self.lookup(provider, mode, name).validate_input(value)

    def tool_input_schema(self, provider: str, mode: ConnectorMode) -> dict[str, object]:
        """Return a closed oneOf envelope for one provider read or write tool."""

        operations = self.list_operations(provider=provider, mode=mode)
        if not operations:
            raise ValidationError("operation catalog has no matching operations")
        variants: list[dict[str, object]] = []
        for operation in operations:
            properties: dict[str, object] = {
                "connection_id": {
                    "maxLength": 36,
                    "pattern": _CONNECTION_ID_PATTERN,
                    "type": "string",
                },
                "input": _thaw_json(operation.input_schema),
                "operation": {"const": operation.name},
            }
            if mode is ConnectorMode.READ:
                properties["cursor"] = {
                    "maxLength": MAX_SEALED_TOKEN_LENGTH,
                    "pattern": _TOKEN_PATTERN,
                    "type": "string",
                }
            else:
                properties["confirmation_token"] = {
                    "maxLength": MAX_SEALED_TOKEN_LENGTH,
                    "pattern": _TOKEN_PATTERN,
                    "type": "string",
                }
            variants.append(
                {
                    "additionalProperties": False,
                    "properties": properties,
                    "required": ["connection_id", "input", "operation"],
                    "type": "object",
                }
            )
        allowed_transport_field_paths = frozenset(
            ("input", *path)
            for operation in operations
            for path in _operation_transport_field_paths(
                operation.provider,
                operation.mode,
                operation.name,
            )
        )
        return _ToolInputSchema(
            {"oneOf": variants},
            allowed_transport_field_paths=allowed_transport_field_paths,
        )


def canonicalize_json(value: object) -> object:
    """Return a detached, bounded JSON value with sorted object keys."""

    return _clone_json(value, depth=0)


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            canonicalize_json(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValidationError("JSON value cannot be canonically encoded") from exc


def canonical_json_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def validate_json(value: object, schema: Mapping[str, object]) -> object:
    """Validate against the deliberately small schema subset used by the catalog."""

    normalized = _normalize_schema(
        schema,
        depth=0,
        require_closed_object=False,
        allow_control_fields=True,
        allowed_transport_field_paths=(
            schema.allowed_transport_field_paths
            if isinstance(schema, _ToolInputSchema)
            else frozenset()
        ),
    )
    return _validate_value(canonicalize_json(value), normalized, depth=0)


def _operation_name(value: object, label: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValidationError(f"connector {label} name is invalid")
    return value


def _scope_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 2_048
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise ValidationError("connector scope is invalid")
    return value


def _validate_scope_alternatives(
    alternatives: object,
) -> tuple[frozenset[str], ...]:
    if not isinstance(alternatives, tuple) or not alternatives:
        raise ValidationError("operation scope alternatives must be a non-empty tuple")
    if len(alternatives) > MAX_COLLECTION_ITEMS:
        raise ValidationError("operation scope alternatives exceed their bound")
    normalized: list[frozenset[str]] = []
    for alternative in alternatives:
        if not isinstance(alternative, frozenset) or len(alternative) > MAX_COLLECTION_ITEMS:
            raise ValidationError("operation scope alternative is invalid")
        normalized.append(frozenset(_scope_text(scope) for scope in alternative))
    if len(set(normalized)) != len(normalized):
        raise ValidationError("operation scope alternatives contain duplicates")
    return tuple(normalized)


def _clone_json(value: object, *, depth: int) -> object:
    if depth > MAX_JSON_DEPTH:
        raise ValidationError("JSON value is too deeply nested")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError("JSON number must be finite")
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_STRING_LENGTH:
            raise ValidationError("JSON string is invalid or too large")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("JSON string is invalid Unicode") from exc
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValidationError("JSON object exceeds its collection bound")
        items: list[tuple[str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str) or not key or "\x00" in key:
                raise ValidationError("JSON object keys must be non-empty strings")
            items.append((key, _clone_json(item, depth=depth + 1)))
        return {key: item for key, item in sorted(items)}
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY_ITEMS:
            raise ValidationError("JSON array exceeds its collection bound")
        return [_clone_json(item, depth=depth + 1) for item in value]
    raise ValidationError("value is not JSON")


def _normalize_schema(
    schema: object,
    *,
    depth: int,
    require_closed_object: bool,
    allow_control_fields: bool,
    allowed_transport_field_paths: frozenset[tuple[str, ...]] = frozenset(),
    property_path: tuple[str, ...] = (),
) -> dict[str, object]:
    if depth > MAX_JSON_DEPTH or not isinstance(schema, Mapping):
        raise ValidationError("JSON schema is invalid or too deeply nested")
    if any(not isinstance(key, str) for key in schema):
        raise ValidationError("JSON schema keys must be strings")
    unknown = set(schema) - _SCHEMA_KEYWORDS
    if unknown:
        raise ValidationError("JSON schema contains an unsupported keyword")
    if not schema:
        raise ValidationError("JSON schema cannot be empty")

    normalized: dict[str, object] = {}
    schema_type = schema.get("type")
    if schema_type is not None:
        if not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES:
            raise ValidationError("JSON schema type is unsupported")
        normalized["type"] = schema_type

    if "const" in schema:
        normalized["const"] = canonicalize_json(schema["const"])
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, (list, tuple)) or not enum or len(enum) > MAX_COLLECTION_ITEMS:
            raise ValidationError("JSON schema enum is invalid")
        values = [canonicalize_json(item) for item in enum]
        if any(
            _strict_equal(left, right)
            for index, left in enumerate(values)
            for right in values[index + 1 :]
        ):
            raise ValidationError("JSON schema enum contains duplicates")
        normalized["enum"] = values
    if "oneOf" in schema:
        one_of = schema["oneOf"]
        if not isinstance(one_of, (list, tuple)) or not one_of or len(one_of) > MAX_SCHEMA_BRANCHES:
            raise ValidationError("JSON schema oneOf is invalid")
        normalized["oneOf"] = [
            _normalize_schema(
                branch,
                depth=depth + 1,
                require_closed_object=False,
                allow_control_fields=allow_control_fields,
                allowed_transport_field_paths=allowed_transport_field_paths,
                property_path=property_path,
            )
            for branch in one_of
        ]

    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, Mapping)
            or len(properties) > MAX_COLLECTION_ITEMS
            or not isinstance(required, (list, tuple))
            or len(required) > MAX_COLLECTION_ITEMS
            or schema.get("additionalProperties") is not False
        ):
            raise ValidationError("JSON object schema must be exact and closed")
        normalized_properties: dict[str, object] = {}
        for name, child in properties.items():
            if not isinstance(name, str) or not name or "\x00" in name:
                raise ValidationError("JSON schema property name is invalid")
            child_path = (*property_path, name)
            if child_path not in allowed_transport_field_paths and (
                not allow_control_fields or name not in {"confirmation_token", "cursor"}
            ):
                _reject_transport_field(name)
            normalized_properties[name] = _normalize_schema(
                child,
                depth=depth + 1,
                require_closed_object=False,
                allow_control_fields=allow_control_fields,
                allowed_transport_field_paths=allowed_transport_field_paths,
                property_path=child_path,
            )
        normalized_required: list[str] = []
        for name in required:
            if not isinstance(name, str) or name not in normalized_properties:
                raise ValidationError("JSON schema requires an undeclared property")
            if name in normalized_required:
                raise ValidationError("JSON schema required fields contain duplicates")
            normalized_required.append(name)
        normalized["additionalProperties"] = False
        normalized["properties"] = dict(sorted(normalized_properties.items()))
        normalized["required"] = normalized_required
    elif any(key in schema for key in ("additionalProperties", "properties", "required")):
        raise ValidationError("JSON object keywords require an object type")
    elif require_closed_object:
        raise ValidationError("operation input schema must be an exact closed object")

    if schema_type == "array":
        if "items" not in schema:
            raise ValidationError("JSON array schema requires exact item semantics")
        normalized["items"] = _normalize_schema(
            schema["items"],
            depth=depth + 1,
            require_closed_object=False,
            allow_control_fields=allow_control_fields,
            allowed_transport_field_paths=allowed_transport_field_paths,
            property_path=(*property_path, "[]"),
        )
    elif "items" in schema:
        raise ValidationError("JSON schema items require an array type")

    _copy_integer_bounds(schema, normalized, "minLength", "maxLength", MAX_STRING_LENGTH)
    _copy_integer_bounds(
        schema,
        normalized,
        "minItems",
        "maxItems",
        MAX_JSON_ARRAY_ITEMS,
    )
    if any(key in normalized for key in ("minLength", "maxLength")) and schema_type != "string":
        raise ValidationError("JSON string bounds require a string type")
    if any(key in normalized for key in ("minItems", "maxItems")) and schema_type != "array":
        raise ValidationError("JSON array bounds require an array type")

    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or not pattern or len(pattern) > 1_024:
            raise ValidationError("JSON schema pattern is invalid")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValidationError("JSON schema pattern is invalid") from exc
        if schema_type != "string":
            raise ValidationError("JSON schema pattern requires a string type")
        normalized["pattern"] = pattern

    for keyword in ("minimum", "maximum"):
        if keyword in schema:
            bound = schema[keyword]
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                raise ValidationError("JSON schema numeric bound is invalid")
            if not math.isfinite(bound):
                raise ValidationError("JSON schema numeric bound is invalid")
            normalized[keyword] = bound
    if any(key in normalized for key in ("minimum", "maximum")) and schema_type not in {
        "integer",
        "number",
    }:
        raise ValidationError("JSON numeric bounds require a numeric type")
    _check_ordered_bounds(normalized, "minimum", "maximum", "numeric")

    if not any(key in normalized for key in ("type", "const", "enum", "oneOf")):
        raise ValidationError("JSON schema does not constrain its value")
    return normalized


def _copy_integer_bounds(
    source: Mapping[object, object],
    target: dict[str, object],
    minimum_name: str,
    maximum_name: str,
    hard_maximum: int,
) -> None:
    for name in (minimum_name, maximum_name):
        if name in source:
            value = source[name]
            if type(value) is not int or not 0 <= value <= hard_maximum:
                raise ValidationError("JSON schema size bound is invalid")
            target[name] = value
    _check_ordered_bounds(target, minimum_name, maximum_name, "size")


def _check_ordered_bounds(
    schema: Mapping[str, object],
    minimum_name: str,
    maximum_name: str,
    label: str,
) -> None:
    minimum = schema.get(minimum_name)
    maximum = schema.get(maximum_name)
    if (
        minimum is not None
        and maximum is not None
        and cast(float | int, minimum) > cast(float | int, maximum)
    ):
        raise ValidationError(f"JSON schema {label} bounds are reversed")


def _validate_value(value: object, schema: Mapping[str, object], *, depth: int) -> object:
    if depth > MAX_JSON_DEPTH:
        raise ValidationError("JSON value is too deeply nested")
    if "oneOf" in schema:
        matches: list[object] = []
        for branch in cast(Sequence[Mapping[str, object]], schema["oneOf"]):
            try:
                matches.append(_validate_value(value, branch, depth=depth + 1))
            except ValidationError:
                continue
        if len(matches) != 1:
            raise ValidationError("JSON oneOf does not select exactly one variant")
        value = matches[0]
    if "const" in schema and not _strict_equal(value, schema["const"]):
        raise ValidationError("JSON value does not match its constant")
    if "enum" in schema and not any(
        _strict_equal(value, allowed) for allowed in cast(Sequence[object], schema["enum"])
    ):
        raise ValidationError("JSON value is not an allowed enum member")

    schema_type = schema.get("type")
    if isinstance(schema_type, str) and not _matches_type(value, schema_type):
        raise ValidationError("JSON value has the wrong type")
    if schema_type == "object":
        obj = cast(dict[str, object], value)
        properties = cast(Mapping[str, Mapping[str, object]], schema["properties"])
        required = cast(Sequence[str], schema["required"])
        if any(name not in obj for name in required):
            raise ValidationError("JSON object is missing a required field")
        if any(name not in properties for name in obj):
            raise ValidationError("JSON object contains an unknown field")
        return {
            name: _validate_value(obj[name], properties[name], depth=depth + 1)
            for name in sorted(obj)
        }
    if schema_type == "array":
        items = cast(list[object], value)
        _validate_length(items, schema, "minItems", "maxItems", "array")
        item_schema = cast(Mapping[str, object], schema["items"])
        return [_validate_value(item, item_schema, depth=depth + 1) for item in items]
    if schema_type == "string":
        text = cast(str, value)
        _validate_length(text, schema, "minLength", "maxLength", "string")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, text) is None:
            raise ValidationError("JSON string does not match its pattern")
    if schema_type in {"integer", "number"}:
        number = cast(int | float, value)
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and number < minimum:
            raise ValidationError("JSON number is below its minimum")
        if isinstance(maximum, (int, float)) and number > maximum:
            raise ValidationError("JSON number is above its maximum")
    return value


def _matches_type(value: object, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "integer":
        return type(value) is int
    if schema_type == "number":
        return type(value) in {int, float} and math.isfinite(cast(float, value))
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return False


def _validate_length(
    value: Sequence[object] | str,
    schema: Mapping[str, object],
    minimum_name: str,
    maximum_name: str,
    label: str,
) -> None:
    minimum = schema.get(minimum_name)
    maximum = schema.get(maximum_name)
    if isinstance(minimum, int) and len(value) < minimum:
        raise ValidationError(f"JSON {label} is shorter than allowed")
    if isinstance(maximum, int) and len(value) > maximum:
        raise ValidationError(f"JSON {label} exceeds its bound")


def _strict_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return len(left) == len(right) and all(
            key in right and _strict_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _reject_transport_field(name: str) -> None:
    normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
    if normalized in _FORBIDDEN_TRANSPORT_FIELDS or any(
        normalized.endswith(term)
        for term in (
            "authorization",
            "endpoint",
            "headers",
            "method",
            "nextlink",
            "token",
            "uploadurl",
            "url",
        )
    ):
        raise ValidationError("operation input contains a forbidden transport field")


def _operation_transport_field_paths(
    provider: str,
    mode: ConnectorMode,
    name: str,
) -> frozenset[tuple[str, ...]]:
    if (provider, mode, name) in _CALENDAR_ATTACHMENT_OPERATIONS:
        return frozenset({_CALENDAR_ATTACHMENT_FILE_URL_PATH})
    return frozenset()

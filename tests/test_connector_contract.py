from __future__ import annotations

import pytest

from continuity_kernel.connector_contract import (
    MAX_COLLECTION_ITEMS,
    ConnectorEffect,
    ConnectorMode,
    OperationCatalog,
    OperationSpec,
    validate_json,
)
from continuity_kernel.errors import ValidationError


def _object(
    properties: dict[str, object],
    *,
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
        "type": "object",
    }


def _spec(
    name: str = "items.list",
    *,
    mode: ConnectorMode = ConnectorMode.READ,
    effect: ConnectorEffect = ConnectorEffect.READ,
    scopes: tuple[frozenset[str], ...] = (frozenset(),),
) -> OperationSpec:
    return OperationSpec(
        provider="demo",
        mode=mode,
        name=name,
        effect=effect,
        endpoint=name,
        required_scopes=scopes,
        input_schema=_object(
            {"query": {"maxLength": 1_024, "minLength": 1, "type": "string"}},
            required=["query"],
        ),
    )


def test_catalog_validates_exact_typed_inputs_and_detaches_them() -> None:
    operation = OperationSpec(
        provider="demo",
        mode=ConnectorMode.READ,
        name="items.list",
        effect=ConnectorEffect.READ,
        endpoint="items.list",
        required_scopes=(frozenset(),),
        input_schema=_object(
            {
                "count": {"maximum": 10, "minimum": 1, "type": "integer"},
                "kind": {"enum": ["brief", "full"], "type": "string"},
                "query": {"maxLength": 32, "minLength": 1, "type": "string"},
            },
            required=["count", "kind", "query"],
        ),
    )
    catalog = OperationCatalog((operation,))
    original = {"query": "inbox", "kind": "brief", "count": 2}

    validated = catalog.validate_input("demo", ConnectorMode.READ, "items.list", original)

    assert validated == {"count": 2, "kind": "brief", "query": "inbox"}
    assert validated is not original
    original["query"] = "changed"
    assert validated["query"] == "inbox"
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "demo",
            ConnectorMode.READ,
            "items.list",
            {"count": 2, "kind": "brief", "query": "inbox", "method": "GET"},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "demo",
            ConnectorMode.READ,
            "items.list",
            {"count": True, "kind": "brief", "query": "inbox"},
        )


def test_catalog_rejects_duplicate_invalid_and_mismatched_operations() -> None:
    operation = _spec()
    with pytest.raises(ValidationError):
        OperationCatalog((operation, operation))
    with pytest.raises(ValidationError):
        _spec(name="https://arbitrary.example")
    with pytest.raises(ValidationError):
        _spec(mode=ConnectorMode.WRITE, effect=ConnectorEffect.READ)
    with pytest.raises(ValidationError):
        OperationCatalog((operation,)).lookup("demo", ConnectorMode.READ, "items.get")


def test_scope_alternatives_are_or_of_complete_scope_sets() -> None:
    operation = _spec(
        scopes=(
            frozenset({"items:read"}),
            frozenset({"items:write", "workspace:member"}),
        )
    )

    assert operation.scope_grant_satisfies(["items:read", "extra"])
    assert operation.scope_grant_satisfies({"items:write", "workspace:member"})
    assert not operation.scope_grant_satisfies(["items:write"])
    assert _spec().scope_grant_satisfies([])


def test_tool_schema_uses_only_connection_operation_input_and_sealed_state() -> None:
    read = _spec()
    write = _spec(
        "items.update",
        mode=ConnectorMode.WRITE,
        effect=ConnectorEffect.SAFE_MUTATION,
    )
    catalog = OperationCatalog((read, write))
    read_schema = catalog.tool_input_schema("demo", ConnectorMode.READ)
    write_schema = catalog.tool_input_schema("demo", ConnectorMode.WRITE)
    connection_id = "con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    read_call = {
        "connection_id": connection_id,
        "cursor": "v1.payload.mac",
        "input": {"query": "inbox"},
        "operation": "items.list",
    }
    write_call = {
        "confirmation_token": "v1.payload.mac",
        "connection_id": connection_id,
        "input": {"query": "inbox"},
        "operation": "items.update",
    }
    assert validate_json(read_call, read_schema) == read_call
    assert validate_json(write_call, write_schema) == write_call
    for forbidden in ("provider", "mode", "method", "url", "headers", "token"):
        with pytest.raises(ValidationError):
            validate_json({**read_call, forbidden: "arbitrary"}, read_schema)
    with pytest.raises(ValidationError):
        validate_json({**write_call, "cursor": "v1.payload.mac"}, write_schema)


def test_schema_boundary_rejects_proxy_nonfinite_oversized_deep_and_ambiguous_values() -> None:
    with pytest.raises(ValidationError):
        OperationSpec(
            provider="demo",
            mode=ConnectorMode.READ,
            name="bad.schema",
            effect=ConnectorEffect.READ,
            endpoint="bad.schema",
            required_scopes=(frozenset(),),
            input_schema=_object({"proxy_url": {"type": "string"}}),
        )
    with pytest.raises(ValidationError):
        validate_json(1, {"oneOf": [{"type": "integer"}, {"type": "number"}]})
    with pytest.raises(ValidationError):
        validate_json(float("nan"), {"type": "number"})
    with pytest.raises(ValidationError):
        validate_json({1: "not JSON"}, _object({}))
    with pytest.raises(ValidationError):
        validate_json(
            list(range(MAX_COLLECTION_ITEMS + 1)),
            {"items": {"type": "integer"}, "type": "array"},
        )

    deep: object = "leaf"
    for _ in range(20):
        deep = [deep]
    with pytest.raises(ValidationError):
        validate_json(deep, {"items": {"type": "string"}, "type": "array"})


def test_schema_subset_handles_large_text_arrays_patterns_and_numeric_bounds() -> None:
    schema = _object(
        {
            "amount": {"maximum": 5, "minimum": 2, "type": "number"},
            "items": {
                "items": {"type": "string"},
                "maxItems": 2,
                "minItems": 1,
                "type": "array",
            },
            "message": {
                "maxLength": 10_000,
                "minLength": 2,
                "pattern": r"^[a-z]+$",
                "type": "string",
            },
        },
        required=["amount", "items", "message"],
    )
    valid = {"amount": 2.5, "items": ["one"], "message": "hello"}
    assert validate_json(valid, schema) == valid
    with pytest.raises(ValidationError):
        validate_json({**valid, "amount": 1}, schema)
    with pytest.raises(ValidationError):
        validate_json({**valid, "items": []}, schema)
    with pytest.raises(ValidationError):
        validate_json({**valid, "message": "HELLO"}, schema)

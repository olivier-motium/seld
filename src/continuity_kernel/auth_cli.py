"""Standalone human-facing connector authentication CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from continuity_kernel.config import resolve_vault
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_auth_transfer import export_auth_archive, import_auth_archive
from continuity_kernel.connector_identifiers import new_connection_id, parse_connection_id
from continuity_kernel.connector_secrets import MAX_SECRET_BYTES
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.vault import Vault


def main(arguments: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        vault = Vault(resolve_vault(args.vault))
        manager = ConnectorAuthManager(vault)
        result = _dispatch(args, manager)
        _print(result, json_output=args.json)
        return 0
    except ContinuityError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "ok": False}), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace, manager: ConnectorAuthManager) -> dict[str, object]:
    if args.command == "status":
        return manager.status()
    if args.command == "add":
        return _add(args, manager)
    if args.command == "credential":
        return _credential(args, manager)
    if args.command == "oauth":
        manager.authorize_oauth(
            parse_connection_id(args.connection_id),
            timeout_seconds=args.timeout,
        )
        return _connection_result(manager, args.connection_id)
    if args.command == "remove":
        return manager.remove(
            parse_connection_id(args.connection_id),
            expected_revision=args.expected_revision,
        )
    if args.command == "export":
        return export_auth_archive(
            manager,
            Path(args.output),
            recipient=args.recipient,
            age_executable=Path(args.age_executable) if args.age_executable else None,
        )
    if args.command == "import":
        return import_auth_archive(
            manager,
            Path(args.archive),
            identity=Path(args.identity),
            age_executable=Path(args.age_executable) if args.age_executable else None,
        )
    raise ValidationError("unsupported connector auth command")


def _add(args: argparse.Namespace, manager: ConnectorAuthManager) -> dict[str, object]:
    now = datetime.now(UTC)
    kind = CredentialKind(args.kind)
    if kind is CredentialKind.OAUTH2:
        client = ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=args.client_id,
            redirect_uris=(args.redirect_uri,),
            authorization_endpoint=args.authorization_endpoint,
            token_endpoint=args.token_endpoint,
        )
    else:
        if any(
            value is not None
            for value in (
                args.client_id,
                args.redirect_uri,
                args.authorization_endpoint,
                args.token_endpoint,
            )
        ):
            raise ValidationError("OAuth client arguments require --kind oauth2")
        client = ClientMetadata(kind=ClientKind.EXTERNAL)
    metadata = ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=args.provider,
        source_ids=tuple(args.source),
        credential_kind=kind,
        account=AccountMetadata(
            fingerprint=args.account_fingerprint,
            label=args.label,
        ),
        scopes=tuple(args.scope),
        client=client,
        health=ConnectionHealth.UNKNOWN,
        created_at=now,
        updated_at=now,
        version=1,
    )
    snapshot = manager.vault.get_connection_snapshot()
    return manager.vault.put_connection(
        expected_revision=snapshot.revision,
        connection=metadata,
        observed_at=now,
    )


def _credential(
    args: argparse.Namespace,
    manager: ConnectorAuthManager,
) -> dict[str, object]:
    connection_id = parse_connection_id(args.connection_id)
    current = manager.tokens.state(connection_id)
    if current is not None and not args.replace:
        raise ValidationError("credential already exists; pass --replace to rotate it")
    if current is None and args.replace:
        raise ValidationError("credential does not exist; omit --replace for the first value")
    value = _read_secret()
    manager.store_credential(
        connection_id,
        value,
        expected_token_version=current.version if current else 0,
    )
    return _connection_result(manager, str(connection_id))


def _connection_result(
    manager: ConnectorAuthManager,
    connection_id: str,
) -> dict[str, object]:
    status = manager.status()
    rows = status["connections"]
    assert isinstance(rows, list)
    connection = next(
        row for row in rows if isinstance(row, dict) and row.get("connection_id") == connection_id
    )
    return {
        "connection": connection,
        "revision": status["revision"],
        "vault_id": status["vault_id"],
    }


def _read_secret() -> bytes:
    if sys.stdin.isatty():
        value = getpass.getpass("Credential (input hidden): ").encode("utf-8")
    else:
        value = sys.stdin.buffer.read(MAX_SECRET_BYTES + 1)
    if not value or len(value) > MAX_SECRET_BYTES:
        raise ValidationError("credential is empty or exceeds its size bound")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsv-auth",
        description="Portable connector authentication independent of any AI host login.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--vault")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show redacted portable and host-local status.")

    add = commands.add_parser("add", help="Create portable non-secret connection metadata.")
    add.add_argument("--provider", required=True)
    add.add_argument("--source", action="append", required=True)
    add.add_argument("--kind", required=True, choices=tuple(item.value for item in CredentialKind))
    add.add_argument("--label")
    add.add_argument("--account-fingerprint")
    add.add_argument("--scope", action="append", default=[])
    add.add_argument("--client-id")
    add.add_argument("--authorization-endpoint")
    add.add_argument("--token-endpoint")
    add.add_argument("--redirect-uri")

    credential = commands.add_parser(
        "credential",
        help="Read one non-OAuth credential from hidden input or stdin.",
    )
    credential.add_argument("connection_id")
    credential.add_argument("--replace", action="store_true")

    oauth = commands.add_parser("oauth", help="Run one native OAuth authorization-code flow.")
    oauth.add_argument("connection_id")
    oauth.add_argument("--timeout", type=float, default=180.0)

    remove = commands.add_parser("remove", help="Revoke host custody, then remove metadata.")
    remove.add_argument("connection_id")
    remove.add_argument("--expected-revision", required=True)

    export = commands.add_parser("export", help="Create an age-encrypted credential archive.")
    export.add_argument("--output", required=True)
    export.add_argument("--recipient", required=True)
    export.add_argument("--age-executable")

    import_command = commands.add_parser(
        "import",
        help="Restore credentials onto a matching or exactly resumable portable snapshot.",
    )
    import_command.add_argument("archive")
    import_command.add_argument("--identity", required=True)
    import_command.add_argument("--age-executable")
    return parser


def _print(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": True, "result": value}, separators=(",", ":")))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))

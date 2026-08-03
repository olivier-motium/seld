"""Standalone human-facing connector authentication CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from continuity_kernel.connector_profiles import (
    PROFILES,
    format_connector_command,
    get_profile,
    list_profiles,
)
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
        status = manager.status()
        if args.vault is not None:
            return _qualify_status_recovery(status, manager.vault.root)
        return status
    if args.command == "profiles":
        return {"profiles": list_profiles()}
    if args.command == "add":
        return _add(args, manager)
    if args.command == "credential":
        return _credential(args, manager)
    if args.command == "oauth":
        manager.authorize_oauth(
            parse_connection_id(args.connection_id),
            timeout_seconds=args.timeout,
            browser_opener=_oauth_browser(args),
            present_authorization_url=_present_authorization_url,
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
        result = import_auth_archive(
            manager,
            Path(args.archive),
            identity=Path(args.identity),
            age_executable=Path(args.age_executable) if args.age_executable else None,
        )
        connection_ids = result.get("connection_ids")
        if not isinstance(connection_ids, list) or not all(
            isinstance(connection_id, str) for connection_id in connection_ids
        ):
            raise ValidationError("connector auth import omitted its connection IDs")
        return {
            **result,
            "resume_commands": [
                _resume_command(
                    connection_id,
                    vault=manager.vault.root if args.vault is not None else None,
                )
                for connection_id in connection_ids
            ],
        }
    raise ValidationError("unsupported connector auth command")


def _qualify_status_recovery(
    status: dict[str, object],
    vault: Path,
) -> dict[str, object]:
    connections = status.get("connections")
    if not isinstance(connections, list):
        raise ValidationError("connector auth status is invalid")
    projected: list[object] = []
    connector_prefix = format_connector_command(("gsv", "--vault", str(vault), "connectors"))
    auth_prefix = format_connector_command(("gsv-auth", "--vault", str(vault)))
    for value in connections:
        if not isinstance(value, dict):
            projected.append(value)
            continue
        row = dict(value)
        recovery = row.get("next")
        if isinstance(recovery, str):
            row["next"] = re.sub(
                r"gsv connectors|gsv-auth",
                lambda match: (
                    connector_prefix if match.group() == "gsv connectors" else auth_prefix
                ),
                recovery,
            )
        projected.append(row)
    return {**status, "connections": projected}


def _resume_command(connection_id: str, *, vault: Path | None) -> str:
    arguments = ["gsv"]
    if vault is not None:
        arguments.extend(("--vault", str(vault)))
    arguments.extend(("connectors", "resume", connection_id))
    return format_connector_command(arguments)


def _add(args: argparse.Namespace, manager: ConnectorAuthManager) -> dict[str, object]:
    now = datetime.now(UTC)
    if args.profile is not None:
        if any(
            value is not None
            for value in (
                args.provider,
                args.source,
                args.kind,
                args.scope,
                args.authorization_endpoint,
                args.token_endpoint,
            )
        ):
            raise ValidationError("profile-owned connector fields cannot be overridden")
        profile = get_profile(args.profile)
        provider = profile.provider
        source_ids = profile.source_ids
        kind = profile.credential_kind
        scopes = profile.read_scopes
        authorization_endpoint = profile.authorization_endpoint
        token_endpoint = profile.token_endpoint
    else:
        if args.provider is None or args.source is None or args.kind is None:
            raise ValidationError("manual add requires --provider, --source, and --kind")
        provider = args.provider
        source_ids = tuple(args.source)
        kind = CredentialKind(args.kind)
        if kind is CredentialKind.OAUTH2:
            raise ValidationError("manual OAuth is unsupported; use a built-in profile")
        scopes = tuple(args.scope or ())
        authorization_endpoint = args.authorization_endpoint
        token_endpoint = args.token_endpoint
    if kind is CredentialKind.OAUTH2:
        if args.client_id is None or args.redirect_uri is None:
            raise ValidationError("OAuth connections require --client-id and --redirect-uri")
        if args.profile == "google":
            _validate_google_redirect(args.redirect_uri)
        elif args.profile == "microsoft":
            _validate_microsoft_redirect(args.redirect_uri)
        elif args.profile == "slack":
            _validate_slack_redirect(args.redirect_uri)
        client = ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=args.client_id,
            redirect_uris=(args.redirect_uri,),
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
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
        provider=provider,
        source_ids=source_ids,
        credential_kind=kind,
        account=AccountMetadata(
            fingerprint=args.account_fingerprint,
            label=args.label,
        ),
        scopes=scopes,
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


def _validate_slack_redirect(redirect_uri: str) -> None:
    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Slack profile redirect URI is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or port in {None, 0}
        or parsed.path != "/oauth/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "Slack profile requires an exact registered http://localhost:<port>/oauth/callback"
        )


def _validate_google_redirect(redirect_uri: str) -> None:
    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Google profile redirect URI is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.path
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "Google profile requires http://127.0.0.1:<port> or http://[::1]:<port>"
        )


def _validate_microsoft_redirect(redirect_uri: str) -> None:
    try:
        parsed = urlsplit(redirect_uri)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Microsoft profile redirect URI is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or port is None
        or parsed.path != "/oauth/callback"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "Microsoft profile requires an exact http://localhost:<port>/oauth/callback"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsv-auth",
        description="Portable connector authentication independent of any AI host login.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--vault")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show redacted portable and host-local status.")
    commands.add_parser("profiles", help="List finite built-in connector profiles.")

    add = commands.add_parser("add", help="Create portable non-secret connection metadata.")
    add.add_argument("--profile", choices=tuple(sorted(PROFILES)))
    add.add_argument("--provider")
    add.add_argument("--source", action="append")
    add.add_argument("--kind", choices=tuple(item.value for item in CredentialKind))
    add.add_argument("--label")
    add.add_argument("--account-fingerprint")
    add.add_argument("--scope", action="append")
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
    oauth.add_argument("--browser", choices=("default", "firefox"), default="default")
    oauth.add_argument("--no-browser", action="store_true")

    remove = commands.add_parser(
        "remove",
        help="CAS-revoke metadata, clear host custody, then remove the record.",
    )
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


def _oauth_browser(args: argparse.Namespace) -> Any:
    if args.no_browser:
        return None
    if args.browser == "default":
        return webbrowser.open
    try:
        return webbrowser.get("firefox").open
    except webbrowser.Error:
        return lambda url: False


def _present_authorization_url(url: str, browser_opened: bool) -> None:
    if browser_opened:
        print("Your browser is open. Finish sign-in there; Seld is waiting…", file=sys.stderr)
        return
    print("Open this sign-in URL in a browser, then finish there:", file=sys.stderr)
    print(url, file=sys.stderr)

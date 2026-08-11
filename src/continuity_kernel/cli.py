"""Command-line interface for humans, installers, Codex, and tests."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

import continuity_kernel.update as self_update
from continuity_kernel import __version__, resident_import, whatsapp
from continuity_kernel.bridge import (
    bridge_status,
    open_bridge,
    open_bridge_in_browser,
    serve_bridge,
    stop_bridge,
)
from continuity_kernel.bridge_launcher import (
    current_gsv_executable,
    install_native_bridge,
    native_bridge_status,
    open_native_bridge,
    uninstall_native_bridge,
)
from continuity_kernel.codex_integration import (
    codex_status,
    install_codex,
    install_codex_transaction,
    uninstall_codex,
)
from continuity_kernel.config import (
    activate_config,
    codex_home,
    load_config,
    resolve_vault,
    restore_config,
    save_config,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_onboarding import (
    BrowserMode,
    ConnectorIdentityReview,
    ConnectorOnboarding,
    provider_revocation_guidance,
)
from continuity_kernel.connector_operations import CONNECTOR_PROFILE
from continuity_kernel.connector_profiles import (
    CONNECTOR_PROFILES,
    ConnectorAccessTier,
)
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED
from continuity_kernel.demo import run_demo
from continuity_kernel.direction import direction_aim, direction_dict
from continuity_kernel.discord_source import DiscordSourceBridge
from continuity_kernel.errors import ContinuityError, SetupError, ValidationError
from continuity_kernel.local_source_delivery import (
    FORWARD_ONLY_RESET,
    SUPPORTED_LOCAL_SOURCES,
    VERIFIED_PREFIX_ADOPTION,
    LocalSourceDelivery,
)
from continuity_kernel.mcp_server import GUIDED_REVIEW_PROFILE, serve
from continuity_kernel.operations import OperationLedger, capture_operation_binding
from continuity_kernel.portfolio import (
    portfolio_dict,
    portfolio_inspection_dict,
    portfolio_item,
)
from continuity_kernel.recall import RecallCompanion
from continuity_kernel.records import (
    TaskEntityLink,
    WorkThreadEntityLink,
    WorkThreadTaskLink,
    record_dict,
)
from continuity_kernel.resident_context import (
    execution_bindings,
    read_resident_guidance,
    resident_context_status,
)
from continuity_kernel.scheduler import MacOSScheduler, scheduler_dict
from continuity_kernel.sense_sweep import heartbeat_status, sense_sweep
from continuity_kernel.slack_tasks import SlackTaskReader
from continuity_kernel.source_recipes import list_recipes
from continuity_kernel.source_state import SOURCE_ERROR_CODES
from continuity_kernel.vault import Vault, doctor_dict

ROLLBACK_PROBE_TIMEOUT_SECONDS = 5
ROLLBACK_PROBE_MAX_OUTPUT_BYTES = 64 * 1024


def main(arguments: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(arguments)
    try:
        if not getattr(args, "command", None):
            if load_config(required=False) is None:
                parser.print_help()
                return 0
            args.command = "bridge"
            args.bridge_command = "open"
            args.no_browser = False
        if args.command == "mcp":
            return serve(
                Vault(resolve_vault(getattr(args, "vault", None))),
                profile=args.profile,
                event_id=args.event_id,
            )
        result = _dispatch(args)
        failure = _result_failure(args, result)
        _print(
            result,
            json_output=args.json,
            raw=getattr(args, "raw", False),
            ok=failure is None,
            error=failure[1] if failure is not None else None,
        )
        if failure is not None:
            if not args.json:
                print(f"Error: {failure[1]}", file=sys.stderr)
            return failure[0]
        return 0
    except ContinuityError as exc:
        oauth_guidance = (
            _connector_oauth_failure_guidance(args)
            if exc.provider_authorization_may_remain
            else None
        )
        if getattr(args, "json", False):
            payload: dict[str, object] = {"error": str(exc), "ok": False}
            if oauth_guidance is not None:
                payload.update(
                    {
                        "provider_access_may_remain": True,
                        "revocation_help": oauth_guidance,
                    }
                )
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
            if oauth_guidance is not None:
                print(
                    "If provider sign-in completed, provider access may remain. "
                    f"{oauth_guidance} Run `gsv connectors list` before retrying.",
                    file=sys.stderr,
                )
        return 2
    except KeyboardInterrupt as exc:
        authorization_may_remain = bool(getattr(exc, "provider_authorization_may_remain", False))
        guidance = _connector_oauth_failure_guidance(args) if authorization_may_remain else None
        if getattr(args, "json", False):
            interrupted_payload: dict[str, object] = {"error": "cancelled", "ok": False}
            if guidance is not None:
                interrupted_payload.update(
                    {
                        "provider_access_may_remain": True,
                        "revocation_help": guidance,
                    }
                )
            print(json.dumps(interrupted_payload, ensure_ascii=False), file=sys.stderr)
        elif guidance is not None:
            print(
                "Cancelled locally after provider sign-in may have started. "
                f"{guidance} Run `gsv connectors list` before retrying or reauthorizing.",
                file=sys.stderr,
            )
        else:
            print("Cancelled.", file=sys.stderr)
        return 130


def _connector_oauth_failure_guidance(args: argparse.Namespace) -> str | None:
    if getattr(args, "command", None) != "connectors" or getattr(
        args, "connectors_command", None
    ) not in {"connect", "reauthorize"}:
        return None
    connector = getattr(args, "connector", None)
    if connector == "discord":
        return None
    profile = CONNECTOR_PROFILES.get(connector) if isinstance(connector, str) else None
    if profile is not None:
        return provider_revocation_guidance(profile.provider)
    return "Review the provider's connected-app settings."


def _dispatch(args: argparse.Namespace) -> Any:
    explicit_vault = getattr(args, "vault", None)
    if args.command == "setup":
        vault_path = resolve_vault(explicit_vault, require_config=False)
        vault = Vault(vault_path)
        initialized = vault.initialize(name=args.name, command="gsv")
        doctor = doctor_dict(vault.doctor())
        connector_registration = _connector_registration_status(vault)
        if not doctor["healthy"]:
            return {
                "bridge": None,
                "codex": None,
                "connectors": connector_registration,
                "doctor": doctor,
                "next": _doctor_next(vault_path, doctor),
                "setup_complete": False,
                "vault": initialized,
            }
        _, previous_config, installed_config = activate_config(vault_path)
        integration = None
        bridge = None
        try:
            if not args.no_codex:
                with install_codex_transaction(
                    vault=vault_path, codex_home=Path(args.codex_home)
                ) as staged:
                    integration = asdict(staged)
        except Exception as exc:
            rollback_error = restore_config(previous_config, expected=installed_config)
            if rollback_error is not None:
                raise SetupError(f"{exc}; {rollback_error}") from exc
            raise
        # The stable Codex ownership receipt is committed before a resident
        # surface can start.  A Bridge failure is therefore an operational
        # repair state, not a reason to roll back a compatible installation or
        # expose a half-committed plugin to a running process.
        if not args.no_bridge:
            try:
                bridge = open_bridge(vault, open_browser=False)
            except Exception as exc:
                # open_bridge owns cleanup for a child it starts.  A failure
                # may instead describe an already-running Bridge for another
                # vault, which setup must never stop implicitly.
                bridge = {
                    "available": False,
                    "browser_opened": False,
                    "error": str(exc),
                    "running": False,
                    "started": False,
                }
        if bridge is not None and bridge.get("running") and not args.no_browser:
            try:
                browser_opened = open_bridge_in_browser(vault)
                bridge = {**bridge, "browser_opened": browser_opened}
            except Exception as exc:
                # Browser launch is a best-effort presentation step after the
                # installation receipt is committed and the Bridge is live.
                # Preserve that working state and surface a manual-open path.
                bridge = {
                    **bridge,
                    "browser_error": str(exc),
                    "browser_opened": False,
                }
        return {
            "bridge": bridge,
            "codex": integration,
            "connectors": connector_registration,
            "doctor": doctor,
            "next": _setup_next(
                no_codex=args.no_codex,
                no_bridge=args.no_bridge,
                no_browser=args.no_browser,
                bridge=bridge,
            ),
            "setup_complete": True,
            "vault": initialized,
        }

    if args.command == "init":
        path = resolve_vault(explicit_vault, require_config=False)
        result = Vault(path).initialize(name=args.name)
        if args.configure:
            save_config(path)
        return result
    if args.command == "demo":
        return run_demo(Path(args.output).expanduser().resolve() if args.output else None)
    if args.command == "rollback-check":
        vault = Vault(resolve_vault(explicit_vault))
        return _rollback_compatibility(vault, Path(args.previous_executable))
    if args.command == "codex":
        home = Path(args.codex_home).expanduser().resolve()
        if args.codex_command == "install":
            path = resolve_vault(explicit_vault)
            return asdict(install_codex(vault=path, codex_home=home))
        if args.codex_command == "status":
            return codex_status(codex_home=home)
        if args.codex_command == "uninstall":
            return uninstall_codex(codex_home=home)
        raise AssertionError("unreachable Codex command")
    if args.command == "bridge":
        if args.bridge_command == "status":
            return bridge_status()
        if args.bridge_command == "stop":
            return stop_bridge()
        if args.bridge_command == "native-status":
            return native_bridge_status()
        if args.bridge_command == "native-open":
            return open_native_bridge()
        if args.bridge_command == "native-uninstall":
            return uninstall_native_bridge(expected_revision=args.expected_revision)
        vault = Vault(resolve_vault(explicit_vault))
        if args.bridge_command == "native-install":
            return install_native_bridge(
                vault,
                executable=current_gsv_executable(),
                expected_revision=args.expected_revision,
            )
        if args.bridge_command == "open":
            return open_bridge(vault, open_browser=not args.no_browser)
        if args.bridge_command == "serve":
            return {
                "port": serve_bridge(
                    vault,
                    port=args.port,
                    instance_id=args.instance_id,
                ),
                "stopped": True,
            }
        raise AssertionError("unreachable Bridge command")
    if args.command == "update":
        if args.update_command == "status":
            return self_update.status()
        if args.update_command == "check":
            return self_update.check(force=args.force)
        vault = Vault(resolve_vault(explicit_vault))
        if args.update_command == "apply":
            return self_update.apply(
                vault,
                from_sha=args.from_sha,
                to_sha=args.to_sha,
                expected_check_revision=args.expected_check_revision,
                approval_ref=args.approval_ref,
            )
        if args.update_command == "recover":
            return self_update.recover(
                vault,
                token=args.token,
                expected_vault_digest=args.expected_vault_digest,
                approval_ref=args.approval_ref,
            )
        raise AssertionError("unreachable update command")
    if args.command == "backup":
        if args.backup_command == "verify":
            return Vault.verify_backup(Path(args.path))
        if args.backup_command == "restore":
            restored = Vault.restore_backup(Path(args.path), Path(args.target))
            restored_path = Path(restored["restored"])
            configuration_matches = _configuration_matches_target(restored_path)
            setup_command = f"gsv --vault {shlex.quote(str(restored_path))} setup"
            status_command = f"gsv --vault {shlex.quote(str(restored_path))} status"
            if configuration_matches is True:
                availability = (
                    "The restored vault is usable now with the explicit status command, and the "
                    "existing configuration already points to it."
                )
            elif configuration_matches is False:
                availability = (
                    "The restored vault is usable now with the explicit status command; the "
                    "existing configuration was not changed."
                )
            else:
                availability = (
                    "The restored vault is usable now with the explicit status command. The "
                    "existing configuration could not be read safely and was not changed."
                )
            return {
                **restored,
                "activation_commands": ["gsv bridge stop", setup_command],
                "activation_required": True,
                "configuration_changed": False,
                "configuration_matches_target": configuration_matches,
                "next": (
                    f"{availability} Run `{status_command}` to inspect it. To bind Codex plus The "
                    "Bridge to this restored vault, first run `gsv bridge stop`; after it confirms "
                    f"the old Bridge stopped, run `{setup_command}`."
                ),
            }
    if args.command == "migration":
        export_path = Path(args.export)
        target = Path(args.target)
        if args.migration_command == "inspect":
            return resident_import.inspect_resident_export(export_path, target)
        if args.migration_command == "apply":
            return resident_import.apply_resident_export(
                export_path,
                target,
                expected_plan_revision=args.expected_plan_revision,
            )
        raise AssertionError("unreachable migration command")

    vault = Vault(resolve_vault(explicit_vault))
    if args.command == "status":
        return vault.status()
    if args.command == "doctor":
        result = doctor_dict(vault.doctor(repair=args.repair))
        try:
            result["codex"] = {"available": True, **codex_status(codex_home=codex_home())}
        except ContinuityError as exc:
            result["codex"] = {"available": False, "error": str(exc)}
        result["connectors"] = _connector_registration_status(vault)
        return result
    if args.command == "context":
        context = vault.context_pack(max_characters=args.max_characters)
        return context if args.format == "markdown" else {"context": context}
    if args.command == "resident-context":
        if args.resident_context_command == "status":
            return resident_context_status(vault.root)
        if args.resident_context_command == "show":
            guidance = read_resident_guidance(vault.root)
            return guidance["content"] if args.format == "markdown" else guidance
        raise AssertionError("unreachable resident-context command")
    if args.command == "execution-bindings":
        return execution_bindings(vault)
    if args.command == "connectors":
        return _connectors(vault, args)
    if args.command.startswith("slack-"):
        if args.command == "slack-capabilities":
            return SlackTaskReader.capabilities()
        reader = SlackTaskReader(vault, connection_id=args.connection_id)
        if args.command == "slack-status":
            return reader.status()
        if args.command == "slack-poll":
            return reader.poll(limit=args.limit)
        if args.command == "slack-search":
            return reader.search(
                args.query,
                max_pages=args.max_pages,
                max_results=args.max_results,
                snippet_chars=args.snippet_chars,
            )
        if args.command == "slack-inbox":
            return reader.inbox(
                since=args.since,
                max_pages=args.max_pages,
                max_results=args.max_results,
                max_conversations=args.max_conversations,
                messages_per_conversation=args.messages_per_conversation,
                snippet_chars=args.snippet_chars,
            )
        if args.command == "slack-context":
            return reader.context(
                args.ref,
                before=args.before,
                after=args.after,
                include_thread=args.include_thread,
                snippet_chars=args.snippet_chars,
            )
        raise AssertionError("unreachable Slack command")
    if args.command == "source":
        if args.source_command == "list":
            return {"catalog": list_recipes(), "state": vault.source_status()}
        if args.source_command == "select":
            return vault.select_sources(
                expected_revision=args.expected_revision,
                sources=tuple(args.source),
            )
        if args.source_command == "record":
            return vault.record_source_observation(
                expected_revision=args.expected_revision,
                source_id=args.source,
                actor_ref=args.actor_ref,
                result=args.result,
                covered_through=args.covered_through,
                completeness=args.completeness,
                account_binding=args.account_binding,
                tool_binding=args.tool_binding,
                cursor=args.cursor,
                evidence_refs=tuple(args.evidence_ref),
                error_code=args.error_code,
            )
        raise AssertionError("unreachable source command")
    if args.command == "discord-source":
        discord_bridge = DiscordSourceBridge(vault)
        if args.discord_source_command == "binding-status":
            return discord_bridge.binding_status()
        if args.discord_source_command == "bind":
            return discord_bridge.bind(
                Path(args.runtime).expanduser().absolute(),
                connection_id=parse_connection_id(args.connection_id),
                expected_revision=args.expected_revision,
            )
        if args.discord_source_command == "unbind":
            return discord_bridge.unbind(expected_revision=args.expected_revision)
        if args.discord_source_command == "status":
            return discord_bridge.status()
        if args.discord_source_command == "poll":
            return discord_bridge.poll(
                limit=args.limit,
                max_content_chars=args.max_content_chars,
            )
        if args.discord_source_command == "acknowledge":
            return discord_bridge.acknowledge(
                ack_token=args.ack_token,
                expected_source_revision=args.expected_source_revision,
            )
        raise AssertionError("unreachable Discord source command")
    if args.command == "local-source":
        delivery = LocalSourceDelivery(
            vault,
            store_root=(
                Path(args.store_root).expanduser().resolve()
                if getattr(args, "store_root", None)
                else None
            ),
            whatsapp_runtime=Path(
                getattr(args, "runtime", str(whatsapp.DEFAULT_RUNTIME))
            ).expanduser(),
            whatsapp_service_label=getattr(args, "service_label", None),
        )
        if args.local_source_command == "status":
            return delivery.status(args.source)
        if args.local_source_command == "baseline":
            return delivery.baseline(args.source)
        if args.local_source_command == "staged-status":
            return resident_import.staged_local_source_checkpoint_status(
                vault,
                delivery=delivery,
            )
        if args.local_source_command == "adopt-staged":
            return resident_import.adopt_staged_local_source_checkpoint(
                vault,
                source=args.source,
                expected_migration_revision=args.expected_migration_revision,
                expected_source_revision=args.expected_source_revision,
                disposition=args.disposition,
                delivery=delivery,
            )
        if args.local_source_command == "poll":
            return delivery.poll(args.source, limit=args.limit)
        if args.local_source_command == "rebaseline":
            return delivery.rebaseline(
                args.source,
                expected_checkpoint_digest=args.expected_checkpoint_digest,
                expected_sequence=args.expected_sequence,
                disposition=args.disposition,
            )
        if args.local_source_command == "acknowledge":
            return delivery.acknowledge(
                args.source,
                token=args.token,
                expected_source_revision=args.expected_source_revision,
                disposition=args.disposition,
                result_refs=tuple(args.result_ref),
                actor_ref=args.actor_ref,
            )
        raise AssertionError("unreachable local source command")
    if args.command == "signal":
        if args.signal_command == "status":
            return vault.resident_signal_status()
        if args.signal_command == "list":
            return vault.list_resident_signals(
                include_acknowledged=args.include_acknowledged,
                limit=args.limit,
                cursor=args.cursor,
            )
        if args.signal_command == "show":
            return vault.get_resident_signal(args.input_id)
        if args.signal_command == "append":
            return vault.append_canonical_signal(
                record_ref=args.record_ref,
                change_type=args.change_type,
            )
        if args.signal_command in {"acknowledge", "ack"}:
            return {
                "acknowledgements": vault.acknowledge_resident_signals(
                    tuple(args.input_id),
                    expected_revision=args.expected_revision,
                    consumer=args.consumer,
                    disposition=args.disposition,
                    result_refs=tuple(args.result_ref),
                ),
                "status": vault.resident_signal_status(),
            }
        if args.signal_command == "compact":
            return vault.compact_resident_signals(retain_recent=args.retain_recent)
        raise AssertionError("unreachable signal command")
    if args.command == "recall":
        recall = RecallCompanion(
            vault.root,
            executable=args.executable,
            index=args.index,
        )
        if args.recall_command == "status":
            return asdict(recall.status(timeout_seconds=args.timeout))
        if args.recall_command == "refresh":
            return asdict(recall.refresh(timeout_seconds=args.timeout))
        if args.recall_command == "rebuild":
            return asdict(recall.rebuild(timeout_seconds=args.timeout))
        if args.recall_command == "search":
            return asdict(
                recall.search(
                    args.query,
                    limit=args.limit,
                    timeout_seconds=args.timeout,
                )
            )
        raise AssertionError("unreachable recall command")
    if args.command == "pulse":
        if args.pulse_command == "status":
            return _pulse_status(vault)
        if args.pulse_command == "sweep":
            return sense_sweep(vault).to_dict()
        raise AssertionError("unreachable pulse command")
    if args.command == "scheduler":
        scheduler = _scheduler_for(vault, interval_seconds=args.interval_seconds)
        if args.scheduler_command == "plan":
            return scheduler_dict(scheduler.plan())
        if args.scheduler_command == "status":
            return scheduler_dict(scheduler.status())
        if args.scheduler_command == "install":
            return scheduler_dict(scheduler.install(expected_revision=args.expected_revision))
        if args.scheduler_command == "run-canary":
            return scheduler_dict(
                scheduler.run_canary(
                    expected_revision=args.expected_revision,
                    timeout_seconds=args.timeout,
                )
            )
        if args.scheduler_command == "uninstall":
            return scheduler_dict(scheduler.uninstall(expected_revision=args.expected_revision))
        raise AssertionError("unreachable scheduler command")
    if args.command == "local-file":
        if args.local_file_command == "grant":
            return vault.grant_local_file_root(args.root)
        if args.local_file_command == "list":
            return vault.list_local_file_grants()
        if args.local_file_command == "revoke":
            return vault.revoke_local_file_grant(args.grant_id)
        if args.local_file_command == "read":
            return vault.read_local_file(
                grant_id=args.grant_id,
                relative_path=args.relative_path,
            )
        raise AssertionError("unreachable local-file command")
    if args.command == "task":
        return _task(vault, args)
    if args.command == "direction":
        return _direction(vault, args)
    if args.command == "portfolio":
        return _portfolio(vault, args)
    if args.command == "entity":
        return _entity(vault, args)
    if args.command == "thread":
        return _thread(vault, args)
    if args.command == "operation":
        return _operation(vault, args)
    if args.command == "document":
        if args.document_command == "show":
            return vault.read_document(args.name)
        content = (
            Path(args.from_file).read_text(encoding="utf-8") if args.from_file else args.content
        )
        return vault.write_document(args.name, content, expected_revision=args.expected_revision)
    if args.command == "backup":
        if args.backup_command == "create":
            return vault.create_backup(Path(args.output) if args.output else None)
        raise AssertionError("unreachable backup command")
    raise AssertionError("unreachable command")


def _connectors(vault: Vault, args: argparse.Namespace) -> dict[str, object]:
    manager = ConnectorAuthManager(vault)
    onboarding = ConnectorOnboarding(manager)
    if args.connectors_command == "client-secret":
        if args.client_secret_command == "set":
            manager.probe_credential_custody()
            if not sys.stdin.isatty():
                raise SetupError(
                    "OAuth client-secret setup needs an interactive terminal so the value "
                    "stays hidden; it is never accepted on the command line"
                )
            value = (
                getpass.getpass(f"{args.provider.title()} OAuth client secret (input hidden): ")
                .strip()
                .encode("utf-8")
            )
            return manager.store_oauth_client_secret(
                args.provider,
                value,
                replace_existing=args.replace,
            )
        if args.client_secret_command == "clear":
            if not args.yes and not _confirm(
                f"Remove the host-local {args.provider.title()} OAuth client secret? [y/N] "
            ):
                return {
                    "client_secret": "unchanged",
                    "nothing_changed": True,
                    "provider": args.provider,
                    "status": "clear_cancelled",
                }
            return manager.clear_oauth_client_secret(args.provider)
        raise AssertionError("unreachable client-secret command")
    if args.connectors_command == "readiness":
        return _connector_registration_status(vault, onboarding=onboarding)
    if args.connectors_command == "list":
        return onboarding.list()
    if args.connectors_command == "status":
        return onboarding.status(args.target)
    if args.connectors_command == "connect":
        if args.with_permanent_delete and (args.connector != "gmail" or args.access != "full"):
            raise ValidationError("--with-permanent-delete is available only for Gmail Full access")
        if args.connector == "discord" and args.connection_id is not None:
            raise ValidationError("--connection-id is unavailable for Discord bot onboarding")
        if args.connector == "discord" and (args.browser is not None or args.no_browser):
            raise ValidationError("browser options are unavailable for Discord bot onboarding")
        if args.connector == "discord" and args.timeout is not None:
            raise ValidationError("--timeout is unavailable for Discord bot onboarding")
        if not args.json:
            print(
                f"Connecting {args.connector.replace('_', ' ').title()} with "
                f"{args.access.title()} access…",
                file=sys.stderr,
            )
        if args.with_permanent_delete:
            _present_permission_update(
                "Permanent delete is ON. Seld can erase Gmail messages without Trash, and "
                "that cannot be undone."
            )
        if args.connector == "discord":
            manager.probe_credential_custody()
            if not sys.stdin.isatty():
                raise SetupError(
                    "Discord bot onboarding needs an interactive terminal so the token stays "
                    "hidden; it is never accepted on the command line"
                )
            token = getpass.getpass("Discord bot token (input hidden): ").encode("utf-8")
            return onboarding.connect_discord(
                token,
                access=args.access,
                confirm_identity=_confirm_connector_identity,
                new_account=args.new_account,
                alias=args.alias,
            )
        opener = _connector_browser_opener(args)
        return onboarding.connect_oauth(
            args.connector,
            access=args.access,
            confirm_identity=_confirm_connector_identity,
            new_account=args.new_account,
            connection_id=args.connection_id,
            alias=args.alias,
            include_permanent_delete=args.with_permanent_delete,
            browser_opener=opener,
            browser_mode=_connector_browser_mode(args),
            present_authorization_url=_present_authorization_url,
            present_permission_update=_present_permission_update,
            timeout_seconds=args.timeout,
        )
    if args.connectors_command == "alias":
        return onboarding.alias(
            args.connection_id,
            args.alias,
            expected_revision=args.expected_revision,
        )
    if args.connectors_command == "resume":
        return onboarding.resume(
            args.connection_id,
            confirm_identity=_confirm_connector_identity,
            alias=args.alias,
        )
    if args.connectors_command == "reauthorize":
        opener = _connector_browser_opener(args)
        return onboarding.reauthorize_oauth(
            args.connection_id,
            confirm_identity=_confirm_connector_identity,
            alias=args.alias,
            browser_opener=opener,
            browser_mode=_connector_browser_mode(args),
            present_authorization_url=_present_authorization_url,
            timeout_seconds=args.timeout,
        )
    if args.connectors_command == "disconnect":
        if not args.yes and not _confirm(
            "Forget this local connection? Provider access will remain authorized. [y/N] "
        ):
            return {
                "connection_id": args.connection_id,
                "next": f"gsv connectors status {args.connection_id}",
                "nothing_changed": True,
                "status": "disconnect_cancelled",
            }
        return onboarding.disconnect(args.connection_id)
    if args.connectors_command == "revocation-help":
        status = onboarding.status(args.connection_id)
        rows = cast(list[dict[str, object]], status["connections"])
        provider = rows[0].get("provider")
        if not isinstance(provider, str):
            raise SetupError("connector provider is unavailable")
        return {
            "connection_id": args.connection_id,
            "next": _revocation_guidance(provider),
            "provider": provider,
            "provider_access_revoked": False,
            "status": "instructions_only",
        }
    raise AssertionError("unreachable connectors command")


def _connector_registration_status(
    vault: Vault,
    *,
    onboarding: ConnectorOnboarding | None = None,
) -> dict[str, object]:
    current = onboarding or ConnectorOnboarding(ConnectorAuthManager(vault))
    readiness = current.registration_readiness()
    ready = all(row.get("status") == "ready" for row in readiness.values())
    return {
        "oauth_registration_ready": ready,
        "registration_readiness": readiness,
        "vault_healthy_independent": True,
    }


def _connector_browser_opener(args: argparse.Namespace) -> Callable[[str], bool] | None:
    if args.no_browser:
        return None
    if args.browser in {None, "default"}:
        return webbrowser.open

    def open_firefox(url: str) -> bool:
        try:
            controller = webbrowser.get("firefox")
            try:
                if bool(controller.open(url)):
                    return True
            except (OSError, RuntimeError, webbrowser.Error):
                pass
        except (OSError, RuntimeError, webbrowser.Error):
            pass
        if sys.platform != "darwin":
            return False
        try:
            completed = subprocess.run(
                ["open", "-b", "org.mozilla.firefox", "-u", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    return open_firefox


def _connector_browser_mode(args: argparse.Namespace) -> BrowserMode:
    if args.no_browser:
        return "manual"
    return args.browser or "default"


def _present_authorization_url(url: str, browser_opened: bool) -> None:
    if browser_opened:
        print("Your browser is open. Finish sign-in there; Seld is waiting…", file=sys.stderr)
    else:
        print(
            "Manual mode: open this sign-in URL on this computer while this command keeps "
            "running, then finish sign-in there:",
            file=sys.stderr,
        )
    print("Sign-in URL (safe to copy into your browser):", file=sys.stderr)
    print(url, file=sys.stderr)


def _present_permission_update(message: str) -> None:
    print(f"Permission update before sign-in: {message}", file=sys.stderr)


def _confirm_connector_identity(review: ConnectorIdentityReview) -> bool:
    lines = [
        f"Provider account: {review.display_label}",
        f"Connector: {review.connector.replace('_', ' ').title()}",
        f"Access: {review.access.value.title()}",
    ]
    if review.permission_update is not None:
        lines.append(f"Permission update: {review.permission_update}")
    if review.connector in {"gmail", "google"}:
        if review.access is ConnectorAccessTier.FULL:
            if review.gmail_settings_control:
                lines.append(
                    "Gmail Full: mailbox changes plus everyday settings such as filters, "
                    "signatures, language, POP/IMAP, and vacation replies. Workspace "
                    "administrator delegation is not requested."
                )
            else:
                lines.append(
                    "Legacy Gmail Full: mailbox changes only. This reauthorization does not add "
                    "everyday settings control; after sign-in, run `gsv connectors status gmail` "
                    "for the separate settings upgrade."
                )
        else:
            lines.append(
                "Gmail Read: messages and current everyday settings are visible, but settings "
                "changes are off."
            )
    if review.connector == "google_calendar":
        if review.access is ConnectorAccessTier.FULL:
            if review.calendar_list_control:
                lines.append(
                    "Google Calendar Full: event and calendar changes plus your own calendar-list "
                    "visibility, color, reminder, notification, and subscription settings."
                )
            else:
                lines.append(
                    "Legacy Google Calendar Full: event and calendar changes, with calendar-list "
                    "settings visible but not changeable. This reauthorization keeps the current "
                    "grant; after sign-in, run `gsv connectors status google_calendar` for the "
                    "separate calendar-list upgrade."
                )
        else:
            lines.append(
                "Google Calendar Read: events, calendars, and calendar-list settings are "
                "visible, but changes are off."
            )
    if review.permanent_delete:
        lines.append(
            "Explicit Gmail purge: ON — Seld can permanently erase Gmail messages, threads, or "
            "batches, skipping the Trash. This cannot be undone."
        )
    elif review.connector == "gmail":
        if review.access is ConnectorAccessTier.FULL:
            lines.append(
                "Explicit Gmail purge: off — ordinary delete operations use recoverable Trash. "
                "A separately confirmed raw-message migration can still use deleted=true to "
                "skip Trash permanently."
            )
        else:
            lines.append("Explicit Gmail purge: not available with Read access.")
    print("\n".join(lines), file=sys.stderr)
    return _confirm("Use this account? [y/N] ")


def _confirm(prompt: str) -> bool:
    try:
        response = input(prompt)
    except EOFError:
        return False
    return response.strip().casefold() in {"y", "yes"}


def _revocation_guidance(provider: str) -> str:
    return (
        provider_revocation_guidance(provider)
        + " Then run `gsv connectors disconnect <connection-id>` locally if it still exists."
    )


def _result_failure(args: argparse.Namespace, result: Any) -> tuple[int, str] | None:
    if not isinstance(result, dict):
        return None
    connector_failure_messages = {
        "account_selection_required": (
            "Choose one candidate account command from result.candidates and retry."
        ),
        "broader_access_already_connected": (
            "This connector already has broader access than selected. Nothing changed; "
            "review result.effective_access and result.downgrade_help."
        ),
        "broader_access_reauthorization_required": (
            "This connector has broader access than selected and needs reauthorization. "
            "Nothing changed; follow result.next or review result.downgrade_help."
        ),
        "cancelled": (
            "Connector sign-in was not approved or was denied; the existing setup is unchanged. "
            "Follow result.next to inspect or retry."
        ),
        "credential_invalid_reconnect_required": (
            "The saved connector credential is invalid; follow result.next to repair it."
        ),
        "credential_missing_reconnect_required": (
            "The connector credential is missing; follow result.next to reconnect."
        ),
        "credential_pointer_invalid_reconnect_required": (
            "The connector credential pointer is invalid; follow result.next to reconnect."
        ),
        "different_account": (
            "A different account was selected; use result.retry or result.new_account."
        ),
        "disconnect_cancelled": "Disconnect cancelled; nothing changed.",
        "identity_binding_missing_reconnect_required": (
            "The connector identity binding is missing; follow result.next to reconnect."
        ),
        "oauth_permissions_missing": (
            "The provider approved fewer permissions than the selected access. Nothing was "
            "saved; follow result.retry."
        ),
        "oauth_permissions_outside_selected_tier": (
            "The provider returned more access than selected. Nothing was saved; follow "
            "result.retry."
        ),
        "oauth_scope_profile_unrecognized": (
            "This saved connection has unrecognized OAuth permissions. Nothing changed; "
            "follow result.next to disconnect it safely before reconnecting."
        ),
        "setup_incomplete": "Connector setup is incomplete; follow result.next to resume.",
    }
    status = result.get("status")
    if (
        args.command == "connectors"
        and isinstance(status, str)
        and status in connector_failure_messages
    ):
        return 3, connector_failure_messages[status]
    if args.command == "setup" and result.get("setup_complete") is False:
        return 3, "Seld setup stopped because the local record is unhealthy; follow result.next."
    if (
        args.command == "setup"
        and result.get("setup_complete") is True
        and isinstance(result.get("bridge"), dict)
        and result["bridge"].get("running") is False
    ):
        return (
            4,
            "Seld installation committed, but the Bridge needs repair; follow result.next.",
        )
    if args.command == "doctor" and result.get("healthy") is False:
        return 3, "Seld doctor found unresolved integrity issues; follow result.issues."
    if (
        args.command == "backup"
        and args.backup_command == "verify"
        and result.get("valid") is False
    ):
        return 3, "Seld backup verification failed; do not restore this archive."
    if (
        args.command == "backup"
        and args.backup_command == "create"
        and result.get("verified") is False
    ):
        return 3, "Seld backup creation did not verify; do not use the reported archive."
    if (
        args.command == "codex"
        and args.codex_command == "uninstall"
        and result.get("cleanup_complete") is False
    ):
        return 3, "Seld cleanup is incomplete; follow result.next and retry."
    if (
        args.command == "update"
        and args.update_command == "check"
        and result.get("state") in {"unavailable", "unsupported"}
    ):
        return 3, "Seld could not complete the update check; inspect result.error_code."
    if args.command == "update" and args.update_command in {"apply", "recover"}:
        if result.get("repair_required") is True:
            return 3, "Seld update recovery needs attention; follow result.recovery_command."
        if result.get("outcome") == "installed_bridge_repair":
            return 4, "Seld updated, but the Bridge needs repair; follow result.recovery_command."
        if args.update_command == "apply" and result.get("rolled_back") is True:
            return 3, "The approved update failed and Seld restored the previous installation."
    return None


def _pulse_status(vault: Vault) -> dict[str, object]:
    return {
        "heartbeat": heartbeat_status(vault.root),
        "signals": vault.resident_signal_status(),
    }


def _scheduler_for(vault: Vault, *, interval_seconds: int) -> MacOSScheduler:
    executable = current_gsv_executable()
    return MacOSScheduler(
        (
            str(executable),
            "--vault",
            str(vault.root),
            "--json",
            "pulse",
            "sweep",
        ),
        vault_root=vault.root,
        interval_seconds=interval_seconds,
    )


def _configuration_matches_target(target: Path) -> bool | str:
    try:
        configuration = load_config(required=False)
        if configuration is None:
            return False
        configured = configuration.vault_path
        if configured == target:
            return True
        return (
            os.path.samefile(configured, target)
            if configured.exists() and target.exists()
            else False
        )
    except (ContinuityError, OSError, UnicodeError, ValueError, RuntimeError):
        return "unknown"


def _doctor_next(vault_path: Path, doctor: dict[str, Any]) -> str:
    command = f"gsv --vault {shlex.quote(str(vault_path))} doctor"
    issues = doctor.get("issues")
    repairable = isinstance(issues, list) and any(
        isinstance(issue, dict) and issue.get("repairable") is True for issue in issues
    )
    if repairable:
        return f"Run `{command} --repair`, then run `{command}` and inspect any remaining issues."
    return f"Inspect the reported issues, then run `{command}` again."


def _setup_next(
    *,
    no_codex: bool,
    no_bridge: bool,
    no_browser: bool,
    bridge: dict[str, Any] | None,
) -> str:
    steps: list[str] = []
    if no_bridge:
        steps.append("The Bridge was not started.")
    elif bridge is not None and bridge.get("running") is False:
        steps.append(
            "Seld and its ChatGPT integration are installed, but the Bridge did not start. "
            "Inspect `gsv --json bridge status`, then retry `gsv bridge open`; no older "
            "executable was substituted."
        )
    elif no_browser:
        steps.append("The Bridge is running locally; run `gsv` when you want to open it.")
    elif bridge is not None and bridge.get("browser_opened") is True:
        steps.append("The Bridge is open.")
    else:
        steps.append("The Bridge is running; run `gsv` to open it.")

    if no_codex:
        steps.append(
            "ChatGPT integration was skipped; run `gsv codex install` when you want a new "
            "ChatGPT task to load this local record."
        )
    else:
        steps.append(
            "Restart the ChatGPT desktop app, open one fresh task, and run `$gsv-onboard` "
            "to describe your world and verify only the sources you choose."
        )
    return " ".join(steps)


def _task(vault: Vault, args: argparse.Namespace) -> Any:
    if args.task_command == "list":
        return {"tasks": [record_dict(item) for item in vault.list_tasks(status=args.status)]}
    if args.task_command == "show":
        return record_dict(vault.get_task(args.id))
    if args.task_command == "create":
        return record_dict(
            vault.create_task(
                identifier=args.id,
                title=args.title,
                outcome=args.outcome,
                status=args.status,
                next_actor=args.next_actor,
                next_action=args.next_action,
                waiting_on=args.waiting_on,
                rank=args.rank,
                active_thread_id=args.active_thread_id,
                superseded_by=args.superseded_by,
                project=args.project,
                workspace=args.workspace,
                attention_at=args.attention_at,
                due=args.due,
                entity_links=tuple(_task_entity_link(value) for value in args.entity_link_json),
                codex_episode_ids=tuple(args.codex_episode_id),
                refs=tuple(args.ref),
            )
        )
    return record_dict(
        vault.update_task(
            args.id,
            expected_revision=args.expected_revision,
            title=args.title,
            outcome=args.outcome,
            status=args.status,
            next_actor=args.next_actor,
            next_action=args.next_action,
            waiting_on=args.waiting_on,
            rank=args.rank,
            active_thread_id=args.active_thread_id,
            superseded_by=args.superseded_by,
            project=args.project,
            workspace=args.workspace,
            attention_at=args.attention_at,
            due=args.due,
            clear_next_actor=args.clear_next_actor,
            clear_next_action=args.clear_next_action,
            clear_waiting_on=args.clear_waiting_on,
            clear_rank=args.clear_rank,
            clear_active_thread_id=args.clear_active_thread_id,
            clear_superseded_by=args.clear_superseded_by,
            clear_project=args.clear_project,
            clear_workspace=args.clear_workspace,
            clear_attention_at=args.clear_attention_at,
            clear_due=args.clear_due,
            add_entity_links=tuple(_task_entity_link(value) for value in args.add_entity_link_json),
            remove_entity_links=tuple(
                _task_entity_link(value) for value in args.remove_entity_link_json
            ),
            add_codex_episode_ids=tuple(args.add_codex_episode_id),
            remove_codex_episode_ids=tuple(args.remove_codex_episode_id),
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
            note=args.note,
        )
    )


def _entity(vault: Vault, args: argparse.Namespace) -> Any:
    if args.entity_command == "list":
        return {"entities": [record_dict(item) for item in vault.list_entities()]}
    if args.entity_command == "show":
        return record_dict(vault.get_entity(args.id))
    if args.entity_command == "resolve":
        return record_dict(vault.resolve_entity(args.id))
    if args.entity_command == "create":
        return record_dict(
            vault.create_entity(
                identifier=args.id,
                title=args.title,
                entity_type=args.entity_type,
                summary=args.summary,
                aliases=tuple(args.alias),
                refs=tuple(args.ref),
                status=args.status,
                recheck_at=args.recheck_at,
            )
        )
    if args.entity_command == "link":
        return record_dict(
            vault.link_entity(
                args.id,
                expected_revision=args.expected_revision,
                predicate=args.predicate,
                target_id=args.target_id,
                refs=tuple(args.ref),
                valid_from=args.valid_from,
                note=args.note,
            )
        )
    if args.entity_command == "unlink":
        return record_dict(
            vault.unlink_entity(
                args.id,
                expected_revision=args.expected_revision,
                predicate=args.predicate,
                target_id=args.target_id,
                refs=tuple(args.ref),
                valid_to=args.valid_to,
                note=args.note,
            )
        )
    if args.entity_command == "merge":
        result = vault.merge_entity(
            args.id,
            merged_into=args.merged_into,
            expected_revision=args.expected_revision,
            expected_target_revision=args.expected_target_revision,
            refs=tuple(args.ref),
            note=args.note,
        )
        return {
            "changed": result.changed,
            "source": record_dict(result.source),
            "target": record_dict(result.target),
        }
    return record_dict(
        vault.update_entity(
            args.id,
            expected_revision=args.expected_revision,
            title=args.title,
            summary=args.summary,
            status=args.status,
            aliases=tuple(args.alias) if args.alias is not None else None,
            add_aliases=tuple(args.add_alias),
            remove_aliases=tuple(args.remove_alias),
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
            recheck_at=args.recheck_at,
            clear_recheck_at=args.clear_recheck_at,
            note=args.note,
        )
    )


def _portfolio(vault: Vault, args: argparse.Namespace) -> Any:
    if args.portfolio_command == "show":
        return portfolio_dict(vault.get_portfolio())
    if args.portfolio_command == "inspect":
        return portfolio_inspection_dict(vault.inspect_portfolio())
    if args.portfolio_command == "migrate-review-session":
        return record_dict(
            vault.migrate_legacy_review_session(
                args.session_id,
                expected_session_revision=args.expected_session_revision,
                expected_review_thread_revision=args.expected_review_thread_revision,
                thread_title=args.thread_title,
                thread_purpose=args.thread_purpose,
                thread_summary=args.thread_summary,
            )
        )
    parsed = []
    for encoded in args.item_json:
        value = _strict_json_object(
            encoded,
            label="Portfolio item",
            required={"task_id", "task_revision", "stance", "reason"},
            allowed={
                "task_id",
                "task_revision",
                "stance",
                "reason",
                "work_thread_id",
                "work_thread_revision",
                "direction_aim_ids",
                "unaligned_reason",
                "source_position",
                "source_task_updated_at",
                "source_thread_updated_at",
            },
        )
        parsed.append(
            portfolio_item(
                task_id_value=value.get("task_id"),
                task_revision=value.get("task_revision"),
                stance=value.get("stance"),
                reason=value.get("reason"),
                work_thread_id=value.get("work_thread_id"),
                work_thread_revision=value.get("work_thread_revision"),
                direction_aim_ids=value.get("direction_aim_ids", ()),
                unaligned_reason=value.get("unaligned_reason"),
                source_position=value.get("source_position"),
                source_task_updated_at=value.get("source_task_updated_at"),
                source_thread_updated_at=value.get("source_thread_updated_at"),
            )
        )
    return portfolio_dict(
        vault.set_portfolio(
            expected_revision=args.expected_revision,
            summary=args.summary,
            items=tuple(parsed),
            direction_revision=args.direction_revision,
            source_direction_updated_at=args.source_direction_updated_at,
            refs=_optional_json_string_tuple(args.refs_json, "Portfolio refs"),
            source_observed_at=args.source_observed_at,
            recorded_at=args.recorded_at,
            review_after=args.review_after,
            note=args.note,
        )
    )


def _direction(vault: Vault, args: argparse.Namespace) -> Any:
    if args.direction_command == "show":
        return direction_dict(vault.get_direction())
    aims = []
    for encoded in args.aim_json:
        value = _strict_json_object(
            encoded,
            label="Direction aim",
            required={"id", "title", "desired_state"},
            allowed={"id", "title", "desired_state"},
        )
        aims.append(
            direction_aim(
                identifier=value.get("id"),
                title=value.get("title"),
                desired_state=value.get("desired_state"),
            )
        )
    return direction_dict(
        vault.set_direction(
            expected_revision=args.expected_revision,
            status=args.status,
            current_chapter=args.current_chapter,
            aims=tuple(aims),
            constraints=_optional_json_string_tuple(args.constraints_json, "Direction constraints"),
            tensions=_optional_json_string_tuple(args.tensions_json, "Direction tensions"),
            refs=_optional_json_string_tuple(args.refs_json, "Direction refs"),
            source_observed_at=args.source_observed_at,
            recorded_at=args.recorded_at,
            recheck_at=args.recheck_at,
            note=args.note,
        )
    )


def _strict_json_object(
    encoded: str,
    *,
    label: str,
    required: set[str],
    allowed: set[str],
) -> dict[str, Any]:
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    keys = set(value)
    if missing := required - keys:
        raise ValidationError(f"{label} is missing field {sorted(missing)[0]}")
    if extra := keys - allowed:
        raise ValidationError(f"{label} has unknown field {sorted(extra)[0]}")
    return value


def _json_object(encoded: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be a JSON object with string keys")
    return dict(value)


def _optional_json_string_tuple(encoded: str | None, label: str) -> tuple[str, ...] | None:
    if encoded is None:
        return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be a JSON string array")
    return tuple(value)


def _task_entity_link(encoded: str) -> TaskEntityLink:
    value = _strict_json_object(
        encoded,
        label="Task entity link",
        required={"role", "entity_id"},
        allowed={"role", "entity_id"},
    )
    role = value.get("role")
    entity_id = value.get("entity_id")
    if not isinstance(role, str) or not isinstance(entity_id, str):
        raise ValidationError("Task entity link role and entity_id must be strings")
    return TaskEntityLink(role, entity_id)


def _thread_entity_link(encoded: str) -> WorkThreadEntityLink:
    value = _strict_json_object(
        encoded,
        label="WorkThread entity link",
        required={"role", "entity_id"},
        allowed={"role", "entity_id"},
    )
    role = value.get("role")
    entity_id = value.get("entity_id")
    if role is not None and not isinstance(role, str):
        raise ValidationError("WorkThread entity link role must be a string or null")
    if not isinstance(entity_id, str):
        raise ValidationError("WorkThread entity link entity_id must be a string")
    return WorkThreadEntityLink(role, entity_id)


def _thread_task_link(encoded: str) -> WorkThreadTaskLink:
    value = _strict_json_object(
        encoded,
        label="WorkThread task link",
        required={"position", "task_id"},
        allowed={"position", "task_id"},
    )
    position = value.get("position")
    identifier = value.get("task_id")
    if isinstance(position, bool) or not isinstance(position, int):
        raise ValidationError("WorkThread task link position must be an integer")
    if not isinstance(identifier, str):
        raise ValidationError("WorkThread task link task_id must be a string")
    return WorkThreadTaskLink(position, identifier)


def _thread(vault: Vault, args: argparse.Namespace) -> Any:
    if args.thread_command == "list":
        return {"threads": [record_dict(item) for item in vault.list_threads(status=args.status)]}
    if args.thread_command == "show":
        return record_dict(vault.get_thread(args.id))
    if args.thread_command == "resolve":
        return record_dict(vault.resolve_thread(args.id))
    if args.thread_command == "create":
        return record_dict(
            vault.create_thread(
                identifier=args.id,
                title=args.title,
                purpose=args.purpose,
                summary=args.summary,
                status=args.status,
                next_move=args.next_move,
                focus_task_id=args.focus_task_id,
                task_ids=tuple(args.task_id),
                entity_ids=tuple(args.entity_id),
                task_links=tuple(_thread_task_link(value) for value in args.task_link_json),
                entity_links=tuple(_thread_entity_link(value) for value in args.entity_link_json),
                closure_condition=args.closure_condition,
                next_actor=args.next_actor,
                waiting_on=args.waiting_on,
                recheck_at=args.recheck_at,
                refs=tuple(args.ref),
            )
        )
    if args.thread_command == "merge":
        result = vault.merge_thread(
            args.id,
            merged_into=args.merged_into,
            expected_revision=args.expected_revision,
            expected_target_revision=args.expected_target_revision,
            absorb_source_entities=args.absorb_source_entities,
            absorb_source_tasks=args.absorb_source_tasks,
            absorb_source_refs=args.absorb_source_refs,
            add_entity_links=tuple(
                _thread_entity_link(value) for value in args.add_entity_link_json
            ),
            add_task_links=tuple(_thread_task_link(value) for value in args.add_task_link_json),
            add_refs=tuple(args.add_ref),
            note=args.note,
        )
        return {
            "changed": result.changed,
            "source": record_dict(result.source),
            "target": record_dict(result.target),
        }
    task_ids = tuple(args.task_id) if args.task_id is not None else None
    entity_ids = tuple(args.entity_id) if args.entity_id is not None else None
    return record_dict(
        vault.update_thread(
            args.id,
            expected_revision=args.expected_revision,
            title=args.title,
            purpose=args.purpose,
            summary=args.summary,
            status=args.status,
            next_move=args.next_move,
            clear_next_move=args.clear_next_move,
            focus_task_id=args.focus_task_id,
            clear_focus_task=args.clear_focus_task,
            task_ids=task_ids,
            entity_ids=entity_ids,
            task_links=(
                tuple(_thread_task_link(value) for value in args.task_link_json)
                if args.task_link_json is not None
                else None
            ),
            entity_links=(
                tuple(_thread_entity_link(value) for value in args.entity_link_json)
                if args.entity_link_json is not None
                else None
            ),
            add_task_links=tuple(_thread_task_link(value) for value in args.add_task_link_json),
            remove_task_ids=tuple(args.remove_task_id),
            add_entity_links=tuple(
                _thread_entity_link(value) for value in args.add_entity_link_json
            ),
            remove_entity_links=tuple(
                _thread_entity_link(value) for value in args.remove_entity_link_json
            ),
            closure_condition=args.closure_condition,
            next_actor=args.next_actor,
            waiting_on=args.waiting_on,
            recheck_at=args.recheck_at,
            clear_closure_condition=args.clear_closure_condition,
            clear_next_actor=args.clear_next_actor,
            clear_waiting_on=args.clear_waiting_on,
            clear_recheck_at=args.clear_recheck_at,
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
            note=args.note,
        )
    )


def _operation(vault: Vault, args: argparse.Namespace) -> Any:
    """Read or disposition Bridge intents without executing their requested effect."""

    ledger = OperationLedger(vault.root)
    binding = capture_operation_binding(vault.root)
    if args.operation_command == "list":
        return ledger.snapshot(
            expected_vault_id=binding.vault_id,
            expected_root_identity=binding.root_identity,
        ).to_dict()
    if args.operation_command in {"accept", "reject"}:
        return ledger.decide(
            event_id=args.event_id,
            decision="accepted" if args.operation_command == "accept" else "rejected",
            actor_ref=args.actor_ref,
            reason_code=args.reason_code,
            expected_queue_revision=args.expected_queue_revision,
            expected_disposition_revision=args.expected_disposition_revision,
            expected_vault_id=args.expected_vault_id,
            expected_root_identity=binding.root_identity,
            result_ref=args.result_ref,
        ).to_dict()
    if args.operation_command == "archive-closed":
        return ledger.archive_closed(
            expected_queue_revision=args.expected_queue_revision,
            expected_disposition_revision=args.expected_disposition_revision,
            expected_vault_id=args.expected_vault_id,
            expected_root_identity=binding.root_identity,
        )
    raise AssertionError("unreachable operation command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsv",
        description="Seld: your private, resident AI chief of staff.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--vault", help="Override the configured vault path.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser(
        "setup", help="Initialize a local record and install the ChatGPT integration."
    )
    setup.add_argument("--name", default="My Seld")
    setup.add_argument("--codex-home", default=str(codex_home()))
    setup.add_argument("--no-bridge", action="store_true")
    setup.add_argument("--no-browser", action="store_true")
    setup.add_argument("--no-codex", action="store_true")

    init = commands.add_parser("init", help="Initialize a local record without changing ChatGPT.")
    init.add_argument("--name", default="My Seld")
    init.add_argument("--configure", action="store_true")

    commands.add_parser("status", help="Show vault identity and counts.")
    doctor = commands.add_parser("doctor", help="Validate storage and ChatGPT integration.")
    doctor.add_argument(
        "--repair", action="store_true", help="Remove interrupted-write temp files only."
    )
    context = commands.add_parser("context", help="Render a bounded context pack.")
    context.add_argument("--format", choices=("markdown", "json"), default="markdown")
    context.add_argument("--max-characters", type=int, default=48_000)
    context.set_defaults(raw=True)

    resident_context = commands.add_parser(
        "resident-context",
        help="Inspect user-imported resident guidance and native skill inventory.",
    )
    resident_context_commands = resident_context.add_subparsers(
        dest="resident_context_command",
        required=True,
    )
    resident_context_commands.add_parser(
        "status",
        help="Show content-free imported guidance and exact $skill metadata.",
    )
    resident_context_show = resident_context_commands.add_parser(
        "show",
        help="Read the exact user-approved resident AGENTS guidance.",
    )
    resident_context_show.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
    )
    resident_context_show.set_defaults(raw=True)

    commands.add_parser(
        "execution-bindings",
        help="Read every explicit active ChatGPT hand and focused WorkThread binding.",
    )

    connectors = commands.add_parser(
        "connectors",
        help="Connect and inspect full-feature provider accounts without exposing credentials.",
    )
    connector_commands = connectors.add_subparsers(
        dest="connectors_command",
        required=True,
    )
    connector_commands.add_parser("list", help="List redacted connector status.")
    connector_commands.add_parser(
        "readiness",
        help="Show whether every packaged OAuth client is ready to sign in on this host.",
    )
    connector_client_secret = connector_commands.add_parser(
        "client-secret",
        help="Store or clear a provider OAuth client secret in the host OS keyring.",
    )
    connector_client_secret_commands = connector_client_secret.add_subparsers(
        dest="client_secret_command",
        required=True,
    )
    connector_client_secret_set = connector_client_secret_commands.add_parser(
        "set",
        help="Read one OAuth client secret from hidden terminal input.",
    )
    connector_client_secret_set.add_argument("provider", choices=("google",))
    connector_client_secret_set.add_argument("--replace", action="store_true")
    connector_client_secret_clear = connector_client_secret_commands.add_parser(
        "clear",
        help="Remove one OAuth client secret from the host OS keyring.",
    )
    connector_client_secret_clear.add_argument("provider", choices=("google",))
    connector_client_secret_clear.add_argument("--yes", action="store_true")
    connector_status = connector_commands.add_parser(
        "status",
        help="Show all connections for one logical connector or one exact connection ID.",
    )
    connector_status.add_argument("target", nargs="?")
    connector_connect = connector_commands.add_parser(
        "connect",
        help="Sign in, verify the exact account, confirm it, and publish only when ready.",
    )
    connector_connect.add_argument("connector", choices=tuple(sorted(CONNECTOR_PROFILES)))
    connector_connect.add_argument("--access", choices=("read", "full"), required=True)
    connector_selector = connector_connect.add_mutually_exclusive_group()
    connector_selector.add_argument("--connection-id")
    connector_selector.add_argument("--new-account", action="store_true")
    connector_connect.add_argument(
        "--alias",
        help=(
            "Optional privacy-safe local account label; provider email is never stored by default."
        ),
    )
    connector_connect.add_argument(
        "--with-permanent-delete",
        action="store_true",
        help="Gmail Full only: request the separate irreversible-delete permission.",
    )
    connector_connect.add_argument("--timeout", type=float)
    connector_browser = connector_connect.add_mutually_exclusive_group()
    connector_browser.add_argument("--browser", choices=("default", "firefox"), default=None)
    connector_browser.add_argument("--no-browser", action="store_true")
    connector_reauthorize = connector_commands.add_parser(
        "reauthorize",
        help="Run OAuth again for one existing verified connection without changing its ID.",
    )
    connector_reauthorize.add_argument("connection_id")
    connector_reauthorize.add_argument(
        "--alias",
        help=(
            "Optional privacy-safe local account label; replaces the stored label for this "
            "connection."
        ),
    )
    connector_reauthorize.add_argument("--timeout", type=float)
    reauthorize_browser = connector_reauthorize.add_mutually_exclusive_group()
    reauthorize_browser.add_argument(
        "--browser",
        choices=("default", "firefox"),
        default=None,
    )
    reauthorize_browser.add_argument("--no-browser", action="store_true")
    connector_alias = connector_commands.add_parser(
        "alias",
        help="Change only the local label of one connector connection.",
    )
    connector_alias.add_argument("connection_id")
    connector_alias.add_argument("--alias", required=True, help="Privacy-safe local label.")
    connector_alias.add_argument(
        "--expected-revision",
        help="Expected CONNECTIONS.md revision; defaults to the revision read for this command.",
    )
    connector_resume = connector_commands.add_parser(
        "resume",
        help="Finish identity confirmation for one retained unverified connection.",
    )
    connector_resume.add_argument("connection_id")
    connector_resume.add_argument(
        "--alias",
        help=(
            "Optional privacy-safe local account label; provider email is never stored by default."
        ),
    )
    connector_disconnect = connector_commands.add_parser(
        "disconnect",
        help="Forget one local connection without claiming provider-side revocation.",
    )
    connector_disconnect.add_argument("connection_id")
    connector_disconnect.add_argument("--yes", action="store_true")
    connector_revoke = connector_commands.add_parser(
        "revocation-help",
        help="Show honest provider-side revocation steps without taking remote action.",
    )
    connector_revoke.add_argument("connection_id")

    slack_status = commands.add_parser(
        "slack-status",
        help="Verify the live identity of one portable Slack connection.",
    )
    slack_status.add_argument("--connection-id")
    slack_capabilities = commands.add_parser(
        "slack-capabilities",
        help="Show the bounded task-shaped Slack read interface.",
    )
    slack_capabilities.add_argument("--connection-id")
    slack_poll = commands.add_parser(
        "slack-poll",
        help="Read and record one bounded current Slack projection.",
    )
    slack_poll.add_argument("--connection-id")
    slack_poll.add_argument("--limit", type=int, default=25)
    slack_search = commands.add_parser(
        "slack-search",
        help="Search Slack through one exact portable read connection.",
    )
    slack_search.add_argument("--connection-id")
    slack_search.add_argument("--query", required=True)
    slack_search.add_argument("--max-pages", type=int, default=1)
    slack_search.add_argument("--max-results", type=int, default=100)
    slack_search.add_argument("--snippet-chars", type=int, default=320)
    slack_inbox = commands.add_parser(
        "slack-inbox",
        help="Group bounded recent Slack results by conversation.",
    )
    slack_inbox.add_argument("--connection-id")
    slack_inbox.add_argument("--since")
    slack_inbox.add_argument("--max-pages", type=int, default=2)
    slack_inbox.add_argument("--max-results", type=int, default=200)
    slack_inbox.add_argument("--max-conversations", type=int, default=12)
    slack_inbox.add_argument("--messages-per-conversation", type=int, default=2)
    slack_inbox.add_argument("--snippet-chars", type=int, default=240)
    slack_context = commands.add_parser(
        "slack-context",
        help="Expand one short-lived opaque reference from Slack search or inbox.",
    )
    slack_context.add_argument("--connection-id")
    slack_context.add_argument("--ref", required=True)
    slack_context.add_argument("--before", type=int, default=5)
    slack_context.add_argument("--after", type=int, default=5)
    slack_context.add_argument(
        "--include-thread",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    slack_context.add_argument("--snippet-chars", type=int, default=1_000)

    source = commands.add_parser(
        "source",
        help="Select sources and record bounded reads made by the resident AI.",
    )
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("list")
    source_select = source_commands.add_parser(
        "select",
        help="CAS-replace the user-approved source set; deselection purges its coverage.",
    )
    source_select.add_argument("--expected-revision", required=True)
    source_select.add_argument("--source", action="append", default=[])
    source_record = source_commands.add_parser(
        "record",
        help="CAS-record one content-free source read or bounded failure.",
    )
    source_record.add_argument("--expected-revision", required=True)
    source_record.add_argument("--source", required=True)
    source_record.add_argument("--actor-ref", required=True)
    source_record.add_argument(
        "--result",
        choices=("success", "explicit_empty", "failure"),
        required=True,
    )
    source_record.add_argument("--covered-through")
    source_record.add_argument("--completeness", choices=("complete", "partial"))
    source_record.add_argument("--account-binding")
    source_record.add_argument("--tool-binding")
    source_record.add_argument("--cursor")
    source_record.add_argument("--evidence-ref", action="append", default=[])
    source_record.add_argument("--error-code", choices=SOURCE_ERROR_CODES)

    discord_source = commands.add_parser(
        "discord-source",
        help="Bind and operate Seld's GET-only Discord ingestion companion.",
    )
    discord_source_commands = discord_source.add_subparsers(
        dest="discord_source_command",
        required=True,
    )
    discord_source_commands.add_parser(
        "binding-status",
        help="Show the content-free host-local companion binding revision.",
    )
    discord_bind = discord_source_commands.add_parser(
        "bind",
        help=(
            "CAS-bind one exact local companion executable to a portable Discord bot "
            "connection; no credentials enter the binding."
        ),
    )
    discord_bind.add_argument("--runtime", required=True)
    discord_bind.add_argument("--connection-id", required=True)
    discord_bind.add_argument("--expected-revision", required=True)
    discord_unbind = discord_source_commands.add_parser(
        "unbind",
        help="CAS-remove the host-local companion binding without touching Discord or its token.",
    )
    discord_unbind.add_argument("--expected-revision", required=True)
    discord_source_commands.add_parser(
        "status",
        help="Verify runtime, account identity, confinement, and checkpoint health.",
    )
    discord_poll = discord_source_commands.add_parser(
        "poll",
        help="Read one bounded transient delivery without advancing the checkpoint.",
    )
    discord_poll.add_argument("--limit", type=int, default=5)
    discord_poll.add_argument("--max-content-chars", type=int, default=280)
    discord_ack = discord_source_commands.add_parser(
        "acknowledge",
        help="Advance only after a matching Discord source receipt is durably readable.",
    )
    discord_ack.add_argument("--ack-token", required=True)
    discord_ack.add_argument("--expected-source-revision", required=True)

    local_source = commands.add_parser(
        "local-source",
        help="Adopt, baseline, poll, repair, and disposition bounded local message evidence.",
    )
    local_source_commands = local_source.add_subparsers(dest="local_source_command", required=True)
    local_source_status = local_source_commands.add_parser(
        "status", help="Show content-free host checkpoint state."
    )
    local_source_status.add_argument("--source", choices=SUPPORTED_LOCAL_SOURCES, required=True)
    local_source_baseline = local_source_commands.add_parser(
        "baseline", help="Start forward-only delivery at the current aggregate cursor."
    )
    local_source_poll = local_source_commands.add_parser(
        "poll", help="Read or replay one bounded transient delta without advancing."
    )
    local_source_ack = local_source_commands.add_parser(
        "acknowledge",
        help="Record an explicit semantic disposition, then advance the host checkpoint.",
    )
    local_source_rebaseline = local_source_commands.add_parser(
        "rebaseline",
        help="Explicitly accept a replaced store and preserve the old checkpoint as history.",
    )
    local_source_staged_status = local_source_commands.add_parser(
        "staged-status",
        help="Show content-free checkpoints staged by a resident migration.",
    )
    local_source_adopt_staged = local_source_commands.add_parser(
        "adopt-staged",
        help="Adopt one exact vault-staged checkpoint without exposing its cursor.",
    )
    for command in (
        local_source_baseline,
        local_source_poll,
        local_source_ack,
        local_source_rebaseline,
        local_source_adopt_staged,
    ):
        command.add_argument("--source", choices=SUPPORTED_LOCAL_SOURCES, required=True)
    for command in (
        local_source_baseline,
        local_source_poll,
        local_source_ack,
        local_source_rebaseline,
        local_source_staged_status,
        local_source_adopt_staged,
    ):
        command.add_argument(
            "--store-root",
            help=(
                "Establish or verify an exact host-local adapter location. The path stays "
                "outside the portable vault and is reused by fresh ChatGPT tool sessions."
            ),
        )
        command.add_argument("--runtime", default=str(whatsapp.DEFAULT_RUNTIME))
        command.add_argument("--service-label")
    local_source_poll.add_argument("--limit", type=int, default=100)
    local_source_ack.add_argument("--token", required=True)
    local_source_ack.add_argument("--expected-source-revision", required=True)
    local_source_ack.add_argument("--disposition", choices=("accepted", "rejected"), required=True)
    local_source_ack.add_argument("--result-ref", action="append", required=True)
    local_source_ack.add_argument("--actor-ref", required=True)
    local_source_rebaseline.add_argument("--expected-checkpoint-digest", required=True)
    local_source_rebaseline.add_argument("--expected-sequence", type=int, required=True)
    local_source_rebaseline.add_argument(
        "--disposition",
        choices=(FORWARD_ONLY_RESET,),
        required=True,
    )
    local_source_adopt_staged.add_argument("--expected-migration-revision", required=True)
    local_source_adopt_staged.add_argument("--expected-source-revision", required=True)
    local_source_adopt_staged.add_argument(
        "--disposition",
        choices=(VERIFIED_PREFIX_ADOPTION,),
        required=True,
    )

    signal = commands.add_parser(
        "signal",
        help="Inspect and disposition the resident AI evidence mailbox.",
    )
    signal_commands = signal.add_subparsers(dest="signal_command", required=True)
    signal_commands.add_parser("status", help="Show validated content-free queue counts.")
    signal_list = signal_commands.add_parser("list", help="List one bounded queue page.")
    signal_list.add_argument("--include-acknowledged", action="store_true")
    signal_list.add_argument("--limit", type=int, default=500)
    signal_list.add_argument("--cursor")
    signal_show = signal_commands.add_parser("show", help="Show one exact evidence envelope.")
    signal_show.add_argument("input_id")
    signal_append = signal_commands.add_parser(
        "append",
        help="Append one content-free canonical record pointer for resident interpretation.",
    )
    signal_append.add_argument("--record-ref", required=True)
    signal_append.add_argument(
        "--change-type",
        choices=("correction", "failure", "observation", "outcome"),
        required=True,
    )
    signal_ack = signal_commands.add_parser(
        "acknowledge",
        aliases=["ack"],
        help="Acknowledge evidence after an explicit durable AI disposition.",
    )
    signal_ack.add_argument("--input-id", action="append", required=True)
    signal_ack.add_argument("--expected-revision", required=True)
    signal_ack.add_argument("--consumer", required=True)
    signal_ack.add_argument("--disposition", choices=("accepted", "rejected"), required=True)
    signal_ack.add_argument("--result-ref", action="append", required=True)
    signal_compact = signal_commands.add_parser(
        "compact",
        help="Archive settled evidence and recover bounded live capacity.",
    )
    signal_compact.add_argument("--retain-recent", type=int, default=1_000)

    recall = commands.add_parser(
        "recall",
        help="Search canonical Markdown through disposable QMD or an exact local fallback.",
    )
    recall.add_argument("--executable", default="qmd")
    recall.add_argument("--index", default="seld")
    recall_commands = recall.add_subparsers(dest="recall_command", required=True)
    recall_status = recall_commands.add_parser("status")
    recall_status.add_argument("--timeout", type=int, default=10)
    recall_refresh = recall_commands.add_parser("refresh")
    recall_refresh.add_argument("--timeout", type=int, default=120)
    recall_rebuild = recall_commands.add_parser("rebuild")
    recall_rebuild.add_argument("--timeout", type=int, default=600)
    recall_search = recall_commands.add_parser("search")
    recall_search.add_argument("query")
    recall_search.add_argument("--limit", type=int, default=8)
    recall_search.add_argument("--timeout", type=int, default=20)

    pulse = commands.add_parser(
        "pulse",
        help="Inspect or run one bounded mechanical resident sweep.",
    )
    pulse_commands = pulse.add_subparsers(dest="pulse_command", required=True)
    pulse_commands.add_parser("status", help="Read the latest content-free sweep heartbeat.")
    pulse_commands.add_parser(
        "sweep",
        help="Run one provider-free mechanical sweep and publish its heartbeat.",
    )

    scheduler = commands.add_parser(
        "scheduler",
        help="Manage the owned macOS launchd wake for the mechanical Pulse.",
    )
    scheduler_commands = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_plan = scheduler_commands.add_parser(
        "plan",
        help="Show the exact launchd command and receipt CAS without changing the host.",
    )
    scheduler_status = scheduler_commands.add_parser(
        "status",
        help="Read owned launchd state without changing it.",
    )
    scheduler_install = scheduler_commands.add_parser(
        "install",
        help="CAS-install or update the owned launchd job.",
    )
    scheduler_canary = scheduler_commands.add_parser(
        "run-canary",
        help="CAS-run one asynchronous launchd proof against the installed job.",
    )
    scheduler_uninstall = scheduler_commands.add_parser(
        "uninstall",
        help="CAS-remove only the exact Seld-owned launchd job.",
    )
    for scheduler_command in (
        scheduler_plan,
        scheduler_status,
        scheduler_install,
        scheduler_canary,
        scheduler_uninstall,
    ):
        scheduler_command.add_argument("--interval-seconds", type=int, default=600)
    for scheduler_mutation in (
        scheduler_install,
        scheduler_canary,
        scheduler_uninstall,
    ):
        scheduler_mutation.add_argument("--expected-revision", required=True)
    scheduler_canary.add_argument("--timeout", type=float, default=10)

    local_file = commands.add_parser(
        "local-file",
        help="Read one explicitly selected local text file through Seld's privacy screen.",
    )
    local_file_commands = local_file.add_subparsers(
        dest="local_file_command",
        required=True,
    )
    local_file_grant = local_file_commands.add_parser(
        "grant",
        help="Grant one exact root to this vault from the local host.",
    )
    local_file_grant.add_argument("--root", required=True)
    local_file_commands.add_parser(
        "list",
        help="List host-local roots granted to this exact vault.",
    )
    local_file_revoke = local_file_commands.add_parser(
        "revoke",
        help="Revoke one host-local root grant from this vault.",
    )
    local_file_revoke.add_argument("--grant-id", required=True)
    local_file_read = local_file_commands.add_parser(
        "read",
        help="Return one bounded file transiently without writing its content to the vault.",
    )
    local_file_read.add_argument("--grant-id", required=True)
    local_file_read.add_argument("--relative-path", required=True)

    task = commands.add_parser("task", help="Create, inspect, and update durable tasks.")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_list = task_commands.add_parser("list")
    task_list.add_argument("--status")
    task_show = task_commands.add_parser("show")
    task_show.add_argument("id")
    task_create = task_commands.add_parser("create")
    _task_create_arguments(task_create)
    task_update = task_commands.add_parser("update")
    task_update.add_argument("id")
    task_update.add_argument("--expected-revision", required=True)
    task_update.add_argument("--title")
    task_update.add_argument("--outcome")
    task_update.add_argument("--status")
    task_update.add_argument("--next-actor", choices=("agent", "human", "external"))
    task_update.add_argument("--next-action")
    task_update.add_argument("--waiting-on")
    task_update.add_argument("--rank", type=int)
    task_update.add_argument("--active-thread-id")
    task_update.add_argument("--superseded-by")
    task_update.add_argument("--project")
    task_update.add_argument("--workspace")
    task_update.add_argument("--attention-at")
    task_update.add_argument("--due")
    task_update.add_argument("--clear-next-actor", action="store_true")
    task_update.add_argument("--clear-next-action", action="store_true")
    task_update.add_argument("--clear-waiting-on", action="store_true")
    task_update.add_argument("--clear-rank", action="store_true")
    task_update.add_argument("--clear-active-thread-id", action="store_true")
    task_update.add_argument("--clear-superseded-by", action="store_true")
    task_update.add_argument("--clear-project", action="store_true")
    task_update.add_argument("--clear-workspace", action="store_true")
    task_update.add_argument("--clear-attention-at", action="store_true")
    task_update.add_argument("--clear-due", action="store_true")
    task_update.add_argument("--add-entity-link-json", action="append", default=[])
    task_update.add_argument("--remove-entity-link-json", action="append", default=[])
    task_update.add_argument("--add-codex-episode-id", action="append", default=[])
    task_update.add_argument("--remove-codex-episode-id", action="append", default=[])
    task_update.add_argument("--add-ref", action="append", default=[])
    task_update.add_argument("--remove-ref", action="append", default=[])
    task_update.add_argument("--note")

    portfolio = commands.add_parser(
        "portfolio", help="Show or author the complete open Portfolio judgment."
    )
    portfolio_commands = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_commands.add_parser("show")
    portfolio_commands.add_parser("inspect")
    portfolio_migrate = portfolio_commands.add_parser(
        "migrate-review-session",
        help="CAS-bind one legacy review task to the canonical review WorkThread.",
    )
    portfolio_migrate.add_argument("--session-id", required=True)
    portfolio_migrate.add_argument("--expected-session-revision", required=True)
    portfolio_migrate.add_argument("--expected-review-thread-revision", required=True)
    portfolio_migrate.add_argument("--thread-title")
    portfolio_migrate.add_argument("--thread-purpose")
    portfolio_migrate.add_argument("--thread-summary")
    portfolio_set = portfolio_commands.add_parser("set")
    portfolio_set.add_argument("--expected-revision", required=True)
    portfolio_set.add_argument("--summary", required=True)
    portfolio_set.add_argument("--direction-revision")
    portfolio_set.add_argument("--item-json", action="append", default=[])
    portfolio_set.add_argument("--source-direction-updated-at")
    portfolio_set.add_argument(
        "--refs-json",
        help="JSON string array replacing Portfolio provenance refs; omitted preserves v3 refs.",
    )
    portfolio_set.add_argument("--source-observed-at")
    portfolio_set.add_argument("--recorded-at")
    portfolio_set.add_argument("--review-after")
    portfolio_set.add_argument(
        "--note",
        help="One authored Portfolio history note appended to existing v3 history.",
    )

    direction = commands.add_parser(
        "direction", help="Show or author the current whole-life Direction."
    )
    direction_commands = direction.add_subparsers(dest="direction_command", required=True)
    direction_commands.add_parser("show")
    direction_set = direction_commands.add_parser("set")
    direction_set.add_argument("--expected-revision", required=True)
    direction_set.add_argument("--status", choices=("provisional", "confirmed"), required=True)
    direction_set.add_argument("--current-chapter", required=True)
    direction_set.add_argument("--aim-json", action="append", default=[], required=True)
    direction_set.add_argument(
        "--constraints-json",
        help="JSON string array replacing constraints; omitted preserves v2 constraints.",
    )
    direction_set.add_argument(
        "--tensions-json",
        help="JSON string array replacing tensions; omitted preserves v2 tensions.",
    )
    direction_set.add_argument(
        "--refs-json",
        help="JSON string array replacing Direction provenance refs; omitted preserves v2 refs.",
    )
    direction_set.add_argument("--source-observed-at")
    direction_set.add_argument("--recorded-at")
    direction_set.add_argument("--recheck-at")
    direction_set.add_argument(
        "--note",
        help="One authored Direction history note appended to existing v2 history.",
    )

    entity = commands.add_parser("entity", help="Create and inspect canonical entities.")
    entity_commands = entity.add_subparsers(dest="entity_command", required=True)
    entity_commands.add_parser("list")
    entity_show = entity_commands.add_parser("show")
    entity_show.add_argument("id")
    entity_resolve = entity_commands.add_parser("resolve")
    entity_resolve.add_argument("id")
    entity_create = entity_commands.add_parser("create")
    entity_create.add_argument("--id", required=True)
    entity_create.add_argument("--title", required=True)
    entity_create.add_argument("--entity-type", required=True)
    entity_create.add_argument("--summary", required=True)
    entity_create.add_argument("--alias", action="append", default=[])
    entity_create.add_argument("--ref", action="append", default=[])
    entity_create.add_argument("--status", default="current")
    entity_create.add_argument("--recheck-at")
    entity_update = entity_commands.add_parser("update")
    entity_update.add_argument("id")
    entity_update.add_argument("--expected-revision", required=True)
    entity_update.add_argument("--title")
    entity_update.add_argument("--summary")
    entity_update.add_argument("--status")
    entity_update.add_argument("--alias", action="append")
    entity_update.add_argument("--add-alias", action="append", default=[])
    entity_update.add_argument("--remove-alias", action="append", default=[])
    entity_update.add_argument("--add-ref", action="append", default=[])
    entity_update.add_argument("--remove-ref", action="append", default=[])
    entity_update.add_argument("--recheck-at")
    entity_update.add_argument("--clear-recheck-at", action="store_true")
    entity_update.add_argument("--note")
    for command_name in ("link", "unlink"):
        relationship = entity_commands.add_parser(command_name)
        relationship.add_argument("id")
        relationship.add_argument("--expected-revision", required=True)
        relationship.add_argument("--predicate", required=True)
        relationship.add_argument("--target-id", required=True)
        relationship.add_argument("--ref", action="append", default=[])
        relationship.add_argument("--note")
        relationship.add_argument("--valid-from" if command_name == "link" else "--valid-to")
    entity_merge = entity_commands.add_parser("merge")
    entity_merge.add_argument("id")
    entity_merge.add_argument("--merged-into", required=True)
    entity_merge.add_argument("--expected-revision", required=True)
    entity_merge.add_argument("--expected-target-revision", required=True)
    entity_merge.add_argument("--ref", action="append", default=[])
    entity_merge.add_argument("--note")

    thread = commands.add_parser("thread", help="Create, inspect, and update work threads.")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_list = thread_commands.add_parser("list")
    thread_list.add_argument("--status")
    thread_show = thread_commands.add_parser("show")
    thread_show.add_argument("id")
    thread_resolve = thread_commands.add_parser("resolve")
    thread_resolve.add_argument("id")
    thread_create = thread_commands.add_parser("create")
    _thread_create_arguments(thread_create)
    thread_update = thread_commands.add_parser("update")
    thread_update.add_argument("id")
    thread_update.add_argument("--expected-revision", required=True)
    thread_update.add_argument("--title")
    thread_update.add_argument("--purpose")
    thread_update.add_argument("--summary")
    thread_update.add_argument("--status")
    thread_update.add_argument("--next-move")
    thread_update.add_argument("--clear-next-move", action="store_true")
    thread_update.add_argument("--focus-task-id")
    thread_update.add_argument("--clear-focus-task", action="store_true")
    thread_update.add_argument("--task-id", action="append")
    thread_update.add_argument("--entity-id", action="append")
    thread_update.add_argument("--task-link-json", action="append")
    thread_update.add_argument("--entity-link-json", action="append")
    thread_update.add_argument("--add-task-link-json", action="append", default=[])
    thread_update.add_argument("--remove-task-id", action="append", default=[])
    thread_update.add_argument("--add-entity-link-json", action="append", default=[])
    thread_update.add_argument("--remove-entity-link-json", action="append", default=[])
    thread_update.add_argument("--closure-condition")
    thread_update.add_argument("--next-actor", choices=("agent", "human", "external"))
    thread_update.add_argument("--waiting-on")
    thread_update.add_argument("--recheck-at")
    thread_update.add_argument("--clear-closure-condition", action="store_true")
    thread_update.add_argument("--clear-next-actor", action="store_true")
    thread_update.add_argument("--clear-waiting-on", action="store_true")
    thread_update.add_argument("--clear-recheck-at", action="store_true")
    thread_update.add_argument("--add-ref", action="append", default=[])
    thread_update.add_argument("--remove-ref", action="append", default=[])
    thread_update.add_argument("--note")
    thread_merge = thread_commands.add_parser("merge")
    thread_merge.add_argument("id")
    thread_merge.add_argument("--merged-into", required=True)
    thread_merge.add_argument("--expected-revision", required=True)
    thread_merge.add_argument("--expected-target-revision", required=True)
    thread_merge.add_argument("--absorb-source-entities", action="store_true")
    thread_merge.add_argument("--absorb-source-tasks", action="store_true")
    thread_merge.add_argument("--absorb-source-refs", action="store_true")
    thread_merge.add_argument("--add-entity-link-json", action="append", default=[])
    thread_merge.add_argument("--add-task-link-json", action="append", default=[])
    thread_merge.add_argument("--add-ref", action="append", default=[])
    thread_merge.add_argument("--note")

    if CONTROL_STORE_SUPPORTED:
        operation = commands.add_parser(
            "operation",
            help=(
                "Read and disposition bounded Bridge intents on macOS/Linux. Never execute "
                "their requested effect."
            ),
        )
        operation_commands = operation.add_subparsers(dest="operation_command", required=True)
        operation_commands.add_parser("list", help="List pending and durably decided intents.")
        for name, help_text in (
            (
                "accept",
                "Acknowledge one intent for later review; this does not approve or execute it.",
            ),
            ("reject", "Reject one intent without changing semantic canon."),
        ):
            disposition = operation_commands.add_parser(name, help=help_text)
            disposition.add_argument("event_id")
            disposition.add_argument("--expected-queue-revision", required=True)
            disposition.add_argument("--expected-disposition-revision", required=True)
            disposition.add_argument("--expected-vault-id", required=True)
            disposition.add_argument("--actor-ref", required=True)
            disposition.add_argument("--reason-code", required=True)
            disposition.add_argument("--result-ref")
        archive_closed = operation_commands.add_parser(
            "archive-closed",
            help="Recover bounded queue capacity after every live intent has a disposition.",
        )
        archive_closed.add_argument("--expected-queue-revision", required=True)
        archive_closed.add_argument("--expected-disposition-revision", required=True)
        archive_closed.add_argument("--expected-vault-id", required=True)

    document = commands.add_parser("document", help="Read or update MIND.md and NOW.md.")
    document_commands = document.add_subparsers(dest="document_command", required=True)
    document_show = document_commands.add_parser("show")
    document_show.add_argument("name", choices=("MIND.md", "NOW.md"))
    document_update = document_commands.add_parser("update")
    document_update.add_argument("name", choices=("MIND.md", "NOW.md"))
    document_update.add_argument("--expected-revision", required=True)
    document_content = document_update.add_mutually_exclusive_group(required=True)
    document_content.add_argument("--content")
    document_content.add_argument("--from-file")

    backup = commands.add_parser("backup", help="Create, verify, or restore a portable backup.")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_commands.add_parser("create")
    backup_create.add_argument("--output")
    backup_verify = backup_commands.add_parser("verify")
    backup_verify.add_argument("path")
    backup_restore = backup_commands.add_parser("restore")
    backup_restore.add_argument("path")
    backup_restore.add_argument("target")

    migration = commands.add_parser(
        "migration",
        help="Inspect or apply one owner-local resident export to a new Seld vault.",
    )
    migration_commands = migration.add_subparsers(dest="migration_command", required=True)
    migration_inspect = migration_commands.add_parser(
        "inspect",
        help="Validate an export and print its exact CAS-bound import plan without writing.",
    )
    migration_inspect.add_argument("export")
    migration_inspect.add_argument("target")
    migration_apply = migration_commands.add_parser(
        "apply",
        help="Publish one validated export to an absent target without changing configuration.",
    )
    migration_apply.add_argument("export")
    migration_apply.add_argument("target")
    migration_apply.add_argument("--expected-plan-revision", required=True)

    demo = commands.add_parser("demo", help="Run the complete synthetic Seld proof.")
    demo.add_argument("--output")

    bridge = commands.add_parser("bridge", help="Open or inspect the local Seld Bridge.")
    bridge_commands = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_open = bridge_commands.add_parser("open", help="Open The Bridge in your browser.")
    bridge_open.add_argument("--no-browser", action="store_true")
    bridge_commands.add_parser("status", help="Show the verified Bridge process state.")
    bridge_commands.add_parser("stop", help="Stop only the verified Seld Bridge process.")
    bridge_native_install = bridge_commands.add_parser(
        "native-install",
        help="Build and install the unsigned local Seld app for this exact vault.",
    )
    bridge_native_install.add_argument("--expected-revision")
    bridge_commands.add_parser(
        "native-status",
        help="Verify the installed native Seld app without opening it.",
    )
    bridge_commands.add_parser(
        "native-open",
        help="Open only the verified current native Seld app.",
    )
    bridge_native_uninstall = bridge_commands.add_parser(
        "native-uninstall",
        help="Remove only the app proven by its Seld ownership receipt.",
    )
    bridge_native_uninstall.add_argument("--expected-revision", required=True)
    bridge_serve = bridge_commands.add_parser("serve", help=argparse.SUPPRESS)
    bridge_serve.add_argument("--port", type=int, default=0)
    bridge_serve.add_argument("--instance-id")

    codex = commands.add_parser(
        "codex",
        help="Manage the supported ChatGPT desktop integration (compatibility command).",
    )
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    for name in ("install", "status", "uninstall"):
        command = codex_commands.add_parser(name)
        command.add_argument("--codex-home", default=str(codex_home()))

    update = commands.add_parser(
        "update",
        help="Check, approve, and recover exact-revision Seld source updates.",
    )
    update_commands = update.add_subparsers(dest="update_command", required=True)
    update_commands.add_parser(
        "status",
        help="Read installed provenance and cached update state without using the network.",
    )
    update_check = update_commands.add_parser(
        "check",
        help="Run the bounded public-main check when its local cache is due.",
    )
    update_check.add_argument(
        "--force",
        action="store_true",
        help="Ignore the six-hour cache for this interactive check.",
    )
    update_apply = update_commands.add_parser(
        "apply",
        help="Install one checked revision after explicit approval in the current ChatGPT task.",
    )
    update_apply.add_argument("--from-sha", required=True)
    update_apply.add_argument("--to-sha", required=True)
    update_apply.add_argument("--expected-check-revision", required=True)
    update_apply.add_argument("--approval-ref", required=True)
    update_recover = update_commands.add_parser(
        "recover",
        help="Finish or restore the one interrupted update named by its recovery token.",
    )
    update_recover.add_argument("--token", required=True)
    update_recover.add_argument(
        "--expected-vault-digest",
        help="Bind an explicitly approved ambiguous recovery to the current vault digest.",
    )
    update_recover.add_argument(
        "--approval-ref",
        help="Current ChatGPT task reference required with --expected-vault-digest.",
    )

    mcp = commands.add_parser("mcp", help="Run the local MCP server.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser("serve")
    mcp_serve.add_argument(
        "--profile",
        choices=(CONNECTOR_PROFILE, GUIDED_REVIEW_PROFILE),
    )
    mcp_serve.add_argument("--event-id")
    rollback_check = commands.add_parser("rollback-check", help=argparse.SUPPRESS)
    rollback_check.add_argument("previous_executable")
    return parser


def _rollback_compatibility(vault: Vault, previous_executable: Path) -> dict[str, Any]:
    """Prove an older executable can read current bytes without changing them."""

    candidate = previous_executable.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        return {"compatible": False, "reason_code": "previous_executable_unavailable"}
    candidate = candidate.resolve()
    try:
        metadata_before = os.lstat(vault.root)
        identity = vault.identity()
        digest_before = vault.logical_digest()
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return {"compatible": False, "reason_code": "current_vault_unavailable"}
    environment = os.environ.copy()
    # Keep the old doctor's optional Codex inspection bounded and local.  Its
    # own executable will reject plugin commands immediately; vault health is
    # the evidence this probe needs.
    environment["GSV_CODEX"] = str(candidate)
    commands: list[tuple[str, ...]] = [("status",), ("doctor",)]
    if CONTROL_STORE_SUPPORTED:
        commands.append(("operation", "list"))
    commands.append(("status",))
    observed: list[dict[str, Any]] = []
    for command in commands:
        result = _run_previous_json(
            candidate,
            vault.root,
            command,
            environment=environment,
        )
        if result is None:
            return {
                "compatible": False,
                "reason_code": "previous_reader_probe_failed",
            }
        observed.append(result)
    status_before = observed[0].get("result")
    doctor = observed[1].get("result")
    status_after = observed[-1].get("result")
    if (
        not isinstance(status_before, dict)
        or not isinstance(doctor, dict)
        or not isinstance(status_after, dict)
        or doctor.get("healthy") is not True
        or status_before.get("vault_id") != identity["vault_id"]
        or status_after.get("vault_id") != identity["vault_id"]
        or status_before.get("digest") != digest_before
        or status_after.get("digest") != digest_before
    ):
        return {"compatible": False, "reason_code": "previous_reader_state_mismatch"}
    try:
        metadata_after = os.lstat(vault.root)
        digest_after = vault.logical_digest()
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return {"compatible": False, "reason_code": "current_vault_recheck_failed"}
    if (int(metadata_before.st_dev), int(metadata_before.st_ino)) != (
        int(metadata_after.st_dev),
        int(metadata_after.st_ino),
    ) or digest_after != digest_before:
        return {"compatible": False, "reason_code": "vault_changed_during_probe"}
    return {
        "compatible": True,
        "digest": digest_before,
        "reason_code": None,
        "vault_id": identity["vault_id"],
    }


def _run_previous_json(
    executable: Path,
    vault_root: Path,
    command: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "--json",
                "--vault",
                str(vault_root),
                *command,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=ROLLBACK_PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or len(completed.stdout) > ROLLBACK_PROBE_MAX_OUTPUT_BYTES:
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload


def _task_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--status", default="captured")
    parser.add_argument("--next-actor", choices=("agent", "human", "external"))
    parser.add_argument("--next-action")
    parser.add_argument("--waiting-on")
    parser.add_argument("--rank", type=int)
    parser.add_argument("--active-thread-id")
    parser.add_argument("--superseded-by")
    parser.add_argument("--project")
    parser.add_argument("--workspace")
    parser.add_argument("--attention-at")
    parser.add_argument("--due")
    parser.add_argument("--entity-link-json", action="append", default=[])
    parser.add_argument("--codex-episode-id", action="append", default=[])
    parser.add_argument("--ref", action="append", default=[])


def _thread_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--status", default="active")
    parser.add_argument("--next-move")
    parser.add_argument("--focus-task-id")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--entity-id", action="append", default=[])
    parser.add_argument("--task-link-json", action="append", default=[])
    parser.add_argument("--entity-link-json", action="append", default=[])
    parser.add_argument("--closure-condition")
    parser.add_argument("--next-actor", choices=("agent", "human", "external"))
    parser.add_argument("--waiting-on")
    parser.add_argument("--recheck-at")
    parser.add_argument("--ref", action="append", default=[])


def _print(
    value: Any,
    *,
    json_output: bool,
    raw: bool,
    ok: bool = True,
    error: str | None = None,
) -> None:
    if raw and isinstance(value, str) and not json_output:
        print(value, end="" if value.endswith("\n") else "\n")
        return
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if json_output:
        payload = {"ok": ok, "result": value}
        if error is not None:
            payload["error"] = error
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))

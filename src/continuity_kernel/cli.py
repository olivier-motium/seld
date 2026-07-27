"""Command-line interface for humans, installers, Codex, and tests."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from continuity_kernel import __version__
from continuity_kernel.bridge import (
    bridge_status,
    open_bridge,
    open_bridge_in_browser,
    serve_bridge,
    stop_bridge,
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
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED
from continuity_kernel.demo import run_demo
from continuity_kernel.direction import direction_aim, direction_dict
from continuity_kernel.errors import ContinuityError, SetupError
from continuity_kernel.mcp_server import GUIDED_REVIEW_PROFILE, serve
from continuity_kernel.operations import OperationLedger, capture_operation_binding
from continuity_kernel.portfolio import (
    portfolio_dict,
    portfolio_inspection_dict,
    portfolio_item,
)
from continuity_kernel.records import record_dict
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
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "ok": False}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> Any:
    explicit_vault = getattr(args, "vault", None)
    if args.command == "setup":
        vault_path = resolve_vault(explicit_vault, require_config=False)
        vault = Vault(vault_path)
        initialized = vault.initialize(name=args.name, command="gsv")
        doctor = doctor_dict(vault.doctor())
        if not doctor["healthy"]:
            return {
                "bridge": None,
                "codex": None,
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
        vault = Vault(resolve_vault(explicit_vault))
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

    vault = Vault(resolve_vault(explicit_vault))
    if args.command == "status":
        return vault.status()
    if args.command == "doctor":
        result = doctor_dict(vault.doctor(repair=args.repair))
        try:
            result["codex"] = {"available": True, **codex_status(codex_home=codex_home())}
        except ContinuityError as exc:
            result["codex"] = {"available": False, "error": str(exc)}
        return result
    if args.command == "context":
        context = vault.context_pack(max_characters=args.max_characters)
        return context if args.format == "markdown" else {"context": context}
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


def _result_failure(args: argparse.Namespace, result: Any) -> tuple[int, str] | None:
    if not isinstance(result, dict):
        return None
    if args.command == "setup" and result.get("setup_complete") is False:
        return 3, "GSV setup stopped because the vault is unhealthy; follow result.next."
    if (
        args.command == "setup"
        and result.get("setup_complete") is True
        and isinstance(result.get("bridge"), dict)
        and result["bridge"].get("running") is False
    ):
        return (
            4,
            "GSV installation committed, but the Bridge needs repair; follow result.next.",
        )
    if args.command == "doctor" and result.get("healthy") is False:
        return 3, "GSV doctor found unresolved integrity issues; follow result.issues."
    if (
        args.command == "backup"
        and args.backup_command == "verify"
        and result.get("valid") is False
    ):
        return 3, "GSV backup verification failed; do not restore this archive."
    if (
        args.command == "backup"
        and args.backup_command == "create"
        and result.get("verified") is False
    ):
        return 3, "GSV backup creation did not verify; do not use the reported archive."
    if (
        args.command == "codex"
        and args.codex_command == "uninstall"
        and result.get("cleanup_complete") is False
    ):
        return 3, "GSV cleanup is incomplete; follow result.next and retry."
    return None


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
            "GSV and its Codex integration are installed, but the Bridge did not start. "
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
            "Codex integration was skipped; run `gsv codex install` when you want a new "
            "Codex hand to load this vault."
        )
    else:
        steps.append(
            "Restart Codex, open one fresh task, and run `$gsv-onboard` to describe your "
            "world and verify only the sources you choose."
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
            clear_next_actor=args.clear_next_actor,
            clear_next_action=args.clear_next_action,
            clear_waiting_on=args.clear_waiting_on,
            clear_rank=args.clear_rank,
            clear_active_thread_id=args.clear_active_thread_id,
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
        )
    )


def _entity(vault: Vault, args: argparse.Namespace) -> Any:
    if args.entity_command == "list":
        return {"entities": [record_dict(item) for item in vault.list_entities()]}
    if args.entity_command == "show":
        return record_dict(vault.get_entity(args.id))
    if args.entity_command == "create":
        return record_dict(
            vault.create_entity(
                identifier=args.id,
                title=args.title,
                entity_type=args.entity_type,
                summary=args.summary,
                aliases=tuple(args.alias),
                refs=tuple(args.ref),
            )
        )
    return record_dict(
        vault.update_entity(
            args.id,
            expected_revision=args.expected_revision,
            title=args.title,
            summary=args.summary,
            aliases=tuple(args.alias) if args.alias is not None else None,
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
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
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("each --item-json value must be a JSON object")
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
            )
        )
    return portfolio_dict(
        vault.set_portfolio(
            expected_revision=args.expected_revision,
            summary=args.summary,
            items=tuple(parsed),
            direction_revision=args.direction_revision,
        )
    )


def _direction(vault: Vault, args: argparse.Namespace) -> Any:
    if args.direction_command == "show":
        return direction_dict(vault.get_direction())
    aims = []
    for encoded in args.aim_json:
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("each --aim-json value must be a JSON object")
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
        )
    )


def _thread(vault: Vault, args: argparse.Namespace) -> Any:
    if args.thread_command == "list":
        return {"threads": [record_dict(item) for item in vault.list_threads(status=args.status)]}
    if args.thread_command == "show":
        return record_dict(vault.get_thread(args.id))
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
                refs=tuple(args.ref),
            )
        )
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
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
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
        description="Local-first durable state for coding agents.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--vault", help="Override the configured vault path.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    commands = parser.add_subparsers(dest="command")

    setup = commands.add_parser(
        "setup", help="Initialize a vault and install the Codex integration."
    )
    setup.add_argument("--name", default="My GSV")
    setup.add_argument("--codex-home", default=str(codex_home()))
    setup.add_argument("--no-bridge", action="store_true")
    setup.add_argument("--no-browser", action="store_true")
    setup.add_argument("--no-codex", action="store_true")

    init = commands.add_parser("init", help="Initialize a vault without changing Codex.")
    init.add_argument("--name", default="My GSV")
    init.add_argument("--configure", action="store_true")

    commands.add_parser("status", help="Show vault identity and counts.")
    doctor = commands.add_parser("doctor", help="Validate storage and Codex integration.")
    doctor.add_argument(
        "--repair", action="store_true", help="Remove interrupted-write temp files only."
    )
    context = commands.add_parser("context", help="Render a bounded context pack.")
    context.add_argument("--format", choices=("markdown", "json"), default="markdown")
    context.add_argument("--max-characters", type=int, default=48_000)
    context.set_defaults(raw=True)

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
    task_update.add_argument("--clear-next-actor", action="store_true")
    task_update.add_argument("--clear-next-action", action="store_true")
    task_update.add_argument("--clear-waiting-on", action="store_true")
    task_update.add_argument("--clear-rank", action="store_true")
    task_update.add_argument("--clear-active-thread-id", action="store_true")
    task_update.add_argument("--add-ref", action="append", default=[])
    task_update.add_argument("--remove-ref", action="append", default=[])

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

    entity = commands.add_parser("entity", help="Create and inspect canonical entities.")
    entity_commands = entity.add_subparsers(dest="entity_command", required=True)
    entity_commands.add_parser("list")
    entity_show = entity_commands.add_parser("show")
    entity_show.add_argument("id")
    entity_create = entity_commands.add_parser("create")
    entity_create.add_argument("--id", required=True)
    entity_create.add_argument("--title", required=True)
    entity_create.add_argument("--entity-type", required=True)
    entity_create.add_argument("--summary", required=True)
    entity_create.add_argument("--alias", action="append", default=[])
    entity_create.add_argument("--ref", action="append", default=[])
    entity_update = entity_commands.add_parser("update")
    entity_update.add_argument("id")
    entity_update.add_argument("--expected-revision", required=True)
    entity_update.add_argument("--title")
    entity_update.add_argument("--summary")
    entity_update.add_argument("--alias", action="append")
    entity_update.add_argument("--add-ref", action="append", default=[])
    entity_update.add_argument("--remove-ref", action="append", default=[])

    thread = commands.add_parser("thread", help="Create, inspect, and update work threads.")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_list = thread_commands.add_parser("list")
    thread_list.add_argument("--status")
    thread_show = thread_commands.add_parser("show")
    thread_show.add_argument("id")
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
    thread_update.add_argument("--add-ref", action="append", default=[])
    thread_update.add_argument("--remove-ref", action="append", default=[])

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

    demo = commands.add_parser("demo", help="Run the complete synthetic GSV proof.")
    demo.add_argument("--output")

    bridge = commands.add_parser("bridge", help="Open or inspect the local GSV Bridge.")
    bridge_commands = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_open = bridge_commands.add_parser("open", help="Open The Bridge in your browser.")
    bridge_open.add_argument("--no-browser", action="store_true")
    bridge_commands.add_parser("status", help="Show the verified Bridge process state.")
    bridge_commands.add_parser("stop", help="Stop only the verified GSV Bridge process.")
    bridge_serve = bridge_commands.add_parser("serve", help=argparse.SUPPRESS)
    bridge_serve.add_argument("--port", type=int, default=0)
    bridge_serve.add_argument("--instance-id")

    codex = commands.add_parser("codex", help="Manage the supported Codex integration.")
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    for name in ("install", "status", "uninstall"):
        command = codex_commands.add_parser(name)
        command.add_argument("--codex-home", default=str(codex_home()))

    mcp = commands.add_parser("mcp", help="Run the local MCP server.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser("serve")
    mcp_serve.add_argument("--profile", choices=(GUIDED_REVIEW_PROFILE,))
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

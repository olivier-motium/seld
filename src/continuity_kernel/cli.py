"""Command-line interface for humans, installers, Codex, and tests."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from continuity_kernel import __version__
from continuity_kernel.atomic import atomic_write
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
    codex_home,
    config_path,
    load_config,
    resolve_vault,
    save_config,
)
from continuity_kernel.demo import run_demo
from continuity_kernel.errors import ContinuityError, SetupError
from continuity_kernel.mcp_server import serve
from continuity_kernel.records import record_dict
from continuity_kernel.vault import Vault, doctor_dict


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
            return serve(Vault(resolve_vault(getattr(args, "vault", None))))
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
        configuration = config_path()
        previous_config_exists = configuration.exists()
        previous_config = configuration.read_bytes() if previous_config_exists else b""
        save_config(vault_path)
        installed_config = configuration.read_bytes()
        integration = None
        bridge = None
        try:
            if args.no_codex:
                if not args.no_bridge:
                    bridge = open_bridge(vault, open_browser=False)
            else:
                with install_codex_transaction(
                    vault=vault_path, codex_home=Path(args.codex_home)
                ) as staged:
                    integration = asdict(staged)
                    if not args.no_bridge:
                        bridge = open_bridge(vault, open_browser=False)
            if bridge is not None and not args.no_browser:
                browser_opened = open_bridge_in_browser(vault)
                bridge = {**bridge, "browser_opened": browser_opened}
        except Exception as exc:
            if bridge is not None and bridge.get("started"):
                stop_bridge()
            rollback_error = _restore_setup_config(
                configuration,
                previous_exists=previous_config_exists,
                previous=previous_config,
                installed=installed_config,
            )
            if rollback_error is not None:
                raise SetupError(f"{exc}; {rollback_error}") from exc
            raise
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
    if args.command == "entity":
        return _entity(vault, args)
    if args.command == "thread":
        return _thread(vault, args)
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
        if args.backup_command == "verify":
            return Vault.verify_backup(Path(args.path))
        if args.backup_command == "restore":
            return Vault.restore_backup(Path(args.path), Path(args.target))
    raise AssertionError("unreachable command")


def _restore_setup_config(
    path: Path, *, previous_exists: bool, previous: bytes, installed: bytes
) -> str | None:
    try:
        current = path.read_bytes() if path.exists() else b""
        if current != installed:
            return "GSV configuration changed concurrently and was left untouched"
        if previous_exists:
            atomic_write(path, previous)
        elif path.exists():
            path.unlink()
        return None
    except OSError as exc:
        return f"could not restore the previous GSV configuration: {exc}"


def _result_failure(args: argparse.Namespace, result: Any) -> tuple[int, str] | None:
    if not isinstance(result, dict):
        return None
    if args.command == "setup" and result.get("setup_complete") is False:
        return 3, "GSV setup stopped because the vault is unhealthy; follow result.next."
    if args.command == "doctor" and result.get("healthy") is False:
        return 3, "GSV doctor found unresolved integrity issues; follow result.issues."
    if (
        args.command == "codex"
        and args.codex_command == "uninstall"
        and result.get("cleanup_complete") is False
    ):
        return 3, "GSV cleanup is incomplete; follow result.next and retry."
    return None


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
        steps.append("Restart Codex, then open a fresh task and ask: What do you remember?")
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
            clear_next_actor=args.clear_next_actor,
            clear_next_action=args.clear_next_action,
            clear_waiting_on=args.clear_waiting_on,
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
            task_ids=task_ids,
            entity_ids=entity_ids,
            add_refs=tuple(args.add_ref),
            remove_refs=tuple(args.remove_ref),
        )
    )


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
    task_update.add_argument("--clear-next-actor", action="store_true")
    task_update.add_argument("--clear-next-action", action="store_true")
    task_update.add_argument("--clear-waiting-on", action="store_true")
    task_update.add_argument("--add-ref", action="append", default=[])
    task_update.add_argument("--remove-ref", action="append", default=[])

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
    thread_update.add_argument("--task-id", action="append")
    thread_update.add_argument("--entity-id", action="append")
    thread_update.add_argument("--add-ref", action="append", default=[])
    thread_update.add_argument("--remove-ref", action="append", default=[])

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
    mcp_commands.add_parser("serve")
    return parser


def _task_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--status", default="captured")
    parser.add_argument("--next-actor", choices=("agent", "human", "external"))
    parser.add_argument("--next-action")
    parser.add_argument("--waiting-on")
    parser.add_argument("--ref", action="append", default=[])


def _thread_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--status", default="active")
    parser.add_argument("--next-move")
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

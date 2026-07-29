from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from continuity_kernel import resident_context
from continuity_kernel.atomic import PINNED_PATH_ROOT_SUPPORTED
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.records import Task, WorkThread, WorkThreadTaskLink
from continuity_kernel.vault_context import build_context_pack


def _write_skill(root: Path, name: str = "exact-skill") -> Path:
    skill = root / "context/resident/skills" / name
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Exact imported skill.\n---\n\n# Exact\n",
        encoding="utf-8",
    )
    (skill / "references/evidence.md").write_text("# Evidence\n\nRead exactly.\n", encoding="utf-8")
    (skill / "scripts/check.py").write_text("print('exact')\n", encoding="utf-8")
    return skill


def test_resident_context_reads_exact_guidance_and_content_free_skill_status(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    resident = vault / "context/resident"
    resident.mkdir(parents=True)
    guidance = "# Resident guidance\n\nKeep this exact.\n"
    (resident / "AGENTS.md").write_text(guidance, encoding="utf-8")
    _write_skill(vault)

    shown = resident_context.read_resident_guidance(vault)
    status = resident_context.resident_context_status(vault)

    assert shown["content"] == guidance
    assert shown["path"] == "context/resident/AGENTS.md"
    assert shown["bytes"] == len(guidance.encode())
    assert status["available"] is True
    assert status["excluded_paths"] == ["context/resident/control"]
    assert status["guidance"] == {
        "bytes": shown["bytes"],
        "path": shown["path"],
        "present": True,
        "sha256": shown["sha256"],
    }
    assert status["skills_total"] == 1
    assert status["skills"][0]["name"] == "exact-skill"
    assert "content" not in status["skills"][0]


def test_legacy_resident_control_is_inert_and_never_bundled(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    resident = vault / "context/resident"
    resident.mkdir(parents=True)
    guidance = "# Resident guidance\n\nUse only this guidance.\n"
    (resident / "AGENTS.md").write_text(guidance, encoding="utf-8")
    _write_skill(vault)
    control = resident / "control"
    control.mkdir()
    (control / "PULSE").write_text("private-host-task-id\n", encoding="utf-8")
    (control / "RESIDENT").write_text("private-host-hand-id\n", encoding="utf-8")

    shown = resident_context.read_resident_guidance(vault)
    status = resident_context.resident_context_status(vault)
    merged = resident_context.add_resident_skills_to_marketplace(vault, {})

    assert shown["content"] == guidance
    assert status["excluded_paths"] == ["context/resident/control"]
    assert "private-host-task-id" not in repr(status)
    assert all("control" not in path.split("/") for path in merged)
    assert b"private-host" not in repr(merged).encode()


def test_imported_skill_tree_is_merged_with_exact_names_references_and_scripts(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    skill = _write_skill(vault)
    packaged: dict[str, bytes | None] = {
        "plugins/gsv/skills/gsv": None,
        "plugins/gsv/skills/gsv/SKILL.md": b"---\nname: gsv\ndescription: Built in.\n---\n",
    }

    merged = resident_context.add_resident_skills_to_marketplace(vault, packaged)

    assert merged["plugins/gsv/skills/gsv/SKILL.md"] == packaged["plugins/gsv/skills/gsv/SKILL.md"]
    imported_markdown = merged["plugins/gsv/skills/exact-skill/SKILL.md"]
    imported_reference = merged["plugins/gsv/skills/exact-skill/references/evidence.md"]
    imported_script = merged["plugins/gsv/skills/exact-skill/scripts/check.py"]
    assert isinstance(imported_markdown, resident_context.ResidentSkillFile)
    assert isinstance(imported_reference, resident_context.ResidentSkillFile)
    assert isinstance(imported_script, resident_context.ResidentSkillFile)
    assert imported_markdown.content == (skill / "SKILL.md").read_bytes()
    assert imported_reference.content == (skill / "references/evidence.md").read_bytes()
    assert imported_script.content == (skill / "scripts/check.py").read_bytes()
    assert imported_markdown.executable is False
    assert imported_reference.executable is False
    assert imported_script.executable is False


@pytest.mark.parametrize("name", ["gsv", "GSV"])
def test_imported_skill_collision_preserves_built_in_skill(tmp_path: Path, name: str) -> None:
    vault = tmp_path / "vault"
    _write_skill(vault, name)
    packaged: dict[str, bytes | None] = {
        "plugins/gsv/skills/gsv": None,
        "plugins/gsv/skills/gsv/SKILL.md": b"---\nname: gsv\ndescription: Built in.\n---\n",
    }

    with pytest.raises(ConflictError, match="preserved the built-in skill"):
        resident_context.add_resident_skills_to_marketplace(vault, packaged)

    assert packaged["plugins/gsv/skills/gsv/SKILL.md"] == (
        b"---\nname: gsv\ndescription: Built in.\n---\n"
    )


def test_imported_context_rejects_links_invalid_utf8_and_secrets(tmp_path: Path) -> None:
    linked_vault = tmp_path / "linked"
    linked = _write_skill(linked_vault)
    target = linked / "references/target.md"
    target.write_text("ordinary\n", encoding="utf-8")
    (linked / "references/link.md").symlink_to(target)
    with pytest.raises(ValidationError, match="symbolic link"):
        resident_context.resident_skills(linked_vault)

    utf8_vault = tmp_path / "utf8"
    utf8 = _write_skill(utf8_vault)
    (utf8 / "scripts/check.py").write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationError, match="UTF-8"):
        resident_context.resident_skills(utf8_vault)

    secret_vault = tmp_path / "secret"
    secret = _write_skill(secret_vault)
    (secret / "references/evidence.md").write_text(
        "Authorization: Bearer should-not-be-copied\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="privacy screening"):
        resident_context.resident_skills(secret_vault)

    entropy_vault = tmp_path / "entropy-secret"
    entropy = _write_skill(entropy_vault)
    (entropy / "references/evidence.md").write_text(
        "6QJv8rE2mK9zWp4Lc7Hd1Tx5Bn3Fg0YsUaIeOoPvNqR\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="high-entropy-token"):
        resident_context.resident_skills(entropy_vault)

    secret_path_vault = tmp_path / "secret-path"
    secret_path = _write_skill(secret_path_vault)
    (secret_path / "references/credentials.json").write_text(
        '{"token": "EXAMPLE_TOKEN"}\n', encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="excluded by privacy policy"):
        resident_context.resident_skills(secret_path_vault)

    oversized_vault = tmp_path / "oversized"
    oversized = oversized_vault / "context/resident"
    oversized.mkdir(parents=True)
    (oversized / "AGENTS.md").write_bytes(b"a" * (resident_context.MAX_GUIDANCE_BYTES + 1))
    with pytest.raises(ValidationError, match="size bound"):
        resident_context.read_resident_guidance(oversized_vault)


@pytest.mark.skipif(not PINNED_PATH_ROOT_SUPPORTED, reason="POSIX pinned read")
def test_imported_skill_snapshot_rejects_a_file_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    skill = _write_skill(vault)
    original = resident_context._validate_imported_skill_content
    raced = False

    def replace_after_read(content: bytes, relative: str) -> None:
        nonlocal raced
        original(content, relative)
        if relative == "exact-skill/SKILL.md" and not raced:
            raced = True
            replacement = skill / "replacement"
            replacement.write_text(
                "---\nname: exact-skill\ndescription: replacement\n---\n",
                encoding="utf-8",
            )
            os.replace(replacement, skill / "SKILL.md")

    monkeypatch.setattr(
        resident_context,
        "_validate_imported_skill_content",
        replace_after_read,
    )

    with pytest.raises(ValidationError, match="changed while it was read"):
        resident_context.resident_skills(vault)


@dataclass
class _ScaleSource:
    tasks: list[Task]
    threads: list[WorkThread]

    def read_document(self, name: str) -> dict[str, str]:
        return {"content": f"# {name}\n\nCurrent.\n"}

    def list_tasks(self) -> list[Task]:
        return self.tasks

    def list_threads(self) -> list[WorkThread]:
        return self.threads


def _task(index: int, *, active: bool) -> Task:
    identifier = f"task-{index:03d}"
    return Task(
        identifier=identifier,
        title=f"Task {index:03d}",
        status="doing" if active else "ready",
        next_actor="agent",
        outcome="Large stored outcome " * 12,
        next_action="Continue from the exact record.",
        waiting_on=None,
        rank=None,
        active_thread_id=f"00000000-0000-4000-8000-{index:012d}" if active else None,
        refs=(),
        created_at="2026-07-29T00:00:00Z",
        updated_at="2026-07-29T00:00:00Z",
        revision=f"{index:064x}"[-64:],
    )


def _thread(index: int, *, focused: bool) -> WorkThread:
    task_id = f"task-{index:03d}"
    return WorkThread(
        identifier=f"thread:work-{index:03d}",
        title=f"Thread {index:03d}",
        status="active",
        purpose="Carry a large exact concern " * 12,
        summary="Current structural state.",
        next_move="Continue.",
        focus_task_id=task_id if focused else None,
        task_links=(WorkThreadTaskLink(1, task_id),),
        entity_links=(),
        refs=(),
        created_at="2026-07-29T00:00:00Z",
        updated_at="2026-07-29T00:00:00Z",
        revision=f"{index + 500:064x}"[-64:],
    )


def test_real_scale_execution_bindings_remain_complete_when_context_omits_details() -> None:
    source = _ScaleSource(
        tasks=[_task(index, active=index >= 296) for index in range(313)],
        threads=[_thread(index, focused=index >= 71) for index in range(89)],
    )

    context = build_context_pack(source, max_characters=48_000)
    bindings = resident_context.execution_bindings(source)

    assert len(context) <= 48_000
    assert "task-312" not in context
    assert "thread:work-088" not in context
    assert bindings["active_hand_count"] == 17
    assert bindings["focused_thread_count"] == 18
    assert bindings["active_hands"][-1]["task_id"] == "task-312"
    assert bindings["focused_threads"][-1] == {
        "focus_task_id": "task-088",
        "revision": f"{588:064x}",
        "status": "active",
        "thread_id": "thread:work-088",
    }

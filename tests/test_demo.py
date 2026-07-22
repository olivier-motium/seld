from __future__ import annotations

from pathlib import Path

import pytest

from continuity_kernel.demo import run_demo
from continuity_kernel.errors import ConflictError


def test_demo_proves_all_public_gsv_guarantees(tmp_path: Path) -> None:
    result = run_demo(tmp_path / "demo")

    assert result["backup_verified"] is True
    assert result["context_resumed"] is True
    assert result["doctor_healthy"] is True
    assert result["logical_restore_equivalent"] is True
    assert result["same_name_entities_disambiguated"] is True
    assert result["stale_write_rejected"] is True
    assert result["task_revision_before"] != result["task_revision_after"]
    assert result["restore"]["temporary_target_removed"] is True
    assert not (tmp_path / "demo-restored").exists()


def test_demo_refuses_to_overwrite_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ConflictError, match="not empty"):
        run_demo(target)

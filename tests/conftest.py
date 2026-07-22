from __future__ import annotations

from pathlib import Path

import pytest

from continuity_kernel.vault import Vault


@pytest.fixture(autouse=True)
def isolated_platform_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTINUITY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CONTINUITY_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    value = Vault(tmp_path / "vault")
    value.initialize(name="Test Continuity")
    return value

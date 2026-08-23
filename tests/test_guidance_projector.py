"""Tests for the native checkout-to-Seld guidance projector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuity_kernel import mcp_server
from continuity_kernel.atomic import atomic_write, sha256_bytes
from continuity_kernel.cli import main
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.resident_context import (
    GUIDANCE_PROJECTION_INTENT,
    GUIDANCE_PROJECTION_MARKER,
    MAX_DOCUMENT_BYTES,
    MAX_GUIDANCE_BYTES,
    read_resident_guidance,
    resident_context_status,
    validate_checkout_guidance_sources,
)
from continuity_kernel.vault import Vault


def _setup_synthetic_checkout(checkout_dir: Path) -> tuple[Path, Path]:
    """Create a synthetic, owner-neutral checkout with canonical guidance sources."""
    checkout_dir.mkdir(parents=True, exist_ok=True)
    agents_path = checkout_dir / "AGENTS.md"
    agents_path.write_text(
        "# Synthetic Guidance\n\nCanonical checkout resident guidance for test execution.\n",
        encoding="utf-8",
    )
    brain_dir = checkout_dir / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    mind_path = brain_dir / "MIND.md"
    mind_path.write_text(
        "# Synthetic Mind\n\n## Purpose\n\nCanonical checkout mind content.\n",
        encoding="utf-8",
    )
    return agents_path, mind_path


def test_guidance_projector_comprehensive_behavior(vault: Vault, tmp_path: Path) -> None:
    """Acceptance test: successful two-file publication, stale-CAS refusal,

    managed direct-MIND refusal, doctor drift, and rollback on injected failure.
    """
    checkout_dir = tmp_path / "synthetic-checkout"
    agents_src, mind_src = _setup_synthetic_checkout(checkout_dir)

    initial_mind = vault.read_document("MIND.md")
    initial_mind_rev = initial_mind["revision"]

    # 1. Successful two-file publication from initial state
    result = vault.project_guidance(
        checkout_dir,
        expected_guidance_revision="absent",
        expected_mind_revision=initial_mind_rev,
    )

    assert result["status"] == "projected"
    assert result["checkout_root"] == str(checkout_dir.resolve())
    assert result["guidance"]["before_revision"] == "absent"
    assert result["mind"]["before_revision"] == initial_mind_rev

    # Readback verification
    published_guidance = read_resident_guidance(vault.root)
    assert published_guidance["content"] == agents_src.read_text(encoding="utf-8")
    assert published_guidance["sha256"] == result["guidance"]["revision"]

    published_mind = vault.read_document("MIND.md")
    assert published_mind["content"] == mind_src.read_text(encoding="utf-8")
    assert published_mind["revision"] == result["mind"]["revision"]

    # Managed marker verification
    marker_path = vault.root / GUIDANCE_PROJECTION_MARKER
    assert marker_path.exists()
    marker_data = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker_data["format_version"] == 1
    assert marker_data["sources"]["AGENTS.md"]["sha256"] == sha256_bytes(agents_src.read_bytes())
    assert marker_data["sources"]["brain/MIND.md"]["sha256"] == sha256_bytes(mind_src.read_bytes())
    assert vault.is_guidance_managed() is True

    # Doctor check when healthy
    assert vault.doctor().healthy is True

    current_guidance_rev = published_guidance["sha256"]
    current_mind_rev = published_mind["revision"]

    # 2. Stale CAS refusal
    with pytest.raises(ConflictError):
        vault.project_guidance(
            checkout_dir,
            expected_guidance_revision="stale-guidance-revision",
            expected_mind_revision=current_mind_rev,
        )

    with pytest.raises(ConflictError):
        vault.project_guidance(
            checkout_dir,
            expected_guidance_revision=current_guidance_rev,
            expected_mind_revision="stale-mind-revision",
        )

    # 3. Case bypass refusal for direct MIND.md update
    for attempt in ("MIND.md", "mind.md", "Mind.md", "  MIND.MD  "):
        with pytest.raises(ConflictError) as exc_info:
            vault.write_document(
                attempt,
                "# Unmanaged Attempt\n\nDirect mutation should be refused.\n",
                expected_revision=current_mind_rev,
            )
        assert "managed by checkout guidance projection" in str(exc_info.value)

    # Direct update to unmanaged documents like NOW.md still succeeds
    now_doc = vault.read_document("NOW.md")
    updated_now = vault.write_document(
        "NOW.md",
        "# Now\n\nUnmanaged document update works as expected.\n",
        expected_revision=now_doc["revision"],
    )
    assert updated_now["revision"] == sha256_bytes(updated_now["content"].encode())

    # 4. Doctor reports source hash drift
    agents_src.write_text("# Modified Guidance Drift\n", encoding="utf-8")
    doctor_drift = vault.doctor()
    assert doctor_drift.healthy is False
    drift_issues = [i for i in doctor_drift.issues if i.code == "guidance-source-drift"]
    assert len(drift_issues) == 1
    assert "AGENTS.md" in drift_issues[0].message or "AGENTS.md" in drift_issues[0].path

    # Restore AGENTS.md
    agents_src.write_text(
        "# Synthetic Guidance\n\nCanonical checkout resident guidance for test execution.\n",
        encoding="utf-8",
    )
    assert vault.doctor().healthy is True

    # Drift in MIND.md
    mind_src.write_text("# Modified Mind Drift\n", encoding="utf-8")
    doctor_drift_mind = vault.doctor()
    assert doctor_drift_mind.healthy is False
    mind_drift_issues = [i for i in doctor_drift_mind.issues if i.code == "guidance-source-drift"]
    assert len(mind_drift_issues) == 1

    # Restore MIND.md
    mind_src.write_text(
        "# Synthetic Mind\n\n## Purpose\n\nCanonical checkout mind content.\n",
        encoding="utf-8",
    )
    assert vault.doctor().healthy is True

    # 5. Rollback on injected partial failure
    guidance_before_bytes = (vault.root / "context/resident/AGENTS.md").read_bytes()
    mind_before_bytes = (vault.root / "MIND.md").read_bytes()
    marker_before_bytes = marker_path.read_bytes()

    agents_src.write_text(
        "# Updated Guidance\n\nNew guidance for projection rollback test.\n",
        encoding="utf-8",
    )
    mind_src.write_text(
        "# Updated Mind\n\nNew mind for projection rollback test.\n",
        encoding="utf-8",
    )

    for fail_point in (
        "before_intent",
        "after_intent",
        "after_guidance_publish",
        "after_mind_publish",
        "after_marker_publish",
        "after_readback",
        "after_audit",
    ):
        with pytest.raises(PersistenceError):
            vault.project_guidance(
                checkout_dir,
                expected_guidance_revision=current_guidance_rev,
                expected_mind_revision=current_mind_rev,
                _fail_during=fail_point,
            )

        # Verify exact rollback and no intent residue
        assert (vault.root / "context/resident/AGENTS.md").read_bytes() == guidance_before_bytes
        assert (vault.root / "MIND.md").read_bytes() == mind_before_bytes
        assert marker_path.read_bytes() == marker_before_bytes
        assert not (vault.root / GUIDANCE_PROJECTION_INTENT).exists()


def test_guidance_projector_crash_interrupted_recovery_via_resident_readers(
    vault: Vault, tmp_path: Path
) -> None:
    """Test that resident readers invoked first after interruption durably recover state."""
    initial_mind_bytes = (vault.root / "MIND.md").read_bytes()

    # Prior guidance state
    guidance_target = vault.root / "context/resident/AGENTS.md"
    guidance_target.parent.mkdir(parents=True, exist_ok=True)
    guidance_target.write_text("# Prior Guidance\n", encoding="utf-8")
    prior_guidance_bytes = guidance_target.read_bytes()

    intent_file = vault.root / GUIDANCE_PROJECTION_INTENT

    # 1. Simulate process death after AGENTS was written: call read_resident_guidance first
    intent_payload = {
        "format_version": 1,
        "guidance_before_hex": prior_guidance_bytes.hex(),
        "mind_before_hex": initial_mind_bytes.hex(),
        "marker_before_hex": None,
        "target_guidance_sha256": "0" * 64,
        "target_mind_sha256": "1" * 64,
    }
    atomic_write(intent_file, json.dumps(intent_payload).encode("utf-8") + b"\n")

    # Dirty post-crash bytes
    guidance_target.write_text("# Dirty Crashed Guidance\n", encoding="utf-8")
    (vault.root / "MIND.md").write_text("# Dirty Crashed Mind\n", encoding="utf-8")

    # read_resident_guidance called FIRST after interruption
    guidance_res = read_resident_guidance(vault.root)
    assert guidance_res["content"] == "# Prior Guidance\n"
    assert guidance_target.read_bytes() == prior_guidance_bytes
    assert (vault.root / "MIND.md").read_bytes() == initial_mind_bytes
    assert not intent_file.exists()

    # 2. Simulate another crash: call resident_context_status first
    atomic_write(intent_file, json.dumps(intent_payload).encode("utf-8") + b"\n")
    guidance_target.write_text("# Dirty Crashed Guidance 2\n", encoding="utf-8")
    (vault.root / "MIND.md").write_text("# Dirty Crashed Mind 2\n", encoding="utf-8")

    status_res = resident_context_status(vault.root)
    assert status_res["guidance"]["present"] is True
    assert status_res["guidance"]["sha256"] == sha256_bytes(prior_guidance_bytes)
    assert guidance_target.read_bytes() == prior_guidance_bytes
    assert (vault.root / "MIND.md").read_bytes() == initial_mind_bytes
    assert not intent_file.exists()


def test_guidance_projector_malformed_intent_fails_closed(vault: Vault) -> None:
    """Test that malformed/corrupted intent fails closed without unlinking the intent file."""
    intent_file = vault.root / GUIDANCE_PROJECTION_INTENT

    # Corrupt intent
    atomic_write(intent_file, b"corrupted-intent-content\n")

    # read_resident_guidance must fail closed
    with pytest.raises(DegradedIntegrityError):
        read_resident_guidance(vault.root)
    assert intent_file.exists()

    # resident_context_status must fail closed
    with pytest.raises(DegradedIntegrityError):
        resident_context_status(vault.root)
    assert intent_file.exists()

    # read_document for MIND.md must fail closed
    with pytest.raises(DegradedIntegrityError):
        vault.read_document("MIND.md")
    assert intent_file.exists()

    # Clean up intent for subsequent tests
    intent_file.unlink()


def test_guidance_projector_malformed_marker_does_not_silently_block_mind(
    vault: Vault, tmp_path: Path
) -> None:
    """Test that malformed/partial markers do not block MIND and doctor reports defect."""
    marker_path = vault.root / GUIDANCE_PROJECTION_MARKER

    # 1. Partial/invalid JSON marker
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text('{"format_version": 1, "checkout_root": "/tmp"}', encoding="utf-8")

    # Marker is not fully valid schema
    assert vault.is_guidance_managed() is False

    # MIND.md updates must NOT be blocked by an invalid marker
    mind_doc = vault.read_document("MIND.md")
    updated = vault.write_document(
        "MIND.md",
        "# Mind\n\nAllowed update when marker is invalid.\n",
        expected_revision=mind_doc["revision"],
    )
    assert "Allowed update" in updated["content"]

    # Doctor must report invalid marker
    doctor_res = vault.doctor()
    assert doctor_res.healthy is False
    assert any(i.code == "invalid-guidance-projection" for i in doctor_res.issues)

    # 2. Corrupted raw bytes marker
    marker_path.write_text("not json", encoding="utf-8")
    assert vault.is_guidance_managed() is False
    doctor_res2 = vault.doctor()
    assert doctor_res2.healthy is False
    assert any(i.code == "invalid-guidance-projection" for i in doctor_res2.issues)


def test_guidance_projector_source_validation(vault: Vault, tmp_path: Path) -> None:
    """Test boundary validation for checkout sources."""
    checkout_dir = tmp_path / "validation-checkout"
    checkout_dir.mkdir(parents=True, exist_ok=True)

    # Missing checkout directory
    with pytest.raises(ValidationError):
        validate_checkout_guidance_sources(tmp_path / "nonexistent")

    # Missing AGENTS.md
    (checkout_dir / "brain").mkdir(parents=True, exist_ok=True)
    (checkout_dir / "brain/MIND.md").write_text("# Mind\n", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_checkout_guidance_sources(checkout_dir)
    assert "guidance file is missing" in str(exc.value)

    # Missing brain/MIND.md
    (checkout_dir / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (checkout_dir / "brain/MIND.md").unlink()
    with pytest.raises(ValidationError) as exc:
        validate_checkout_guidance_sources(checkout_dir)
    assert "document file is missing" in str(exc.value)

    # Null bytes
    (checkout_dir / "AGENTS.md").write_bytes(b"# Agents\x00with null")
    (checkout_dir / "brain/MIND.md").write_text("# Mind\n", encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_checkout_guidance_sources(checkout_dir)
    assert "null byte" in str(exc.value)

    # Oversized guidance
    (checkout_dir / "AGENTS.md").write_bytes(b"A" * (MAX_GUIDANCE_BYTES + 1))
    with pytest.raises(ValidationError):
        validate_checkout_guidance_sources(checkout_dir)

    # Oversized document
    (checkout_dir / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (checkout_dir / "brain/MIND.md").write_bytes(b"M" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ValidationError):
        validate_checkout_guidance_sources(checkout_dir)


def test_guidance_projector_symlink_rejection(vault: Vault, tmp_path: Path) -> None:
    """Test that symlinks in checkout sources are strictly refused."""
    real_target = tmp_path / "real_file.md"
    real_target.write_text("# Real content\n", encoding="utf-8")

    checkout_dir = tmp_path / "symlink-checkout"
    checkout_dir.mkdir(parents=True, exist_ok=True)
    brain_dir = checkout_dir / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)

    # Symlink AGENTS.md
    agents_link = checkout_dir / "AGENTS.md"
    agents_link.symlink_to(real_target)
    (brain_dir / "MIND.md").write_text("# Mind\n", encoding="utf-8")

    with pytest.raises(ValidationError) as exc:
        validate_checkout_guidance_sources(checkout_dir)
    assert "must be a regular file" in str(exc.value)

    # Symlink brain/MIND.md
    agents_link.unlink()
    agents_link.write_text("# Agents\n", encoding="utf-8")
    mind_link = brain_dir / "MIND.md"
    mind_link.unlink()
    mind_link.symlink_to(real_target)

    with pytest.raises(ValidationError) as exc:
        validate_checkout_guidance_sources(checkout_dir)
    assert "must be a regular file" in str(exc.value)


def test_guidance_projector_doctor_vault_target_drift(vault: Vault, tmp_path: Path) -> None:
    """Test doctor reports on vault target file drift."""
    checkout_dir = tmp_path / "doctor-target-checkout"
    _setup_synthetic_checkout(checkout_dir)

    mind = vault.read_document("MIND.md")
    vault.project_guidance(
        checkout_dir,
        expected_guidance_revision="absent",
        expected_mind_revision=mind["revision"],
    )

    # 1. Vault target drift: modify context/resident/AGENTS.md directly
    (vault.root / "context/resident/AGENTS.md").write_text("# Tampered Guidance\n")
    doctor_res = vault.doctor()
    assert doctor_res.healthy is False
    assert any(i.code == "guidance-projection-drift" for i in doctor_res.issues)

    # 2. Vault target drift: delete MIND.md
    (vault.root / "MIND.md").unlink()
    doctor_res = vault.doctor()
    assert doctor_res.healthy is False
    assert any(i.code == "guidance-projection-drift" for i in doctor_res.issues)


def test_guidance_projector_cli(
    vault: Vault, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the CLI surface for resident-context project."""
    checkout_dir = tmp_path / "cli-checkout"
    _setup_synthetic_checkout(checkout_dir)

    mind = vault.read_document("MIND.md")

    rc = main(
        [
            "--vault",
            str(vault.root),
            "resident-context",
            "project",
            "--checkout-root",
            str(checkout_dir),
            "--expected-guidance-revision",
            "absent",
            "--expected-mind-revision",
            mind["revision"],
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "projected"


def test_guidance_projector_mcp(vault: Vault, tmp_path: Path) -> None:
    """Test the MCP surface for gsv_resident_guidance_project."""
    checkout_dir = tmp_path / "mcp-checkout"
    _setup_synthetic_checkout(checkout_dir)

    mind = vault.read_document("MIND.md")

    result = mcp_server._call(
        "gsv_resident_guidance_project",
        {
            "checkout_root": str(checkout_dir),
            "expected_guidance_revision": "absent",
            "expected_mind_revision": mind["revision"],
        },
        vault=vault,
    )
    assert result["status"] == "projected"


def test_guidance_projector_real_checkout_canary(vault: Vault) -> None:
    """Production-path canary test against the real second-brain checkout."""
    real_checkout = (
        Path.home() / "Desktop" / "motium_github" / "second-brain-issue131-final-20260823"
    )
    if not real_checkout.exists():
        pytest.skip(f"Real checkout not found at {real_checkout}")

    sources = validate_checkout_guidance_sources(real_checkout)
    assert sources.guidance.bytes > 0
    assert sources.mind.bytes > 0

    initial_mind = vault.read_document("MIND.md")
    result = vault.project_guidance(
        real_checkout,
        expected_guidance_revision="absent",
        expected_mind_revision=initial_mind["revision"],
    )

    assert result["status"] == "projected"
    published_guidance = read_resident_guidance(vault.root)
    assert len(published_guidance["content"]) > 0
    assert published_guidance["sha256"] == result["guidance"]["revision"]

    published_mind = vault.read_document("MIND.md")
    assert len(published_mind["content"]) > 0
    assert published_mind["revision"] == result["mind"]["revision"]

    # Verify doctor passes on real checkout projection
    assert vault.doctor().healthy is True

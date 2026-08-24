from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from continuity_kernel.connector_local_tls import (
    CA_CERT_FILENAME,
    CA_KEY_FILENAME,
    LEAF_CERT_FILENAME,
    LEAF_KEY_FILENAME,
    connector_tls_dir,
    ensure_local_tls,
    generate_local_ca,
    install_ca_to_trust_store,
    is_ca_trusted,
    issue_leaf_certificate,
    load_local_tls,
)


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_cert_generation_produces_a_leaf_signed_by_the_ca(tmp_path: Path) -> None:
    ca_key = tmp_path / CA_KEY_FILENAME
    ca_crt = tmp_path / CA_CERT_FILENAME
    leaf_key = tmp_path / LEAF_KEY_FILENAME
    leaf_crt = tmp_path / LEAF_CERT_FILENAME

    generate_local_ca(ca_key, ca_crt)
    assert ca_key.is_file()
    assert ca_crt.is_file()
    if os.name != "nt":
        assert _file_mode(ca_key) == 0o600
        assert _file_mode(ca_crt) == 0o600

    constraints = subprocess.run(
        ["openssl", "x509", "-in", str(ca_crt), "-text", "-noout"],
        capture_output=True,
        check=False,
    )
    assert constraints.returncode == 0
    assert b"X509v3 Name Constraints: critical" in constraints.stdout
    assert b"DNS:localhost" in constraints.stdout
    assert b"127.0.0.1" in constraints.stdout
    assert b"0:0:0:0:0:0:0:1" in constraints.stdout

    issue_leaf_certificate(ca_key, ca_crt, leaf_key, leaf_crt)
    assert leaf_key.is_file()
    assert leaf_crt.is_file()
    if os.name != "nt":
        assert _file_mode(leaf_key) == 0o600
        assert _file_mode(leaf_crt) == 0o600

    # Verify signature using openssl CLI
    res = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ca_crt), str(leaf_crt)],
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0
    assert b"OK" in res.stdout


def test_ensure_local_tls_creates_material_once_with_proper_permissions(tmp_path: Path) -> None:
    state_dir = tmp_path / "connector-tls"
    material = ensure_local_tls(state_dir=state_dir, install_trust=False)

    assert material.directory == state_dir
    assert material.ca_key_path.is_file()
    assert material.ca_cert_path.is_file()
    assert material.leaf_key_path.is_file()
    assert material.leaf_cert_path.is_file()

    if os.name != "nt":
        assert _file_mode(state_dir) == 0o700
        assert _file_mode(material.ca_key_path) == 0o600
        assert _file_mode(material.ca_cert_path) == 0o600
        assert _file_mode(material.leaf_key_path) == 0o600
        assert _file_mode(material.leaf_cert_path) == 0o600

    # Record modification times
    ca_mtime = material.ca_cert_path.stat().st_mtime_ns
    leaf_mtime = material.leaf_cert_path.stat().st_mtime_ns

    # Call again, should reuse existing files without regenerating
    material2 = ensure_local_tls(state_dir=state_dir, install_trust=False)
    assert material2.ca_cert_path.stat().st_mtime_ns == ca_mtime
    assert material2.leaf_cert_path.stat().st_mtime_ns == leaf_mtime

    # Load existing material
    loaded = load_local_tls(state_dir=state_dir)
    assert loaded is not None
    assert loaded.ca_key_path == material.ca_key_path


def test_load_local_tls_returns_none_when_files_missing(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty-tls"
    empty_dir.mkdir()
    assert load_local_tls(state_dir=empty_dir) is None


def test_connector_tls_dir_resolves_default_or_explicit(tmp_path: Path) -> None:
    explicit = tmp_path / "custom-tls"
    assert connector_tls_dir(explicit) == explicit.resolve()

    default = connector_tls_dir()
    assert default.name == "connector-tls"


def test_install_ca_to_trust_store_surfaces_prompt_sentence(tmp_path: Path) -> None:
    ca_crt = tmp_path / CA_CERT_FILENAME
    ca_crt.write_bytes(b"dummy cert")

    prompt_messages: list[str] = []

    def mock_prompt(msg: str) -> None:
        prompt_messages.append(msg)

    with (
        patch("sys.platform", "darwin"),
        patch("shutil.which", return_value="/usr/bin/security"),
        patch("continuity_kernel.connector_local_tls.is_ca_trusted", return_value=True) as trusted,
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = install_ca_to_trust_store(ca_crt, prompt_callback=mock_prompt)
        assert result is True
        assert len(prompt_messages) == 1
        assert "administrator" in prompt_messages[0].lower()
        assert "macos" in prompt_messages[0].lower()
        mock_run.assert_called_once_with(
            [
                "/usr/bin/security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-p",
                "ssl",
                str(ca_crt),
            ],
            capture_output=True,
            check=False,
        )
        trusted.assert_called_once_with(ca_crt)


def test_is_ca_trusted_checks_macos_ssl_trust_policy(tmp_path: Path) -> None:
    ca_crt = tmp_path / CA_CERT_FILENAME
    ca_crt.write_bytes(b"diagnostic certificate")

    with (
        patch("sys.platform", "darwin"),
        patch("shutil.which", return_value="/usr/bin/security"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        assert is_ca_trusted(ca_crt) is False
        mock_run.assert_called_with(
            [
                "/usr/bin/security",
                "verify-cert",
                "-c",
                str(ca_crt),
                "-p",
                "ssl",
                "-L",
                "-l",
                "-q",
            ],
            capture_output=True,
            check=False,
        )

        mock_run.return_value = MagicMock(returncode=0)
        assert is_ca_trusted(ca_crt) is True

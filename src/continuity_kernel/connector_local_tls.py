"""Local TLS certificate authority and loopback leaf certificate management."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Final

from continuity_kernel.atomic import atomic_write, exclusive_lock
from continuity_kernel.config import data_dir
from continuity_kernel.errors import SetupError

CA_KEY_FILENAME: Final = "ca.key"
CA_CERT_FILENAME: Final = "ca.crt"
LEAF_KEY_FILENAME: Final = "localhost.key"
LEAF_CERT_FILENAME: Final = "localhost.crt"
LOCK_FILENAME: Final = "local-tls.lock"
DEFAULT_HOSTS: Final = ("localhost", "127.0.0.1", "::1")


def connector_tls_dir(state_dir: Path | str | None = None) -> Path:
    """Return host-local connector TLS directory."""
    if state_dir is not None:
        return Path(state_dir).expanduser().resolve()
    return (data_dir() / "connector-tls").resolve()


def _find_openssl() -> str:
    openssl_path = shutil.which("openssl")
    if openssl_path:
        return openssl_path
    if sys.platform == "darwin" and os.path.exists("/usr/bin/openssl"):
        return "/usr/bin/openssl"
    raise SetupError("openssl executable was not found; cannot generate local TLS certificates")


def generate_local_ca(
    ca_key_path: Path,
    ca_cert_path: Path,
    *,
    common_name: str = "Seld Local Connector CA",
    days: int = 3650,
) -> None:
    """Generate a self-signed local Certificate Authority."""
    openssl = _find_openssl()
    with tempfile.TemporaryDirectory() as temp_dir:
        ca_config_path = Path(temp_dir) / "ca.cnf"
        ca_config = f"""[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_ca
prompt = no

[req_distinguished_name]
CN = {common_name}

[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical, digitalSignature, cRLSign, keyCertSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
"""
        ca_config_path.write_text(ca_config, encoding="utf-8")
        temp_ca_key = Path(temp_dir) / "ca.key"
        temp_ca_crt = Path(temp_dir) / "ca.crt"

        cmd = [
            openssl,
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(temp_ca_key),
            "-sha256",
            "-days",
            str(days),
            "-out",
            str(temp_ca_crt),
            "-config",
            str(ca_config_path),
        ]
        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode != 0:
            error_message = res.stderr.decode("utf-8", errors="replace").strip()
            raise SetupError(f"Failed to generate local Certificate Authority: {error_message}")

        ca_key_bytes = temp_ca_key.read_bytes()
        ca_crt_bytes = temp_ca_crt.read_bytes()
        atomic_write(ca_key_path, ca_key_bytes, mode=0o600)
        atomic_write(ca_cert_path, ca_crt_bytes, mode=0o600)
        if os.name != "nt":
            ca_key_path.chmod(0o600)
            ca_cert_path.chmod(0o600)


def issue_leaf_certificate(
    ca_key_path: Path,
    ca_cert_path: Path,
    leaf_key_path: Path,
    leaf_cert_path: Path,
    *,
    hosts: tuple[str, ...] = DEFAULT_HOSTS,
    days: int = 825,
) -> None:
    """Issue a leaf TLS certificate signed by the local CA for loopback hosts."""
    openssl = _find_openssl()
    with tempfile.TemporaryDirectory() as temp_dir:
        dns_names: list[str] = []
        ip_addrs: list[str] = []
        for host in hosts:
            clean_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
            if ":" in clean_host or (
                clean_host.replace(".", "").isdigit() and clean_host.count(".") == 3
            ):
                ip_addrs.append(clean_host)
            else:
                dns_names.append(clean_host)

        alt_lines: list[str] = []
        for i, dns in enumerate(dns_names, start=1):
            alt_lines.append(f"DNS.{i} = {dns}")
        for i, ip in enumerate(ip_addrs, start=1):
            alt_lines.append(f"IP.{i} = {ip}")
        alt_names_section = "\n".join(alt_lines)

        leaf_config_path = Path(temp_dir) / "leaf.cnf"
        leaf_config = f"""[v3_req]
basicConstraints = critical,CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
{alt_names_section}
"""
        leaf_config_path.write_text(leaf_config, encoding="utf-8")
        temp_leaf_key = Path(temp_dir) / "leaf.key"
        temp_leaf_csr = Path(temp_dir) / "leaf.csr"
        temp_leaf_crt = Path(temp_dir) / "leaf.crt"

        req_cmd = [
            openssl,
            "req",
            "-new",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(temp_leaf_key),
            "-out",
            str(temp_leaf_csr),
            "-subj",
            "/CN=localhost",
        ]
        res = subprocess.run(req_cmd, capture_output=True, check=False)
        if res.returncode != 0:
            error_message = res.stderr.decode("utf-8", errors="replace").strip()
            raise SetupError(f"Failed to generate CSR for local TLS certificate: {error_message}")

        sign_cmd = [
            openssl,
            "x509",
            "-req",
            "-in",
            str(temp_leaf_csr),
            "-CA",
            str(ca_cert_path),
            "-CAkey",
            str(ca_key_path),
            "-CAcreateserial",
            "-out",
            str(temp_leaf_crt),
            "-days",
            str(days),
            "-sha256",
            "-extfile",
            str(leaf_config_path),
            "-extensions",
            "v3_req",
        ]
        res = subprocess.run(sign_cmd, capture_output=True, check=False)
        if res.returncode != 0:
            error_message = res.stderr.decode("utf-8", errors="replace").strip()
            raise SetupError(f"Failed to sign local TLS certificate with CA: {error_message}")

        leaf_key_bytes = temp_leaf_key.read_bytes()
        leaf_crt_bytes = temp_leaf_crt.read_bytes()
        atomic_write(leaf_key_path, leaf_key_bytes, mode=0o600)
        atomic_write(leaf_cert_path, leaf_crt_bytes, mode=0o600)
        if os.name != "nt":
            leaf_key_path.chmod(0o600)
            leaf_cert_path.chmod(0o600)


def is_ca_trusted(ca_cert_path: Path) -> bool:
    """Check if the CA certificate is already trusted in the macOS trust store."""
    if sys.platform != "darwin":
        return True
    security_bin = shutil.which("security")
    if not security_bin:
        return False
    try:
        certificate_der = ssl.PEM_cert_to_DER_cert(ca_cert_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return False

    expected_hash = sha1(certificate_der, usedforsecurity=False).hexdigest().upper()
    for keychain_path in (
        Path("/Library/Keychains/System.keychain"),
        Path.home() / "Library/Keychains/login.keychain-db",
    ):
        res = subprocess.run(
            [security_bin, "find-certificate", "-a", "-Z", str(keychain_path)],
            capture_output=True,
            check=False,
        )
        if res.returncode != 0:
            continue
        for line in res.stdout.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("SHA-1 hash:"):
                continue
            observed_hash = line.partition(":")[2].replace(":", "").strip().upper()
            if observed_hash == expected_hash:
                return True
    return False


def install_ca_to_trust_store(
    ca_cert_path: Path,
    *,
    prompt_callback: Callable[[str], None] | None = None,
) -> bool:
    """Install the CA into the macOS system trust store using security add-trusted-cert.

    Surfaces a single plain sentence before triggering the macOS admin prompt.
    Never requests, stores, or echoes passwords.
    """
    if sys.platform != "darwin":
        return False
    security_bin = shutil.which("security")
    if not security_bin:
        raise SetupError("macOS security tool not found")

    prompt_message = (
        "Seld needs administrator approval to install the local connector Certificate "
        "Authority into the macOS system trust store."
    )
    if prompt_callback is not None:
        prompt_callback(prompt_message)
    else:
        print(prompt_message, flush=True)

    cmd = [
        security_bin,
        "add-trusted-cert",
        "-d",
        "-r",
        "trustRoot",
        "-p",
        "ssl",
        str(ca_cert_path),
    ]
    res = subprocess.run(cmd, capture_output=True, check=False)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise SetupError(f"Failed to install CA certificate into macOS trust store: {err}")
    return True


@dataclass(frozen=True)
class LocalTLSMaterial:
    directory: Path
    ca_key_path: Path
    ca_cert_path: Path
    leaf_key_path: Path
    leaf_cert_path: Path

    def create_ssl_context(self) -> ssl.SSLContext:
        """Create a server-side SSL context using the issued leaf certificate."""
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(
            certfile=str(self.leaf_cert_path),
            keyfile=str(self.leaf_key_path),
        )
        return context

    def create_client_ssl_context(self) -> ssl.SSLContext:
        """Create a client-side SSL context trusting the local CA."""
        context = ssl.create_default_context(cafile=str(self.ca_cert_path))
        return context


def ensure_local_tls(
    *,
    state_dir: Path | str | None = None,
    install_trust: bool = True,
    prompt_callback: Callable[[str], None] | None = None,
) -> LocalTLSMaterial:
    """Generate CA and leaf certificate if needed, and optionally install into trust store."""
    tls_dir = connector_tls_dir(state_dir)
    tls_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        tls_dir.chmod(0o700)

    lock_dir = tls_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        lock_dir.chmod(0o700)

    ca_key_path = tls_dir / CA_KEY_FILENAME
    ca_cert_path = tls_dir / CA_CERT_FILENAME
    leaf_key_path = tls_dir / LEAF_KEY_FILENAME
    leaf_cert_path = tls_dir / LEAF_CERT_FILENAME

    with exclusive_lock(lock_dir / LOCK_FILENAME):
        ca_exists = ca_key_path.exists() and ca_cert_path.exists()
        if not ca_exists:
            generate_local_ca(ca_key_path, ca_cert_path)

        leaf_exists = leaf_key_path.exists() and leaf_cert_path.exists()
        if not leaf_exists:
            issue_leaf_certificate(ca_key_path, ca_cert_path, leaf_key_path, leaf_cert_path)

        if install_trust and sys.platform == "darwin" and not is_ca_trusted(ca_cert_path):
            install_ca_to_trust_store(ca_cert_path, prompt_callback=prompt_callback)

    return LocalTLSMaterial(
        directory=tls_dir,
        ca_key_path=ca_key_path,
        ca_cert_path=ca_cert_path,
        leaf_key_path=leaf_key_path,
        leaf_cert_path=leaf_cert_path,
    )


def load_local_tls(*, state_dir: Path | str | None = None) -> LocalTLSMaterial | None:
    """Load existing local TLS material if all files exist."""
    tls_dir = connector_tls_dir(state_dir)
    ca_key_path = tls_dir / CA_KEY_FILENAME
    ca_cert_path = tls_dir / CA_CERT_FILENAME
    leaf_key_path = tls_dir / LEAF_KEY_FILENAME
    leaf_cert_path = tls_dir / LEAF_CERT_FILENAME

    if (
        ca_key_path.is_file()
        and ca_cert_path.is_file()
        and leaf_key_path.is_file()
        and leaf_cert_path.is_file()
    ):
        return LocalTLSMaterial(
            directory=tls_dir,
            ca_key_path=ca_key_path,
            ca_cert_path=ca_cert_path,
            leaf_key_path=leaf_key_path,
            leaf_cert_path=leaf_cert_path,
        )
    return None

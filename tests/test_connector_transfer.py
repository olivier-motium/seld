from __future__ import annotations

import gc
import hashlib
import io
import os
import stat
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from continuity_kernel.connector_contract import canonical_json_digest
from continuity_kernel.connector_transfer import (
    ArtifactStore,
    PreparedUpload,
    PreparedUploadBinding,
    PreparedUploadBundle,
    PreparedUploadCache,
    TransferStore,
)
from continuity_kernel.connector_transport import (
    MAX_STREAM_BODY_BYTES,
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorTransport,
    ResponseLike,
)
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.local_files import MAX_FILE_TRANSFER_BYTES, LocalFileGrantStore
from continuity_kernel.vault import Vault


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> None:
        self.status = status
        header_values = Message()
        values = headers.items() if isinstance(headers, dict) else headers or []
        for name, value in values:
            header_values[name] = value
        self.headers: object = header_values
        self._body = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _BodyReader(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


class _Scheduled:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[_Scheduled] = []

    def __call__(self, _delay: float, callback: Callable[[], None]) -> _Scheduled:
        scheduled = _Scheduled(callback)
        self.calls.append(scheduled)
        return scheduled

    def fire(self) -> None:
        scheduled = next(call for call in reversed(self.calls) if not call.cancelled)
        scheduled.cancelled = True
        scheduled.callback()


def _grant_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Vault, LocalFileGrantStore]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Transfer proof")
    return vault, LocalFileGrantStore(
        vault_root=vault.root,
        vault_id=vault.identity()["vault_id"],
    )


def _credential() -> ConnectorCredential:
    return ConnectorCredential(AuthorizationScheme.BEARER, "secret-value")


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_file_ref_hashes_streams_and_rejects_swap_or_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    payload = b"x" * (2 * 1024 * 1024)
    file_path = selected / "nested.bin"
    file_path.write_bytes(payload)
    grant_id = store.create(selected)["grant"]["grant_id"]

    reference = store.resolve_file_ref(grant_id, "nested.bin")
    assert reference.size == len(payload)
    assert reference.mtime_ns == reference.modified_ns
    assert reference.sha256 == hashlib.sha256(payload).hexdigest()
    assert "nested.bin" not in repr(reference)
    assert "x" * 32 not in repr(reference)
    assert b"".join(reference.iter_chunks(chunk_size=64 * 1024)) == payload

    file_path.write_bytes(b"y" * len(payload))
    with pytest.raises(ValidationError, match=r"reference identity|content changed"):
        reference.revalidate()

    file_path.unlink()
    file_path.symlink_to(selected / "other.bin")
    with pytest.raises(ValidationError, match=r"regular file|changed|eligible"):
        list(reference.iter_chunks())

    file_path.unlink()
    file_path.write_bytes(payload)
    fresh = store.resolve_file_ref(grant_id, "nested.bin")
    store.assert_transfer_authorized(grant_id, "nested.bin")
    store.revoke(grant_id)
    with pytest.raises(NotFoundError):
        store.assert_transfer_authorized(grant_id, "nested.bin")
    with pytest.raises(NotFoundError):
        list(fresh.iter_chunks())


def test_transfer_handles_are_opaque_bound_single_use_and_ttl_expiring() -> None:
    now = [100.0]
    store = TransferStore(clock=lambda: now[0])
    location = "https://files.slack.com/upload/v1/provider-secret"
    handle = store.issue(
        {"location": location},
        binding={"connection": "con-1", "operation": "upload"},
        ttl_seconds=2,
    )
    assert location not in handle
    with pytest.raises(ConflictError, match="binding"):
        store.consume(handle, binding={"connection": "con-2", "operation": "upload"})
    assert store.consume(handle, binding={"connection": "con-1", "operation": "upload"}) == {
        "location": location
    }
    with pytest.raises(ConflictError, match=r"unavailable|consumed"):
        store.consume(handle, binding={"connection": "con-1", "operation": "upload"})

    expiring = store.issue("continuation", binding="read", ttl_seconds=1)
    now[0] += 1
    with pytest.raises(ConflictError, match=r"unavailable|expired"):
        store.consume(expiring, binding="read")

    mutable = {"location": "https://files.slack.com/original", "ranges": ["0-"]}
    frozen = store.issue(mutable, binding="immutable")
    mutable["location"] = "https://attacker.invalid/replaced"
    cast(list[str], mutable["ranges"]).append("999-")
    assert store.consume(frozen, binding="immutable") == {
        "location": "https://files.slack.com/original",
        "ranges": ["0-"],
    }


def test_artifact_store_is_private_atomic_bounded_and_cleans_up(tmp_path: Path) -> None:
    now = [100.0]
    artifacts = ArtifactStore(tmp_path / "artifacts", clock=lambda: now[0], max_bytes=32)
    assert stat.S_IMODE(os.lstat(artifacts.root).st_mode) == 0o700

    writer = artifacts.start("../report.txt", expected_size=3)
    assert list(artifacts.root.glob("*.part"))
    writer.write(b"abc")
    receipt = writer.finish()
    final = receipt.path
    assert receipt.size == 3
    assert receipt.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert final.read_bytes() == b"abc"
    assert stat.S_IMODE(os.lstat(final).st_mode) == 0o600
    assert not list(artifacts.root.glob("*.part"))
    assert "report.txt" not in repr(receipt)
    assert str(final) not in repr(receipt)
    assert receipt.to_dict() == {
        "artifact_id": receipt.artifact_id,
        "bytes": 3,
        "cleanup": "after_expiry_while_runtime_active_or_on_next_start",
        "expires_at": "1970-01-02T00:01:40+00:00",
        "filename": "report.txt",
        "media_type": None,
        "path": str(final),
        "sha256": hashlib.sha256(b"abc").hexdigest(),
        "storage": "owner_only_transient_host_cache",
        "transient": True,
    }

    second = artifacts.start("report.txt", expected_size=3)
    second.write(b"xyz")
    second_receipt = second.finish()
    assert second_receipt.path != receipt.path
    assert second_receipt.path.read_bytes() == b"xyz"
    assert receipt.path.read_bytes() == b"abc"

    failed = artifacts.start("failed.bin", expected_size=4)
    failed.write(b"bad")
    with pytest.raises(ValidationError, match="declared length"):
        failed.finish()
    assert not any(path.name.endswith("--failed.bin") for path in artifacts.root.iterdir())
    assert not list(artifacts.root.glob("*.part"))

    expiring = artifacts.start("expired.bin", ttl_seconds=1)
    now[0] += 1
    assert artifacts.cleanup() == 0
    expiring.write(b"still-live")
    expiring_receipt = expiring.finish()
    assert expiring_receipt.path.read_bytes() == b"still-live"
    artifacts.close()

    expiring_store = ArtifactStore(tmp_path / "expiring", clock=lambda: now[0], max_bytes=32)
    completed = expiring_store.start("completed.bin", expected_size=2, ttl_seconds=1)
    completed.write(b"ok")
    completed_receipt = completed.finish()
    expiring_store.close()
    assert completed_receipt.path.exists()
    now[0] += 1
    restarted = ArtifactStore(tmp_path / "expiring", clock=lambda: now[0], max_bytes=32)
    assert not completed_receipt.path.exists()
    restarted.close()


def test_artifact_store_timer_removes_completed_artifact_at_expiry(tmp_path: Path) -> None:
    now = [100.0]
    scheduler = _Scheduler()
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        clock=lambda: now[0],
        scheduler=scheduler,
    )
    writer = artifacts.start("automatic.bin", expected_size=2, ttl_seconds=1)
    writer.write(b"ok")
    receipt = writer.finish()
    assert receipt.path.exists()
    assert scheduler.calls

    now[0] = receipt.expires_at
    scheduler.fire()

    assert not receipt.path.exists()
    artifacts.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_abandoned_writer_is_reclaimed_without_killing_active_writer(tmp_path: Path) -> None:
    now = [100.0]
    artifacts = ArtifactStore(tmp_path / "artifacts", clock=lambda: now[0], max_bytes=32)
    abandoned = artifacts.start("abandoned.bin", ttl_seconds=1)
    abandoned.write(b"partial")
    assert list(artifacts.root.glob("*.part"))
    del abandoned
    gc.collect()
    assert not list(artifacts.root.glob("*.part"))

    active = artifacts.start("active.bin", expected_size=4, ttl_seconds=1)
    active.write(b"li")
    now[0] += 2
    assert artifacts.cleanup() == 0
    active.write(b"ve")
    receipt = active.finish()
    assert receipt.path.read_bytes() == b"live"
    artifacts.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_prepared_upload_is_anonymous_and_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, grants = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    original = selected / "payload.bin"
    original.write_bytes(b"original")
    grant_id = grants.create(selected)["grant"]["grant_id"]
    reference = grants.resolve_file_ref(grant_id, "payload.bin")
    artifacts = ArtifactStore(tmp_path / "artifacts")

    prepared = artifacts.prepare_upload(
        reference,
        media_type="application/octet-stream",
        max_bytes=32,
    )
    original.write_bytes(b"replaced")
    assert prepared.binding() == {
        "bytes": 8,
        "filename": "payload.bin",
        "media_type": "application/octet-stream",
        "sha256": hashlib.sha256(b"original").hexdigest(),
    }
    assert b"".join(prepared.iter_chunks(chunk_size=2)) == b"original"
    assert not list(artifacts.root.glob("*.stage"))
    assert "payload.bin" not in repr(prepared)
    prepared.close()
    with pytest.raises(ValidationError, match="closed"):
        list(prepared.iter_chunks())
    artifacts.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_prepared_upload_cache_keeps_mismatches_retryable_and_closes_owned_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    _, grants = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    original = selected / "payload.bin"
    original.write_bytes(b"original")
    grant_id = grants.create(selected)["grant"]["grant_id"]
    reference = grants.resolve_file_ref(grant_id, "payload.bin")
    artifacts = ArtifactStore(tmp_path / "artifacts")

    def bundle() -> tuple[PreparedUploadBundle, PreparedUpload]:
        upload = artifacts.prepare_upload(reference)
        marker = {"grant_id": grant_id, "relative_path": "payload.bin"}
        return (
            PreparedUploadBundle(
                (
                    PreparedUploadBinding(
                        path=("attachments", 0, "local_file"),
                        selector_digest=canonical_json_digest(marker),
                        grant_id=grant_id,
                        upload=upload,
                    ),
                )
            ),
            upload,
        )

    scheduler = _Scheduler()
    cache = PreparedUploadCache(clock=lambda: now[0], scheduler=scheduler)
    first, first_upload = bundle()
    token = "v1.reviewed.signature"
    cache.put(token, first, ttl_seconds=2)
    assert cache.peek("v1.other.signature") is None
    assert cache.peek(token) is first

    taken = cache.take(token, expected=first)
    assert taken is first
    assert cache.peek(token) is None
    assert b"".join(first_upload.iter_chunks()) == b"original"
    taken.close()
    with pytest.raises(ValidationError, match="closed"):
        list(first_upload.iter_chunks())

    timed, timed_upload = bundle()
    cache.put(token, timed, ttl_seconds=1)
    now[0] += 1
    scheduler.fire()
    with pytest.raises(ValidationError, match="closed"):
        list(timed_upload.iter_chunks())

    expiring, expiring_upload = bundle()
    cache.put(token, expiring, ttl_seconds=1)
    pending_expiry = scheduler.calls[-1]
    with cache.hold(token) as held:
        assert held is expiring
        now[0] += 1
        pending_expiry.callback()
        assert b"".join(expiring_upload.iter_chunks()) == b"original"
    assert cache.peek(token) is None
    with pytest.raises(ValidationError, match="closed"):
        list(expiring_upload.iter_chunks())

    outstanding, outstanding_upload = bundle()
    cache.put(token, outstanding)
    cache.close()
    with pytest.raises(ValidationError, match="closed"):
        list(outstanding_upload.iter_chunks())
    with pytest.raises(ValidationError, match="cache is closed"):
        cache.peek(token)
    artifacts.close()


def test_prepared_upload_cache_reserves_aggregate_bytes_before_snapshotting() -> None:
    cache = PreparedUploadCache(
        scheduler=_Scheduler(),
        max_snapshot_bytes=8,
    )
    reservation = cache.reserve(8)
    with pytest.raises(ConflictError, match="byte capacity"):
        cache.reserve(1)
    reservation.release()
    replacement = cache.reserve(8)
    replacement.release()
    cache.close()


def test_stream_transport_counts_bytes_generates_headers_and_cleans_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Request] = []

    def upload(request: Request, timeout: float) -> ResponseLike:
        del timeout
        captured.append(request)
        reader = cast(_BodyReader, request.data)
        body = bytearray()
        while True:
            block = reader.read(2)
            if not block:
                break
            body.extend(block)
        assert bytes(body) == b"abcdef"
        return _Response(b"", status=308, headers={"Range": "bytes=0-5"})

    transport = ConnectorTransport(opener=upload)
    uploaded = transport.request_stream(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.PUT,
        path="/upload/drive/v3/files/file",
        credential=_credential(),
        source=(chunk for chunk in (b"abc", b"def")),
        content_length=6,
        content_type="application/octet-stream",
        byte_offset=0,
        total_length=6,
    )
    assert uploaded.status == 308
    assert uploaded.bytes_sent == 6
    assert uploaded.next_offset == 6
    assert captured[0].get_header("Content-length") == "6"
    assert captured[0].get_header("Content-range") == "bytes 0-5/6"
    assert captured[0].get_header("X-upload-content-length") is None
    assert captured[0].get_header("Authorization") == "Bearer secret-value"

    def bad_upload(request: Request, timeout: float) -> ResponseLike:
        del timeout
        reader = cast(_BodyReader, request.data)
        while reader.read(2):
            pass
        return _Response(b"")

    with pytest.raises(ConnectorOutcomeUnknown, match="outcome is unknown"):
        ConnectorTransport(opener=bad_upload).request_stream(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            path="/upload/drive/v3/files/file",
            credential=_credential(),
            source=(chunk for chunk in (b"abc",)),
            content_length=4,
        )

    def unauthenticated_upload(request: Request, timeout: float) -> ResponseLike:
        del timeout
        assert request.get_header("Authorization") is None
        reader = cast(_BodyReader, request.data)
        assert reader.read() == b"ok"
        return _Response(b"ok")

    location_transport = ConnectorTransport(opener=unauthenticated_upload)
    location_transport.request_stream(
        origin=ConnectorOrigin.SLACK,
        method=ConnectorMethod.POST,
        location="https://files.slack.com/upload/v1/opaque",
        source=(chunk for chunk in (b"ok",)),
        content_length=2,
    )
    with pytest.raises(ValidationError, match="do not accept credentials"):
        location_transport.request_stream(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            location="https://files.slack.com/upload/v1/opaque",
            credential=_credential(),
            source=(chunk for chunk in (b"ok",)),
            content_length=2,
        )

    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    artifacts = ArtifactStore(tmp_path / "download-artifacts")

    def download(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        return _Response(b"abc", headers={"Content-Length": "3"})

    download_transport = ConnectorTransport(opener=download)
    writer = artifacts.start("download.bin", expected_size=3)
    result = download_transport.download_stream(
        origin=ConnectorOrigin.GOOGLE,
        path="/drive/v3/files/file",
        credential=_credential(),
        sink=writer,
        expected_length=3,
    )
    assert result.bytes_received == 3
    assert result.artifact is not None
    assert result.artifact.path.read_bytes() == b"abc"

    def short_download(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        return _Response(b"ab", headers={"Content-Length": "3"})

    failing_transport = ConnectorTransport(opener=short_download)
    failing = artifacts.start("short.bin", expected_size=3)
    with pytest.raises(ValidationError, match=r"Content-Length|byte count"):
        failing_transport.download_stream(
            origin=ConnectorOrigin.GOOGLE,
            path="/drive/v3/files/file",
            credential=_credential(),
            sink=failing,
            expected_length=3,
        )
    assert not any(path.name.endswith("--short.bin") for path in artifacts.root.iterdir())
    assert not list(artifacts.root.glob("*.part"))
    artifacts.close()


def test_stream_upload_handles_default_308_and_preserves_control_response() -> None:
    headers = Message()
    headers["Range"] = "bytes=0-2"

    def redirected(request: Request, timeout: float) -> ResponseLike:
        del timeout
        reader = cast(_BodyReader, request.data)
        assert reader.read() == b"abc"
        raise HTTPError(
            request.full_url,
            308,
            "Resume Incomplete",
            headers,
            io.BytesIO(b'{"next":"3"}'),
        )

    result = ConnectorTransport(opener=redirected).request_stream(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.PUT,
        path="/upload/drive/v3/files/file",
        credential=_credential(),
        source=(b"abc",),
        content_length=3,
    )
    assert result.status == 308
    assert result.next_offset == 3
    assert result.body == b'{"next":"3"}'
    assert result.json() == {"next": "3"}
    assert result.headers["range"] == "bytes=0-2"


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_prepared_upload_resumes_exact_snapshot_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, grants = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    original = selected / "payload.bin"
    original.write_bytes(b"abcdefgh")
    grant_id = grants.create(selected)["grant"]["grant_id"]
    reference = grants.resolve_file_ref(grant_id, "payload.bin")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    prepared = artifacts.prepare_upload(reference)

    def upload(request: Request, timeout: float) -> ResponseLike:
        del timeout
        assert request.get_header("Content-length") == "3"
        assert request.get_header("Content-range") == "bytes 3-5/8"
        reader = cast(_BodyReader, request.data)
        assert reader.read() == b"def"
        return _Response(b"", status=308, headers={"Range": "bytes=0-5"})

    result = ConnectorTransport(opener=upload).request_stream(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.PUT,
        path="/upload/drive/v3/files/file",
        credential=_credential(),
        source=prepared,
        content_length=3,
        byte_offset=3,
        total_length=8,
    )
    assert result.next_offset == 6
    prepared.close()
    artifacts.close()


@pytest.mark.parametrize("acknowledgement", [None, "bytes=0-1", "bytes=2-4"])
def test_resumable_upload_rejects_missing_or_mismatched_acknowledgement(
    acknowledgement: str | None,
) -> None:
    def upload(request: Request, timeout: float) -> ResponseLike:
        del timeout
        reader = cast(_BodyReader, request.data)
        assert reader.read() == b"abc"
        headers = {} if acknowledgement is None else {"Range": acknowledgement}
        return _Response(b"", status=308, headers=headers)

    with pytest.raises(ValidationError, match="acknowledgement"):
        ConnectorTransport(opener=upload).request_stream(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            path="/upload/drive/v3/files/file",
            credential=_credential(),
            source=(b"abc",),
            content_length=3,
        )


def test_resumed_upload_rejects_live_iterable_before_dispatch() -> None:
    called = False

    def upload(request: Request, timeout: float) -> ResponseLike:
        nonlocal called
        del request, timeout
        called = True
        return _Response(b"")

    with pytest.raises(ValidationError, match="immutable prepared upload"):
        ConnectorTransport(opener=upload).request_stream(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            path="/upload/drive/v3/files/file",
            credential=_credential(),
            source=(b"abc",),
            content_length=3,
            byte_offset=3,
            total_length=6,
        )
    assert called is False


def test_stream_upload_does_not_count_unread_source_as_transferred() -> None:
    def early_response(request: Request, timeout: float) -> ResponseLike:
        del timeout
        reader = cast(_BodyReader, request.data)
        assert reader.read(2) == b"ab"
        return _Response(b"ok")

    with pytest.raises(ConnectorOutcomeUnknown, match="outcome is unknown"):
        ConnectorTransport(opener=early_response).request_stream(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            path="/upload/drive/v3/files/file",
            credential=_credential(),
            source=(b"abcdef",),
            content_length=6,
        )


def test_download_redirect_drops_bearer_and_range_status_is_coherent(
    tmp_path: Path,
) -> None:
    calls: list[Request] = []

    def redirect_then_download(request: Request, timeout: float) -> ResponseLike:
        del timeout
        calls.append(request)
        if len(calls) == 1:
            assert request.get_header("Authorization") == "Bearer secret-value"
            headers = Message()
            headers["Location"] = "https://content.googleapis.com/download/signed"
            raise HTTPError(request.full_url, 302, "Found", headers, io.BytesIO())
        assert request.full_url == "https://content.googleapis.com/download/signed"
        assert request.get_header("Authorization") is None
        return _Response(b"abc", headers={"Content-Length": "3"})

    artifacts = ArtifactStore(tmp_path / "redirect-artifacts")
    writer = artifacts.start("redirect.bin", expected_size=3)
    result = ConnectorTransport(opener=redirect_then_download).download_stream(
        origin=ConnectorOrigin.GOOGLE,
        path="/drive/v3/files/file",
        query=(("alt", "media"),),
        credential=_credential(),
        sink=writer,
        expected_length=3,
    )
    assert len(calls) == 2
    assert result.artifact is not None
    assert result.artifact.path.read_bytes() == b"abc"

    def ignored_range(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        return _Response(b"abc", status=200, headers={"Content-Length": "3"})

    ranged = artifacts.start("range.bin", expected_size=3)
    with pytest.raises(ValidationError, match="ignored the requested"):
        ConnectorTransport(opener=ignored_range).download_stream(
            origin=ConnectorOrigin.GOOGLE,
            path="/drive/v3/files/file",
            query=(("alt", "media"),),
            credential=_credential(),
            sink=ranged,
            range_start=0,
            range_end=2,
        )
    artifacts.close()


def test_stream_policies_reject_wrong_credentials_and_duplicate_safety_headers(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path / "policy-artifacts")
    wrong_scheme = ConnectorCredential(AuthorizationScheme.BEARER, "wrong-for-discord")
    with pytest.raises(ValidationError, match="Discord connectors require bot"):
        ConnectorTransport(opener=lambda request, timeout: _Response(b"x")).download_stream(
            origin=ConnectorOrigin.DISCORD,
            path="/api/v10/channels/channel/messages/message",
            credential=wrong_scheme,
            sink=artifacts.start("wrong.bin"),
        )

    def duplicate_headers(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        return _Response(
            b"abc",
            headers=[("Content-Length", "3"), ("Content-Length", "4")],
        )

    with pytest.raises(ValidationError, match="duplicate safety header"):
        ConnectorTransport(opener=duplicate_headers).download_stream(
            origin=ConnectorOrigin.GOOGLE,
            path="/drive/v3/files/file",
            credential=_credential(),
            sink=artifacts.start("duplicate.bin"),
        )
    artifacts.close()


def test_stream_lane_matches_largest_supported_provider_without_raising_json_bounds() -> None:
    assert MAX_FILE_TRANSFER_BYTES == 5 * 1024**4
    assert MAX_STREAM_BODY_BYTES == MAX_FILE_TRANSFER_BYTES

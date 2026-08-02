from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from continuity_kernel.connector_gmail_transfer import (
    GMAIL_INLINE_MAX_BYTES,
    GMAIL_UPLOAD_MAX_BYTES,
    GmailMessagePartBodyDecoder,
    GmailMimeAttachment,
    GmailMimeUpload,
)
from continuity_kernel.connector_transfer import ArtifactReceipt, PreparedUpload
from continuity_kernel.errors import ValidationError


@dataclass
class _RecordingWriter:
    content: bytearray = field(default_factory=bytearray)
    finished: bool = False
    aborted: bool = False

    def write(self, content: bytes) -> int:
        self.content.extend(content)
        return len(content)

    def finish(self) -> ArtifactReceipt:
        self.finished = True
        return ArtifactReceipt(
            "artifact-id",
            "gmail-attachment.bin",
            "application/octet-stream",
            len(self.content),
            hashlib.sha256(self.content).hexdigest(),
            1.0,
            Path("artifact.bin"),
        )

    def abort(self) -> None:
        self.aborted = True


def _message_part_body(content: bytes, *, padded: bool = False) -> bytes:
    encoded = base64.urlsafe_b64encode(content).decode("ascii")
    if not padded:
        encoded = encoded.rstrip("=")
    return json.dumps(
        {"size": len(content), "attachmentId": "provider-id", "data": encoded}
    ).encode("utf-8")


def _raw_message(content: bytes) -> bytes:
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    return json.dumps({"raw": encoded}).encode("utf-8")


def test_message_part_body_decodes_chunk_split_base64url_into_a_stream() -> None:
    content = bytes(range(256)) * 64
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer)
    body = _message_part_body(content)
    for offset in range(0, len(body), 5):
        decoder.write(body[offset : offset + 5])
    receipt = decoder.finish()

    assert receipt is not None
    assert bytes(writer.content) == content
    assert decoder.decoded_size == len(content)
    assert decoder.declared_size == len(content)
    assert writer.finished is True
    assert writer.aborted is False


def test_message_part_body_accepts_padded_data_and_bounded_inline_content() -> None:
    content = b"\xfb\xff\x00"
    decoder = GmailMessagePartBodyDecoder()
    body = _message_part_body(content, padded=True)
    decoder.write(body[:7])
    decoder.write(body[7:])
    decoder.finish()

    assert decoder.inline_content == content


def test_raw_message_decodes_chunk_split_base64url_into_an_artifact() -> None:
    content = bytes(range(256)) * 64
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer, encoded_field="raw")
    body = _raw_message(content)
    for offset in range(0, len(body), 5):
        decoder.write(body[offset : offset + 5])
    receipt = decoder.finish()

    assert receipt is not None
    assert bytes(writer.content) == content
    assert decoder.decoded_size == len(content)
    assert decoder.declared_size is None
    assert writer.finished is True
    assert writer.aborted is False


@pytest.mark.parametrize(
    "body",
    (
        b'{"raw":"YQ=="',
        b'{"raw":"bad*"}',
        b'{"raw":"YQ==","size":1}',
        b'{"raw":"YQ==","sizeEstimate":1}',
        b'{"raw":"YQ==","raw":"Yg=="}',
    ),
)
def test_raw_message_rejects_malformed_truncated_or_unknown_fields_without_a_receipt(
    body: bytes,
) -> None:
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer, encoded_field="raw")
    with pytest.raises(ValidationError, match="Gmail raw-message"):
        decoder.write(body)
        decoder.finish()

    assert writer.aborted is True
    assert writer.finished is False


def test_message_part_body_aborts_after_partial_artifact_output() -> None:
    content = b"x" * 5_000
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer)
    with pytest.raises(ValidationError):
        decoder.write(b'{"data":"' + encoded.encode("ascii") + b'!","size":5000}')
    assert writer.content
    assert writer.aborted is True
    assert writer.finished is False


def test_message_part_body_keeps_exact_declared_size_validation() -> None:
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer)
    decoder.write(b'{"data":"YQ==","size":2}')

    with pytest.raises(ValidationError, match="size does not match decoded data"):
        decoder.finish()

    assert bytes(writer.content) == b"a"
    assert writer.aborted is True
    assert writer.finished is False


@pytest.mark.parametrize(
    "body",
    (
        b'{"data":"YQ==X","size":1}',
        b'{"data":"bad*","size":3}',
        b'{"data":"YQ==","data":"Yg=="}',
        b'{"data":"YQ==","size":2}',
        b'{"data":"YQ=="',
        b'{"data":"YQ=="} trailing',
    ),
)
def test_message_part_body_rejects_malformed_or_truncated_json_and_aborts(
    body: bytes,
) -> None:
    writer = _RecordingWriter()
    decoder = GmailMessagePartBodyDecoder(writer=writer)
    with pytest.raises(ValidationError):
        decoder.write(body)
        decoder.finish()
    assert writer.aborted is True
    assert writer.finished is False


def test_inline_message_part_body_rejects_content_over_the_small_compatibility_bound() -> None:
    content = b"x" * (GMAIL_INLINE_MAX_BYTES + 1)
    decoder = GmailMessagePartBodyDecoder()
    with pytest.raises(ValidationError, match="bounded compatibility"):
        decoder.write(_message_part_body(content))
    assert decoder.decoded_size > GMAIL_INLINE_MAX_BYTES


def _prepared_upload(tmp_path: Path) -> PreparedUpload:
    content = b"prepared attachment content"
    path = tmp_path / "source.bin"
    path.write_bytes(content)
    return PreparedUpload(
        filename="source.bin",
        media_type="application/octet-stream",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=os.open(path, os.O_RDONLY),
    )


def test_rfc2822_upload_is_reiterable_and_finishes_after_the_final_payload(
    tmp_path: Path,
) -> None:
    prepared = _prepared_upload(tmp_path)
    try:
        upload = GmailMimeUpload(
            headers=(("Subject", "Bounded draft"),),
            body=b'Content-Type: text/plain; charset="utf-8"\r\n\r\nBody\r\n',
            attachments=(
                GmailMimeAttachment(
                    filename="inline.txt",
                    mime_type="text/plain",
                    inline_content=b"inline content",
                ),
                GmailMimeAttachment(
                    filename="prepared.bin",
                    mime_type="application/octet-stream",
                    upload=prepared,
                ),
            ),
        )
        first = b"".join(upload)
        second = b"".join(upload)
        assert first == second
        assert len(first) == upload.size
        assert first.rstrip().endswith(b"--")
        parsed = BytesParser(policy=policy.default).parsebytes(first)
        boundary = parsed.get_boundary()
        assert boundary is not None
        assert len(boundary) == 62
        assert len(boundary) <= 70
    finally:
        prepared.close()


def test_rfc2822_upload_applies_the_documented_total_media_limit(tmp_path: Path) -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)
    oversized = PreparedUpload(
        filename="oversized.bin",
        media_type="application/octet-stream",
        size=GMAIL_UPLOAD_MAX_BYTES,
        sha256="0" * 64,
        descriptor=descriptor,
    )
    with pytest.raises(ValidationError, match="documented provider upload limit"):
        GmailMimeUpload(
            headers=(),
            body=b"Content-Type: text/plain\r\n\r\nBody\r\n",
            attachments=(
                GmailMimeAttachment(
                    filename="oversized.bin",
                    mime_type="application/octet-stream",
                    upload=oversized,
                ),
            ),
        )
    oversized.close()

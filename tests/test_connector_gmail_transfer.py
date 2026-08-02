from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.header import Header
from email.parser import BytesParser
from pathlib import Path

import pytest

from continuity_kernel.connector_gmail_transfer import (
    GMAIL_INLINE_MAX_BYTES,
    GMAIL_MIGRATION_UPLOAD_MAX_BYTES,
    GMAIL_UPLOAD_MAX_BYTES,
    GmailMessagePartBodyDecoder,
    GmailMimeAttachment,
    GmailMimeUpload,
    gmail_raw_message_preview,
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


@contextmanager
def _prepared_raw_message(tmp_path: Path, content: bytes) -> Iterator[PreparedUpload]:
    path = tmp_path / "message.eml"
    path.write_bytes(content)
    upload = PreparedUpload(
        filename="message.eml",
        media_type="message/rfc822",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=os.open(path, os.O_RDONLY),
    )
    try:
        yield upload
    finally:
        upload.close()


def _folded_oversized_address_header(name: str, address: str) -> bytes:
    value = Header(
        f"{'safe ' * 250}<{address}>",
        charset="us-ascii",
        header_name=name,
        maxlinelen=76,
    ).encode(linesep="\r\n")
    return f"{name}: {value}".encode("ascii")


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


def test_raw_message_preview_handles_a_chunk_split_header_delimiter(tmp_path: Path) -> None:
    prefix = b"To: owner@example.test\r\n" + b"".join(
        f"X-Pad-{index}: ".encode("ascii") + b"x" * 900 + b"\r\n" for index in range(4)
    )
    tail_prefix = b"X-Tail: "
    tail_size = 4_094 - len(prefix) - len(tail_prefix)
    assert 0 < tail_size < 990
    headers = prefix + tail_prefix + b"x" * tail_size
    assert len(headers) == 4_094
    content = headers + b"\r\n\r\nBody"

    with _prepared_raw_message(tmp_path, content) as upload:
        preview = gmail_raw_message_preview(upload, strict_send=True, require_date=False)

    assert preview["headers_parsed"] is True
    assert preview["to"] == ["owner@example.test"]


def test_raw_message_preview_exposes_every_repeated_bcc_without_repr_leak(
    tmp_path: Path,
) -> None:
    content = (
        b"Bcc: Hidden One <hidden-one@example.test>\r\n"
        b"Bcc: Hidden Team: Hidden Two <hidden-two@example.test>, "
        b"Hidden Three <hidden-three@example.test>;\r\n"
        b"Subject: Review routing\r\n\r\nBody"
    )

    with _prepared_raw_message(tmp_path, content) as upload:
        preview = gmail_raw_message_preview(upload, strict_send=True, require_date=False)

    assert preview["to"] == []
    assert preview["cc"] == []
    assert preview["bcc"] == [
        "Hidden One <hidden-one@example.test>",
        "Hidden Two <hidden-two@example.test>",
        "Hidden Three <hidden-three@example.test>",
    ]
    assert "hidden-one" not in repr(preview)
    assert repr(preview) == "GmailRawMessagePreview(<redacted>)"


def test_raw_message_preview_decodes_groups_singletons_and_date(tmp_path: Path) -> None:
    subject = base64.b64encode("Résumé plan".encode()).decode("ascii")
    content = (
        b"From: =?utf-8?q?Jos=C3=A9?= <jose@example.test>\r\n"
        b"Sender: Operations <ops@example.test>\r\n"
        b"Reply-To: Replies <reply@example.test>\r\n"
        b"To: Launch Team: Alice <alice@example.test>, "
        b"=?utf-8?q?B=C3=A9atrice?= <bea@example.test>;\r\n"
        b"Cc: Carol <carol@example.test>\r\n"
        + f"Subject: =?utf-8?b?{subject}?=\r\n".encode("ascii")
        + b"Date: Sun, 02 Aug 2026 10:00:00 +0200\r\n"
        b"Message-ID: <message-1@example.test>\r\n\r\nBody"
    )

    with _prepared_raw_message(tmp_path, content) as upload:
        preview = gmail_raw_message_preview(upload, strict_send=True, require_date=False)

    assert preview == {
        "bcc": [],
        "cc": ["Carol <carol@example.test>"],
        "date": "Sun, 02 Aug 2026 10:00:00 +0200",
        "from": "José <jose@example.test>",
        "headers_parsed": True,
        "message_id": "<message-1@example.test>",
        "parsed_date": "2026-08-02T10:00:00+02:00",
        "reply_to": "Replies <reply@example.test>",
        "sender": "Operations <ops@example.test>",
        "subject": "Résumé plan",
        "to": ["Alice <alice@example.test>", "Béatrice <bea@example.test>"],
        "warnings": [],
    }


def test_raw_send_never_lets_a_long_display_name_hide_the_recipient_mailbox(
    tmp_path: Path,
) -> None:
    content = (
        _folded_oversized_address_header("To", "recipient@example.test")
        + b"\r\nSubject: Review exact recipient\r\n\r\nBody"
    )

    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="displayed recipient address exceeds"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert migration["headers_parsed"] is False
    assert migration["to"] == []


def test_raw_send_rejects_resent_recipient_routing_while_migration_remains_readable(
    tmp_path: Path,
) -> None:
    content = (
        b"To: recipient@example.test\r\n"
        b"Resent-Bcc: hidden@example.test\r\n"
        b"Subject: Ambiguous routing\r\n\r\nBody"
    )
    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="Resent recipient headers"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert migration["headers_parsed"] is True
    assert migration["to"] == ["recipient@example.test"]


@pytest.mark.parametrize("name", ("From", "Sender", "Reply-To"))
def test_raw_preview_bounds_every_displayed_sender_address(
    tmp_path: Path,
    name: str,
) -> None:
    content = (
        _folded_oversized_address_header(name, "sender@example.test")
        + b"\r\nTo: recipient@example.test\r\n\r\nBody"
    )

    with (
        _prepared_raw_message(tmp_path, content) as upload,
        pytest.raises(ValidationError, match="displayed address header exceeds"),
    ):
        gmail_raw_message_preview(upload, strict_send=True, require_date=False)


def test_date_header_mode_reports_non_date_header_defects_accurately(tmp_path: Path) -> None:
    content = (
        b"Date: Sun, 02 Aug 2026 10:00:00 +0200\r\n"
        + _folded_oversized_address_header("To", "recipient@example.test")
        + b"\r\n\r\nBody"
    )

    with (
        _prepared_raw_message(tmp_path, content) as upload,
        pytest.raises(
            ValidationError,
            match="headers could not be validated for dateHeader",
        ),
    ):
        gmail_raw_message_preview(upload, strict_send=False, require_date=True)


@pytest.mark.parametrize(
    "headers",
    (
        b"To: owner@example.test\r\nSubject: injected\x00value",
        b"To: owner@example.test\nBcc: hidden@example.test",
        b"To: owner@example.test\rSubject: forged",
        b"To: owner@example.test\r\nSubject: one\r\nSubject: two",
        b"From: sender@example.test\r\nSubject: no recipient",
        b"Bcc: undisclosed-recipients:;\r\nSubject: hidden recipient",
    ),
)
def test_raw_send_rejects_control_injection_duplicates_and_ambiguous_routing(
    tmp_path: Path,
    headers: bytes,
) -> None:
    with (
        _prepared_raw_message(tmp_path, headers + b"\r\n\r\nBody") as upload,
        pytest.raises(ValidationError, match="Gmail raw message headers are invalid"),
    ):
        gmail_raw_message_preview(upload, strict_send=True, require_date=False)


def test_received_time_migration_keeps_malformed_legacy_mail_opaque(tmp_path: Path) -> None:
    content = b"Legacy-Header without a colon\n\nLegacy body"
    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="headers are invalid"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        preview = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert preview["headers_parsed"] is False
    assert preview["to"] == []
    assert preview["subject"] is None
    assert preview["warnings"] == [
        "Message headers could not be safely parsed; the exact prepared message will be "
        "processed opaquely."
    ]


def test_raw_utf8_subject_is_visible_for_send_and_migration(
    tmp_path: Path,
) -> None:
    content = "To: recipient@example.test\r\nSubject: Café\r\n\r\nBody".encode()
    with _prepared_raw_message(tmp_path, content) as upload:
        strict = gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert strict["to"] == ["recipient@example.test"]
    assert strict["subject"] == "Café"
    assert migration["headers_parsed"] is True
    assert migration["subject"] == "Café"


def test_raw_utf8_address_tokens_fail_closed_for_send_and_stay_opaque_for_migration(
    tmp_path: Path,
) -> None:
    content = "To: Béatrice <recipient@example.test>\r\nSubject: Café\r\n\r\nBody".encode()
    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="headers are invalid"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert migration["headers_parsed"] is False
    assert migration["to"] == []


def test_invalid_utf8_header_bytes_fail_closed_for_send_and_stay_opaque_for_migration(
    tmp_path: Path,
) -> None:
    content = b"To: recipient@example.test\r\nSubject: \xff\r\n\r\nBody"
    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="invalid UTF-8 bytes"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert migration["headers_parsed"] is False
    assert migration["subject"] is None


@pytest.mark.parametrize("control", ("\u0085", "\u202e"))
def test_raw_visual_control_characters_fail_closed_for_send_and_stay_opaque_for_migration(
    tmp_path: Path,
    control: str,
) -> None:
    content = (f"To: recipient@example.test\r\nSubject: before{control}after\r\n\r\nBody").encode()
    with _prepared_raw_message(tmp_path, content) as upload:
        with pytest.raises(ValidationError, match="decoded header contains a control character"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        migration = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert migration["headers_parsed"] is False
    assert migration["subject"] is None


@pytest.mark.parametrize(
    "headers",
    (
        b"From: sender@example.test",
        b"Date: invalid date",
        b"Date: Sun, 02 Aug 2026 10:00:00 +0200\r\nDate: Sun, 02 Aug 2026 11:00:00 +0200",
    ),
)
def test_date_header_migration_requires_exactly_one_valid_date(
    tmp_path: Path,
    headers: bytes,
) -> None:
    with (
        _prepared_raw_message(tmp_path, headers + b"\r\n\r\nBody") as upload,
        pytest.raises(ValidationError, match="use internal_date_source=receivedTime"),
    ):
        gmail_raw_message_preview(upload, strict_send=False, require_date=True)


def test_date_header_migration_returns_the_parsed_effective_date(tmp_path: Path) -> None:
    content = b"Date: Sun, 02 Aug 2026 10:00:00 +0200\r\n\r\nBody"
    with _prepared_raw_message(tmp_path, content) as upload:
        preview = gmail_raw_message_preview(upload, strict_send=False, require_date=True)

    assert preview["date"] == "Sun, 02 Aug 2026 10:00:00 +0200"
    assert preview["parsed_date"] == "2026-08-02T10:00:00+02:00"


@pytest.mark.parametrize(
    "headers",
    (
        b"To: owner@example.test\r\nX-Long: " + b"x" * 991,
        b"To: owner@example.test\r\n" + b"\r\n".join(b"X: x" for _ in range(512)),
        b"To: owner@example.test\r\n"
        + b"\r\n".join(f"X-Pad-{index}: ".encode("ascii") + b"x" * 900 for index in range(300)),
    ),
)
def test_raw_message_preview_enforces_line_field_and_total_header_bounds(
    tmp_path: Path,
    headers: bytes,
) -> None:
    with _prepared_raw_message(tmp_path, headers + b"\r\n\r\nBody") as upload:
        with pytest.raises(ValidationError, match="headers are invalid"):
            gmail_raw_message_preview(upload, strict_send=True, require_date=False)
        opaque = gmail_raw_message_preview(upload, strict_send=False, require_date=False)

    assert opaque["headers_parsed"] is False


def test_raw_message_preview_reads_one_bounded_chunk_and_never_reopens_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = b"To: owner@example.test\r\nSubject: bounded"
    content = headers + b"\r\n\r\n" + b"\x00" * (1024 * 1024)
    path = tmp_path / "unlinked-message.eml"
    path.write_bytes(content)
    upload = PreparedUpload(
        filename="unlinked-message.eml",
        media_type="message/rfc822",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=os.open(path, os.O_RDONLY),
    )
    path.unlink()
    original_iter_chunks = upload.iter_chunks
    observed_bytes = 0

    def tracked_iter_chunks(
        *,
        offset: int = 0,
        length: int | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        nonlocal observed_bytes
        for chunk in original_iter_chunks(offset=offset, length=length, chunk_size=chunk_size):
            observed_bytes += len(chunk)
            yield chunk

    monkeypatch.setattr(upload, "iter_chunks", tracked_iter_chunks)
    try:
        preview = gmail_raw_message_preview(upload, strict_send=True, require_date=False)
    finally:
        upload.close()

    assert preview["subject"] == "bounded"
    assert observed_bytes == 4 * 1024
    assert observed_bytes < len(content)


def test_gmail_migration_upload_limit_matches_the_provider_contract() -> None:
    assert GMAIL_MIGRATION_UPLOAD_MAX_BYTES == 157_286_400


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

"""Streaming Gmail MIME uploads and MessagePartBody downloads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Final, Protocol

from continuity_kernel.connector_transfer import (
    MAX_ARTIFACT_BYTES,
    ArtifactReceipt,
    PreparedUpload,
)
from continuity_kernel.errors import ValidationError

GMAIL_UPLOAD_MAX_BYTES: Final = 36_700_160
GMAIL_INLINE_MAX_BYTES: Final = 180_000

_BASE64URL: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_JSON_WHITESPACE: Final = frozenset({9, 10, 13, 32})
_MAX_SMALL_STRING_BYTES: Final = 16 * 1024
_BASE64_DECODE_BLOCK_BYTES: Final = 4 * 1024
_MIME_BOUNDARY_PREFIX: Final = "_seld_gmail_"
_MIME_BOUNDARY_DIGEST_HEX_LENGTH: Final = 48
_MIME_BOUNDARY_MAX_LENGTH: Final = 70


class _ArtifactWriter(Protocol):
    def write(self, content: bytes) -> int: ...

    def finish(self) -> ArtifactReceipt: ...

    def abort(self) -> None: ...


@dataclass(frozen=True)
class GmailMimeAttachment:
    """One attachment body, either bounded inline bytes or an immutable upload."""

    filename: str
    mime_type: str
    inline_content: bytes | None = None
    upload: PreparedUpload | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.filename, str)
            or not self.filename
            or len(self.filename) > 512
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.filename)
        ):
            raise ValidationError("Gmail attachment filename is invalid")
        if (
            not isinstance(self.mime_type, str)
            or not self.mime_type
            or len(self.mime_type) > 256
            or self.mime_type.count("/") != 1
            or any(
                not character.isprintable() or character.isspace() for character in self.mime_type
            )
            or any(not part for part in self.mime_type.split("/", 1))
        ):
            raise ValidationError("Gmail attachment MIME type is invalid")
        if (self.inline_content is None) == (self.upload is None):
            raise ValidationError("Gmail attachment requires exactly one binary source")
        if self.inline_content is not None and not isinstance(self.inline_content, bytes):
            raise ValidationError("Gmail inline attachment content is invalid")
        if self.upload is not None and not isinstance(self.upload, PreparedUpload):
            raise ValidationError("Gmail prepared attachment upload is invalid")

    @property
    def size(self) -> int:
        if self.inline_content is not None:
            return len(self.inline_content)
        assert self.upload is not None
        return self.upload.size


@dataclass(frozen=True)
class _MimePart:
    headers: bytes
    attachment: GmailMimeAttachment


class GmailMimeUpload:
    """Re-iterable RFC 2822 media body that never materializes prepared files."""

    def __init__(
        self,
        *,
        headers: Sequence[tuple[str, str]],
        body: bytes,
        attachments: Sequence[GmailMimeAttachment],
    ) -> None:
        if not isinstance(body, bytes):
            raise ValidationError("Gmail MIME body is invalid")
        if len(attachments) > 16:
            raise ValidationError("Gmail attachment count exceeds its bound")
        if any(not isinstance(item, GmailMimeAttachment) for item in attachments):
            raise ValidationError("Gmail MIME attachment is invalid")
        for name, value in headers:
            if (
                not isinstance(name, str)
                or not name
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
                or not isinstance(value, str)
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
                or "\r" in value
                or "\n" in value
            ):
                raise ValidationError("Gmail MIME header is invalid")

        boundary = _mime_boundary(headers, body, attachments)
        root = EmailMessage(policy=SMTP)
        for name, value in headers:
            root[name] = value
        root["MIME-Version"] = "1.0"
        # Serialize a non-multipart placeholder so EmailMessage does not emit
        # an empty closing boundary before the streaming payload begins.
        root["Content-Type"] = "application/octet-stream"
        root_bytes = root.as_bytes(policy=SMTP)
        placeholder = b"Content-Type: application/octet-stream\r\n"
        replacement = f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n'.encode("ascii")
        if placeholder not in root_bytes:
            raise ValidationError("Gmail MIME headers are malformed")
        root_bytes = root_bytes.replace(placeholder, replacement, 1)
        if root_bytes.endswith(b"\r\n") and not root_bytes.endswith(b"\r\n\r\n"):
            root_bytes += b"\r\n"
        if not root_bytes.endswith(b"\r\n\r\n"):
            raise ValidationError("Gmail MIME headers are malformed")

        parts = tuple(
            _MimePart(headers=_attachment_headers(item), attachment=item) for item in attachments
        )
        boundary_bytes = boundary.encode("ascii")
        prefix = root_bytes + b"--" + boundary_bytes + b"\r\n" + body
        delimiter = b"--" + boundary_bytes
        if not parts:
            prefix += delimiter + b"--\r\n"
            size = len(prefix)
        else:
            part_sizes = tuple(
                len(delimiter) + 2 + len(part.headers) + _base64_lines_length(part.attachment.size)
                for part in parts
            )
            size = len(prefix) + sum(part_sizes) + len(delimiter) + 4
        self._prefix = prefix
        self._boundary = boundary_bytes
        self._parts = parts
        self.size = size
        if self.size > GMAIL_UPLOAD_MAX_BYTES:
            raise ValidationError(
                "Gmail draft MIME upload exceeds the documented provider upload limit"
            )

    def __repr__(self) -> str:
        return f"GmailMimeUpload(size={self.size!r}, attachments=<{len(self._parts)} redacted>)"

    def __iter__(self) -> Iterator[bytes]:
        yield self._prefix
        if not self._parts:
            return
        delimiter = b"--" + self._boundary
        for part in self._parts:
            yield delimiter + b"\r\n"
            yield part.headers
            attachment = part.attachment
            if attachment.inline_content is not None:
                chunks: Iterable[bytes] = (attachment.inline_content,)
            else:
                assert attachment.upload is not None
                chunks = attachment.upload.iter_chunks()
            yield from _iter_base64_lines(chunks)
        yield delimiter + b"--\r\n"


class GmailMessagePartBodyDecoder:
    """Incrementally parse one Gmail MessagePartBody and decode its data field."""

    def __init__(
        self,
        *,
        writer: _ArtifactWriter | None = None,
    ) -> None:
        self._writer = writer
        self._state = "start"
        self._closed = False
        self._finished = False
        self._string_token = bytearray()
        self._string_kind: str | None = None
        self._string_escape = False
        self._pending_key: str | None = None
        self._seen: set[str] = set()
        self._size_digits = bytearray()
        self._declared_size: int | None = None
        self._saw_data = False
        self._base64_pending = ""
        self._base64_encoded = bytearray()
        self._base64_padding_complete = False
        self._decoded_size = 0
        self._inline_content = bytearray()

    @property
    def decoded_size(self) -> int:
        return self._decoded_size

    @property
    def declared_size(self) -> int | None:
        return self._declared_size

    @property
    def inline_content(self) -> bytes:
        if self._writer is not None:
            raise ValidationError("Gmail inline content is unavailable for artifact delivery")
        if not self._finished:
            raise ValidationError("Gmail attachment content is not complete")
        return bytes(self._inline_content)

    def write(self, content: bytes) -> int:
        if self._closed:
            raise ValidationError("Gmail attachment decoder is closed")
        if not isinstance(content, bytes):
            raise ValidationError("Gmail attachment response returned a non-binary chunk")
        try:
            for byte in content:
                self._consume(byte)
        except Exception as exc:
            self.abort()
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("Gmail attachment response is malformed") from exc
        return len(content)

    def finish(self) -> ArtifactReceipt | None:
        if self._closed:
            raise ValidationError("Gmail attachment decoder is closed")
        try:
            if self._state != "done" or not self._saw_data:
                raise ValidationError("Gmail attachment response is truncated")
            if self._declared_size is not None and self._declared_size != self._decoded_size:
                raise ValidationError("Gmail attachment size does not match decoded data")
            if self._writer is not None:
                receipt = self._writer.finish()
                if not isinstance(receipt, ArtifactReceipt):
                    raise ValidationError("Gmail attachment artifact receipt is invalid")
            else:
                receipt = None
            self._closed = True
            self._finished = True
            return receipt
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._writer.abort()

    def _consume(self, byte: int) -> None:
        if self._state in {
            "start",
            "key_or_end",
            "after_key",
            "value",
            "after_value",
            "size_end",
            "done",
        }:
            self._consume_structure(byte)
            return
        if self._state in {"key", "attachment_id"}:
            self._consume_small_string(byte)
            return
        if self._state == "data":
            self._consume_data(byte)
            return
        if self._state == "size":
            self._consume_size(byte)
            return
        raise ValidationError("Gmail attachment response is malformed")

    def _consume_structure(self, byte: int) -> None:
        if self._state == "start":
            if byte in _JSON_WHITESPACE:
                return
            if byte != ord("{"):
                raise ValidationError("Gmail attachment response is malformed")
            self._state = "key_or_end"
            return
        if self._state == "key_or_end":
            if byte in _JSON_WHITESPACE:
                return
            if byte == ord("}"):
                self._state = "done"
                return
            if byte != ord('"'):
                raise ValidationError("Gmail attachment response is malformed")
            self._begin_small_string("key")
            return
        if self._state == "after_key":
            if byte in _JSON_WHITESPACE:
                return
            if byte != ord(":"):
                raise ValidationError("Gmail attachment response is malformed")
            self._state = "value"
            return
        if self._state == "value":
            if byte in _JSON_WHITESPACE:
                return
            key = self._pending_key
            if key not in {"attachmentId", "data", "size"}:
                raise ValidationError("Gmail attachment response contains an unknown field")
            if key in {"attachmentId", "data"}:
                if byte != ord('"'):
                    raise ValidationError("Gmail attachment response is malformed")
                self._begin_small_string("attachment_id" if key == "attachmentId" else "data")
                if key == "data":
                    self._string_token.clear()
                    self._string_kind = None
                    self._state = "data"
                return
            if not 48 <= byte <= 57:
                raise ValidationError("Gmail attachment response is malformed")
            self._size_digits = bytearray((byte,))
            self._state = "size"
            return
        if self._state in {"after_value", "size_end"}:
            if byte in _JSON_WHITESPACE:
                return
            if self._state == "size_end" and byte not in {ord(","), ord("}")}:
                raise ValidationError("Gmail attachment response is malformed")
            if byte == ord(","):
                self._state = "key_or_end"
                return
            if byte == ord("}"):
                self._state = "done"
                return
            raise ValidationError("Gmail attachment response is malformed")
        if self._state == "done" and byte not in _JSON_WHITESPACE:
            raise ValidationError("Gmail attachment response has trailing data")

    def _begin_small_string(self, kind: str) -> None:
        self._string_token = bytearray(b'"')
        self._string_kind = kind
        self._string_escape = False
        self._state = kind

    def _consume_small_string(self, byte: int) -> None:
        if byte < 0x20:
            raise ValidationError("Gmail attachment response contains an invalid string")
        self._string_token.append(byte)
        if len(self._string_token) > _MAX_SMALL_STRING_BYTES:
            raise ValidationError("Gmail attachment response string exceeds its bound")
        if self._string_escape:
            self._string_escape = False
            return
        if byte == ord("\\"):
            self._string_escape = True
            return
        if byte != ord('"'):
            return
        try:
            value = json.loads(self._string_token.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("Gmail attachment response contains an invalid string") from exc
        if not isinstance(value, str):
            raise ValidationError("Gmail attachment response contains an invalid string")
        kind = self._string_kind
        if kind == "key":
            if value in self._seen:
                raise ValidationError("Gmail attachment response contains a duplicate field")
            self._seen.add(value)
            self._pending_key = value
            self._state = "after_key"
            return
        if kind == "attachment_id":
            self._state = "after_value"
            return
        raise ValidationError("Gmail attachment response is malformed")

    def _consume_data(self, byte: int) -> None:
        if byte == ord('"'):
            if self._base64_padding_complete or not self._base64_pending:
                self._finish_data()
                return
            if "=" in self._base64_pending or len(self._base64_pending) not in {2, 3}:
                raise ValidationError("Gmail attachment base64url data is invalid")
            self._emit_base64(self._base64_pending + "=" * (4 - len(self._base64_pending)))
            self._base64_pending = ""
            self._finish_data()
            return
        if byte == ord("\\") or byte < 0x20:
            raise ValidationError("Gmail attachment base64url data is invalid")
        character = chr(byte)
        if character == "=":
            if self._base64_padding_complete or len(self._base64_pending) < 2:
                raise ValidationError("Gmail attachment base64url data is invalid")
            self._base64_pending += character
        elif character in _BASE64URL:
            if self._base64_padding_complete:
                raise ValidationError("Gmail attachment base64url data is invalid")
            self._base64_pending += character
        else:
            raise ValidationError("Gmail attachment base64url data is invalid")
        if len(self._base64_pending) == 4:
            self._emit_base64(self._base64_pending)
            self._base64_padding_complete = "=" in self._base64_pending
            self._base64_pending = ""

    def _finish_data(self) -> None:
        self._flush_base64()
        self._saw_data = True
        self._state = "after_value"

    def _emit_base64(self, value: str) -> None:
        self._base64_encoded.extend(value.encode("ascii"))
        if len(self._base64_encoded) < _BASE64_DECODE_BLOCK_BYTES:
            return
        self._flush_base64()

    def _flush_base64(self) -> None:
        if not self._base64_encoded:
            return
        try:
            decoded = base64.b64decode(bytes(self._base64_encoded), altchars=b"-_", validate=True)
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise ValidationError("Gmail attachment base64url data is invalid") from exc
        self._base64_encoded.clear()
        if decoded:
            self._emit(decoded)

    def _emit(self, decoded: bytes) -> None:
        self._decoded_size += len(decoded)
        if self._decoded_size > MAX_ARTIFACT_BYTES:
            raise ValidationError("Gmail attachment exceeds the local artifact bound")
        if self._writer is not None:
            written = self._writer.write(decoded)
            if written != len(decoded):
                raise ValidationError("Gmail attachment artifact wrote an unexpected byte count")
            return
        if self._decoded_size > GMAIL_INLINE_MAX_BYTES:
            raise ValidationError("Gmail inline attachment exceeds its bounded compatibility limit")
        self._inline_content.extend(decoded)

    def _consume_size(self, byte: int) -> None:
        if 48 <= byte <= 57:
            if self._size_digits == bytearray(b"0"):
                raise ValidationError("Gmail attachment response size is invalid")
            if len(self._size_digits) >= 20:
                raise ValidationError("Gmail attachment response size is invalid")
            self._size_digits.append(byte)
            return
        if byte in _JSON_WHITESPACE:
            self._finish_size()
            self._state = "size_end"
            return
        if byte in {ord(","), ord("}")}:
            self._finish_size()
            self._state = "after_value"
            self._consume_structure(byte)
            return
        raise ValidationError("Gmail attachment response size is invalid")

    def _finish_size(self) -> None:
        try:
            value = int(self._size_digits.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise ValidationError("Gmail attachment response size is invalid") from exc
        if value > MAX_ARTIFACT_BYTES:
            raise ValidationError("Gmail attachment response size exceeds its bound")
        self._declared_size = value


def _mime_boundary(
    headers: Sequence[tuple[str, str]],
    body: bytes,
    attachments: Sequence[GmailMimeAttachment],
) -> str:
    digest = hashlib.sha256()
    for name, value in headers:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    digest.update(body)
    for item in attachments:
        digest.update(item.filename.encode("utf-8"))
        digest.update(item.mime_type.encode("utf-8"))
        digest.update(str(item.size).encode("ascii"))
        digest.update(
            item.upload.sha256.encode("ascii")
            if item.upload is not None
            else hashlib.sha256(item.inline_content or b"").hexdigest().encode("ascii")
        )
    seed = digest.hexdigest()[:_MIME_BOUNDARY_DIGEST_HEX_LENGTH]
    for suffix in range(100):
        boundary = f"{_MIME_BOUNDARY_PREFIX}{seed}_{suffix:x}"
        if len(boundary) > _MIME_BOUNDARY_MAX_LENGTH:
            raise ValidationError("Gmail MIME boundary exceeds the RFC 2046 length limit")
        if boundary.encode("ascii") not in body:
            return boundary
    raise ValidationError("Gmail MIME boundary could not be made unique")


def _attachment_headers(attachment: GmailMimeAttachment) -> bytes:
    part = EmailMessage(policy=SMTP)
    part.set_type(attachment.mime_type)
    part["Content-Transfer-Encoding"] = "base64"
    part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
    result = part.as_bytes(policy=SMTP)
    if result.endswith(b"\r\n") and not result.endswith(b"\r\n\r\n"):
        result += b"\r\n"
    if not result.endswith(b"\r\n\r\n"):
        raise ValidationError("Gmail attachment MIME headers are malformed")
    return result


def _base64_lines_length(size: int) -> int:
    if size == 0:
        return 0
    lines = (size + 56) // 57
    remainder = size % 57
    last_size = 57 if remainder == 0 else remainder
    return (lines - 1) * 78 + 4 * ((last_size + 2) // 3) + 2


def _iter_base64_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    remainder = b""
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise ValidationError("Gmail MIME source returned a non-binary chunk")
        if not chunk:
            continue
        pending = remainder + chunk
        complete = len(pending) - len(pending) % 57
        for offset in range(0, complete, 57):
            yield base64.b64encode(pending[offset : offset + 57]) + b"\r\n"
        remainder = pending[complete:]
    if remainder:
        yield base64.b64encode(remainder) + b"\r\n"

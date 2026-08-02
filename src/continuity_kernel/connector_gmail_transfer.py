"""Streaming Gmail MIME uploads and MessagePartBody downloads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import SMTP
from email.utils import parsedate_to_datetime
from typing import Final, Literal, Protocol, cast

from continuity_kernel.connector_transfer import (
    MAX_ARTIFACT_BYTES,
    ArtifactReceipt,
    PreparedUpload,
)
from continuity_kernel.errors import ValidationError

# Source: https://gmail.googleapis.com/$discovery/rest?version=v1
# users.messages.send.mediaUpload.maxSize=36700160 and
# users.messages.{insert,import}.mediaUpload.maxSize=157286400, verified 2026-08-02.
GMAIL_UPLOAD_MAX_BYTES: Final = 36_700_160
GMAIL_MIGRATION_UPLOAD_MAX_BYTES: Final = 157_286_400
GMAIL_INLINE_MAX_BYTES: Final = 180_000

_BASE64URL: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_JSON_WHITESPACE: Final = frozenset({9, 10, 13, 32})
_MAX_SMALL_STRING_BYTES: Final = 16 * 1024
_BASE64_DECODE_BLOCK_BYTES: Final = 4 * 1024
_MIME_BOUNDARY_PREFIX: Final = "_seld_gmail_"
_MIME_BOUNDARY_DIGEST_HEX_LENGTH: Final = 48
_MIME_BOUNDARY_MAX_LENGTH: Final = 70
_RAW_HEADER_MAX_BYTES: Final = 256 * 1024
_RAW_HEADER_MAX_FIELDS: Final = 512
_RAW_HEADER_MAX_LINE_BYTES: Final = 998
_RAW_HEADER_READ_CHUNK_BYTES: Final = 4 * 1024
_RAW_DISPLAYED_ADDRESS_MAX_CHARS: Final = 998
_RAW_SINGLETON_FIELDS: Final = ("from", "sender", "reply-to", "subject", "date", "message-id")
_RAW_RECIPIENT_FIELDS: Final = ("to", "cc", "bcc")
_RAW_RESENT_RECIPIENT_FIELDS: Final = ("resent-to", "resent-cc", "resent-bcc")
_RAW_HEADER_NAME_BYTES: Final = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_RAW_BIDI_CONTROL_CODEPOINTS: Final = frozenset(
    {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
)
_OPAQUE_HEADERS_WARNING: Final = (
    "Message headers could not be safely parsed; the exact prepared message will be "
    "processed opaquely."
)


class _ArtifactWriter(Protocol):
    def write(self, content: bytes) -> int: ...

    def finish(self) -> ArtifactReceipt: ...

    def abort(self) -> None: ...


class _Mailbox(Protocol):
    username: str
    domain: str

    def __str__(self) -> str: ...


class _AddressGroup(Protocol):
    display_name: str | None
    addresses: tuple[_Mailbox, ...]


class _AddressHeader(Protocol):
    addresses: tuple[_Mailbox, ...]
    groups: tuple[_AddressGroup, ...]


class _RawHeaderError(ValueError):
    """A content-level RFC822 header error safe to collapse for migration."""


class _RedactedRawMessagePreview(dict[str, object]):
    """JSON-safe confirmation data whose incidental representation reveals no headers."""

    def __repr__(self) -> str:
        return "GmailRawMessagePreview(<redacted>)"


def gmail_raw_message_preview(
    upload: PreparedUpload,
    *,
    strict_send: bool,
    require_date: bool,
) -> dict[str, object]:
    """Inspect only bounded headers from one immutable raw-message snapshot.

    Strict send previews reject any routing ambiguity. Migration previews may
    instead return an explicit opaque result so legacy mail can still be
    imported byte-for-byte. A migration that derives ``internalDate`` from the
    message remains fail-closed because Gmail needs one valid ``Date`` header.
    """

    if not isinstance(upload, PreparedUpload):
        raise ValidationError("Gmail raw message upload is invalid")
    if type(strict_send) is not bool or type(require_date) is not bool:
        raise ValidationError("Gmail raw message preview mode is invalid")
    if type(upload.size) is not int or upload.size < 0:
        raise ValidationError("Gmail raw message upload size is invalid")
    if upload.size == 0:
        raise ValidationError("Gmail raw message is empty")

    try:
        raw_headers = _read_raw_headers(upload)
        preview = _parse_raw_headers(raw_headers, require_recipients=strict_send)
        if require_date and preview["parsed_date"] is None:
            raise _RawHeaderError("one valid Date header is required")
        return preview
    except _RawHeaderError as exc:
        if require_date:
            raise ValidationError(
                "Gmail raw message headers could not be validated for dateHeader; "
                "use internal_date_source=receivedTime for legacy or malformed mail"
            ) from exc
        if strict_send:
            raise ValidationError(f"Gmail raw message headers are invalid: {exc}") from exc
        return _opaque_raw_message_preview()


def _read_raw_headers(upload: PreparedUpload) -> bytes:
    read_length = min(upload.size, _RAW_HEADER_MAX_BYTES + len(b"\r\n\r\n"))
    buffered = bytearray()
    for chunk in upload.iter_chunks(
        length=read_length,
        chunk_size=_RAW_HEADER_READ_CHUNK_BYTES,
    ):
        buffered.extend(chunk)
        delimiter = buffered.find(b"\r\n\r\n")
        if delimiter >= 0:
            return bytes(buffered[:delimiter])
    if upload.size > _RAW_HEADER_MAX_BYTES:
        raise _RawHeaderError("header section exceeds its byte bound")
    raise _RawHeaderError("header terminator is missing")


def _parse_raw_headers(
    raw_headers: bytes,
    *,
    require_recipients: bool,
) -> _RedactedRawMessagePreview:
    _validate_raw_header_lines(raw_headers)
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(raw_headers + b"\r\n\r\n")
        if message.defects:
            raise _RawHeaderError("the RFC822 parser reported a defect")
        for header in message.values():
            if getattr(header, "defects", ()):
                raise _RawHeaderError("an RFC822 header contains a parser defect")
            _validate_decoded_header(str(header))
        for name in _RAW_SINGLETON_FIELDS:
            if len(message.get_all(name, [])) > 1:
                raise _RawHeaderError(f"{name} header is duplicated")
        if require_recipients and any(
            message.get_all(name, []) for name in _RAW_RESENT_RECIPIENT_FIELDS
        ):
            raise _RawHeaderError("Resent recipient headers make routing ambiguous")

        recipients = {
            name: _recipient_header_values(message.get_all(name, []), require=require_recipients)
            for name in _RAW_RECIPIENT_FIELDS
        }
        if require_recipients and not any(recipients.values()):
            raise _RawHeaderError("at least one concrete To, Cc, or Bcc mailbox is required")

        date_value = _singleton_header_value(message.get_all("date", []))
        parsed_date: str | None = None
        if date_value is not None:
            try:
                date = parsedate_to_datetime(date_value)
            except (TypeError, ValueError) as exc:
                raise _RawHeaderError("Date header is invalid") from exc
            if date is None:
                raise _RawHeaderError("Date header is invalid")
            parsed_date = date.isoformat()

        return _RedactedRawMessagePreview(
            {
                "bcc": recipients["bcc"],
                "cc": recipients["cc"],
                "date": date_value,
                "from": _singleton_header_value(
                    message.get_all("from", []),
                    maximum_length=_RAW_DISPLAYED_ADDRESS_MAX_CHARS,
                ),
                "headers_parsed": True,
                "message_id": _singleton_header_value(message.get_all("message-id", [])),
                "parsed_date": parsed_date,
                "reply_to": _singleton_header_value(
                    message.get_all("reply-to", []),
                    maximum_length=_RAW_DISPLAYED_ADDRESS_MAX_CHARS,
                ),
                "sender": _singleton_header_value(
                    message.get_all("sender", []),
                    maximum_length=_RAW_DISPLAYED_ADDRESS_MAX_CHARS,
                ),
                "subject": _singleton_header_value(message.get_all("subject", [])),
                "to": recipients["to"],
                "warnings": [],
            }
        )
    except _RawHeaderError:
        raise
    except Exception as exc:
        raise _RawHeaderError("headers could not be parsed safely") from exc


def _validate_raw_header_lines(raw_headers: bytes) -> None:
    try:
        raw_headers.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _RawHeaderError("headers contain invalid UTF-8 bytes") from exc
    lines = raw_headers.split(b"\r\n") if raw_headers else []
    fields = 0
    for line in lines:
        if len(line) > _RAW_HEADER_MAX_LINE_BYTES:
            raise _RawHeaderError("a header line exceeds the RFC5322 byte bound")
        if any(_unsafe_raw_header_byte(byte) for byte in line):
            raise _RawHeaderError("a header contains a bare line ending or control character")
        if line[:1] in {b" ", b"\t"}:
            if fields == 0:
                raise _RawHeaderError("a header continuation has no field")
            continue
        name, separator, _value = line.partition(b":")
        if not separator or not name or any(byte not in _RAW_HEADER_NAME_BYTES for byte in name):
            raise _RawHeaderError("a header field name is invalid")
        fields += 1
        if fields > _RAW_HEADER_MAX_FIELDS:
            raise _RawHeaderError("header field count exceeds its bound")


def _unsafe_raw_header_byte(byte: int) -> bool:
    return (byte < 0x20 and byte != 0x09) or byte == 0x7F


def _validate_decoded_header(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise _RawHeaderError("a decoded header is invalid Unicode") from exc
    if any(
        (ord(character) < 0x20 and character != "\t")
        or 0x7F <= ord(character) <= 0x9F
        or ord(character) in _RAW_BIDI_CONTROL_CODEPOINTS
        for character in value
    ):
        raise _RawHeaderError("a decoded header contains a control character")


def _recipient_header_values(headers: Sequence[object], *, require: bool) -> list[str]:
    result: list[str] = []
    for raw_header in headers:
        header = cast(_AddressHeader, raw_header)
        addresses = tuple(header.addresses)
        if require and not addresses:
            raise _RawHeaderError("a recipient header has no concrete mailbox")
        if require and any(
            group.display_name is not None and not group.addresses for group in header.groups
        ):
            raise _RawHeaderError("a recipient group hides its concrete mailboxes")
        for address in addresses:
            if (
                not isinstance(address.username, str)
                or not address.username
                or not isinstance(address.domain, str)
                or not address.domain
            ):
                raise _RawHeaderError("a recipient mailbox is ambiguous")
            value = str(address)
            _validate_decoded_header(value)
            if len(value) > _RAW_DISPLAYED_ADDRESS_MAX_CHARS:
                raise _RawHeaderError("a displayed recipient address exceeds its bound")
            result.append(value)
    return result


def _singleton_header_value(
    headers: Sequence[object],
    *,
    maximum_length: int | None = None,
) -> str | None:
    if not headers:
        return None
    value = str(headers[0])
    _validate_decoded_header(value)
    if maximum_length is not None and len(value) > maximum_length:
        raise _RawHeaderError("a displayed address header exceeds its bound")
    return value


def _opaque_raw_message_preview() -> _RedactedRawMessagePreview:
    return _RedactedRawMessagePreview(
        {
            "bcc": [],
            "cc": [],
            "date": None,
            "from": None,
            "headers_parsed": False,
            "message_id": None,
            "parsed_date": None,
            "reply_to": None,
            "sender": None,
            "subject": None,
            "to": [],
            "warnings": [_OPAQUE_HEADERS_WARNING],
        }
    )


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
            raise ValidationError("Gmail MIME upload exceeds the documented provider upload limit")

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
    """Incrementally parse one Gmail response and decode one base64url field."""

    def __init__(
        self,
        *,
        writer: _ArtifactWriter | None = None,
        encoded_field: Literal["data", "raw"] = "data",
    ) -> None:
        if encoded_field not in {"data", "raw"}:
            raise ValidationError("Gmail encoded response field is invalid")
        self._writer = writer
        self._encoded_field = encoded_field
        self._response_kind = "attachment" if encoded_field == "data" else "raw-message"
        self._allowed_fields = (
            frozenset({"attachmentId", "data", "size"})
            if encoded_field == "data"
            else frozenset({"raw"})
        )
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
            raise self._error("content is not complete")
        return bytes(self._inline_content)

    def write(self, content: bytes) -> int:
        if self._closed:
            raise self._error("decoder is closed")
        if not isinstance(content, bytes):
            raise self._error("response returned a non-binary chunk")
        try:
            for byte in content:
                self._consume(byte)
        except Exception as exc:
            self.abort()
            if isinstance(exc, ValidationError):
                raise
            raise self._error("response is malformed") from exc
        return len(content)

    def finish(self) -> ArtifactReceipt | None:
        if self._closed:
            raise self._error("decoder is closed")
        try:
            if self._state != "done" or not self._saw_data:
                raise self._error("response is truncated")
            if self._declared_size is not None and self._declared_size != self._decoded_size:
                raise self._error("size does not match decoded data")
            if self._writer is not None:
                receipt = self._writer.finish()
                if not isinstance(receipt, ArtifactReceipt):
                    raise self._error("artifact receipt is invalid")
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

    def _error(self, detail: str) -> ValidationError:
        return ValidationError(f"Gmail {self._response_kind} {detail}")

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
        raise self._error("response is malformed")

    def _consume_structure(self, byte: int) -> None:
        if self._state == "start":
            if byte in _JSON_WHITESPACE:
                return
            if byte != ord("{"):
                raise self._error("response is malformed")
            self._state = "key_or_end"
            return
        if self._state == "key_or_end":
            if byte in _JSON_WHITESPACE:
                return
            if byte == ord("}"):
                self._state = "done"
                return
            if byte != ord('"'):
                raise self._error("response is malformed")
            self._begin_small_string("key")
            return
        if self._state == "after_key":
            if byte in _JSON_WHITESPACE:
                return
            if byte != ord(":"):
                raise self._error("response is malformed")
            self._state = "value"
            return
        if self._state == "value":
            if byte in _JSON_WHITESPACE:
                return
            key = self._pending_key
            if key not in self._allowed_fields:
                raise self._error("response contains an unknown field")
            if key in {"attachmentId", self._encoded_field}:
                if byte != ord('"'):
                    raise self._error("response is malformed")
                self._begin_small_string("attachment_id" if key == "attachmentId" else "data")
                if key == self._encoded_field:
                    self._string_token.clear()
                    self._string_kind = None
                    self._state = "data"
                return
            if not 48 <= byte <= 57:
                raise self._error("response is malformed")
            self._size_digits = bytearray((byte,))
            self._state = "size"
            return
        if self._state in {"after_value", "size_end"}:
            if byte in _JSON_WHITESPACE:
                return
            if self._state == "size_end" and byte not in {ord(","), ord("}")}:
                raise self._error("response is malformed")
            if byte == ord(","):
                self._state = "key_or_end"
                return
            if byte == ord("}"):
                self._state = "done"
                return
            raise self._error("response is malformed")
        if self._state == "done" and byte not in _JSON_WHITESPACE:
            raise self._error("response has trailing data")

    def _begin_small_string(self, kind: str) -> None:
        self._string_token = bytearray(b'"')
        self._string_kind = kind
        self._string_escape = False
        self._state = kind

    def _consume_small_string(self, byte: int) -> None:
        if byte < 0x20:
            raise self._error("response contains an invalid string")
        self._string_token.append(byte)
        if len(self._string_token) > _MAX_SMALL_STRING_BYTES:
            raise self._error("response string exceeds its bound")
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
            raise self._error("response contains an invalid string") from exc
        if not isinstance(value, str):
            raise self._error("response contains an invalid string")
        kind = self._string_kind
        if kind == "key":
            if value in self._seen:
                raise self._error("response contains a duplicate field")
            self._seen.add(value)
            self._pending_key = value
            self._state = "after_key"
            return
        if kind == "attachment_id":
            self._state = "after_value"
            return
        raise self._error("response is malformed")

    def _consume_data(self, byte: int) -> None:
        if byte == ord('"'):
            if self._base64_padding_complete or not self._base64_pending:
                self._finish_data()
                return
            if "=" in self._base64_pending or len(self._base64_pending) not in {2, 3}:
                raise self._error("base64url data is invalid")
            self._emit_base64(self._base64_pending + "=" * (4 - len(self._base64_pending)))
            self._base64_pending = ""
            self._finish_data()
            return
        if byte == ord("\\") or byte < 0x20:
            raise self._error("base64url data is invalid")
        character = chr(byte)
        if character == "=":
            if self._base64_padding_complete or len(self._base64_pending) < 2:
                raise self._error("base64url data is invalid")
            self._base64_pending += character
        elif character in _BASE64URL:
            if self._base64_padding_complete:
                raise self._error("base64url data is invalid")
            self._base64_pending += character
        else:
            raise self._error("base64url data is invalid")
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
            raise self._error("base64url data is invalid") from exc
        self._base64_encoded.clear()
        if decoded:
            self._emit(decoded)

    def _emit(self, decoded: bytes) -> None:
        self._decoded_size += len(decoded)
        if self._decoded_size > MAX_ARTIFACT_BYTES:
            raise self._error("exceeds the local artifact bound")
        if self._writer is not None:
            written = self._writer.write(decoded)
            if written != len(decoded):
                raise self._error("artifact wrote an unexpected byte count")
            return
        if self._decoded_size > GMAIL_INLINE_MAX_BYTES:
            raise self._error("inline content exceeds its bounded compatibility limit")
        self._inline_content.extend(decoded)

    def _consume_size(self, byte: int) -> None:
        if 48 <= byte <= 57:
            if self._size_digits == bytearray(b"0"):
                raise self._error("response size is invalid")
            if len(self._size_digits) >= 20:
                raise self._error("response size is invalid")
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
        raise self._error("response size is invalid")

    def _finish_size(self) -> None:
        try:
            value = int(self._size_digits.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise self._error("response size is invalid") from exc
        if value > MAX_ARTIFACT_BYTES:
            raise self._error("response size exceeds its bound")
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

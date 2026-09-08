"""Bounded local text extraction for connected-app artifacts.

This module deliberately accepts only a local path.  Providers fetch artifacts
through their own authenticated adapters; extraction neither contacts a service
nor interprets active document content.
"""

from __future__ import annotations

import csv
import io
import json
import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, Final, Literal, cast
from xml.etree import ElementTree

MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
# Keep room for the app-corpus Markdown header and its bounded source title.
MAX_TEXT_BYTES: Final = 3 * 1024 * 1024
MAX_JSON_VALUES: Final = 100_000
MAX_CSV_ROWS: Final = 20_000
MAX_CSV_CELLS: Final = 100_000
MAX_DOCX_MEMBERS: Final = 2_000
EXTRACTION_TIMEOUT_SECONDS: Final = 15
_MAX_ERROR_BYTES: Final = 64 * 1024
_CHUNK_BYTES: Final = 64 * 1024
_PATH_FLAGS: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_TEXT_MIMES: Final = frozenset(
    {
        "application/sql",
        "application/x-sh",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
        "text/calendar",
        "text/markdown",
        "text/plain",
        "text/x-markdown",
        "text/x-rst",
        "text/xml",
    }
)
_HTML_MIMES: Final = frozenset({"application/xhtml+xml", "text/html"})
_JSON_MIMES: Final = frozenset({"application/json", "application/ld+json"})
_CSV_MIMES: Final = frozenset({"application/csv", "text/csv", "text/tab-separated-values"})
_DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PDF_MIME: Final = "application/pdf"
_IMAGE_MIMES: Final = frozenset(
    {"image/bmp", "image/gif", "image/jpeg", "image/png", "image/tiff", "image/webp"}
)
_EXTENSION_KINDS: Final = {
    ".bmp": "image",
    ".csv": "csv",
    ".docx": "docx",
    ".gif": "image",
    ".htm": "html",
    ".html": "html",
    ".jpeg": "image",
    ".jpg": "image",
    ".json": "json",
    ".md": "text",
    ".pdf": "pdf",
    ".png": "image",
    ".text": "text",
    ".tif": "image",
    ".tiff": "image",
    ".tsv": "csv",
    ".txt": "text",
    ".webp": "image",
    ".xlsx": "xlsx",
    ".xml": "text",
}
_BLOCK_TAGS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)


@dataclass(frozen=True)
class ExtractionResult:
    """Text and truthful coverage for one local artifact."""

    text: str
    status: Literal["ok", "partial", "gap"]
    reason: str | None
    omissions: tuple[str, ...]


class _InputTooLargeError(Exception):
    pass


class _PrivateTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        clean = tag.casefold()
        if clean in {"script", "style", "template"}:
            self.hidden_depth += 1
        if clean in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        clean = tag.casefold()
        if clean in _BLOCK_TAGS:
            self.parts.append("\n")
        if clean in {"script", "style", "template"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def extract_text(path: Path | str, mime: str | None = None) -> ExtractionResult:
    """Extract bounded searchable text from one regular local artifact.

    Results never claim success for an unsupported, encrypted, unavailable, or
    oversized document.  Callers can retain ``omissions`` beside their corpus
    document metadata without exposing the source artifact.
    """

    candidate = _path(path)
    if candidate is None:
        return _gap("local artifact path is invalid", "invalid_path")
    kind = _kind(candidate, mime)
    if kind is None:
        return _gap("local artifact type is unsupported", "unsupported_document_type")
    if not getattr(os, "O_NOFOLLOW", 0):
        return _gap("secure local artifact reads are unavailable on this platform", "unsafe_file")
    try:
        descriptor, initial = _open_regular_file(candidate)
    except FileNotFoundError:
        return _gap("local artifact is unavailable", "artifact_unavailable")
    except _InputTooLargeError:
        return _gap(
            f"local artifact exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit",
            "input_too_large",
        )
    except OSError:
        return _gap("local artifact is not a safe regular file", "unsafe_file")

    try:
        if kind == "text":
            result = _plain_text(descriptor)
        elif kind == "html":
            result = _html_text(descriptor)
        elif kind == "json":
            result = _json_text(descriptor)
        elif kind == "csv":
            result = _csv_text(descriptor)
        elif kind == "docx":
            result = _docx_text(descriptor)
        elif kind == "xlsx":
            result = _xlsx_text(descriptor)
        elif kind == "pdf":
            result = _external_text(descriptor, candidate.suffix, "pdf")
        else:
            result = _external_text(descriptor, candidate.suffix, "image")
        if not _unchanged(descriptor, initial):
            return _gap("local artifact changed while text was extracted", "artifact_changed")
        return result
    except _InputTooLargeError:
        return _gap(
            f"local artifact exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB input limit",
            "input_too_large",
        )
    except (OSError, UnicodeError, ValueError):
        return _gap("local artifact could not be read", "artifact_unreadable")
    finally:
        os.close(descriptor)


def _path(value: Path | str) -> Path | None:
    if not isinstance(value, (Path, str)):
        return None
    try:
        return Path(value).expanduser()
    except (OSError, ValueError):
        return None


def _kind(path: Path, mime: str | None) -> str | None:
    clean_mime = _mime(mime)
    if mime is not None and clean_mime is None:
        return None
    if clean_mime in _TEXT_MIMES:
        return "text"
    if clean_mime in _HTML_MIMES:
        return "html"
    if clean_mime in _JSON_MIMES:
        return "json"
    if clean_mime in _CSV_MIMES:
        return "csv"
    if clean_mime == _DOCX_MIME:
        return "docx"
    if clean_mime == _XLSX_MIME:
        return "xlsx"
    if clean_mime == _PDF_MIME:
        return "pdf"
    if clean_mime in _IMAGE_MIMES:
        return "image"
    if clean_mime is not None:
        return None
    return _EXTENSION_KINDS.get(path.suffix.casefold())


def _mime(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    clean = value.split(";", 1)[0].strip().casefold()
    return clean or None


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    descriptor = os.open(path, _PATH_FLAGS)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("local artifact is not a regular file")
        if metadata.st_size > MAX_INPUT_BYTES:
            raise _InputTooLargeError
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _unchanged(descriptor: int, initial: os.stat_result) -> bool:
    current = os.fstat(descriptor)
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == initial.st_dev
        and current.st_ino == initial.st_ino
        and current.st_size == initial.st_size
        and current.st_mtime_ns == initial.st_mtime_ns
    )


def _plain_text(descriptor: int) -> ExtractionResult:
    text, replaced = _decode(_read_descriptor(descriptor, MAX_TEXT_BYTES + 1))
    return _decoded_result(text, replaced=replaced)


def _html_text(descriptor: int) -> ExtractionResult:
    source, replaced = _decode(_read_descriptor(descriptor, MAX_INPUT_BYTES + 1))
    parser = _PrivateTextParser()
    try:
        parser.feed(source)
        parser.close()
    except ValueError:
        return _gap("HTML document is invalid", "invalid_html")
    text, text_limited = _trim_text("".join(parser.parts))
    return _decoded_result(text, replaced=replaced, limited=text_limited)


def _json_text(descriptor: int) -> ExtractionResult:
    source, replaced = _decode(_read_descriptor(descriptor, MAX_INPUT_BYTES + 1))
    try:
        value = json.loads(source)
    except (json.JSONDecodeError, RecursionError):
        return _gap("JSON document is invalid", "invalid_json")
    lines: list[str] = []
    omitted = False
    values = 0
    pending: list[tuple[str, object]] = [("", value)]
    while pending:
        prefix, current = pending.pop()
        values += 1
        if values > MAX_JSON_VALUES:
            omitted = True
            break
        if isinstance(current, dict):
            for key, item in reversed(tuple(current.items())):
                if not isinstance(key, str):
                    continue
                pending.append((f"{prefix}.{key}" if prefix else key, item))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                pending.append((f"{prefix}[{index}]", current[index]))
        elif current is not None:
            lines.append(f"{prefix}: {_json_scalar(current)}" if prefix else _json_scalar(current))
    text, text_limited = _trim_text("\n".join(lines))
    if omitted:
        return _partial(text, "JSON value limit reached", "json_value_limit")
    return _decoded_result(text, replaced=replaced, limited=text_limited)


def _json_scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _csv_text(descriptor: int) -> ExtractionResult:
    source, replaced = _decode(_read_descriptor(descriptor, MAX_INPUT_BYTES + 1))
    try:
        reader = csv.reader(io.StringIO(source, newline=""))
        lines: list[str] = []
        cells = 0
        for row_number, row in enumerate(reader, start=1):
            cells += len(row)
            if row_number > MAX_CSV_ROWS or cells > MAX_CSV_CELLS:
                text, text_limited = _trim_text("\n".join(lines))
                del text_limited
                return _partial(text, "CSV row or cell limit reached", "csv_value_limit")
            lines.append(" | ".join(row))
    except csv.Error:
        return _gap("CSV document is invalid", "invalid_csv")
    text, text_limited = _trim_text("\n".join(lines))
    return _decoded_result(text, replaced=replaced, limited=text_limited)


def _docx_text(descriptor: int) -> ExtractionResult:
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as handle:
            duplicate = -1
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_DOCX_MEMBERS:
                    return _gap("DOCX archive has too many members", "document_structure_limit")
                if any(info.flag_bits & 0x1 for info in infos) or any(
                    info.filename == "EncryptedPackage" for info in infos
                ):
                    return _gap("DOCX document is encrypted", "encrypted_document")
                try:
                    info = archive.getinfo("word/document.xml")
                except KeyError:
                    return _gap("DOCX document text is unavailable", "missing_document_text")
                if info.file_size > MAX_TEXT_BYTES:
                    return _gap("DOCX document text exceeds the output limit", "output_too_large")
                with archive.open(info) as entry:
                    source = _read_stream(entry, MAX_TEXT_BYTES + 1)
    except (NotImplementedError, OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return _gap("DOCX document is invalid", "invalid_docx")
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    if len(source) > MAX_TEXT_BYTES:
        return _gap("DOCX document text exceeds the output limit", "output_too_large")
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError:
        return _gap("DOCX document XML is invalid", "invalid_docx")
    paragraphs = [
        _docx_paragraph(paragraph) for paragraph in root.iter() if _local_name(paragraph.tag) == "p"
    ]
    text, limited = _trim_text("\n".join(value for value in paragraphs if value))
    return _result_from_text(text, limited=limited)


def _xlsx_text(descriptor: int) -> ExtractionResult:
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as handle:
            duplicate = -1
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_DOCX_MEMBERS:
                    return _gap("XLSX archive has too many members", "document_structure_limit")
                if any(info.flag_bits & 0x1 for info in infos) or any(
                    info.filename == "EncryptedPackage" for info in infos
                ):
                    return _gap("XLSX document is encrypted", "encrypted_document")
                indexed = {info.filename: info for info in infos}
                if len(indexed) != len(infos):
                    return _gap("XLSX archive has duplicate members", "invalid_xlsx")
                workbook = _xlsx_xml(
                    archive, indexed.get("xl/workbook.xml"), maximum=MAX_TEXT_BYTES
                )
                relationships = _xlsx_xml(
                    archive, indexed.get("xl/_rels/workbook.xml.rels"), maximum=MAX_TEXT_BYTES
                )
                if workbook is None or relationships is None:
                    return _gap(
                        "XLSX workbook structure is unavailable",
                        "missing_workbook_structure",
                    )
                sheet_members = _xlsx_sheet_members(workbook, relationships, indexed)
                if sheet_members is None:
                    return _gap("XLSX workbook structure is invalid", "invalid_xlsx")
                shared_info = indexed.get("xl/sharedStrings.xml")
                selected = [
                    info
                    for info in (
                        indexed["xl/workbook.xml"],
                        indexed["xl/_rels/workbook.xml.rels"],
                        shared_info,
                        *(member for _name, member in sheet_members),
                    )
                    if info is not None
                ]
                if sum(info.file_size for info in selected) > MAX_INPUT_BYTES:
                    return _gap("XLSX XML exceeds the input limit", "document_structure_limit")
                shared_strings = _xlsx_shared_strings(archive, shared_info)
                if shared_strings is None:
                    return _gap("XLSX shared strings are invalid", "invalid_xlsx")
                output = _BoundedText()
                output.line(
                    "Cell values are stored values; number and date display formats "
                    "are not applied."
                )
                for sheet_name, info in sheet_members:
                    root = _xlsx_xml(archive, info)
                    if root is None:
                        return _gap("XLSX worksheet XML is invalid", "invalid_xlsx")
                    if not output.line(f"Worksheet: {sheet_name}"):
                        break
                    if not _xlsx_write_sheet(root, shared_strings, output):
                        break
    except (
        ElementTree.ParseError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return _gap("XLSX document is invalid", "invalid_xlsx")
    finally:
        if duplicate >= 0:
            os.close(duplicate)
    return _result_from_text(output.text, limited=output.limited)


def _xlsx_xml(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo | None, *, maximum: int = MAX_INPUT_BYTES
) -> ElementTree.Element[str] | None:
    if info is None or info.file_size > maximum:
        return None
    with archive.open(info) as entry:
        source = _read_stream(entry, maximum + 1)
    if len(source) > maximum:
        return None
    return ElementTree.fromstring(source)


def _xlsx_sheet_members(
    workbook: ElementTree.Element[str],
    relationships: ElementTree.Element[str],
    indexed: dict[str, zipfile.ZipInfo],
) -> list[tuple[str, zipfile.ZipInfo]] | None:
    targets: dict[str, str] = {}
    for relationship in relationships:
        if _local_name(relationship.tag) != "Relationship":
            continue
        relation_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        relation_type = relationship.attrib.get("Type")
        if (
            isinstance(relation_id, str)
            and isinstance(target, str)
            and isinstance(relation_type, str)
            and relation_type.endswith("/worksheet")
            and relationship.attrib.get("TargetMode") != "External"
        ):
            member = _xlsx_member_path(target)
            if member is not None:
                targets[relation_id] = member
    sheets: list[tuple[str, zipfile.ZipInfo]] = []
    for sheet in workbook.iter():
        if _local_name(sheet.tag) != "sheet":
            continue
        name = sheet.attrib.get("name")
        relation_id = _xlsx_attribute(sheet, "id")
        if not isinstance(name, str) or not name or not isinstance(relation_id, str):
            return None
        member_name = targets.get(relation_id)
        info = indexed.get(member_name) if member_name is not None else None
        if info is None:
            return None
        sheets.append((name, info))
    return sheets or None


def _xlsx_member_path(target: str) -> str | None:
    if not target or "\x00" in target:
        return None
    parts = target.lstrip("/").split("/") if target.startswith("/") else ["xl", *target.split("/")]
    if not all(part and part not in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _xlsx_shared_strings(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo | None
) -> list[str] | None:
    if info is None:
        return []
    root = _xlsx_xml(archive, info)
    if root is None:
        return None
    return [_xlsx_node_text(item) for item in root if _local_name(item.tag) == "si"]


class _BoundedText:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = 0
        self.limited = False

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def line(self, value: str) -> bool:
        if self._parts and not self.write("\n"):
            return False
        return self.write(value)

    def write(self, value: str) -> bool:
        encoded = value.encode("utf-8")
        remaining = MAX_TEXT_BYTES - self._bytes
        if len(encoded) <= remaining:
            self._parts.append(value)
            self._bytes += len(encoded)
            return True
        if remaining > 0:
            self._parts.append(encoded[:remaining].decode("utf-8", errors="ignore"))
            self._bytes = MAX_TEXT_BYTES
        self.limited = True
        return False


def _xlsx_write_sheet(
    root: ElementTree.Element[str], shared_strings: list[str], output: _BoundedText
) -> bool:
    for row in root.iter():
        if _local_name(row.tag) != "row":
            continue
        row_started = False
        for position, cell in enumerate(row, start=1):
            if _local_name(cell.tag) != "c":
                continue
            value = _xlsx_cell_value(cell, shared_strings)
            if value is None:
                continue
            if row_started:
                if not output.write(" | "):
                    return False
            elif not output.line(""):
                return False
            row_started = True
            reference = cell.attrib.get("r") or f"cell {position}"
            if not output.write(reference) or not output.write(": ") or not output.write(value):
                return False
    return True


def _xlsx_cell_value(cell: ElementTree.Element[str], shared_strings: list[str]) -> str | None:
    formula = _xlsx_child(cell, "f")
    cached = _xlsx_cached_value(cell, shared_strings)
    if formula is None:
        return cached
    expression = (formula.text or "").strip()
    label = f"={expression}" if expression else "shared formula"
    if cached is None:
        return f"formula {label}; cached result unavailable"
    return f"formula {label}; cached result: {cached}"


def _xlsx_cached_value(cell: ElementTree.Element[str], shared_strings: list[str]) -> str | None:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = _xlsx_child(cell, "is")
        return "" if inline is None else _xlsx_node_text(inline)
    value = _xlsx_child(cell, "v")
    if value is None:
        return None
    raw = value.text or ""
    if cell_type == "s":
        index = int(raw)
        if index < 0 or index >= len(shared_strings):
            raise ValueError("XLSX shared string index is invalid")
        return shared_strings[index]
    if cell_type == "b":
        return {"0": "FALSE", "1": "TRUE"}.get(raw, raw)
    if cell_type == "d":
        return f"stored ISO date: {raw}"
    return raw


def _xlsx_child(element: ElementTree.Element[str], name: str) -> ElementTree.Element[str] | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _xlsx_attribute(element: ElementTree.Element[str], name: str) -> str | None:
    return next((value for key, value in element.attrib.items() if _local_name(key) == name), None)


def _xlsx_node_text(element: ElementTree.Element[str]) -> str:
    return "".join(node.text or "" for node in element.iter() if _local_name(node.tag) == "t")


def _docx_paragraph(paragraph: ElementTree.Element[str]) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = _local_name(node.tag)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _external_text(descriptor: int, suffix: str, kind: Literal["image", "pdf"]) -> ExtractionResult:
    executable = (
        "/opt/homebrew/bin/pdftotext"
        if kind == "pdf"
        else shutil.which(
            "tesseract",
            path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        )
    )
    if executable is None or not os.path.isfile(executable):
        return _gap(f"{kind.upper()} text extractor is unavailable", "extractor_unavailable")
    with _private_directory() as temporary:
        extension = ".pdf" if kind == "pdf" else _image_extension(suffix)
        try:
            source = _snapshot_input(descriptor, temporary / f"source{extension}")
        except _InputTooLargeError:
            raise
        except OSError:
            return _gap("local artifact could not be copied for extraction", "artifact_unreadable")
        command = (
            (executable, "-enc", "UTF-8", str(source), "-")
            if kind == "pdf"
            else (executable, str(source), "stdout", "-l", "eng")
        )
        try:
            outcome = _run_bounded(command, temporary)
        except OSError:
            return _gap(f"{kind.upper()} text extractor is unavailable", "extractor_unavailable")
    if outcome.timed_out:
        return _gap(
            f"{kind.upper()} text extraction timed out after {EXTRACTION_TIMEOUT_SECONDS} seconds",
            "extraction_timed_out",
        )
    errors = outcome.errors.decode("utf-8", errors="replace").casefold()
    if outcome.returncode != 0:
        if "encrypt" in errors or "password" in errors:
            return _gap(f"{kind.upper()} document is encrypted", "encrypted_document")
        return _gap(f"{kind.upper()} text extraction failed", "extraction_failed")
    text, replaced = _decode(outcome.output)
    if not text.strip():
        return _gap(f"{kind.upper()} document has no extractable text", "no_extractable_text")
    return _decoded_result(text, replaced=replaced, limited=outcome.output_limited)


def _image_extension(suffix: str) -> str:
    clean = suffix.casefold()
    image_extensions = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
    return clean if clean in image_extensions else ".img"


@dataclass(frozen=True)
class _ProcessOutcome:
    errors: bytes
    output: bytes
    output_limited: bool
    returncode: int
    timed_out: bool


def _run_bounded(command: tuple[str, ...], temporary: Path) -> _ProcessOutcome:
    output_path = temporary / "output.txt"
    errors_path = temporary / "errors.txt"
    output_descriptor = _create_private_file(output_path)
    errors_descriptor = _create_private_file(errors_path)
    output_limited = False
    timed_out = False
    returncode = -1
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=temporary,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            },
            close_fds=True,
        ) as process:
            assert process.stdout is not None
            assert process.stderr is not None
            selector = selectors.DefaultSelector()
            selector.register(
                process.stdout,
                selectors.EVENT_READ,
                (output_descriptor, MAX_TEXT_BYTES),
            )
            selector.register(
                process.stderr,
                selectors.EVENT_READ,
                (errors_descriptor, _MAX_ERROR_BYTES),
            )
            written = {output_descriptor: 0, errors_descriptor: 0}
            deadline = time.monotonic() + EXTRACTION_TIMEOUT_SECONDS
            stopped = False
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        process.kill()
                        stopped = True
                        break
                    for key, _ in selector.select(remaining):
                        stream = cast(io.BufferedReader, key.fileobj)
                        block = os.read(stream.fileno(), _CHUNK_BYTES)
                        if not block:
                            selector.unregister(stream)
                            continue
                        destination, limit = cast(tuple[int, int], key.data)
                        permitted = max(0, limit - written[destination])
                        if permitted:
                            _write_all(destination, block[:permitted])
                            written[destination] += min(len(block), permitted)
                        if len(block) > permitted:
                            if destination == output_descriptor:
                                output_limited = True
                            process.kill()
                            stopped = True
                            break
                    if stopped:
                        break
                if stopped:
                    process.wait(timeout=1)
                else:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait(timeout=1)
            finally:
                selector.close()
            returncode = process.returncode if process.returncode is not None else -1
    finally:
        os.close(output_descriptor)
        os.close(errors_descriptor)
    return _ProcessOutcome(
        errors=_read_private_file(errors_path, _MAX_ERROR_BYTES),
        output=_read_private_file(output_path, MAX_TEXT_BYTES),
        output_limited=output_limited,
        returncode=returncode,
        timed_out=timed_out,
    )


class _PrivateDirectory:
    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="seld-app-text-"))
        if os.name != "nt":
            self.path.chmod(0o700)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        del exception_type, exception, traceback
        shutil.rmtree(self.path, ignore_errors=True)


def _private_directory() -> _PrivateDirectory:
    return _PrivateDirectory()


def _create_private_file(path: Path) -> int:
    descriptor = os.open(path, _WRITE_FLAGS, 0o600)
    if os.name != "nt":
        fchmod = getattr(os, "fchmod", None)
        if not callable(fchmod):
            os.close(descriptor)
            raise OSError("private file permissions cannot be set on this platform")
        try:
            fchmod(descriptor, 0o600)
        except OSError:
            os.close(descriptor)
            raise
    return descriptor


def _snapshot_input(source_descriptor: int, destination: Path) -> Path:
    destination_descriptor = _create_private_file(destination)
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        copied = 0
        while block := os.read(source_descriptor, _CHUNK_BYTES):
            copied += len(block)
            if copied > MAX_INPUT_BYTES:
                raise _InputTooLargeError
            _write_all(destination_descriptor, block)
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)
    return destination


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("private extraction write failed")
        view = view[written:]


def _read_private_file(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, _PATH_FLAGS)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            return b""
        return _read_descriptor(descriptor, maximum)
    finally:
        os.close(descriptor)


def _read_descriptor(descriptor: int, maximum: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    return _read_stream(_descriptor_chunks(descriptor), maximum)


def _descriptor_chunks(descriptor: int) -> Iterator[bytes]:
    while block := os.read(descriptor, _CHUNK_BYTES):
        yield block


def _read_stream(stream: Iterator[bytes] | BinaryIO, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    iterator = _file_chunks(cast(BinaryIO, stream)) if hasattr(stream, "read") else stream
    for block in iterator:
        remaining = maximum - total
        if remaining <= 0:
            break
        chunks.append(block[:remaining])
        total += min(len(block), remaining)
        if len(block) > remaining:
            break
    return b"".join(chunks)


def _file_chunks(stream: BinaryIO) -> Iterator[bytes]:
    while block := stream.read(_CHUNK_BYTES):
        yield block


def _decode(source: bytes) -> tuple[str, bool]:
    encoding = "utf-8-sig"
    if source.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    text = source.decode(encoding, errors="replace")
    return text, "\ufffd" in text


def _trim_text(text: str) -> tuple[str, bool]:
    compact = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
    encoded = compact.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return compact, False
    trimmed = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return trimmed, True


def _result_from_text(text: str, *, limited: bool) -> ExtractionResult:
    trimmed, trimmed_limited = _trim_text(text)
    if not trimmed:
        return _gap("document has no extractable text", "no_extractable_text")
    if limited or trimmed_limited:
        return _partial(
            trimmed,
            f"extracted text was truncated at {MAX_TEXT_BYTES // (1024 * 1024)} MiB",
            "text_truncated",
        )
    return ExtractionResult(text=trimmed, status="ok", reason=None, omissions=())


def _decoded_result(text: str, *, replaced: bool, limited: bool = False) -> ExtractionResult:
    result = _result_from_text(text, limited=limited)
    if replaced and result.status == "ok":
        return _partial(
            result.text,
            "some document bytes could not be decoded",
            "undecodable_bytes",
        )
    return result


def _partial(text: str, reason: str, omission: str) -> ExtractionResult:
    return ExtractionResult(text=text, status="partial", reason=reason, omissions=(omission,))


def _gap(reason: str, omission: str) -> ExtractionResult:
    return ExtractionResult(text="", status="gap", reason=reason, omissions=(omission,))

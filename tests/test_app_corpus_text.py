from __future__ import annotations

import zipfile
from pathlib import Path

from pytest import MonkeyPatch

from continuity_kernel import app_corpus_text


def test_extract_text_handles_local_document_content_and_names_coverage_gaps(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    plain = tmp_path / "note.txt"
    plain.write_text("Project cedar is ready.", encoding="utf-8")
    html = tmp_path / "note.html"
    html.write_text("<p>Visible plan</p><script>secret()</script>", encoding="utf-8")
    payload = tmp_path / "note.json"
    payload.write_text('{"project":"cedar","ready":true}', encoding="utf-8")
    table = tmp_path / "note.csv"
    table.write_text("name,status\ncedar,ready\n", encoding="utf-8")
    document = tmp_path / "note.docx"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:t>DOCX plan</w:t>'
            "</w:r></w:p></w:body></w:document>",
        )

    assert app_corpus_text.extract_text(plain).text == "Project cedar is ready."
    assert app_corpus_text.extract_text(html).text == "Visible plan"
    assert app_corpus_text.extract_text(payload).text == "project: cedar\nready: true"
    assert app_corpus_text.extract_text(table).text == "name | status\ncedar | ready"
    assert app_corpus_text.extract_text(document).text == "DOCX plan"

    linked = tmp_path / "linked.txt"
    linked.symlink_to(plain)
    assert app_corpus_text.extract_text(linked).status == "gap"
    assert app_corpus_text.extract_text(linked).omissions == ("unsafe_file",)

    unsupported = tmp_path / "note.bin"
    unsupported.write_bytes(b"data")
    assert app_corpus_text.extract_text(unsupported).omissions == ("unsupported_document_type",)

    monkeypatch.setattr(app_corpus_text, "MAX_INPUT_BYTES", 32)
    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * 33)
    assert app_corpus_text.extract_text(oversized).omissions == ("input_too_large",)

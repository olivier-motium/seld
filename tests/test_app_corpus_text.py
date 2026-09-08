from __future__ import annotations

import zipfile
from pathlib import Path

from pytest import MonkeyPatch

import continuity_kernel.app_corpus_text as app_corpus_text


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


def test_extract_text_reads_xlsx_worksheets_and_cached_formula_results(tmp_path: Path) -> None:
    document = tmp_path / "plan.xlsx"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="urn:spreadsheet" xmlns:r="urn:relationships"><sheets>'
            '<sheet name="Planning" sheetId="1" r:id="rId1"/>'
            '<sheet name="Archive" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="urn:package-relationships">'
            '<Relationship Id="rId1" Type="'
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="'
            'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet2.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="urn:spreadsheet"><si><t>Task</t></si></sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="urn:spreadsheet"><sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1"><v>2</v></c>'
            '<c r="C1"><f>SUM(B1:B2)</f><v>5</v></c>'
            '<c r="D1"><f>NOW()</f></c>'
            '<c r="E1" t="inlineStr"><is><t>Inline note</t></is></c>'
            '<c r="F1" t="b"><v>1</v></c>'
            "</row></sheetData></worksheet>",
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            '<worksheet xmlns="urn:spreadsheet"><sheetData><row r="1">'
            '<c r="A1" t="str"><v>archived</v></c>'
            "</row></sheetData></worksheet>",
        )

    result = app_corpus_text.extract_text(
        document,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert result.status == "ok"
    assert result.text == (
        "Cell values are stored values; number and date display formats are not applied.\n"
        "Worksheet: Planning\n"
        "A1: Task | B1: 2 | C1: formula =SUM(B1:B2); cached result: 5 | "
        "D1: formula =NOW(); cached result unavailable | E1: Inline note | F1: TRUE\n"
        "Worksheet: Archive\n"
        "A1: archived"
    )

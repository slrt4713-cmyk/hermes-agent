import json
import zipfile

from gateway.document_extraction import build_inline_document_text


def _write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def test_text_document_injects_utf8_content(tmp_path):
    path = tmp_path / "notes.csv"
    raw = b"name,value\nrevenue,835000\n"
    path.write_bytes(raw)

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".csv",
        display_name="notes.csv",
        raw_bytes=raw,
    )

    assert "[Content of notes.csv]" in result
    assert "revenue,835000" in result


def test_large_text_document_is_not_injected(tmp_path):
    path = tmp_path / "large.txt"
    raw = b"x" * (101 * 1024)
    path.write_bytes(raw)

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".txt",
        display_name="large.txt",
        raw_bytes=raw,
    )

    assert result == ""


def test_invalid_text_document_is_not_injected(tmp_path):
    path = tmp_path / "binary.txt"
    raw = bytes(range(128, 256))
    path.write_bytes(raw)

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".txt",
        display_name="binary.txt",
        raw_bytes=raw,
    )

    assert result == ""


def test_docx_document_extracts_paragraph_text(tmp_path):
    path = tmp_path / "proposal.docx"
    _write_zip(
        path,
        {
            "word/document.xml": """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                    <w:body>
                        <w:p><w:r><w:t>Executive summary</w:t></w:r></w:p>
                        <w:p><w:r><w:t>Simon setup details</w:t></w:r></w:p>
                    </w:body>
                </w:document>
            """,
        },
    )

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".docx",
        display_name="proposal.docx",
    )

    assert "Executive summary" in result
    assert "Simon setup details" in result


def test_xlsx_document_extracts_shared_strings(tmp_path):
    path = tmp_path / "model.xlsx"
    _write_zip(
        path,
        {
            "xl/sharedStrings.xml": """
                <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                    <si><t>Metric</t></si>
                    <si><t>Revenue</t></si>
                </sst>
            """,
            "xl/worksheets/sheet1.xml": """
                <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                    <sheetData>
                        <row><c t="s"><v>0</v></c><c><v>2026</v></c></row>
                        <row><c t="s"><v>1</v></c><c><v>835000</v></c></row>
                    </sheetData>
                </worksheet>
            """,
        },
    )

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".xlsx",
        display_name="model.xlsx",
    )

    assert "Sheet 1" in result
    assert "Metric\t2026" in result
    assert "Revenue\t835000" in result


def test_pptx_document_extracts_slide_text(tmp_path):
    path = tmp_path / "deck.pptx"
    _write_zip(
        path,
        {
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                    <p:cSld><p:spTree><p:sp><p:txBody>
                        <a:p><a:r><a:t>Launch plan</a:t></a:r></a:p>
                    </p:txBody></p:sp></p:spTree></p:cSld>
                </p:sld>
            """,
        },
    )

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".pptx",
        display_name="deck.pptx",
    )

    assert "Slide 1" in result
    assert "Launch plan" in result


def test_ipynb_document_extracts_cell_sources(tmp_path):
    path = tmp_path / "analysis.ipynb"
    raw = json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "source": ["# Findings\n", "Context"]},
                {"cell_type": "code", "source": "print('ok')"},
            ]
        }
    ).encode()
    path.write_bytes(raw)

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".ipynb",
        display_name="analysis.ipynb",
        raw_bytes=raw,
    )

    assert "Cell 1 (markdown)" in result
    assert "# Findings" in result
    assert "print('ok')" in result


def test_zip_document_is_not_extracted(tmp_path):
    path = tmp_path / "archive.zip"
    _write_zip(path, {"payload.txt": "do not auto extract"})

    result = build_inline_document_text(
        cached_path=str(path),
        ext=".zip",
        display_name="archive.zip",
    )

    assert result == ""

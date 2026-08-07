#!/usr/bin/env python3
"""
Tests for structured-document extraction in the read_file tool.

Covers .ipynb / .docx / .xlsx extraction (ported from Kilo-Org/kilocode
#10733, #10737, #10740) and the read_file_tool integration: pagination,
line-numbering, graceful fallback on malformed input, and hidden-sheet
omission.

Run with:  python -m pytest tests/tools/test_read_extract.py -v
"""

import json
import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools.read_extract import (
    ExtractionError,
    extract_document_text,
    is_extractable_document,
)
from tools.file_tools import read_file_tool


# ---------------------------------------------------------------------------
# Fixture builders — construct minimal valid OOXML / notebook files.
# ---------------------------------------------------------------------------

def _write_notebook(path, cells, nbformat=4):
    nb = {"cells": cells, "metadata": {}, "nbformat": nbformat, "nbformat_minor": 5}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(nb, fh)


def _write_docx(path, document_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)


def _write_xlsx(path, *, workbook, rels, shared, sheets):
    """sheets: dict of part-name -> xml string."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", shared)
        for part, xml in sheets.items():
            z.writestr(part, xml)


def _write_pptx(path, slides, *, order=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for number, xml in slides.items():
            z.writestr(f"ppt/slides/slide{number}.xml", xml)
        if order is not None:
            slide_ids = "".join(
                f'<p:sldId id="{256 + index}" r:id="rId{number}"/>'
                for index, number in enumerate(order)
            )
            z.writestr(
                "ppt/presentation.xml",
                f'<p:presentation xmlns:p="{_NS_P}" xmlns:r="{_NS_R}">'
                f"<p:sldIdLst>{slide_ids}</p:sldIdLst></p:presentation>",
            )
            relationships = "".join(
                f'<Relationship Id="rId{number}" Type="{_NS_R}/slide" '
                f'Target="slides/slide{number}.xml"/>'
                for number in slides
            )
            z.writestr(
                "ppt/_rels/presentation.xml.rels",
                f'<Relationships xmlns="{_NS_PKG_REL}">{relationships}</Relationships>',
            )


_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


# ---------------------------------------------------------------------------
# is_extractable_document
# ---------------------------------------------------------------------------

class TestIsExtractable(unittest.TestCase):
    def test_recognized_extensions(self):
        self.assertTrue(is_extractable_document("a.ipynb"))
        self.assertTrue(is_extractable_document("/x/B.DOCX"))
        self.assertTrue(is_extractable_document("report.xlsx"))
        self.assertTrue(is_extractable_document("slides.pptx"))
        self.assertTrue(is_extractable_document("report.pdf"))

    def test_unrecognized_extensions(self):
        self.assertFalse(is_extractable_document("a.py"))
        self.assertFalse(is_extractable_document("a.txt"))


# ---------------------------------------------------------------------------
# Notebooks (.ipynb) — #10733
# ---------------------------------------------------------------------------

class TestNotebookExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_nb_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_markdown_and_code_in_order(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": ["# Title\n", "para"]},
            {"cell_type": "code", "source": "x = 1\nprint(x)",
             "outputs": [{"output_type": "stream", "text": ["1\n"]}],
             "execution_count": 1},
        ])
        text = extract_document_text(p)
        self.assertIn("# Title", text)
        self.assertIn("print(x)", text)
        # Output payloads must NOT leak into the extracted text.
        self.assertNotIn("output_type", text)
        self.assertNotIn("execution_count", text)
        # Order preserved: markdown before code.
        self.assertLess(text.index("Title"), text.index("print(x)"))


    def test_empty_cells_raises(self):
        p = os.path.join(self.tmp, "empty.ipynb")
        _write_notebook(p, [])
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_input_size_limit_is_applied_before_json_parsing(self):
        p = os.path.join(self.tmp, "large.ipynb")
        _write_notebook(p, [{"cell_type": "code", "source": "print(1)"}])
        with patch("tools.read_extract._MAX_DOCUMENT_INPUT_BYTES", 1):
            with self.assertRaisesRegex(ExtractionError, "too large"):
                extract_document_text(p)


# ---------------------------------------------------------------------------
# Word documents (.docx) — #10737
# ---------------------------------------------------------------------------

class TestDocxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_docx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, body):
        return (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                f'<w:body>{body}</w:body></w:document>')

    def test_paragraphs_and_runs(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, self._doc(
            '<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>World</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Second</w:t></w:r></w:p>'))
        text = extract_document_text(p)
        self.assertIn("Hello World", text)
        self.assertIn("Second", text)


    def test_missing_document_xml_raises(self):
        p = os.path.join(self.tmp, "nodoc.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("other.xml", "<x/>")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_oversized_document_xml_is_rejected(self):
        p = os.path.join(self.tmp, "large.docx")
        _write_docx(p, self._doc("<w:p><w:r><w:t>content</w:t></w:r></w:p>"))
        with patch("tools.read_extract._MAX_XML_PART_BYTES", 1), patch.object(
            zipfile.ZipFile,
            "open",
            side_effect=AssertionError("oversized member must not be decompressed"),
        ) as open_member:
            with self.assertRaisesRegex(ExtractionError, "too large"):
                extract_document_text(p)
        open_member.assert_not_called()


# ---------------------------------------------------------------------------
# Excel workbooks (.xlsx) — #10740
# ---------------------------------------------------------------------------

class TestXlsxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_xlsx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, path, *, include_hidden=True):
        r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        hidden_sheet = (f'<sheet name="Hidden" sheetId="2" state="hidden" '
                        f'xmlns:r="{r}" r:id="rId2"/>') if include_hidden else ""
        workbook = (
            f'<workbook xmlns="{_NS_S}" xmlns:r="{r}"><sheets>'
            f'<sheet name="Data" sheetId="1" r:id="rId1"/>{hidden_sheet}'
            f'</sheets></workbook>')
        rels = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="x"/>'
            '</Relationships>')
        shared = (f'<sst xmlns="{_NS_S}"><si><t>Name</t></si><si><t>Score</t></si>'
                  f'<si><t>Alice</t></si></sst>')
        sheet1 = (
            f'<worksheet xmlns="{_NS_S}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>95</v></c></row>'
            '</sheetData></worksheet>')
        sheet2 = (f'<worksheet xmlns="{_NS_S}"><sheetData>'
                  '<row r="1"><c r="A1" t="str"><v>SECRETDATA</v></c></row>'
                  '</sheetData></worksheet>')
        _write_xlsx(path, workbook=workbook, rels=rels, shared=shared,
                    sheets={"xl/worksheets/sheet1.xml": sheet1,
                            "xl/worksheets/sheet2.xml": sheet2})

    def test_visible_sheet_content(self):
        p = os.path.join(self.tmp, "wb.xlsx")
        self._build(p)
        text = extract_document_text(p)
        self.assertIn("Data", text)        # sheet label
        self.assertIn("Name\tScore", text)  # shared-string header row
        self.assertIn("Alice\t95", text)    # string + numeric cells


    def test_not_a_zip_raises(self):
        p = os.path.join(self.tmp, "bad.xlsx")
        with open(p, "wb") as fh:
            fh.write(b"nope")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_oversized_worksheet_is_rejected(self):
        p = os.path.join(self.tmp, "large.xlsx")
        self._build(p, include_hidden=False)
        with patch("tools.read_extract._MAX_XML_PART_BYTES", 100):
            with self.assertRaisesRegex(ExtractionError, "too large"):
                extract_document_text(p)

    def test_rendered_output_stops_at_text_budget(self):
        p = os.path.join(self.tmp, "bounded.xlsx")
        self._build(p, include_hidden=False)

        with patch("tools.read_extract._MAX_EXTRACTED_TEXT_BYTES", 32):
            text = extract_document_text(p)

        self.assertIn("truncated", text)
        self.assertLess(len(text.encode("utf-8")), 128)


# ---------------------------------------------------------------------------
# PowerPoint presentations (.pptx)
# ---------------------------------------------------------------------------

class TestPptxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_pptx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _slide(*texts):
        runs = "".join(f"<a:r><a:t>{text}</a:t></a:r>" for text in texts)
        return f'<p:sld xmlns:a="{_NS_A}" xmlns:p="urn:p"><p:cSld>{runs}</p:cSld></p:sld>'

    def test_slides_fall_back_to_numeric_order_without_presentation_metadata(self):
        p = os.path.join(self.tmp, "deck.pptx")
        _write_pptx(p, {10: self._slide("Tenth"), 2: self._slide("Second")})

        text = extract_document_text(p)

        self.assertLess(text.index("Second"), text.index("Tenth"))
        self.assertIn("# -- Slide 1 --", text)
        self.assertIn("# -- Slide 2 --", text)

    def test_presentation_metadata_controls_slide_order(self):
        p = os.path.join(self.tmp, "reordered.pptx")
        _write_pptx(
            p,
            {2: self._slide("Second file"), 10: self._slide("Tenth file")},
            order=[10, 2],
        )

        text = extract_document_text(p)

        self.assertLess(text.index("Tenth file"), text.index("Second file"))

    def test_oversized_slide_is_rejected_before_decompression(self):
        p = os.path.join(self.tmp, "large.pptx")
        _write_pptx(p, {1: self._slide("content")})

        with patch("tools.read_extract._MAX_PPTX_SLIDE_BYTES", 1):
            with self.assertRaisesRegex(ExtractionError, "oversized slide"):
                extract_document_text(p)

    def test_malformed_presentation_raises(self):
        p = os.path.join(self.tmp, "bad.pptx")
        with open(p, "wb") as fh:
            fh.write(b"not a zip")

        with self.assertRaises(ExtractionError):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# PDF documents (.pdf)
# ---------------------------------------------------------------------------

class TestPdfExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_pdf_")
        self.path = os.path.join(self.tmp, "report.pdf")
        with open(self.path, "wb") as fh:
            fh.write(b"%PDF-1.4")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    class _FakeProcess:
        def __init__(self, output=b"", returncode=0):
            self.stdout = io.BytesIO(output)
            self._final_returncode = returncode
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self):
            if self.returncode is None:
                self.returncode = self._final_returncode
            return self.returncode

    def test_pdftotext_output_is_returned(self):
        process = self._FakeProcess(b"Extracted report")

        with patch("tools.read_extract.shutil.which", return_value="/usr/bin/pdftotext"), patch(
            "tools.read_extract.subprocess.Popen", return_value=process
        ) as popen:
            text = extract_document_text(self.path)

        self.assertEqual(text, "Extracted report\n")
        self.assertEqual(popen.call_args.args[0][-2], self.path)
        self.assertEqual(popen.call_args.args[0][-1], "-")
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_output_is_bounded_while_process_is_running(self):
        process = self._FakeProcess(b"abcdefgh")

        with patch("tools.read_extract._MAX_EXTRACTED_TEXT_BYTES", 4), patch(
            "tools.read_extract.shutil.which", return_value="/usr/bin/pdftotext"
        ), patch("tools.read_extract.subprocess.Popen", return_value=process):
            text = extract_document_text(self.path)

        self.assertTrue(process.killed)
        self.assertTrue(text.startswith("abcd"))
        self.assertIn("truncated", text)

    def test_timeout_is_reported_as_extraction_error(self):
        process = self._FakeProcess()

        class ImmediateTimer:
            daemon = False

            def __init__(self, _seconds, callback):
                self.callback = callback

            def start(self):
                self.callback()

            def cancel(self):
                pass

        with patch("tools.read_extract.shutil.which", return_value="/usr/bin/pdftotext"), patch(
            "tools.read_extract.subprocess.Popen", return_value=process
        ), patch("tools.read_extract.threading.Timer", ImmediateTimer):
            with self.assertRaisesRegex(ExtractionError, "timed out"):
                extract_document_text(self.path)

        self.assertTrue(process.killed)

    def test_nonzero_exit_is_reported(self):
        process = self._FakeProcess(returncode=2)
        with patch("tools.read_extract.shutil.which", return_value="/usr/bin/pdftotext"), patch(
            "tools.read_extract.subprocess.Popen", return_value=process
        ):
            with self.assertRaisesRegex(ExtractionError, "exit code 2"):
                extract_document_text(self.path)

    def test_missing_pdftotext_is_reported(self):
        with patch("tools.read_extract.shutil.which", return_value=None):
            with self.assertRaisesRegex(ExtractionError, "not installed"):
                extract_document_text(self.path)


# ---------------------------------------------------------------------------
# read_file_tool integration
# ---------------------------------------------------------------------------

class TestReadFileToolIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_int_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notebook_read_is_line_numbered(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": "# H"},
            {"cell_type": "code", "source": "print(1)"},
        ])
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("1|", res["content"])  # line-number gutter
        self.assertIn("print(1)", res["content"])


    def test_corrupt_docx_falls_through_to_binary_guard(self):
        p = os.path.join(self.tmp, "bad.docx")
        with open(p, "wb") as fh:
            fh.write(b"not a zip")
        res = json.loads(read_file_tool(p))
        # Should NOT crash; falls through to the binary-extension guard.
        self.assertIn("error", res)
        self.assertIn("binary", res["error"].lower())

    def test_docx_read_extracts(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                        '<w:body><w:p><w:r><w:t>Report body</w:t></w:r></w:p>'
                        '</w:body></w:document>'))
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("Report body", res["content"])


if __name__ == "__main__":
    unittest.main()

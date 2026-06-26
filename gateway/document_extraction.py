"""Inline text extraction for user-uploaded gateway documents."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

from gateway.platforms.base import _TEXT_INJECT_EXTENSIONS

logger = logging.getLogger(__name__)

MAX_INLINE_DOCUMENT_TEXT_BYTES = 100 * 1024
MAX_ZIP_ENTRY_BYTES = 5 * 1024 * 1024
PDF_EXTRACTION_TIMEOUT_SECONDS = 15

INLINE_DOCUMENT_EXTENSIONS = _TEXT_INJECT_EXTENSIONS | {
    ".docx",
    ".xlsx",
    ".pptx",
    ".pdf",
    ".ipynb",
}

_XML_TEXT_TAGS = {
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "{http://schemas.openxmlformats.org/drawingml/2006/main}t",
    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t",
}


def safe_document_display_name(filename: str | None, fallback: str = "document") -> str:
    leaf_name = Path(filename or fallback).name
    cleaned = leaf_name.replace("\x00", "").strip()
    cleaned = re.sub(r"[^\w.\- ]", "_", cleaned)
    return cleaned or fallback


def build_inline_document_text(
    *,
    cached_path: str,
    ext: str,
    display_name: str | None = None,
    raw_bytes: bytes | None = None,
) -> str:
    """Return formatted inline text for supported document types, or an empty string."""
    normalized_ext = (ext or Path(cached_path).suffix).lower()
    if normalized_ext not in INLINE_DOCUMENT_EXTENSIONS:
        return ""

    try:
        extracted = _extract_document_text(
            Path(cached_path),
            normalized_ext,
            raw_bytes=raw_bytes,
        )
    except Exception:
        logger.warning(
            "Failed to extract inline document text from %s",
            cached_path,
            exc_info=True,
        )
        return ""

    extracted = extracted.strip()
    if not extracted:
        return ""

    if len(extracted.encode("utf-8")) > MAX_INLINE_DOCUMENT_TEXT_BYTES:
        extracted = _truncate_utf8(extracted, MAX_INLINE_DOCUMENT_TEXT_BYTES)
        extracted = f"{extracted}\n\n[Content truncated at 100 KB]"

    safe_name = safe_document_display_name(display_name or Path(cached_path).name)
    return f"[Content of {safe_name}]:\n{extracted}"


def _extract_document_text(
    path: Path,
    ext: str,
    *,
    raw_bytes: bytes | None = None,
) -> str:
    if ext in _TEXT_INJECT_EXTENSIONS:
        data = raw_bytes if raw_bytes is not None else path.read_bytes()
        if len(data) > MAX_INLINE_DOCUMENT_TEXT_BYTES:
            return ""
        return data.decode("utf-8")
    if ext == ".docx":
        return _extract_docx_text(path)
    if ext == ".xlsx":
        return _extract_xlsx_text(path)
    if ext == ".pptx":
        return _extract_pptx_text(path)
    if ext == ".pdf":
        return _extract_pdf_text(path)
    if ext == ".ipynb":
        return _extract_ipynb_text(path, raw_bytes=raw_bytes)
    return ""


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _read_zip_entry(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_ZIP_ENTRY_BYTES:
        raise ValueError(f"Office XML entry too large: {name}")
    return zf.read(info)


def _parse_xml_entry(zf: zipfile.ZipFile, name: str) -> ElementTree.Element:
    data = _read_zip_entry(zf, name)
    return ElementTree.fromstring(data)


def _iter_xml_text(root: ElementTree.Element) -> Iterable[str]:
    for element in root.iter():
        if element.tag in _XML_TEXT_TAGS and element.text:
            yield element.text


def _extract_docx_text(path: Path) -> str:
    lines: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            if not (
                name == "word/document.xml"
                or name.startswith("word/header")
                or name.startswith("word/footer")
            ):
                continue
            root = _parse_xml_entry(zf, name)
            paragraphs = root.findall(
                ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
            )
            for paragraph in paragraphs:
                text = "".join(_iter_xml_text(paragraph)).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = _parse_xml_entry(zf, "xl/sharedStrings.xml")
    strings: list[str] = []
    for item in root.findall(
        ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"
    ):
        strings.append("".join(_iter_xml_text(item)).strip())
    return strings


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
    if cell_type == "s" and value is not None and value.text:
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "inlineStr":
        return "".join(_iter_xml_text(cell)).strip()
    return (value.text or "").strip() if value is not None else ""


def _extract_xlsx_text(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared = _shared_strings(zf)
        worksheet_names = sorted(
            name
            for name in names
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )
        for index, name in enumerate(worksheet_names, start=1):
            root = _parse_xml_entry(zf, name)
            rows: list[str] = []
            for row in root.findall(
                ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"
            ):
                values = [
                    _cell_value(cell, shared)
                    for cell in row.findall(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"
                    )
                ]
                compact_values = [value for value in values if value]
                if compact_values:
                    rows.append("\t".join(compact_values))
            if rows:
                sections.append(f"Sheet {index}\n" + "\n".join(rows))
    return "\n\n".join(sections)


def _slide_sort_key(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _extract_pptx_text(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slide_names = sorted(
            (
                name
                for name in zf.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=_slide_sort_key,
        )
        for index, name in enumerate(slide_names, start=1):
            root = _parse_xml_entry(zf, name)
            lines = [text.strip() for text in _iter_xml_text(root) if text.strip()]
            if lines:
                sections.append(f"Slide {index}\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _extract_pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-q", "-nopgbrk", str(path), "-"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=PDF_EXTRACTION_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        logger.info("pdftotext is not installed, skipping PDF inline extraction")
        return ""
    except subprocess.TimeoutExpired:
        logger.warning("pdftotext timed out for %s", path)
        return ""

    if result.returncode != 0:
        logger.warning("pdftotext failed for %s: %s", path, result.stderr.strip())
        return ""
    return result.stdout


def _extract_ipynb_text(path: Path, *, raw_bytes: bytes | None = None) -> str:
    data = raw_bytes if raw_bytes is not None else path.read_bytes()
    if len(data) > MAX_INLINE_DOCUMENT_TEXT_BYTES:
        return ""
    notebook = json.loads(data.decode("utf-8"))
    cells = notebook.get("cells", [])
    sections: list[str] = []
    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        cell_type = cell.get("cell_type", "cell")
        source = cell.get("source", "")
        if isinstance(source, list):
            text = "".join(str(part) for part in source).strip()
        else:
            text = str(source).strip()
        if text:
            sections.append(f"Cell {index} ({cell_type})\n{text}")
    return "\n\n".join(sections)

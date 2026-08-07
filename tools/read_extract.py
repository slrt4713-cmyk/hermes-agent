"""Stdlib document-to-text extraction for ``read_file``.

Supports Jupyter notebooks, DOCX, XLSX, PPTX, and PDF. Office formats use only
the standard library; PDF extraction requires ``pdftotext``. Malformed or unsafe
documents raise :class:`ExtractionError` so callers can fall back to normal
text/binary handling.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

__all__ = ["EXTRACTABLE_EXTENSIONS", "ExtractionError", "extract_document_text", "is_extractable_document"]

EXTRACTABLE_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx", ".pptx", ".pdf"})
_MAX_DOCUMENT_INPUT_BYTES = 50 * 1024 * 1024
_MAX_EXTRACTED_TEXT_BYTES = 5 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_XML_PART_BYTES = 16 * 1024 * 1024
_MAX_SHARED_STRINGS_BYTES = 32 * 1024 * 1024
_MAX_SELECTED_XML_BYTES = 64 * 1024 * 1024
_MAX_PPTX_SLIDES = 2_000
_MAX_PPTX_SLIDE_BYTES = 5 * 1024 * 1024
_PDF_EXTRACTION_TIMEOUT_SECONDS = 30
_MAX_XLSX_SHEETS = 1_000
_MAX_XLSX_ROWS_PER_SHEET = 5000
_MAX_XLSX_COLS = 256

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractionError(Exception):
    """Raised when a supported-looking document cannot be rendered as text."""


def _extension(path: str) -> str:
    ext = Path(path).suffix.lower()
    return ext if ext in EXTRACTABLE_EXTENSIONS else ""


def is_extractable_document(path: str) -> bool:
    return bool(_extension(path))


def extract_document_text(path: str) -> str:
    ext = _extension(path)
    if not ext:
        raise ExtractionError(f"Unsupported document type: {path!r}")
    _validate_document_size(path, ext.removeprefix(".").upper())
    if ext == ".ipynb":
        return _extract_notebook(path)
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".xlsx":
        return _extract_xlsx(path)
    if ext == ".pptx":
        return _extract_pptx(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    raise AssertionError(f"Unhandled extractable document type: {ext}")


def _validate_document_size(path: str, label: str) -> None:
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > _MAX_DOCUMENT_INPUT_BYTES:
        raise ExtractionError(
            f"{label} is too large to extract safely: {size} bytes"
        )


def _decode_bounded_text(data: bytes) -> str:
    truncated = len(data) > _MAX_EXTRACTED_TEXT_BYTES
    text = data[:_MAX_EXTRACTED_TEXT_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if truncated:
        text += "\n\n[... extracted text truncated ...]"
    return text + "\n"


def _append_bounded_utf8(buffer: bytearray, value: str) -> bool:
    remaining = _MAX_EXTRACTED_TEXT_BYTES + 1 - len(buffer)
    if remaining <= 0:
        return False
    encoded = value.encode("utf-8")
    buffer.extend(encoded[:remaining])
    return len(encoded) <= remaining and len(buffer) <= _MAX_EXTRACTED_TEXT_BYTES


def _validate_archive(zf: zipfile.ZipFile, label: str) -> list[zipfile.ZipInfo]:
    members = zf.infolist()
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise ExtractionError(f"{label} contains too many archive members")
    if len({info.filename for info in members}) != len(members):
        raise ExtractionError(f"{label} contains duplicate archive members")
    return members


def _read_zip_member(
    zf: zipfile.ZipFile,
    member: str | zipfile.ZipInfo,
    *,
    label: str,
    max_bytes: int | None = None,
) -> bytes:
    limit = _MAX_XML_PART_BYTES if max_bytes is None else max_bytes
    try:
        info = zf.getinfo(member) if isinstance(member, str) else member
    except KeyError as exc:
        raise ExtractionError(f"Missing {member}") from exc
    if info.file_size > limit:
        raise ExtractionError(
            f"{label} part is too large to extract safely: {info.filename}"
        )
    try:
        with zf.open(info) as source:
            data = source.read(limit + 1)
    except (
        EOFError,
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ) as exc:
        raise ExtractionError(f"Could not read {label} part {info.filename}: {exc}") from exc
    if len(data) > limit:
        raise ExtractionError(
            f"{label} part exceeds the extraction limit: {info.filename}"
        )
    return data


def _validate_selected_size(
    members: list[zipfile.ZipInfo], label: str, max_bytes: int = _MAX_SELECTED_XML_BYTES
) -> None:
    total = sum(info.file_size for info in members)
    if total > max_bytes:
        raise ExtractionError(f"{label} XML content is too large to extract safely")


def _source_text(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(item for item in source if isinstance(item, str))
    return ""


def _extract_notebook(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nb = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"Not a valid notebook: {exc}") from exc
    if not isinstance(nb, dict):
        raise ExtractionError("Notebook root is not an object")

    cells = nb.get("cells")
    if not isinstance(cells, list):
        cells = [
            cell
            for ws in nb.get("worksheets", [])
            if isinstance(ws, dict)
            for cell in ws.get("cells", [])
        ]
    if not cells:
        raise ExtractionError("Notebook contains no cells")

    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}
    out: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        typ = cell.get("cell_type")
        if typ not in labels:
            continue
        counts[typ] += 1
        suffix = f" {counts[typ]}" if typ != "raw" else ""
        out.extend((f"# ── {labels[typ]} cell{suffix} ──", _source_text(cell.get("source", "")).rstrip("\n"), ""))
    if not out:
        raise ExtractionError("Notebook contains no readable cells")
    return _decode_bounded_text("\n".join(out).rstrip("\n").encode("utf-8"))


def _zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(_read_zip_member(zf, name, label="document"))
    except ET.ParseError as exc:
        raise ExtractionError(f"Malformed XML in {name}: {exc}") from exc


def _extract_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            _validate_archive(zf, "DOCX")
            root = _zip_xml(zf, "word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid DOCX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    w = f"{{{_NS_W}}}"
    lines: list[str] = []
    for para in root.iter(f"{w}p"):
        buf: list[str] = []
        for node in para.iter():
            if node.tag == f"{w}t":
                buf.append(node.text or "")
            elif node.tag == f"{w}tab":
                buf.append("\t")
            elif node.tag in {f"{w}br", f"{w}cr"}:
                buf.append("\n")
        lines.extend("".join(buf).split("\n"))
    if not any(line.strip() for line in lines):
        raise ExtractionError("DOCX contains no extractable text")
    return _decode_bounded_text("\n".join(lines).rstrip("\n").encode("utf-8"))


def _extract_xlsx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            members = _validate_archive(zf, "XLSX")
            names = {info.filename for info in members}
            shared = _shared_strings(zf, names)
            sheets = _workbook_sheets(zf)
            if len(sheets) > _MAX_XLSX_SHEETS:
                raise ExtractionError("XLSX contains too many sheets")
            rels = _workbook_rels(zf, names)
            visible_parts = [
                _sheet_part(rels.get(rid, ""))
                for _name, state, rid in sheets
                if state not in {"hidden", "veryHidden"}
            ]
            sheet_infos = [zf.getinfo(part) for part in visible_parts if part in names]
            _validate_selected_size(sheet_infos, "XLSX")
            out = bytearray()
            limit_reached = False
            for name, state, rid in sheets:
                if state in {"hidden", "veryHidden"}:
                    continue
                part = _sheet_part(rels.get(rid, ""))
                if part not in names:
                    continue
                try:
                    rows = _sheet_rows(
                        _read_zip_member(zf, part, label="XLSX worksheet"), shared
                    )
                except ET.ParseError:
                    continue
                if not _append_bounded_utf8(out, f"# ── Sheet: {name} ──\n"):
                    limit_reached = True
                    break
                for row in rows:
                    for index, value in enumerate(row):
                        if index and not _append_bounded_utf8(out, "\t"):
                            limit_reached = True
                            break
                        if not _append_bounded_utf8(out, value):
                            limit_reached = True
                            break
                    if limit_reached or not _append_bounded_utf8(out, "\n"):
                        limit_reached = True
                        break
                if limit_reached:
                    break
                if not rows:
                    if not _append_bounded_utf8(out, "(empty)\n"):
                        break
                if not _append_bounded_utf8(out, "\n"):
                    break
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid XLSX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    if not out:
        raise ExtractionError("XLSX has no visible sheets with content")
    return _decode_bounded_text(bytes(out).rstrip(b"\n"))


def _slide_sort_key(name: str) -> tuple[int, str]:
    match = re.search(r"/slide(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 10**9, name)


def _pptx_target_part(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        return posixpath.normpath(normalized.lstrip("/"))
    return posixpath.normpath(posixpath.join("ppt", normalized))


def _ordered_pptx_slides(
    zf: zipfile.ZipFile, slides: list[zipfile.ZipInfo]
) -> list[zipfile.ZipInfo]:
    fallback = sorted(slides, key=lambda item: _slide_sort_key(item.filename))
    names = set(zf.namelist())
    presentation_path = "ppt/presentation.xml"
    rels_path = "ppt/_rels/presentation.xml.rels"
    if presentation_path not in names or rels_path not in names:
        return fallback

    try:
        presentation = ET.fromstring(
            _read_zip_member(zf, presentation_path, label="PPTX presentation")
        )
        relationships = ET.fromstring(
            _read_zip_member(zf, rels_path, label="PPTX relationships")
        )
    except ET.ParseError:
        return fallback

    relationship_tag = f"{{{_NS_PKG_REL}}}Relationship"
    rid_to_part = {
        rel.get("Id", ""): _pptx_target_part(rel.get("Target", ""))
        for rel in relationships.iter(relationship_tag)
        if rel.get("Id")
        and rel.get("Target")
        and (rel.get("Type") or "").endswith("/slide")
    }
    slides_by_name = {info.filename: info for info in slides}
    p = f"{{{_NS_P}}}"
    r = f"{{{_NS_REL}}}"
    ordered: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    for slide_id in presentation.iter(f"{p}sldId"):
        part = rid_to_part.get(slide_id.get(f"{r}id", ""), "")
        if part in slides_by_name and part not in seen:
            ordered.append(slides_by_name[part])
            seen.add(part)
    return ordered or fallback


def _extract_pptx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            members = _validate_archive(zf, "PPTX")
            slides = [
                info
                for info in members
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename)
            ]
            if not slides:
                raise ExtractionError("PPTX contains no slides")
            if len(slides) > _MAX_PPTX_SLIDES:
                raise ExtractionError("PPTX contains too many slides")
            if any(info.file_size > _MAX_PPTX_SLIDE_BYTES for info in slides):
                raise ExtractionError("PPTX contains an oversized slide")
            _validate_selected_size(slides, "PPTX")

            a = f"{{{_NS_A}}}"
            out: list[str] = []
            for index, info in enumerate(_ordered_pptx_slides(zf, slides), start=1):
                try:
                    root = ET.fromstring(
                        _read_zip_member(
                            zf,
                            info,
                            label="PPTX slide",
                            max_bytes=_MAX_PPTX_SLIDE_BYTES,
                        )
                    )
                except ET.ParseError:
                    continue
                texts = [
                    (node.text or "").strip()
                    for node in root.iter(f"{a}t")
                    if (node.text or "").strip()
                ]
                if texts:
                    out.extend((f"# -- Slide {index} --", "\n".join(texts), ""))
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"Not a valid PPTX: {exc}") from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    if not out:
        raise ExtractionError("PPTX contains no extractable text")
    return _decode_bounded_text("\n".join(out).rstrip("\n").encode("utf-8"))


def _extract_pdf(path: str) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise ExtractionError("pdftotext is not installed")

    timed_out = threading.Event()
    process = None
    timer = None
    try:
        process = subprocess.Popen(
            [
                pdftotext,
                "-layout",
                "-nopgbrk",
                "-enc",
                "UTF-8",
                path,
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise ExtractionError("PDF extraction did not provide an output stream")

        def _kill_on_timeout() -> None:
            if process is not None and process.poll() is None:
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass

        timer = threading.Timer(
            _PDF_EXTRACTION_TIMEOUT_SECONDS,
            _kill_on_timeout,
        )
        timer.daemon = True
        timer.start()
        data = process.stdout.read(_MAX_EXTRACTED_TEXT_BYTES + 1)
        output_limited = len(data) > _MAX_EXTRACTED_TEXT_BYTES
        if output_limited and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        returncode = process.wait()
    except OSError as exc:
        raise ExtractionError(f"PDF extraction failed: {exc}") from exc
    finally:
        if timer is not None:
            timer.cancel()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                try:
                    process.kill()
                finally:
                    process.wait()

    if timed_out.is_set():
        raise ExtractionError(
            f"PDF extraction timed out after {_PDF_EXTRACTION_TIMEOUT_SECONDS} seconds"
        )
    if not output_limited and returncode != 0:
        raise ExtractionError(f"pdftotext failed with exit code {returncode}")

    text = _decode_bounded_text(data)
    if not text:
        raise ExtractionError("PDF contains no extractable text")
    return text


def _shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(
            _read_zip_member(
                zf,
                "xl/sharedStrings.xml",
                label="XLSX shared strings",
                max_bytes=_MAX_SHARED_STRINGS_BYTES,
            )
        )
    except ET.ParseError:
        return []
    s = f"{{{_NS_S}}}"
    return ["".join(t.text or "" for t in item.iter(f"{s}t")) for item in root.iter(f"{s}si")]


def _workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str, str]]:
    root = _zip_xml(zf, "xl/workbook.xml")
    s, r = f"{{{_NS_S}}}", f"{{{_NS_REL}}}"
    return [
        (sheet.get("name", "Sheet"), sheet.get("state", "visible"), sheet.get(f"{r}id", ""))
        for sheet in root.iter(f"{s}sheet")
    ]


def _workbook_rels(zf: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    try:
        root = ET.fromstring(
            _read_zip_member(zf, rels_path, label="XLSX relationships")
        )
    except ET.ParseError:
        return {}
    rel_tag = f"{{{_NS_PKG_REL}}}Relationship"
    return {rel.get("Id", ""): rel.get("Target", "") for rel in root.iter(rel_tag) if rel.get("Id")}


def _sheet_part(target: str) -> str:
    target = target.lstrip("/")
    return posixpath.normpath(target if target.startswith("xl/") else f"xl/{target}")


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return max(idx - 1, 0)


def _sheet_rows(xml_bytes: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml_bytes)
    s = f"{{{_NS_S}}}"
    rows: list[list[str]] = []
    for row in root.iter(f"{s}row"):
        if len(rows) >= _MAX_XLSX_ROWS_PER_SHEET:
            break
        cells: dict[int, str] = {}
        max_col = -1
        for cell in row.iter(f"{s}c"):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col >= _MAX_XLSX_COLS:
                continue
            cells[col] = _cell_value(cell, shared, s)
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)] if max_col >= 0 else [])
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    return rows


def _cell_value(cell: ET.Element, shared: list[str], s: str) -> str:
    value = cell.findtext(f"{s}v") or ""
    typ = cell.get("t", "")
    if typ == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if typ == "inlineStr":
        inline = cell.find(f"{s}is")
        return "" if inline is None else "".join(t.text or "" for t in inline.iter(f"{s}t"))
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    if typ == "e":
        return value or "#ERROR"
    return value

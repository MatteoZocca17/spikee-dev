"""Deterministic PDF and DOCX rendering for Document Forge."""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from html import unescape
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import reportlab
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import FloatObject, RectangleObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DOCUMENT_XML = "word/document.xml"
DOCX_CONTENT_TYPES = "[Content_Types].xml"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PDF_TECHNIQUES = {"invisible_text", "white_text", "tiny_text", "visible_text"}
DOCX_TECHNIQUES = {"hidden_text", "white_text", "tiny_text", "visible_text"}


def _safe_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_")
    return result or "document"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _font_name() -> str:
    # ReportLab ships this font on every supported operating system.
    font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    name = "DocumentForgeVera"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(font_path)))
    return name


def _wrap_text(text: str, font: str, size: float, width: float) -> list[str]:
    if width <= 0:
        raise ValueError("PDF page is too narrow for the text margins")
    lines: list[str] = []
    for paragraph in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            # Split oversized tokens too (for example URLs and dataset markers).
            if pdfmetrics.stringWidth(word, font, size) > width:
                if current:
                    lines.append(current)
                    current = ""
                for character in word:
                    if (
                        current
                        and pdfmetrics.stringWidth(current + character, font, size)
                        > width
                    ):
                        lines.append(current)
                        current = ""
                    current += character
                continue
            candidate = f"{current} {word}".strip()
            if not current or pdfmetrics.stringWidth(candidate, font, size) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines or [""]


def _pdf_overlay(
    width: float, height: float, payload: str, technique: str
) -> PdfReader:
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(width, height), pageCompression=1)
    font = _font_name()
    if technique == "visible_text":
        size, leading, x, y = 8.0, 10.0, 24.0, height - 24.0 - 8.0
    elif technique == "tiny_text":
        size, leading, x, y = 1.0, 1.2, 24.0, height - 24.0
    else:
        size, leading, x, y = 6.0, 7.0, 24.0, height - 24.0

    lines = _wrap_text(payload, font, size, width - 48.0)
    if y - (len(lines) - 1) * leading - size < 24.0:
        raise ValueError(
            f"Payload does not fit on the selected PDF page with technique={technique}; "
            "shorten the entry or use technique=visible_text to reserve extra space"
        )

    text = pdf.beginText(x, y)
    text.setFont(font, size)
    text.setLeading(leading)
    if technique == "invisible_text":
        text.setTextRenderMode(3)
    elif technique == "white_text":
        text.setFillColorRGB(1, 1, 1)
    elif technique == "tiny_text":
        text.setFillColorRGB(0.92, 0.92, 0.92)
    else:
        text.setFillColorRGB(0, 0, 0)

    for line in lines:
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()
    stream.seek(0)
    return PdfReader(stream)


def _add_visible_pdf_text(page: Any, payload: str) -> tuple[float, list[str]]:
    """Reserve new space outside all original content, including graphics.

    Extending below the media box avoids exposing previously cropped content.
    Normalize rotation first so the band is below the displayed page.
    """
    if page.rotation:
        page.transfer_rotation_to_content()
    crop = RectangleObject(page.cropbox)
    media = RectangleObject(page.mediabox)
    width = float(crop.width)
    lines = _wrap_text(payload, _font_name(), 8.0, width - 48.0)
    band_height = 56.0 + len(lines) * 10.0
    bottom = min(float(media.bottom), float(crop.bottom)) - band_height
    if max(float(media.top), float(crop.top)) - bottom > 14400:
        raise ValueError(
            "Visible payload exceeds the PDF page-height limit; use a shorter entry"
        )
    # Preserve the original crop when enlarging the visible area.
    contents = page.get_contents()
    if contents is not None:
        contents.operations = (
            [
                ([], b"q"),
                (
                    [
                        FloatObject(value)
                        for value in (crop.left, crop.bottom, crop.width, crop.height)
                    ],
                    b"re",
                ),
                ([], b"W"),
                ([], b"n"),
            ]
            + contents.operations
            + [([], b"Q")]
        )
        page.replace_contents(contents)
    overlay = _pdf_overlay(width, band_height, payload, "visible_text")
    page.merge_transformed_page(
        overlay.pages[0],
        Transformation().translate(float(crop.left), bottom),
    )
    page.mediabox = RectangleObject(
        [
            min(media.left, crop.left),
            bottom,
            max(media.right, crop.right),
            max(media.top, crop.top),
        ]
    )
    page.cropbox = RectangleObject([crop.left, bottom, crop.right, crop.top])
    # Explicit production boxes must not clip the new band on export/print.
    for name, key in (
        ("trimbox", "/TrimBox"),
        ("bleedbox", "/BleedBox"),
        ("artbox", "/ArtBox"),
    ):
        if key in page:
            setattr(page, name, RectangleObject(page.cropbox))
    return band_height, lines


def _inject_pdf(
    source: Path,
    destination: Path,
    payload: str,
    technique: str,
    position: str,
    page: str | None,
    anchor: str | None,
    occurrence: int,
) -> dict[str, Any]:
    if technique not in PDF_TECHNIQUES:
        raise ValueError(
            f"PDF technique must be one of: {', '.join(sorted(PDF_TECHNIQUES))}"
        )
    reader = PdfReader(str(source))
    if reader.is_encrypted or not reader.pages:
        raise ValueError(
            f"PDF must be unencrypted and contain at least one page: {source}"
        )
    if position != "page" or page is None:
        raise ValueError("Renderer requires a resolved PDF page")
    selected = int(page) - 1
    if not 0 <= selected < len(reader.pages):
        raise ValueError("Resolved PDF page is out of range")
    writer = PdfWriter(clone_from=reader)
    selected_page = writer.pages[selected]
    band_height = None
    expected_payload = payload
    if technique == "visible_text":
        band_height, lines = _add_visible_pdf_text(selected_page, payload)
        expected_payload = "\n".join(lines)
    else:
        if selected_page.rotation:
            selected_page.transfer_rotation_to_content()
        crop = selected_page.cropbox
        width = float(crop.width)
        overlay = _pdf_overlay(
            width,
            float(crop.height),
            payload,
            technique,
        )
        selected_page.merge_transformed_page(
            overlay.pages[0],
            Transformation().translate(float(crop.left), float(crop.bottom)),
        )
        size = 1.0 if technique == "tiny_text" else 6.0
        expected_payload = "\n".join(
            _wrap_text(payload, _font_name(), size, width - 48.0)
        )
    metadata = dict(reader.metadata or {})
    metadata["/Producer"] = "Spikee Document Forge"
    writer.add_metadata(metadata)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        writer.write(handle)

    check = PdfReader(str(destination))
    if len(check.pages) != len(reader.pages):
        raise RuntimeError("Generated PDF failed page-count validation")
    extracted = check.pages[selected].extract_text() or ""
    if " ".join(expected_payload.split()) not in " ".join(extracted.split()):
        raise RuntimeError(
            "Generated PDF payload was not recoverable from the selected page"
        )
    return {
        "document_type": "pdf",
        "resolved_page": selected + 1,
        "source_pages": len(reader.pages),
        "generated_pages": len(check.pages),
        "visible_text_band_height": band_height,
    }


def _validate_docx(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Not a valid DOCX ZIP package: {path}")
    with zipfile.ZipFile(path) as archive:
        required = {DOCX_CONTENT_TYPES, DOCUMENT_XML}
        if not required.issubset(archive.namelist()):
            raise ValueError(f"DOCX is missing required package parts: {path}")
        for name in required:
            try:
                ET.fromstring(archive.read(name))
            except ET.ParseError as error:
                raise ValueError(
                    f"DOCX contains malformed XML in {name}: {path}"
                ) from error


def _docx_paragraph(payload: str, technique: str) -> str:
    for character in payload:
        code = ord(character)
        if not (
            code in (9, 10, 13)
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        ):
            raise ValueError(
                f"DOCX payload contains an XML-invalid character U+{code:04X}"
            )
    text = escape(payload)
    if technique == "hidden_text":
        properties = "<w:rPr><w:vanish/></w:rPr>"
    elif technique == "white_text":
        properties = '<w:rPr><w:color w:val="FFFFFF"/><w:sz w:val="12"/></w:rPr>'
    elif technique == "tiny_text":
        properties = '<w:rPr><w:color w:val="D9D9D9"/><w:sz w:val="2"/></w:rPr>'
    else:
        properties = '<w:rPr><w:b/><w:sz w:val="20"/></w:rPr>'
    return f'<w:p><w:r>{properties}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _paragraph_text(xml: str) -> str:
    values = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, flags=re.DOTALL)
    text = "".join(re.sub(r"<[^>]+>", "", value) for value in values)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _docx_paragraph_ranges(xml: str) -> list[tuple[int, int, str]]:
    ranges = []
    for match in re.finditer(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, flags=re.DOTALL):
        ranges.append((match.start(), match.end(), _paragraph_text(match.group(0))))
    return ranges


def _select_docx_offset(
    xml: str,
    position: str,
    anchor: str | None,
    occurrence: int,
    relative: str,
) -> tuple[int, str | None]:
    body_start_match = re.search(r"<w:body(?:\s[^>]*)?>", xml)
    body_end = xml.rfind("</w:body>")
    if not body_start_match or body_end < 0:
        raise ValueError("Could not locate the DOCX body")
    if position == "start":
        return body_start_match.end(), None
    if position == "end":
        section = xml.rfind("<w:sectPr", body_start_match.end(), body_end)
        return (section if section >= 0 else body_end), None
    if position in {"anchor", "auto"}:
        if not anchor:
            raise ValueError(f"position={position} requires a resolvable anchor")
        matches = [
            item
            for item in _docx_paragraph_ranges(xml)
            if anchor.casefold() in item[2].casefold()
        ]
        if not matches:
            raise ValueError(f"DOCX anchor not found: {anchor!r}")
        if occurrence < 1 or occurrence > len(matches):
            raise ValueError(
                f"anchor_occurrence={occurrence} is invalid; found {len(matches)} match(es)"
            )
        start, end, matched_text = matches[occurrence - 1]
        if relative not in {"before", "after"}:
            raise ValueError("relative must be 'before' or 'after'")
        return (start if relative == "before" else end), matched_text
    if position == "page":
        raise ValueError("position=page is only supported for PDF sources")
    raise ValueError(f"Unsupported DOCX position: {position}")

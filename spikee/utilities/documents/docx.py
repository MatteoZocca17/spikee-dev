"""DOCX paragraph and image insertion preserving other package parts."""

import re
import xml.etree.ElementTree as ET
import zipfile
from html import unescape
from pathlib import Path
from xml.sax.saxutils import escape

DOCUMENT_XML = "word/document.xml"
DOCX_CONTENT_TYPES = "[Content_Types].xml"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_TECHNIQUES = {"hidden_text", "white_text", "tiny_text", "visible_text"}


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


def write_docx(
    source, output, payload, technique, plan, mode, png=None, dimensions=None
):
    _validate_docx(source)
    with zipfile.ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    xml = parts[DOCUMENT_XML].decode("utf-8")
    ranges = _docx_paragraph_ranges(xml)
    if plan.get("position") in ("start", "end"):
        offset, _ = _select_docx_offset(
            xml, plan["position"], None, 1, plan["relative"]
        )
    else:
        start, end, _ = ranges[plan["unit_id"] - 1]
        offset = start if plan["relative"] == "before" else end
    paragraph = _docx_paragraph(payload, technique) if mode != "image" else ""
    if png is not None:
        rel_path = "word/_rels/document.xml.rels"
        ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        rels = (
            ET.fromstring(parts[rel_path])
            if rel_path in parts
            else ET.Element(f"{{{ns}}}Relationships")
        )
        used = {node.get("Id") for node in rels}
        i = 1
        while f"rIdForge{i}" in used or f"word/media/forge{i}.png" in parts:
            i += 1
        rid = f"rIdForge{i}"
        parts[f"word/media/forge{i}.png"] = png
        ET.SubElement(
            rels,
            f"{{{ns}}}Relationship",
            Id=rid,
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            Target=f"media/forge{i}.png",
        )
        parts[rel_path] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)
        types = ET.fromstring(parts[DOCX_CONTENT_TYPES])
        cns = "http://schemas.openxmlformats.org/package/2006/content-types"
        if not any(n.get("Extension", "").lower() == "png" for n in types):
            ET.SubElement(
                types, f"{{{cns}}}Default", Extension="png", ContentType="image/png"
            )
        parts[DOCX_CONTENT_TYPES] = ET.tostring(
            types, encoding="utf-8", xml_declaration=True
        )
        # Use the section's page width and margins where available.
        root = ET.fromstring(xml)
        wns = "{" + WORD_NS + "}"
        section = root.find(".//" + wns + "sectPr")
        usable = 8640
        if section is not None:
            size, margin = section.find(wns + "pgSz"), section.find(wns + "pgMar")
            if size is not None and margin is not None:
                usable = (
                    int(size.get(wns + "w", "12240"))
                    - int(margin.get(wns + "left", "1800"))
                    - int(margin.get(wns + "right", "1800"))
                )
        cx = min(usable * 635, dimensions[0] * 9525)
        cy = int(cx * dimensions[1] / dimensions[0])
        if cx <= 0 or cy > 8 * 914400:
            raise ValueError("Image does not fit a Word page; shorten or resize it")
        doc_id = (
            max(
                [int(n.get("id", "0")) for n in root.iter() if n.tag.endswith("}docPr")]
                + [0]
            )
            + 1
        )
        paragraph += f'''<w:p><w:r><w:drawing>
<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{doc_id}" name="Dataset image"/>
<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="0" name="Dataset image"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'''
    generated = xml[:offset] + paragraph + xml[offset:]
    ET.fromstring(generated)
    parts[DOCUMENT_XML] = generated.encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    _validate_docx(output)
    if mode != "image" and escape(payload) not in generated:
        raise RuntimeError("DOCX payload verification failed")

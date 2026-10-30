"""Deterministic image generation and embedding; no LLM-generated drawing code."""

import io
import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, Transformation
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from . import renderer


def make_image(payload, image_path=None):
    if image_path:
        with Image.open(image_path) as original:
            original.load()
            image = original.convert("RGB")
    else:
        font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        font = ImageFont.truetype(str(font_path), 24)
        lines = []
        for paragraph in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            current = ""
            for char in paragraph:
                if current and font.getlength(current + char) > 1120:
                    lines.append(current)
                    current = ""
                current += char
            lines.append(current)
        if len(lines) > 400:
            raise ValueError("Image payload exceeds 400 lines; shorten the entry")
        image = Image.new("RGB", (1200, 64 + len(lines) * 32), "white")
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(lines):
            draw.text((40, 24 + i * 32), line, fill="black", font=font)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue(), image.size


def add_pdf_image(page, png, dimensions):
    if page.rotation:
        page.transfer_rotation_to_content()
    width = float(page.cropbox.width) - 48
    if width <= 0:
        raise ValueError("PDF page is too narrow for an image")
    height = width * dimensions[1] / dimensions[0]
    if height > 3000:
        raise ValueError(
            "Image is too tall for readable PDF placement; shorten the entry"
        )
    # Reuse the renderer's crop-preserving reserved band.
    band, _ = renderer._add_visible_pdf_text(page, "\n" * math.ceil(height / 10))
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(float(page.cropbox.width), band))
    pdf.drawImage(ImageReader(io.BytesIO(png)), 24, 24, width=width, height=height)
    pdf.save()
    page.merge_transformed_page(
        PdfReader(stream).pages[0],
        Transformation().translate(
            float(page.cropbox.left), float(page.cropbox.bottom)
        ),
    )


def write_docx(
    source, output, payload, technique, plan, mode, png=None, dimensions=None
):
    renderer._validate_docx(source)
    with zipfile.ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    xml = parts[renderer.DOCUMENT_XML].decode("utf-8")
    ranges = renderer._docx_paragraph_ranges(xml)
    if plan.get("position") in ("start", "end"):
        offset, _ = renderer._select_docx_offset(
            xml, plan["position"], None, 1, plan["relative"]
        )
    else:
        start, end, _ = ranges[plan["unit_id"] - 1]
        offset = start if plan["relative"] == "before" else end
    paragraph = renderer._docx_paragraph(payload, technique) if mode != "image" else ""
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
        types = ET.fromstring(parts[renderer.DOCX_CONTENT_TYPES])
        cns = "http://schemas.openxmlformats.org/package/2006/content-types"
        if not any(n.get("Extension", "").lower() == "png" for n in types):
            ET.SubElement(
                types, f"{{{cns}}}Default", Extension="png", ContentType="image/png"
            )
        parts[renderer.DOCX_CONTENT_TYPES] = ET.tostring(
            types, encoding="utf-8", xml_declaration=True
        )
        # Use the section's page width and margins where available.
        root = ET.fromstring(xml)
        wns = "{" + renderer.WORD_NS + "}"
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
    parts[renderer.DOCUMENT_XML] = generated.encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    renderer._validate_docx(output)
    if mode != "image" and escape(payload) not in generated:
        raise RuntimeError("DOCX payload verification failed")

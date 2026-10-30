"""Render instruction text or supplied images to PNG."""

import io
from pathlib import Path

import reportlab
from PIL import Image, ImageDraw, ImageFont


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

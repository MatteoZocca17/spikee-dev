"""Render instruction text or supplied images to PNG."""

import io
import re
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


def render_prompt_image(payload, options):
    """Render pixels only; never store the payload in PNG text/EXIF metadata."""
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("Image payload must be nonempty text")
    if len(payload) > 100000:
        raise ValueError("Image payload exceeds 100000 characters")

    def integer(name, default, minimum, maximum):
        value = options.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(f"{name} must be an integer")
        try:
            result = int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
        if not minimum <= result <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return result

    def colour(name, default):
        value = options.get(name, default)
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError(f"{name} must be a hex colour such as #E6E6E6")
        return value

    size = integer("font_size", 24, 1, 256)
    padding = integer("padding", 24, 0, 512)
    width = integer("width", 1200, 64, 4096)
    foreground = colour("text_color", "#000000")
    background = colour("background_color", "#FFFFFF")
    layout = options.get("layout", "append")
    if layout not in ("append", "overlay"):
        raise ValueError("layout must be append or overlay")
    font_path = options.get("font_path") or str(
        Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    )
    font = ImageFont.truetype(font_path, size)
    template = None
    if options.get("template"):
        with Image.open(options["template"]) as original:
            if original.width * original.height > 40000000:
                raise ValueError("Template exceeds 40 million pixels")
            # Composite transparency onto the configured background; do not copy metadata.
            template = Image.new("RGB", original.size, background)
            rgba = original.convert("RGBA")
            template.paste(rgba, mask=rgba.getchannel("A"))
        width = template.width if layout == "overlay" else max(width, template.width)
    available = width - 2 * padding
    if available < 1:
        raise ValueError("Image width must exceed twice the padding")
    lines = []
    for paragraph in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = ""
        for char in paragraph:
            if font.getlength(char) > available:
                raise ValueError("Image is too narrow for the selected font")
            if line and font.getlength(line + char) > available:
                lines.append(line)
                line = ""
            line += char
        lines.append(line)
    ascent, descent = font.getmetrics()
    leading = ascent + descent + max(1, size // 4)
    text_height = 2 * padding + len(lines) * leading
    if template is not None and layout == "overlay":
        if text_height > template.height:
            raise ValueError("Payload does not fit the template; use layout=append")
        height = template.height
        top = padding
    else:
        height = text_height + (template.height if template is not None else 0)
        top = padding + (template.height if template is not None else 0)
    if height > 16384 or width * height > 40000000:
        raise ValueError("Rendered image exceeds size limits; shorten the payload")
    image = Image.new("RGB", (width, height), background)
    if template is not None:
        image.paste(template, (0, 0))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text(
            (padding, top + index * leading),
            line,
            font=font,
            fill=foreground,
            anchor="lt",
        )
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue(), image.size

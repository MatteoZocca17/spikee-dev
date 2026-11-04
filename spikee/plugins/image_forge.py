"""Create raster prompt PNGs or embed them in PDF templates."""

import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from spikee.templates.plugin import FatalPluginError, Plugin
from spikee.utilities.documents.artifacts import encode, read_plugin_config, sha256_file
from spikee.utilities.enums import ModuleTag

RENDER_OPTIONS = {
    "font_size",
    "font_path",
    "width",
    "padding",
    "text_color",
    "background_color",
    "template",
    "layout",
}
PLACEMENT_OPTIONS = {
    "source",
    "position",
    "page",
    "anchor",
    "anchor_occurrence",
    "relative",
    "field",
    "instruction",
    "model",
    "section_mode",
    "fallback_field",
    "cache_dir",
}


class ImageForge(Plugin):
    TERMINAL_OUTPUT = True

    def get_description(self):
        return [
            ModuleTag.IMAGE,
            ModuleTag.FORMATTING,
        ], "Render payload pixels to PNG or a PDF copy."

    def get_available_option_values(self):
        return ["output_format=png", "output_format=pdf"], False

    def transform(self, content: str, exclude_patterns=None, plugin_option="") -> str:
        try:
            return generate(content, plugin_option)
        except Exception as exc:
            raise FatalPluginError(f"image_forge: {exc}") from exc


def generate(payload, option_string=""):
    from PIL import Image

    from spikee.utilities.documents.images import render_prompt_image
    from spikee.utilities.documents.placement import save_json

    opts = read_plugin_config(
        option_string,
        RENDER_OPTIONS | PLACEMENT_OPTIONS | {"output_format", "output_dir"},
    )
    numeric = {"font_size", "width", "padding", "page", "anchor_occurrence"}
    for key, value in opts.items():
        if isinstance(value, bool) or not isinstance(
            value, (str, int) if key in numeric else str
        ):
            raise TypeError(f"Invalid value type for {key}")
    output_format = opts.get("output_format", "png")
    if output_format not in ("png", "pdf"):
        raise ValueError("output_format must be png or pdf")
    if output_format == "png" and set(opts) & PLACEMENT_OPTIONS:
        raise ValueError("PDF source/placement options require output_format=pdf")
    if output_format == "pdf" and (
        not opts.get("source") or Path(opts["source"]).suffix.lower() != ".pdf"
    ):
        raise ValueError("output_format=pdf requires source=<PDF template>")
    png, dimensions = render_prompt_image(payload, opts)
    output_dir = Path(opts.get("output_dir", "generated-files/image-forge")).resolve()
    if output_format == "pdf":
        from spikee.plugins.document_forge import generate as generate_document

        with tempfile.TemporaryDirectory(prefix="spikee-image-") as temporary:
            image_path = Path(temporary) / "payload.png"
            image_path.write_bytes(png)
            config = {
                key: value for key, value in opts.items() if key in PLACEMENT_OPTIONS
            }
            config.update(
                output_dir=str(output_dir),
                content_mode="image",
                technique="visible_text",
                image_path=str(image_path),
            )
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            encoded = generate_document(payload, f"config={config_path}")
        # Record durable rendering provenance, not the deleted temporary PNG path.
        from spikee.utilities.documents.artifacts import PREFIX

        record = json.loads(encoded[len(PREFIX) :])
        record.update(
            plugin="image_forge",
            image_path=None,
            rendering={key: opts[key] for key in RENDER_OPTIONS if key in opts},
        )
        output = Path(record["generated_file"])
        save_json(output.with_suffix(".pdf.json"), record)
        return encode(record)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"prompt__{uuid.uuid4().hex[:12]}.png"
    try:
        output.write_bytes(png)
        with Image.open(output) as check:
            check.load()
            if check.size != dimensions or check.info:
                raise RuntimeError("PNG verification failed")
        record = {
            "version": 1,
            "plugin": "image_forge",
            "generated_file": str(output),
            "artifact_sha256": sha256_file(output),
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "dimensions": list(dimensions),
            "validation": "pixels-no-text-metadata",
            "ocr_verified": False,
            "visual_review": "not_performed",
            "rendering": {key: opts[key] for key in RENDER_OPTIONS if key in opts},
        }
        save_json(output.with_suffix(".png.json"), record)
        return encode(record)
    except Exception:
        output.unlink(missing_ok=True)
        output.with_suffix(".png.json").unlink(missing_ok=True)
        raise

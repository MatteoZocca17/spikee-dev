"""Generate document artifacts while preserving the supplied instruction text."""

import hashlib
import json
import re
import uuid
from pathlib import Path

from spikee.templates.plugin import FatalPluginError, Plugin
from spikee.utilities.enums import ModuleTag
from spikee.utilities.modules import parse_options


class DocumentForge(Plugin):
    TERMINAL_OUTPUT = True

    def get_description(self):
        return (
            [ModuleTag.LLM, ModuleTag.FORMATTING],
            (
                "Embed instructions in PDF/DOCX copies; uses an LLM only for "
                "position=llm/auto or section_mode=per_entry"
            ),
        )

    def get_available_option_values(self):
        return [
            "position=llm",
            "position=start",
            "position=end",
            "position=page",
            "position=anchor",
        ], True

    def transform(
        self,
        content: str,
        exclude_patterns: list[str] | None = None,
        plugin_option: str = "",
    ) -> str:
        # No payload transformation: excluded substrings are preserved too.
        try:
            return generate(content, plugin_option or "")
        except Exception as exc:
            raise FatalPluginError(f"document_forge: {exc}") from exc


ALLOWED = {
    "config",
    "source",
    "field",
    "instruction",
    "model",
    "position",
    "page",
    "anchor",
    "anchor_occurrence",
    "relative",
    "technique",
    "text_color",
    "content_mode",
    "image_path",
    "output_dir",
    "cache_dir",
    "section_mode",
    "fallback_field",
}


def local_path(value):
    path = Path(value)
    return (path if path.is_absolute() else Path.cwd() / path).resolve()


def generate(payload, option_string):
    # Keep plugin discovery independent of optional document libraries.
    try:
        from pypdf import PdfReader, PdfWriter

        from spikee.utilities.documents import docx, pdf
        from spikee.utilities.documents._progress import report
        from spikee.utilities.documents.artifacts import encode, safe_name, sha256_file
        from spikee.utilities.documents.docx import write_docx
        from spikee.utilities.documents.images import make_image
        from spikee.utilities.documents.pdf import add_pdf_image
        from spikee.utilities.documents.placement import (
            index_document,
            save_json,
            select_plan,
        )
        from spikee.utilities.documents.sections import entry_options
    except ModuleNotFoundError as exc:
        raise ImportError(
            "Document Forge requires document dependencies: install this checkout with pip install -e '.[documents]'"
        ) from exc
    if not isinstance(payload, str):
        raise TypeError(
            "document_forge expects text input; use image_path for supplied images"
        )
    cli = parse_options(option_string)
    options = {}
    if cli.get("config"):
        options = json.loads(local_path(cli["config"]).read_text(encoding="utf-8"))
        if not isinstance(options, dict):
            raise ValueError("Plugin config must be a JSON object")
    options.update(cli)
    unknown = set(options) - ALLOWED
    if unknown:
        raise ValueError(f"Unknown document_forge options: {sorted(unknown)}")
    for key, value in options.items():
        if key in ("page", "anchor_occurrence"):
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError(f"{key} must be an integer")
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    if not options.get("source"):
        raise ValueError("source=<PDF-or-DOCX-path> is required")
    source = local_path(options["source"])
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in (".pdf", ".docx"):
        raise ValueError("Only PDF and DOCX sources are supported")
    mode = options.get("content_mode", "text")
    if mode not in ("text", "image", "text_image"):
        raise ValueError("content_mode must be text, image, or text_image")
    technique = options.get("technique", "visible_text")
    allowed = (
        pdf.PDF_TECHNIQUES if source.suffix.lower() == ".pdf" else docx.DOCX_TECHNIQUES
    )
    if technique not in allowed:
        raise ValueError(f"technique must be one of {sorted(allowed)}")
    text_color = options.get("text_color")
    if text_color is not None:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", text_color):
            raise ValueError("text_color must be a hex colour such as #E6E6E6")
        if source.suffix.lower() != ".pdf" or mode != "text":
            raise ValueError(
                "text_color currently requires a PDF with content_mode=text"
            )
        if technique not in ("visible_text", "tiny_text"):
            raise ValueError("text_color requires technique=visible_text or tiny_text")
    if mode != "text" and technique != "visible_text":
        raise ValueError(
            "Image modes require technique=visible_text; hidden images are unsupported"
        )
    if mode == "text" and options.get("image_path"):
        raise ValueError("image_path requires content_mode=image or text_image")
    image_path = (
        local_path(options["image_path"]) if options.get("image_path") else None
    )
    if mode == "text_image" and image_path is None:
        raise ValueError("content_mode=text_image requires image_path")
    cache_dir = local_path(options.get("cache_dir", "generated-files/document-cache"))
    index = index_document(source, cache_dir)
    resolved_options, entry_section = entry_options(payload, options, cache_dir)
    plan = select_plan(source, index, resolved_options, cache_dir)
    if type(plan.get("unit_id")) is not int or not 1 <= plan["unit_id"] <= len(
        index["units"]
    ):
        raise ValueError("Cached planner location is invalid; clear the plan cache")
    if sha256_file(source) != index["source_sha256"]:
        raise RuntimeError("Source changed during planning; retry")
    output_dir = local_path(options.get("output_dir", "generated-files/document-forge"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output = (
        output_dir
        / f"{safe_name(source.stem)}__{uuid.uuid4().hex[:12]}{source.suffix.lower()}"
    )
    png, dimensions = (
        make_image(payload, image_path) if mode != "text" else (None, None)
    )
    try:
        report(f"Rendering {mode} at location {plan['unit_id']}")
        if source.suffix.lower() == ".pdf":
            if mode != "image":
                pdf._inject_pdf(
                    source,
                    output,
                    payload,
                    technique,
                    "page",
                    str(plan["unit_id"]),
                    None,
                    1,
                    text_color=text_color,
                )
            if png is not None:
                writer = PdfWriter(
                    clone_from=output if mode == "text_image" else source
                )
                add_pdf_image(writer.pages[plan["unit_id"] - 1], png, dimensions)
                writer.write(output)
            check = PdfReader(output)
            if len(check.pages) != len(index["units"]):
                raise RuntimeError("PDF page count changed")
            if png is not None and not list(check.pages[plan["unit_id"] - 1].images):
                raise RuntimeError("Embedded PDF image was not recoverable")
        else:
            write_docx(source, output, payload, technique, plan, mode, png, dimensions)
        if sha256_file(source) != index["source_sha256"]:
            raise RuntimeError("Source changed during generation")
        record = {
            "version": 1,
            "generated_file": str(output),
            "source": str(source),
            "source_sha256": index["source_sha256"],
            "artifact_sha256": sha256_file(output),
            "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "plan": plan,
            "entry_section": entry_section,
            "technique": technique,
            "text_color": text_color,
            "content_mode": mode,
            "image_path": str(image_path) if image_path else None,
            "validation": "structure-and-image-presence"
            if mode == "image"
            else "structure-and-text",
            "visual_review": "not_performed",
            "ocr_verified": False,
        }
        save_json(output.with_suffix(output.suffix + ".json"), record)
        report(f"Artifact saved and validated: {output.name}")
        return encode(record)
    except Exception:
        output.unlink(missing_ok=True)
        raise

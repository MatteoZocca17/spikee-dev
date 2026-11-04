"""Embed a payload in PDF Document Info metadata without changing page content."""

import hashlib
import uuid
from pathlib import Path

from spikee.templates.plugin import FatalPluginError, Plugin
from spikee.utilities.documents.artifacts import (
    encode,
    read_plugin_config,
    safe_name,
    sha256_file,
)
from spikee.utilities.enums import ModuleTag


class MetadataForge(Plugin):
    TERMINAL_OUTPUT = True

    def get_description(self):
        return [ModuleTag.FORMATTING], "Embed instructions in PDF metadata fields."

    def get_available_option_values(self):
        return ["field=subject", "field=title", "field=keywords", "field=all"], False

    def transform(self, content: str, exclude_patterns=None, plugin_option="") -> str:
        try:
            return generate(content, plugin_option)
        except Exception as exc:
            raise FatalPluginError(f"metadata_forge: {exc}") from exc


def generate(payload, option_string=""):
    from pypdf import PdfReader, PdfWriter

    from spikee.utilities.documents.placement import save_json

    if not isinstance(payload, str):
        raise TypeError("metadata_forge expects text input")
    opts = read_plugin_config(option_string, {"source", "field", "output_dir"})
    if any(not isinstance(value, str) for value in opts.values()):
        raise ValueError("Metadata plugin options must be strings")
    if not opts.get("source"):
        raise ValueError("source=<PDF path> is required")
    source = Path(opts["source"]).resolve()
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError("source must be an existing PDF")
    field = opts.get("field", "subject").lower()
    fields = {"title": "/Title", "subject": "/Subject", "keywords": "/Keywords"}
    if field not in (*fields, "all"):
        raise ValueError("field must be title, subject, keywords, or all")
    selected = list(fields.values()) if field == "all" else [fields[field]]
    source_hash = sha256_file(source)
    reader = PdfReader(source)
    if reader.is_encrypted or not reader.pages:
        raise ValueError("Source PDF must be unencrypted and nonempty")
    output_dir = Path(
        opts.get("output_dir", "generated-files/metadata-forge")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{safe_name(source.stem)}__{uuid.uuid4().hex[:12]}.pdf"
    try:
        writer = PdfWriter(clone_from=reader)
        writer.add_metadata({key: payload for key in selected})
        writer.write(output)
        check = PdfReader(output)
        if any(check.metadata.get(key) != payload for key in selected):
            raise RuntimeError("PDF metadata payload did not round-trip")
        if len(check.pages) != len(reader.pages) or any(
            (
                original.get_contents().get_data()
                if original.get_contents() is not None
                else b""
            )
            != (
                copy.get_contents().get_data()
                if copy.get_contents() is not None
                else b""
            )
            for original, copy in zip(reader.pages, check.pages)
        ):
            raise RuntimeError("PDF page content changed during metadata generation")
        if sha256_file(source) != source_hash:
            raise RuntimeError("Source changed during generation")
        record = {
            "version": 1,
            "plugin": "metadata_forge",
            "generated_file": str(output),
            "artifact_sha256": sha256_file(output),
            "source": str(source),
            "source_sha256": source_hash,
            "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "metadata_fields": selected,
            "validation": "metadata-and-page-content",
        }
        save_json(output.with_suffix(".pdf.json"), record)
        return encode(record)
    except Exception:
        output.unlink(missing_ok=True)
        output.with_suffix(".pdf.json").unlink(missing_ok=True)
        raise

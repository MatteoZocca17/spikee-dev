"""Generate document artifacts while preserving the supplied instruction text."""

from spikee.templates.plugin import Plugin


class DocumentForge(Plugin):
    def get_description(self):
        return (
            [],
            "Embed instructions in PDF/DOCX copies with LLM or explicit placement",
        )

    def get_available_option_values(self):
        return [], False

    def transform(
        self,
        content: str,
        exclude_patterns: list[str] | None = None,
        plugin_option: str = "",
    ) -> str:
        # Lazy import: listing plugins must work without the documents extra.
        try:
            from spikee.document_engine.pipeline import generate
        except ModuleNotFoundError as exc:
            raise ImportError(
                "Document Forge requires document dependencies: pip install 'spikee[documents]'"
            ) from exc
        return generate(content, plugin_option or "")

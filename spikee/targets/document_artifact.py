"""Check document artifact integrity; no application submission or attack judging."""

from spikee.templates.target import Target


class DocumentArtifact(Target):
    def get_description(self):
        return (
            [],
            "Check artifact integrity; success means intact file, not attack success",
        )

    def process_input(self, input_text, system_message=None, target_options=None):
        from spikee.utilities.documents.artifacts import inspect_record

        if target_options:
            raise ValueError("document_artifact does not take target options")
        try:
            _, record = inspect_record(input_text)
        except ValueError as exc:
            return False, {"check": "artifact_integrity", "reason": str(exc)}
        return True, {"check": "artifact_integrity", "record": record}

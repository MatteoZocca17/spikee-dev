"""Check document artifact integrity; no application submission or attack judging."""

from spikee.templates.target import Target


class DocumentArtifact(Target):
    def get_description(self):
        return [], "Validate Document Forge artifact paths and hashes"

    def process_input(self, input_text, system_message=None, target_options=None):
        from spikee.document_engine.artifacts import inspect_record

        if target_options:
            raise ValueError("document_artifact does not take target options")
        return inspect_record(input_text)

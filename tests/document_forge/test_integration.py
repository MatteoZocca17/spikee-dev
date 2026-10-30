"""Exercise the generator boundary, CLI failures and integrity judge semantics."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas

from spikee.generator import (
    apply_plugin,
    generate_variations,
    validate_terminal_plugins,
)
from spikee.judge import call_judge
from spikee.plugins.document_forge import DocumentForge, generate
from spikee.targets.document_artifact import DocumentArtifact
from spikee.templates.plugin import FatalPluginError, Plugin
from spikee.utilities.documents import artifacts, placement
from spikee.utilities.enums import ModuleTag


class FailingPlugin(Plugin):
    def __init__(self, error):
        self.error = error

    def transform(self, content: str, exclude_patterns=None, plugin_option=""):
        raise self.error


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seed = self.root / "seeds"
        self.seed.mkdir()
        for name in ("base_user_inputs", "jailbreaks", "instructions"):
            self.write_seed(name, [])
        self.write_seed(
            "standalone_user_inputs",
            [
                {
                    "id": "sample",
                    "content": "Sample instruction",
                    "judge_name": "canary",
                    "judge_args": "TEST",
                }
            ],
        )
        self.source = self.root / "template.pdf"
        pdf = canvas.Canvas(str(self.source))
        pdf.drawString(30, 500, "Project milestones")
        pdf.save()
        self.options = (
            f"source={self.source},position=end,output_dir={self.root / 'out'},"
            f"cache_dir={self.root / 'cache'}"
        )

    def write_seed(self, name, rows):
        (self.seed / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def cli(self, *args):
        repository = Path(__file__).resolve().parents[2]
        return subprocess.run(
            [sys.executable, "-c", "from spikee.cli import main; main()", *args],
            check=False,
            cwd=self.root,
            env={
                **os.environ,
                "PYTHONPATH": str(repository),
                "PYTHONIOENCODING": "utf-8",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def dataset(
        self,
        *args,
        standalone=True,
        plugin_only=True,
        plugin="document_forge",
        options=None,
    ):
        return self.cli(
            "generate",
            "--seed-folder",
            str(self.seed),
            *(["--plugin-only"] if plugin_only else []),
            "--plugins",
            plugin,
            "--plugin-options",
            f"document_forge:{options or self.options}",
            *(["--include-standalone-inputs"] if standalone else []),
            *args,
        )

    def assert_error(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertFalse((self.root / "out").exists())
        self.assertFalse((self.root / "datasets").exists())

    def test_fatal_errors_propagate_but_ordinary_errors_still_skip(self):
        with self.assertRaises(FatalPluginError):
            apply_plugin("fatal", FailingPlugin(FatalPluginError("fatal")), "x")
        self.assertEqual(
            apply_plugin("ordinary", FailingPlugin(RuntimeError("ordinary")), "x"), []
        )

    def test_missing_template_is_fatal_only_at_plugin_boundary(self):
        options = f"source={self.root / 'missing.pdf'},position=end"
        with self.assertRaises(FileNotFoundError):
            generate("x", options)
        with self.assertRaises(FatalPluginError):
            DocumentForge().transform("x", plugin_option=options)

    def test_metadata_and_offline_generation(self):
        plugin = DocumentForge()
        self.assertIn(ModuleTag.LLM, plugin.get_description()[0])
        self.assertTrue(plugin.get_available_option_values()[1])
        with patch.object(
            placement, "get_llm", side_effect=AssertionError("unexpected LLM")
        ):
            record = plugin.transform(
                "Exact TEST", exclude_patterns=["TEST"], plugin_option=self.options
            )
        valid, meta = DocumentArtifact().process_input(record)
        self.assertTrue(call_judge({}, valid))
        self.assertEqual(meta["check"], "artifact_integrity")

    def test_terminal_chain_validation(self):
        ordinary = FailingPlugin(RuntimeError("unused"))
        terminal = DocumentForge()
        self.assertFalse(ordinary.TERMINAL_OUTPUT)
        self.assertEqual(validate_terminal_plugins([("plain", ordinary)]), [])
        self.assertEqual(
            validate_terminal_plugins(
                [("plain~df", [("plain", ordinary), ("df", terminal)])]
            ),
            ["df"],
        )
        with self.assertRaises(SystemExit) as error:
            validate_terminal_plugins(
                [("df~plain", [("df", terminal), ("plain", ordinary)])]
            )
        self.assertEqual(error.exception.code, 1)

    def test_single_plugin_cli_missing_source(self):
        result = self.cli(
            "generate",
            "plugin",
            "-i",
            "x",
            "--plugins",
            "document_forge",
            "--plugin-options",
            "document_forge:source=missing.pdf",
        )
        self.assert_error(result, "[ERROR] document_forge:")

    def test_pipe_rejected_in_both_cli_commands(self):
        self.assert_error(
            self.cli(
                "generate", "plugin", "-i", "x", "--plugins", "document_forge|base64"
            ),
            "must be the last element",
        )
        self.assert_error(
            self.dataset(plugin="document_forge|base64"), "must be the last element"
        )

    def test_dataset_rejects_prefix_and_suffix(self):
        for fix in ("prefix=before", "suffix=after"):
            with self.subTest(fix=fix):
                self.assert_error(
                    self.dataset("--include-fixes", fix),
                    "incompatible with prefixes/suffixes",
                )

    def test_dataset_rejects_document_permutations(self):
        for name in ("base_user_inputs", "jailbreaks", "instructions"):
            self.write_seed(name, [{"id": "sample", "text": "Example"}])
        self.assert_error(self.dataset(), "cannot be applied to document entries")

    def test_dataset_requires_standalone_flag(self):
        self.assert_error(
            self.dataset(standalone=False), "require --include-standalone-inputs"
        )

    def test_dataset_requires_plugin_only(self):
        self.assert_error(self.dataset(plugin_only=False), "require --plugin-only")

    def test_threaded_permutations_propagate_only_fatal_errors(self):
        def run(error):
            return generate_variations(
                [{"id": "doc", "document": "Base document"}],
                [{"id": "jb", "text": "<INSTRUCTION>"}],
                [{"id": "instr", "instruction": "Sample instruction"}],
                ["end"],
                ["\n<PLACEHOLDER>\n"],
                ["none"],
                [("stub", FailingPlugin(error))],
                adv_prefixes=[None],
                adv_suffixes=[None],
                output_format="user-input",
                plugin_only=True,
                num_threads=2,
            )

        with self.assertRaises(FatalPluginError):
            run(FatalPluginError("fatal"))
        dataset, _ = run(RuntimeError("ordinary"))
        self.assertEqual(dataset, [])

    def test_dataset_missing_source_fails_in_serial_and_threaded_paths(self):
        for threads in (1, 2):
            with self.subTest(threads=threads):
                self.assert_error(
                    self.dataset(
                        "--threads", str(threads), options="source=missing.pdf"
                    ),
                    "document_forge:",
                )

    def test_generated_dataset_and_integrity_results(self):
        result = self.dataset()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        datasets = list((self.root / "datasets").glob("*.jsonl"))
        self.assertEqual(len(datasets), 1)
        rows = [json.loads(line) for line in datasets[0].read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        value = rows[0].get("content", rows[0].get("text"))
        self.assertTrue(value.startswith(artifacts.PREFIX))
        target = DocumentArtifact()
        valid, meta = target.process_input(value)
        self.assertTrue(call_judge(rows[0], valid))
        self.assert_cli_integrity(datasets[0], "intact", True)
        Path(meta["record"]["generated_file"]).write_bytes(b"changed")
        valid, meta = target.process_input(value)
        self.assertFalse(call_judge(rows[0], valid))
        self.assertIn("changed", meta["reason"])
        self.assert_cli_integrity(datasets[0], "changed", False)

    def assert_cli_integrity(self, dataset, tag, expected):
        result = self.cli(
            "test",
            "--dataset",
            str(dataset),
            "--target",
            "document_artifact",
            "--threads",
            "1",
            "--no-auto-resume",
            "--tag",
            tag,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = []
        for file in (self.root / "results").glob("*.jsonl"):
            records.extend(json.loads(line) for line in file.read_text().splitlines())
        matches = [row for row in records if row.get("success") is expected]
        self.assertTrue(matches, records)
        self.assertEqual(matches[0]["meta"]["check"], "artifact_integrity")
        if not expected:
            self.assertIn("changed", matches[0]["meta"]["reason"])

    def test_invalid_records_return_false_but_target_options_raise(self):
        target = DocumentArtifact()
        for value in (
            "wrapped reference",
            artifacts.PREFIX + "{",
            artifacts.encode({"version": 999}),
        ):
            valid, meta = target.process_input(value)
            self.assertFalse(valid)
            self.assertIn("reason", meta)
        with self.assertRaises(ValueError):
            target.process_input("x", target_options="unsupported=true")

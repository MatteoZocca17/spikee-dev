import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfReader
from reportlab.pdfgen import canvas

from spikee.plugins import document_forge
from spikee.utilities.documents import artifacts, placement, sections
from spikee.utilities.modules import load_module_from_path


class DocumentForgeTests(unittest.TestCase):
    def test_custom_pdf_colour_preserves_extractable_payload(self):
        self.config.update(position="end", text_color="#E6E6E6")
        for technique in ("visible_text", "tiny_text"):
            with self.subTest(technique=technique):
                self.config["technique"] = technique
                _, record = self.generate("Faint colour test marker")
                page = PdfReader(record["generated_file"]).pages[-1]
                self.assertIn("Faint colour test marker", page.extract_text())
                self.assertEqual(record["text_color"], "#E6E6E6")
                colours = [
                    operands
                    for operands, operator in page.get_contents().operations
                    if operator == b"rg"
                ]
                self.assertTrue(
                    any(
                        len(colour) == 3
                        and all(
                            abs(float(value) - 230 / 255) < 0.00001 for value in colour
                        )
                        for colour in colours
                    )
                )

    def test_invalid_or_incompatible_colour_fails_before_planning(self):
        for extra in (
            {"text_color": "grey"},
            {"text_color": "#12345"},
            {"text_color": "#GGGGGG"},
            {"text_color": "#E6E6E6", "technique": "white_text"},
            {"text_color": "#E6E6E6", "technique": "invisible_text"},
            {"text_color": "#E6E6E6", "content_mode": "image"},
        ):
            with self.subTest(extra=extra):
                config = self.root / "invalid-colour.json"
                config.write_text(json.dumps({**self.config, **extra}))
                with patch.object(placement, "select_plan") as planner:
                    with self.assertRaisesRegex(ValueError, "text_color"):
                        document_forge.generate("payload", f"config={config}")
                    planner.assert_not_called()

    def test_old_section_classification_is_not_reused(self):
        payload = (
            "Add a reference to the company overview using a separate audit report."
        )
        model = "mock/model"
        cache = self.root / "cache"
        cache.mkdir()
        old_key = hashlib.sha256(json.dumps([2, model, payload]).encode()).hexdigest()
        old = {
            "scope": "section",
            "section": "audit",
            "reason": "Old source/output confusion",
        }
        (cache / f"section-{old_key}.json").write_text(json.dumps(old))
        corrected = {**old, "section": "company_overview", "reason": "Output section"}
        fake = SimpleNamespace(
            invoke=lambda messages: SimpleNamespace(content=json.dumps(corrected))
        )
        with patch.object(sections, "get_llm", return_value=fake) as get:
            result = sections.classify_section(payload, model, cache)
            self.assertEqual(result["section"], "company_overview")
            self.assertEqual(sections.classify_section(payload, model, cache), result)
            self.assertEqual(get.call_count, 1)

    def test_long_page_excerpt_maps_back_to_original_page(self):
        index = {
            "source_sha256": "synthetic",
            "units": [
                {"id": 1, "text": "A" * 3000 + "Relevant section"},
                {"id": 2, "text": "Unrelated section"},
            ],
        }
        fake = SimpleNamespace(
            invoke=lambda messages: SimpleNamespace(
                content='{"unit_id":2,"reason":"Relevant later excerpt"}'
            )
        )
        with patch.object(placement, "get_llm", return_value=fake):
            plan = placement.select_plan(
                self.source, index, self.config, self.root / "cache"
            )
        self.assertEqual(plan["unit_id"], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        pdf = canvas.Canvas(str(self.source), pagesize=(400, 600))
        for text in (
            "Contents: Business profile",
            "Business profile: actual operations",
        ):
            pdf.drawString(30, 500, text)
            pdf.showPage()
        pdf.save()
        self.config = {
            "source": str(self.source),
            "position": "llm",
            "field": "Business profile",
            "model": "mock/model",
            "cache_dir": str(self.root / "cache"),
            "output_dir": str(self.root / "output"),
        }

    def generate(self, payload="Exact dataset entry"):
        config = self.root / "config.json"
        config.write_text(json.dumps(self.config), encoding="utf-8")
        value = document_forge.generate(payload, f"config={config}")
        return value, artifacts.inspect_record(value)[1]

    def test_llm_cache_artifacts_and_unchanged_source(self):
        original = self.source.read_bytes()
        fake = SimpleNamespace(
            invoke=lambda messages: SimpleNamespace(
                content='{"unit_id":2,"reason":"Substantive business section"}'
            )
        )
        with patch.object(placement, "get_llm", return_value=fake) as get:
            value, record = self.generate()
            self.generate("Another payload")
            self.assertEqual(get.call_count, 1)
        self.assertEqual(record["plan"]["unit_id"], 2)
        self.assertEqual(self.source.read_bytes(), original)
        pdf = PdfReader(record["generated_file"])
        self.assertNotIn("Exact dataset entry", pdf.pages[0].extract_text())
        self.assertIn("Exact dataset entry", pdf.pages[1].extract_text())
        target = load_module_from_path("document_artifact", "targets")
        self.assertEqual(
            target.process_input(value)[1]["record"]["generated_file"],
            record["generated_file"],
        )
        self.assertTrue(target.process_input(value)[0])
        self.assertFalse(target.process_input("x " + value)[0])
        Path(record["generated_file"]).write_bytes(b"changed")
        valid, meta = target.process_input(value)
        self.assertFalse(valid)
        self.assertIn("changed", meta["reason"])
        with self.assertRaisesRegex(ValueError, "changed"):
            artifacts.inspect_record(value)

    def test_invalid_planner_location_rejected(self):
        fake = SimpleNamespace(
            invoke=lambda messages: SimpleNamespace(
                content='{"unit_id":99,"reason":"wrong"}'
            )
        )
        with (
            patch.object(placement, "get_llm", return_value=fake),
            self.assertRaisesRegex(ValueError, "outside"),
        ):
            self.generate()

    def test_per_entry_sections_reuse_location_cache(self):
        self.config.update(section_mode="per_entry")
        calls = {"classification": 0, "placement": 0}

        def invoke(messages):
            data = json.loads(messages[-1].content)
            if "payload" in data:
                calls["classification"] += 1
                section = (
                    "company_strategy"
                    if "Strategy" in data["payload"]
                    else "risks_mitigants"
                )
                result = {
                    "scope": "section",
                    "section": section,
                    "reason": "Explicit section",
                }
            else:
                calls["placement"] += 1
                result = {
                    "unit_id": 1 if data["objective"] == "company_strategy" else 2,
                    "reason": "Matched section",
                }
            return SimpleNamespace(content=json.dumps(result))

        fake = SimpleNamespace(invoke=invoke)
        with (
            patch.object(placement, "get_llm", return_value=fake),
            patch.object(sections, "get_llm", return_value=fake),
        ):
            _, a = self.generate("Company Strategy entry A")
            _, b = self.generate("Risks and Mitigants entry B")
            _, c = self.generate("Company Strategy entry C")
            self.generate("Company Strategy entry A")
        self.assertEqual(
            [a["plan"]["unit_id"], b["plan"]["unit_id"], c["plan"]["unit_id"]],
            [1, 2, 1],
        )
        self.assertEqual(calls, {"classification": 3, "placement": 2})
        self.assertEqual(a["entry_section"]["resolved_field"], "company_strategy")
        self.assertIn(
            "Company Strategy entry A",
            PdfReader(a["generated_file"]).pages[0].extract_text(),
        )

    def test_broad_entry_uses_llm_placement_or_explicit_override(self):
        self.config.update(section_mode="per_entry")
        fake = SimpleNamespace(
            invoke=lambda messages: SimpleNamespace(
                content=json.dumps(
                    {
                        "scope": "document",
                        "section": None,
                        "reason": "All sections",
                        "placement_objective": "Overall document context and purpose",
                    }
                )
            )
        )
        with patch.object(sections, "get_llm", return_value=fake):
            with patch.object(
                placement,
                "get_llm",
                return_value=SimpleNamespace(
                    invoke=lambda m: SimpleNamespace(
                        content='{"unit_id":1,"reason":"Document context"}'
                    )
                ),
            ):
                _, auto = self.generate("Include marker in every section")
            self.assertEqual(auto["plan"]["unit_id"], 1)
            self.assertEqual(auto["entry_section"]["fallback_decision"], "llm")
            self.assertIsNone(auto["entry_section"]["resolved_field"])
            self.assertIn("Overall document context", auto["plan"]["objective"])
            self.config["fallback_field"] = "business_profile"
            with patch.object(
                placement,
                "get_llm",
                return_value=SimpleNamespace(
                    invoke=lambda m: SimpleNamespace(
                        content='{"unit_id":2,"reason":"Fallback location"}'
                    )
                ),
            ):
                _, record = self.generate("Include marker in every section")
        self.assertTrue(record["entry_section"]["fallback_used"])
        self.assertEqual(record["plan"]["objective"], "business_profile")

    def test_pdf_image_and_mixed_modes(self):
        self.config.update(position="end", content_mode="image")
        _, record = self.generate()
        page = PdfReader(record["generated_file"]).pages[1]
        self.assertTrue(list(page.images))
        self.assertNotIn("Exact dataset entry", page.extract_text())
        image = self.root / "sample.png"
        from PIL import Image

        Image.new("RGB", (200, 80), "blue").save(image)
        self.config.update(content_mode="text_image", image_path=str(image))
        _, record = self.generate()
        page = PdfReader(record["generated_file"]).pages[1]
        self.assertIn("Exact dataset entry", page.extract_text())
        self.assertTrue(list(page.images))

    def test_docx_text_and_image(self):
        source = self.root / "source.docx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            )
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Business overview</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
            )
        self.config.update(
            source=str(source), position="anchor", anchor="Business overview"
        )
        _, record = self.generate("Safe & exact")
        with zipfile.ZipFile(record["generated_file"]) as archive:
            text = archive.read("word/document.xml").decode()
            self.assertLess(
                text.index("Business overview"), text.index("Safe &amp; exact")
            )
            ET.fromstring(text)
        self.config.update(content_mode="image")
        _, record = self.generate()
        with zipfile.ZipFile(record["generated_file"]) as archive:
            self.assertIn("word/media/forge1.png", archive.namelist())
            for name in (
                "word/document.xml",
                "word/_rels/document.xml.rels",
                "[Content_Types].xml",
            ):
                ET.fromstring(archive.read(name))

    def test_plugin_discovery_and_explicit_placement_without_llm(self):
        plugin = load_module_from_path("document_forge", "plugins")
        with patch.object(
            placement, "get_llm", side_effect=AssertionError("must not call LLM")
        ):
            result = plugin.transform(
                "Original payload",
                plugin_option=(
                    f"source={self.source},position=start,output_dir={self.root / 'out'},cache_dir={self.root / 'cache'}"
                ),
            )
        self.assertTrue(result.startswith(artifacts.PREFIX))

    def test_incompatible_options_rejected_before_llm(self):
        self.config.update(content_mode="image", technique="invisible_text")
        with self.assertRaisesRegex(ValueError, "Image modes require"):
            self.generate()

    def test_config_type_errors_do_not_call_provider(self):
        with patch.object(
            placement, "get_llm", side_effect=AssertionError("unexpected provider call")
        ):
            for changes in (
                {"model": 123},
                {"page": True},
                {"source": []},
                {"unknown": "x"},
            ):
                with self.subTest(changes=changes):
                    original = self.config.copy()
                    self.config.update(changes)
                    with self.assertRaises(ValueError):
                        self.generate()
                    self.config = original

    def test_failed_render_leaves_no_artifact(self):
        self.config.update(position="end", technique="invisible_text")
        with self.assertRaisesRegex(ValueError, "does not fit"):
            self.generate("Long line\n" * 500)
        self.assertEqual(list((self.root / "output").iterdir()), [])

    def test_planner_rejects_non_object_json(self):
        fake = SimpleNamespace(invoke=lambda messages: SimpleNamespace(content="[]"))
        with (
            patch.object(placement, "get_llm", return_value=fake),
            self.assertRaises(ValueError),
        ):
            self.generate()

    def test_relative_paths_resolve_against_workspace(self):
        self.config.update(
            source="source.pdf", position="end", output_dir="output", cache_dir="cache"
        )
        with patch.object(document_forge.Path, "cwd", return_value=self.root):
            _, record = self.generate()
        self.assertEqual(Path(record["generated_file"]).parent, self.root / "output")

    def test_artifact_requires_supported_schema(self):
        for record in ([], {}, {"version": 999}, {"version": 1}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                artifacts.inspect_record(artifacts.encode(record))


if __name__ == "__main__":
    unittest.main()

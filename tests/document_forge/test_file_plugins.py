import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from spikee.plugins.image_forge import ImageForge
from spikee.plugins.metadata_forge import MetadataForge
from spikee.templates.plugin import FatalPluginError
from spikee.utilities.documents.artifacts import inspect_record
from spikee.utilities.modules import load_module_from_path


class FilePluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        pdf = canvas.Canvas(str(self.source))
        pdf.setTitle("Original title")
        pdf.setAuthor("Original author")
        pdf.drawString(30, 700, "Original document content")
        pdf.save()
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def generate(self, plugin, options, payload="RASTER TEST MARKER 123"):
        config = self.root / "config.json"
        config.write_text(json.dumps({"output_dir": str(self.root / "out"), **options}))
        encoded = plugin.transform(payload, plugin_option=f"config={config}")
        return inspect_record(encoded)[1]

    def test_metadata_roundtrip_preserves_pages_and_other_fields(self):
        record = self.generate(
            MetadataForge(), {"source": str(self.source)}, "Metadata café test"
        )
        result = PdfReader(record["generated_file"])
        self.assertEqual(result.metadata.subject, "Metadata café test")
        self.assertEqual(result.metadata.title, "Original title")
        self.assertEqual(result.metadata.author, "Original author")
        original_page = PdfReader(self.source).pages[0]
        self.assertEqual(
            result.pages[0].get_contents().get_data(),
            original_page.get_contents().get_data(),
        )
        self.assertNotIn("Metadata café test", result.pages[0].extract_text())
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash
        )

    def test_all_metadata_fields_and_consecutive_unique_outputs(self):
        options = {"source": str(self.source), "field": "all"}
        first = self.generate(MetadataForge(), options)
        second = self.generate(MetadataForge(), options)
        self.assertNotEqual(first["generated_file"], second["generated_file"])
        metadata = PdfReader(first["generated_file"]).metadata
        for key in ("/Title", "/Subject", "/Keywords"):
            self.assertEqual(metadata[key], "RASTER TEST MARKER 123")

    def test_png_is_pixels_and_contains_no_text_metadata(self):
        template = self.root / "template.png"
        info = PngInfo()
        info.add_text("Comment", "source metadata must not be copied")
        Image.new("RGB", (300, 300), "blue").save(template, pnginfo=info)
        for layout in ("append", "overlay"):
            record = self.generate(
                ImageForge(),
                {
                    "template": str(template),
                    "layout": layout,
                    "width": 300,
                    "text_color": "#E6E6E6",
                    "font_size": 16,
                },
            )
            with Image.open(record["generated_file"]) as result:
                self.assertEqual(result.info, {})
                self.assertGreater(len(result.getcolors(maxcolors=100000)), 1)
                if layout == "append":
                    self.assertGreater(result.height, 300)
                    self.assertEqual(result.getpixel((0, 0)), (0, 0, 255))
                else:
                    self.assertEqual(result.size, (300, 300))
            self.assertNotIn(
                b"RASTER TEST MARKER", Path(record["generated_file"]).read_bytes()
            )

    def test_pdf_contains_image_but_no_selectable_payload(self):
        record = self.generate(
            ImageForge(),
            {
                "source": str(self.source),
                "output_format": "pdf",
                "position": "end",
            },
        )
        result = PdfReader(record["generated_file"])
        self.assertEqual(len(result.pages), 1)
        self.assertIn("Original document content", result.pages[0].extract_text())
        self.assertNotIn("RASTER TEST MARKER", result.pages[0].extract_text())
        self.assertGreater(len(result.pages[0].images), 0)
        self.assertIsNone(record["image_path"])
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash
        )

    def test_invalid_settings_are_fatal(self):
        for options in (
            {"font_size": 0},
            {"font_size": True},
            {"font_size": 2.5},
            {"text_color": "red"},
            {"width": 64, "padding": 50},
            {"layout": "unknown"},
            {"template": "missing.png"},
            {"output_format": "pdf"},
            {"position": "end"},
            {"unknown": "option"},
        ):
            with self.subTest(options=options), self.assertRaises(FatalPluginError):
                self.generate(ImageForge(), options)
        with self.assertRaises(FatalPluginError):
            self.generate(
                MetadataForge(), {"source": str(self.source), "field": "bogus"}
            )
        writer = PdfWriter(clone_from=self.source)
        writer.encrypt("password")
        encrypted = self.root / "encrypted.pdf"
        writer.write(encrypted)
        with self.assertRaises(FatalPluginError):
            self.generate(MetadataForge(), {"source": str(encrypted)})

    def test_overlay_rejects_clipping_and_long_tokens_wrap(self):
        template = self.root / "small.png"
        Image.new("RGB", (100, 40), "white").save(template)
        with self.assertRaises(FatalPluginError):
            self.generate(
                ImageForge(),
                {"template": str(template), "layout": "overlay"},
                "x" * 200,
            )
        record = self.generate(ImageForge(), {"width": 200}, "x" * 200)
        self.assertGreater(record["dimensions"][1], 100)

    def test_discovery_and_cli(self):
        for name in ("metadata_forge", "image_forge"):
            self.assertTrue(load_module_from_path(name, "plugins").TERMINAL_OUTPUT)
            options = f"{name}:output_dir={self.root / 'cli'}"
            if name == "metadata_forge":
                options += f",source={self.source}"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from spikee.cli import main; main()",
                    "generate",
                    "plugin",
                    "-i",
                    "CLI test marker",
                    "--plugins",
                    name,
                    "--plugin-options",
                    options,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SPIKEE_DOCUMENT_V1:", result.stdout)

    def test_standalone_dataset_generation(self):
        seeds = self.root / "seeds"
        seeds.mkdir()
        for name in ("base_user_inputs", "jailbreaks", "instructions"):
            (seeds / f"{name}.jsonl").write_text("")
        (seeds / "standalone_user_inputs.jsonl").write_text(
            json.dumps(
                {
                    "id": "raster-test",
                    "text": "Synthetic dataset marker",
                    "judge_name": "canary",
                    "judge_args": "Synthetic dataset marker",
                }
            )
            + "\n"
        )
        for name in ("metadata_forge", "image_forge"):
            options = f"{name}:output_dir={self.root / name}"
            if name == "metadata_forge":
                options += f",source={self.source}"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from spikee.cli import main; main()",
                    "generate",
                    "--seed-folder",
                    str(seeds),
                    "--include-standalone-inputs",
                    "--plugin-only",
                    "--plugins",
                    name,
                    "--plugin-options",
                    options,
                    "--tag",
                    name,
                ],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sidecars = list((self.root / name).glob("*.json"))
            self.assertEqual(len(sidecars), 1)
            self.assertEqual(json.loads(sidecars[0].read_text())["plugin"], name)


if __name__ == "__main__":
    unittest.main()

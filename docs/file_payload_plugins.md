# Metadata and raster image payloads

Install this checkout with `python -m pip install -e ".[documents]"`.
These plugins generate local files and return the same `SPIKEE_DOCUMENT_V1:`
artifact records as Document Forge. They do not upload files or call an LLM by
default. Source files are never overwritten. Outputs have unique names and JSON
sidecars with integrity hashes; sidecars do not contain the original payload.

## PDF metadata: `metadata_forge`

```powershell
spikee generate plugin -i "Synthetic metadata instruction." `
  --plugins metadata_forge `
  --plugin-options "metadata_forge:source=template.pdf,field=subject"
```

| Option | Meaning | Default |
| --- | --- | --- |
| `source` | Existing unencrypted, nonempty PDF | Required |
| `field` | `title`, `subject`, `keywords`, or `all` | `subject` |
| `output_dir` | Destination for PDF copies and sidecars | `generated-files/metadata-forge` |
| `config` | JSON configuration file; explicit CLI options override it | None |

The payload replaces the selected Document Info field(s); other metadata and page
content are preserved. This does not inject into XMP, annotations, attachments,
or visible page text. Existing XMP may contain different values: ingestion tools
can prioritise XMP or ignore metadata entirely. Page content streams and metadata
are checked after writing. Digital signatures are not preserved as valid after
modification; use unsigned templates for testing.

## Raster prompts: `image_forge`

Standalone PNG with a custom font size and colour:

```powershell
spikee generate plugin -i "Synthetic image instruction." `
  --plugins image_forge `
  --plugin-options "image_forge:font_size=24,text_color=#E6E6E6"
```

Embed the raster prompt in a PDF copy:

```powershell
spikee generate plugin -i "Synthetic image instruction." `
  --plugins image_forge `
  --plugin-options "image_forge:output_format=pdf,source=template.pdf,position=end"
```

| Option | Meaning | Default |
| --- | --- | --- |
| `output_format` | `png` or `pdf` | `png` |
| `font_size` | Font size in image pixels, 1–256 | `24` |
| `font_path` | Local TrueType/OpenType font | ReportLab's bundled Vera font |
| `width` | Image canvas width in pixels, 64–4096 | `1200` |
| `padding` | Text padding in pixels, 0–512 | `24` |
| `text_color` | Text colour as `#RRGGBB` | `#000000` |
| `background_color` | Background colour as `#RRGGBB` | `#FFFFFF` |
| `template` | Optional background image, e.g. PNG or JPEG | None |
| `layout` | `append` below the image, or `overlay` on it | `append` |
| `source` | PDF template, required for PDF output | None |
| `output_dir` | Destination for files and sidecars | `generated-files/image-forge` |
| `config` | JSON configuration file; CLI options override it | None |

For PDF output, the placement options from [Document Forge](document_forge.md)
also apply: `position`, `page`, `anchor`, `anchor_occurrence`, `relative`, `model`,
`field`, `instruction`, `section_mode`, `fallback_field`, and `cache_dir`. The
default PDF position is `llm`, which requires a model and placement objective;
`position=end` needs neither. LLM placement can send template excerpts (and the
payload in per-entry mode) to the configured provider.

The prompt is drawn into pixels: no selectable text, OCR layer, alt text, or PNG
text metadata is added for it. OCR and vision models may still read those pixels.
Existing PDF template text remains selectable. PDF images use Document Forge's
reserved band below the selected page, not an overlay on the original PDF body.
Font size is measured in PNG pixels; PDF placement scales the image to page width.

An image template retains its dimensions for overlay; append expands the canvas
if necessary. Text wraps, including long unbroken strings, and preserves line
breaks. Overlays that would clip the payload fail. Images are limited to 40 million
pixels and 16,384 pixels in height; PDF placement has its own image-height limit.
Vera has limited character coverage; provide a suitable font for other scripts.
Rendered appearance and OCR readability still require review on your target.

## Dataset generation and composition

Both plugins are terminal file producers. Use standalone seeds with empty
`base_user_inputs.jsonl`, `jailbreaks.jsonl`, and `instructions.jsonl`:

```powershell
spikee generate --seed-folder datasets/my-seeds `
  --include-standalone-inputs --plugin-only --plugins image_forge `
  --plugin-options "image_forge:output_format=png,font_size=24" `
  --tag raster-prompts
```

Substitute `metadata_forge` and its options to generate metadata variants. Do not
use prefixes/suffixes or place a text plugin after these plugins. Existing text
transformations can run before rendering, e.g. `--plugins "base64~image_forge"`.
`exclude_patterns` is ignored by the file producers: the incoming payload is
embedded in full. Each invocation produces one artifact; run explicit settings
separately instead of implicitly generating every option combination.

All paths, including paths in JSON configurations, resolve against the current
working directory. Use JSON for values containing commas or semicolons.

`spikee test --dataset <dataset.jsonl> --target document_artifact` checks file
integrity for PNG and PDF records. It does not send images to a vision model or
measure attack success. Application targets must open and upload the generated
file. For Spikee's existing native base64 image content, use `text2image` instead.

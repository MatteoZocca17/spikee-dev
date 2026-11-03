# Document Forge

Document Forge creates PDF or DOCX copies containing supplied instruction text.
Give it a template and a placement objective: a Spikee LLM provider selects a
page or paragraph, then Python inserts the text. The model does not rewrite the
payload or generate editing code. Explicit placement works without an LLM.

## Install and try

From this checkout, install into your development environment:

```sh
python -m pip install -e ".[documents]"
```

From your Spikee workspace, provide an existing `template.pdf` and run:

```sh
spikee generate plugin -i "Document Forge sample instruction." --plugins document_forge --plugin-options "document_forge:source=template.pdf,position=end"
```

This creates a copy under `generated-files/document-forge`, plus a JSON sidecar.
The command returns a `SPIKEE_DOCUMENT_V1:` artifact record containing the path,
hashes and placement decision. It does not upload the document to an application.
The source remains unchanged.

For LLM placement, copy [the example config](../examples/document-forge/config.json)
to your workspace. Set `source`, replace `provider/model` with a configured Spikee
provider/model identifier, and describe the desired section in `field` (or use
`instruction` for a detailed placement objective). Configure credentials using
your provider's normal environment variables.

```sh
spikee generate plugin -i "Document Forge sample instruction." --plugins document_forge --plugin-options "document_forge:config=config.json"
```

Relative paths, including those inside a config file, resolve against the current
workspace directory. Explicit plugin options override JSON settings. Use JSON for
values containing commas or semicolons because Spikee treats these as delimiters.

## Placement and rendering

| Option | Meaning | Default |
| --- | --- | --- |
| `source` | Existing PDF/DOCX path | Required |
| `position` | `llm`, `auto` (alias), `start`, `end`, `page`, `anchor` | `llm` |
| `field` / `instruction` | Placement objective; instruction takes precedence | Required for fixed LLM placement |
| `model` | Spikee provider/model identifier | Required for LLM use |
| `page` | One-based PDF page, with `position=page` | None |
| `anchor` | Text to match, with `position=anchor` | None |
| `anchor_occurrence` | One-based matching page or paragraph | `1` |
| `relative` | DOCX insertion `before` or `after` a paragraph | `after` |
| `section_mode` | `fixed` objective or `per_entry` classification | `fixed` |
| `fallback_field` | Optional fixed section for broad per-entry instructions | LLM chooses context |
| `technique` | `visible_text`, `white_text`, `tiny_text`; PDF `invisible_text`; DOCX `hidden_text` | `visible_text` |
| `text_color` | PDF text colour as `#RRGGBB`, with `visible_text` or `tiny_text` and `content_mode=text` | Technique default |
| `content_mode` | `text`, `image`, `text_image` | `text` |
| `image_path` | Supplied image; required for `text_image` | None |
| `output_dir` | Copies and JSON decision records | `generated-files/document-forge` |
| `cache_dir` | Extracted text and placement cache | `generated-files/document-cache` |

`section_mode=per_entry` classifies the payload's intended section, then locates
it in the template. It supersedes `field` and `instruction`. Broad or ambiguous
entries use a model-selected context unless `fallback_field` is provided. Each
entry produces one document with one insertion, even for document-wide instructions.

Image mode renders the payload as an image, or inserts `image_path` instead of
the payload when supplied. Mixed mode inserts both the text and supplied image.
Image modes require `technique=visible_text`.

For faint grey text on a white background, use
`technique=visible_text,text_color=#E6E6E6`. This changes the text colour, not
opacity or placement: visible PDF text still occupies a reserved band below the
selected page. `tiny_text` also supports a custom colour. Background colours are
not detected automatically. DOCX, image modes, `white_text`, and `invisible_text`
reject `text_color` instead of silently ignoring it. Without this option, existing
colours remain unchanged.

## Dataset integration

Use standalone entries in your seed folder and run `spikee generate` with
`--include-standalone-inputs --plugin-only --plugins document_forge` and the same
plugin options. Keep the default `--format user-input`. Any text transformations
must happen before Document Forge. Do not wrap or transform the resulting artifact
record. Both generation commands reject pipes where a terminal-output plugin
is followed by another plugin. Dataset generation also rejects prefixes/suffixes,
nonempty composable document permutations, and a missing
`--include-standalone-inputs` flag. These checks run before generation; no requested
plugin or document permutations are silently excluded.

For a standalone-only seed folder, keep `base_user_inputs.jsonl`,
`jailbreaks.jsonl` and `instructions.jsonl` present but empty, and place your entries
in `standalone_user_inputs.jsonl`. Alternatively, existing instruction/jailbreak
filters can exclude the composable entries.

Typical startup errors identify:

- A terminal plugin that must be the last element of a pipe.
- Terminal plugins incompatible with prefixes/suffixes (`--include-fixes`).
- Terminal plugins that cannot be applied to document entries.
- Terminal plugins that require `--include-standalone-inputs`.
- Terminal plugins that require `--plugin-only`, so untransformed entries are not
  added to the dataset alongside artifact references.

Document Forge failures abort either generation command with exit code 1 and an
error message, in both serial and multithreaded (`--threads`) generation. Already-created artifacts are not rolled back. Other plugins keep
their existing warning-and-skip behaviour unless they opt into `FatalPluginError`.
`exclude_patterns` is ignored because Document Forge embeds the original payload
without performing text transformations.

`document_artifact` is an integrity-check target:

```sh
spikee test --dataset datasets/your-generated-dataset.jsonl --target document_artifact --threads 1 --no-auto-resume
```

The target returns `(True, metadata)` for an intact file, or `(False, metadata)`
for an invalid reference, malformed record, or missing/altered file. Failure
metadata contains `reason`; success metadata contains the original `record`.
Nonempty target options remain configuration errors. Spikee uses the boolean
directly, so the reported success rate measures artifact integrity.

Integrity validation is not a prompt-injection success result. To evaluate an
application, implement a target that reads the artifact record, uploads the file
through that application's interface, and returns its response to your judges.

## Data handling and limits

- Fixed LLM placement sends extracted document excerpts and the placement objective
  to the selected provider. Per-entry mode also sends the payload for classification.
  The prompts treat both document text and payloads as untrusted data. Selected IDs
  are validated against the supplied candidates; no model-generated code is executed.
- Extracted text is cached by source hash. Placement is cached by source hash,
  objective, model and relative position. Changing provider settings under the same
  identifier may require clearing the cache. Cache directories contain document text.
- Visible PDF insertion adds a reserved band below a selected page and increases
  its height. It does not edit or reflow existing paragraphs. Hidden text must fit
  within the selected page. Fonts cover a limited character set; PDF text that fails
  extraction verification is rejected. Image text is not OCR-verified.
- DOCX insertion is paragraph-based and currently expects the standard `w:` prefix.
  Complex text boxes, nested paragraphs, unusual namespace prefixes and intricate
  table layouts are outside verified support. Templates with no supported paragraphs
  are rejected. Visually inspect output before using a new template family.
- Scanned PDFs need OCR before LLM placement. The planner uses text, not vision.
  Large files are reviewed in bounded batches; the first run can require multiple
  provider calls. Provider errors stop generation without automatic retries.
- Set `SPIKEE_API_TIMEOUT` (seconds) to bound the provider request, including
  client retries. The shared provider bridge reuses the client event loop across requests
  and does not wait for unrelated background tasks after a response. Pending
  tasks are cancelled when the provider is released, after closing its AnyLLM
  client on the same loop. Client cleanup has a five-second timeout.
  Cancellation cleanup can extend the elapsed time; this is not a hard process
  deadline. Without this setting, no request deadline is added by the bridge.
- Artifact records use absolute local paths. Moving artifacts between machines
  requires updating references. Output has structural/text checks, not automatic
  visual approval. `visual_review` in each sidecar records this limitation.

## Development

`spikee/plugins/document_forge.py` owns configuration and generation orchestration.
Its helpers live under `spikee/utilities/documents/`: `placement.py` and
`sections.py` select locations through Spikee providers; `pdf.py`, `docx.py`, and
`images.py` handle formats; `artifacts.py` handles output references and hashes.
The private `_progress.py` helper keeps provider-wait messages off standard output.
Optional document libraries load when generation starts, so plugin discovery
does not require the `documents` extra.

```sh
python -m unittest discover -s tests/document_forge -v
```

Tests generate synthetic templates and mock the LLM; no assessment documents or
credentials are required. CI runs these checks on Windows and Linux. Live provider
selection and visual appearance on representative templates need separate checks.

# Doc → PPTX agent

`doc-to-pptx-agent` — converts a PDF or TXT document into a PowerPoint presentation. It
extracts real embedded images from the PDF and can fall back to NVIDIA NIM FLUX to generate
images for slides that don't have one.

**Dependencies** (installed on first spawn): `pymupdf`, `pdfplumber`, `pillow`.

## Spawn

```text
@catalog spawn doc-to-pptx-agent
```

## Usage

Give it a source file and where to save the deck:

```text
turn /docs/report.pdf into a 10-slide deck at /out/report.pptx
```

## Structured operations

| Field | Meaning |
|-------|---------|
| `file_path` | absolute path to the source PDF or TXT |
| `output_path` | where to save the `.pptx` |
| `slide_count` | target slides (default 8) |
| `theme` | e.g. `dark executive`, `minimal light` |
| `nim_fallback` | use NIM images for slides without a PDF image (default true) |
| `min_img_width` / `min_img_height` | minimum px size to accept a PDF image (default 200 / 150) |

Returns `pptx_path`, `slide_count`, `title`, `images_extracted`, `images_generated`, and
`error`.

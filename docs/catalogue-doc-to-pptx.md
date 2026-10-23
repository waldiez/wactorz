# Doc → PPTX agent

`doc-to-pptx-agent` — converts a PDF or TXT document into a PowerPoint presentation. It
extracts real embedded images from the PDF and can fall back to NVIDIA NIM FLUX to generate
images for slides that don't have one.

**Dependencies** (installed on first spawn): `python-pptx`, `pymupdf`, `pdfplumber`, `pillow`.
The deck is built in Python; nothing else needs installing.

## Spawn

```text
@catalog spawn doc-to-pptx-agent
```

## Usage

Give it a source file and where to save the deck:

```text
turn ~/Documents/report.pdf into a 10-slide deck at ~/Documents/report.pptx
```

Or attach the PDF or text file in the chat and ask for a deck; it is passed on by its upload id.

## Where it reads and writes

The request is written by a model, from whatever it was reading, so the agent reads only

- files inside the *document folders*: those named in `WACTORZ_DOCUMENT_DIRS` (separated like
  `PATH`: `:` on Linux and macOS, `;` on Windows), or, when it is unset, your Documents folder;
- files attached in the chat, by their upload id;

and writes a deck only as a `.pptx` file inside a document folder, into a folder that already
exists. With no `output_path` the deck goes to a new private temp folder, and its path comes back
as `pptx_path`. Nothing under the state directory is ever read or written, even when a document
folder contains it. Paths are compared after following symlinks.

Windows can move the Documents folder (to OneDrive, for one); name the moved folder in
`WACTORZ_DOCUMENT_DIRS`. On a node, the node's own variable and Documents folder apply, and
uploads, which are kept on the server, cannot be read there.

## Structured operations

| Field | Meaning |
|-------|---------|
| `file_path` | path to the source PDF or TXT, inside a document folder |
| `file_id` | or: the upload id of a file attached in the chat |
| `output_path` | a `.pptx` path inside a document folder (default: a new temp folder) |
| `slide_count` | target slides (default 8) |
| `theme` | e.g. `dark executive`, `minimal light` |
| `nim_fallback` | use NIM images for slides without a PDF image (default true) |
| `min_img_width` / `min_img_height` | minimum px size to accept a PDF image (default 200 / 150) |

Returns `pptx_path`, `slide_count`, `title`, `images_extracted`, `images_generated`, and
`error`.

"""DEMO AGENT — doc-to-pptx-agent  (v2 — PDF image extraction + NIM fallback)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Converts a PDF or TXT document into a polished PowerPoint presentation.

Image strategy (priority order):
  1. Extract real embedded images from the PDF (via PyMuPDF)
     - Filters out tiny decorations (< MIN_IMG_W × MIN_IMG_H pixels)
     - Distributes images across slides by source-page proximity
  2. For slides that got no real image → delegate to image-gen-agent (NIM FLUX)
  3. If image-gen-agent is not running → slide gets no image (text-only layout)

TXT files skip step 1 entirely and go straight to NIM generation.

This is a DEMO agent — do NOT modify the core framework files when refurnishing.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEPENDENCIES
────────────
Python (pip install):
    python-pptx      → import pptx   (builds the deck)
    pymupdf          → import fitz   (PDF image extraction + text)
    pdfplumber                        (PDF text extraction — more accurate for text)
    Pillow           → import PIL     (image resize / convert)

Optional (for NIM fallback):
    image-gen-agent must be spawned and running
    NIM API key persisted:  @main remember nim_api_key = nvapi-...

FILES
─────
Read from, and written to, only where wactorz.documents allows: a folder named
in WACTORZ_DOCUMENT_DIRS (by default the user's Documents folder), or a chat
upload by its id. Never anything under the state directory.


SPAWN CONFIG
────────────
{
  "name":        "doc-to-pptx-agent",
  "type":        "dynamic",
  "description": "Converts PDF or TXT documents into PowerPoint presentations. Extracts real embedded images from PDF first; falls back to NIM FLUX image generation for slides without images.",
  "capabilities": ["document_to_pptx", "pdf_to_presentation", "pptx_generation", "document_conversion"],
  "input_schema": {
    "file_path":       "str  — path to the source PDF or TXT, inside a document folder",
    "file_id":         "str  — or: the upload id of a PDF or TXT attached in the chat",
    "output_path":     "str  — a .pptx path inside a document folder; default a private temp dir",
    "slide_count":     "int  — target number of slides, default 8",
    "theme":           "str  — color theme hint, e.g. 'dark executive', 'minimal light', default 'dark executive'",
    "nim_fallback":    "bool — generate NIM images for slides without a real PDF image, default true",
    "min_img_width":   "int  — minimum pixel width to accept a PDF image, default 200",
    "min_img_height":  "int  — minimum pixel height to accept a PDF image, default 150"
  },
  "output_schema": {
    "pptx_path":         "str       — absolute path to the generated .pptx, or null on failure",
    "slide_count":       "int       — number of slides generated",
    "title":             "str       — detected presentation title",
    "images_extracted":  "int       — number of real images pulled from the PDF",
    "images_generated":  "int       — number of images generated via NIM",
    "error":             "str|null  — error message if failed"
  },
  "poll_interval": 3600,
  "code": "<the source of this file>"
}


TASK PAYLOAD EXAMPLES
──────────────────────
Minimal:
  { "file_path": "/home/user/Documents/report.pdf", "output_path": "/home/user/Documents/report.pptx" }

Full:
  {
    "file_path":      "/home/user/Documents/report.pdf",
    "output_path":    "/home/user/Documents/report.pptx",
    "slide_count":    10,
    "theme":          "minimal light",
    "nim_fallback":   true,
    "min_img_width":  300,
    "min_img_height": 200
  }

An attached file:
  { "file_id": "0f3c9a5e4b2d4c1e8a7b6c5d4e3f2a1b" }

No NIM (extracted images only, text-only for slides without one):
  {
    "file_path":    "/home/user/Documents/report.pdf",
    "output_path":  "/home/user/Documents/report.pptx",
    "nim_fallback": false
  }
"""

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from wactorz.documents import DocumentRefused, readable_document, writable_document

# ─────────────────────────────────────────────────────────────────────────────
# 1. DOCUMENT READING
# ─────────────────────────────────────────────────────────────────────────────


def _read_pdf_text(path):
    """Extract text from PDF using pdfplumber (best for clean text)."""
    import pdfplumber  # pyright: ignore[reportMissingImports]  # optional: installed with the recipe

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
    return "\n\n".join(pages)


def _read_txt(path: str) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode file: {path}")


def _is_pdf(path: str) -> bool:
    """Whether `path` is a PDF, by its first bytes: an upload has no extension to go by."""
    with Path(path).open("rb") as handle:
        return handle.read(5) == b"%PDF-"


def _read_document(path: str) -> str:
    if _is_pdf(path):
        return _read_pdf_text(path)
    return _read_txt(path)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PDF IMAGE EXTRACTION  (PyMuPDF)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_pdf_images(pdf_path: str, work_dir, min_w=200, min_h=150) -> list[dict[str, Any]]:
    """Extract embedded images from a PDF using PyMuPDF (fitz).

    Returns a list of dicts:
      { "path": "/tmp/.../img_p0_x7.png",
        "page": 0,           ← 0-based source page index
        "width": 1024,
        "height": 768 }

    Images smaller than min_w × min_h are skipped (logos, bullets, decorations).
    CMYK images are converted to RGB so they save cleanly as PNG.
    """
    import fitz  # pyright: ignore[reportMissingImports]  # optional: installed with the recipe (pymupdf)

    results = []
    doc = fitz.open(pdf_path)
    seen = set()  # deduplicate by xref across pages

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        image_list = page.get_images(full=True)

        for _idx, img_info in enumerate(image_list):
            xref = img_info[0]
            if xref in seen:
                continue
            seen.add(xref)

            try:
                pix = fitz.Pixmap(doc, xref)

                # Skip tiny images
                if pix.width < min_w or pix.height < min_h:
                    pix = None
                    continue

                # Convert CMYK → RGB (4-channel to 3-channel)
                if pix.n - pix.alpha > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                out_path = str(Path(work_dir) / f"pdf_img_p{page_idx}_x{xref}.png")
                pix.save(out_path)
                results.append(
                    {
                        "path": out_path,
                        "page": page_idx,
                        "width": pix.width,
                        "height": pix.height,
                    }
                )
                pix = None

            except Exception:  # noqa: S112  # a corrupt or unsupported image is skipped, by design
                continue

    doc.close()
    return results


def _assign_images_to_slides(pdf_images, slides, pdf_page_count) -> dict[str, Any]:
    """Assign extracted PDF images to slides by page proximity.

    Strategy:
    - Map each slide index to a "target PDF page range" proportionally
      (slide 0 → pages 0..k, slide 1 → pages k..2k, etc.)
    - For each slide, pick the largest unused image whose source page
      falls in that slide's range
    - Any leftover images (not assigned) are stored for NIM-fallback slots

    Returns: dict  slide_index → image_path | None
    """
    if not pdf_images or not slides:
        return {}

    n_slides = len(slides)
    n_pages = max(pdf_page_count, 1)
    assignment = {}
    used_paths = set()

    # Sort images by size descending (prefer larger/more prominent images)
    sorted_imgs = sorted(pdf_images, key=lambda i: i["width"] * i["height"], reverse=True)

    for slide in slides:
        idx = slide["index"]
        stype = slide.get("type", "content")

        # Title and closing slides get the largest available image
        if stype in ("title", "closing"):
            for img in sorted_imgs:
                if img["path"] not in used_paths:
                    assignment[idx] = img["path"]
                    used_paths.add(img["path"])
                    break
            else:
                assignment[idx] = None
            continue

        # Content slides: map slide position to a page range
        frac_start = (idx / n_slides) * n_pages
        frac_end = ((idx + 1) / n_slides) * n_pages
        page_start = int(frac_start)
        page_end = max(int(frac_end), page_start + 1)

        best = None
        for img in sorted_imgs:
            if img["path"] in used_paths:
                continue
            if page_start <= img["page"] < page_end:
                best = img
                break

        if best:
            assignment[idx] = best["path"]
            used_paths.add(best["path"])
        else:
            assignment[idx] = None  # will trigger NIM fallback

    return assignment


# ─────────────────────────────────────────────────────────────────────────────
# 3. LLM OUTLINE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

OUTLINE_PROMPT = """
You are a presentation designer. Extract a structured slide outline from the document below.

Return ONLY valid JSON — no markdown fences, no explanation, no preamble:

{{
  "title": "Presentation title",
  "theme_colors": {{
    "bg_dark":    "1E2761",
    "bg_light":   "F5F7FA",
    "accent":     "4A90D9",
    "text_dark":  "1A1A2E",
    "text_light":  "FFFFFF"
  }},
  "slides": [
    {{
      "index":        0,
      "type":         "title",
      "title":        "Slide title",
      "subtitle":     "Optional subtitle or tagline",
      "bullets":      [],
      "image_prompt": "A cinematic wide shot of ... (detailed FLUX prompt, 20-40 words)"
    }},
    {{
      "index":        1,
      "type":         "content",
      "title":        "Section Title",
      "subtitle":     "",
      "bullets":      ["Key point one", "Key point two", "Key point three"],
      "image_prompt": "Abstract illustration of ... (detailed FLUX prompt, 20-40 words)"
    }}
  ]
}}

Rules:
- Produce exactly {slide_count} slides
- First slide: type "title". Last slide: type "closing" (summary / thank you).
- All others: type "content"
- image_prompt: vivid, thematic, 20-40 words. Style: photorealistic OR flat vector, NO text in image.
- bullets: max 5 per slide, max 7 words each
- theme_colors: choose palette that fits the document topic

Document text (truncated to 4000 chars):
{doc_text}
"""


async def _extract_outline(agent, doc_text, slide_count):
    prompt = OUTLINE_PROMPT.format(
        slide_count=slide_count,
        doc_text=doc_text[:4000],
    )
    raw = await agent.llm.chat(prompt)
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw.removeprefix("json")
    raw = raw.strip().rstrip("```").strip()  # noqa: B005

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 4. NIM FALLBACK — generate images for slides that got no PDF image
# ─────────────────────────────────────────────────────────────────────────────


async def _nim_generate_missing(agent, slides, assignment, work_dir):
    """For every slide whose assignment[idx] is None, request image generation
    from image-gen-agent (NIM FLUX.1-dev).  Updates assignment in-place.
    Returns count of successfully generated images.
    """
    missing = [s for s in slides if assignment.get(s["index"]) is None]
    if not missing:
        return 0

    async def _request(slide):
        idx = slide["index"]
        prompt = slide.get("image_prompt", "")
        if not prompt:
            return idx, None
        out_path = str(Path(work_dir) / f"nim_img_{idx}.png")
        try:
            result = await agent.send_to(
                "image-gen-agent",
                {
                    "prompt": prompt,
                    "output_path": out_path,
                    "width": 1024,
                    "height": 576,
                    "steps": 20,
                },
            )
            if result and result.get("image_path") and Path(result["image_path"]).exists():
                return idx, result["image_path"]
        except Exception as e:
            await agent.log(f"NIM fallback for slide {idx} failed: {e}")
            return idx, None
        else:
            return idx, None

    results = await asyncio.gather(*[_request(s) for s in missing])
    generated = 0
    for idx, path in results:
        assignment[idx] = path
        if path:
            generated += 1
    return generated


# ─────────────────────────────────────────────────────────────────────────────
# 5. DECK BUILDER  (python-pptx)
# ─────────────────────────────────────────────────────────────────────────────

_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")

#: The palette a slide falls back to, colour by colour.
_DEFAULT_PALETTE = {
    "bg_dark": "1E2761",
    "bg_light": "F5F7FA",
    "accent": "4A90D9",
    "text_dark": "1A1A2E",
    "text_light": "FFFFFF",
}

#: The slide, in inches: 16:9 at PowerPoint's default width.
_SLIDE_W, _SLIDE_H = 10.0, 5.625

_EMU_PER_INCH = 914400

#: How far a bullet's text sits from the left edge, and how far its bullet hangs.
_BULLET_INDENT = 0.25


def _hex_color(value, fallback):
    """Six hex digits, or the default.

    The palette is chosen by the model from the *document's* content, so it is
    whatever the document steered it to. Anything that is not six hex digits
    falls back rather than raising, because a bad palette is not a reason to
    fail a conversion the user asked for.
    """
    return value if isinstance(value, str) and _HEX6.fullmatch(value) else fallback


def _palette(outline) -> dict[str, str]:
    colors = outline.get("theme_colors")
    colors = colors if isinstance(colors, dict) else {}
    return {key: _hex_color(colors.get(key), default) for key, default in _DEFAULT_PALETTE.items()}


def _emu(inches: float) -> int:
    return round(inches * _EMU_PER_INCH)


def _box(x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
    return _emu(x), _emu(y), _emu(w), _emu(h)


def _rgb(hex6: str) -> Any:
    from pptx.dml.color import RGBColor  # optional dependency: installed with the recipe

    return RGBColor.from_string(hex6)


def _opacity(transparency: int) -> str:
    """A transparency percentage as DrawingML's opacity, in thousandths of a percent."""
    return str((100 - transparency) * 1000)


def _rect(slide, box: tuple[int, int, int, int], color: str, transparency: int = 0) -> None:
    """A filled rectangle with no visible outline."""
    from pptx.enum.shapes import MSO_SHAPE  # optional dependency: installed with the recipe
    from pptx.oxml.ns import qn  # optional dependency: installed with the recipe

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *box)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.color.rgb = _rgb(color)
    shape.shadow.inherit = False
    if transparency:
        # python-pptx has no property for a fill's transparency; DrawingML
        # carries it as an alpha on the colour.
        for colour in shape.element.xpath("./p:spPr/a:solidFill/a:srgbClr"):
            colour.append(colour.makeelement(qn("a:alpha"), {"val": _opacity(transparency)}))


def _picture(slide, path: str, box: tuple[int, int, int, int], transparency: int = 0) -> bool:
    """`path` filling `box`, cropped rather than stretched; False if it cannot be read.

    An image that cannot be read leaves the slide without one rather than
    failing the deck: the text is what the slide is for.
    """
    from pptx.oxml.ns import qn  # optional dependency: installed with the recipe

    try:
        pic = slide.shapes.add_picture(path, *box)
    except (OSError, ValueError):
        return False
    width, height = pic.image.size
    box_ratio, image_ratio = box[2] / box[3], (width / height if height else 0)
    if image_ratio > box_ratio:
        pic.crop_left = pic.crop_right = (1 - box_ratio / image_ratio) / 2
    elif 0 < image_ratio < box_ratio:
        pic.crop_top = pic.crop_bottom = (1 - image_ratio / box_ratio) / 2
    if transparency:
        for blip in pic.element.xpath(".//a:blip"):
            blip.append(blip.makeelement(qn("a:alphaModFix"), {"amt": _opacity(transparency)}))
    return True


def _bulleted(textbox) -> None:
    """Every paragraph of `textbox` as a bullet point, hanging from its first line."""
    from pptx.oxml.ns import qn  # optional dependency: installed with the recipe

    for paragraph in textbox.element.xpath(".//a:p"):
        properties = paragraph.get_or_add_pPr()
        properties.set("marL", str(_emu(_BULLET_INDENT)))
        properties.set("indent", str(-_emu(_BULLET_INDENT)))
        properties.append(properties.makeelement(qn("a:buChar"), {"char": "•"}))


def _text(
    slide,
    box: tuple[int, int, int, int],
    lines: list[Any],
    style: dict[str, Any],
) -> Any:
    """A text box holding `lines`, one paragraph each, all in `style`.

    `style` takes ``size`` and ``color``, and optionally ``bold``, ``italic``,
    ``font``, ``align`` (``"left"`` or ``"center"``), ``tight`` (no inner
    margin) and ``space_after`` (points after each paragraph).
    """
    from pptx.enum.text import (
        MSO_ANCHOR,
        PP_ALIGN,
    )  # optional dependency: installed with the recipe
    from pptx.util import Pt  # optional dependency: installed with the recipe

    textbox = slide.shapes.add_textbox(*box)
    frame = textbox.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.TOP
    if style.get("tight"):
        frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    for i, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        paragraph.alignment = PP_ALIGN.CENTER if style.get("align") == "center" else PP_ALIGN.LEFT
        if style.get("space_after"):
            paragraph.space_after = Pt(style["space_after"])
        run = paragraph.add_run()
        run.text = str(line)
        run.font.size = Pt(style["size"])
        run.font.bold = bool(style.get("bold"))
        run.font.italic = bool(style.get("italic"))
        run.font.name = style.get("font", "Calibri")
        run.font.color.rgb = _rgb(style["color"])
    return textbox


def _title_slide(slide, spec, image, colors) -> None:
    if image and _picture(slide, image, _box(4.6, 0, 5.4, _SLIDE_H)):
        # A dark veil over the picture, so the title beside it stays readable.
        _rect(slide, _box(4.6, 0, 5.4, _SLIDE_H), colors["bg_dark"], transparency=45)
    _text(
        slide,
        _box(0.3, 1.7, 5.5, 1.2),
        [spec.get("title", "")],
        {"size": 38, "bold": True, "color": colors["text_light"]},
    )
    if spec.get("subtitle"):
        _text(
            slide,
            _box(0.3, 3.05, 5.5, 0.7),
            [spec["subtitle"]],
            {"size": 17, "italic": True, "color": colors["accent"], "font": "Calibri Light"},
        )


def _closing_slide(slide, spec, image, colors) -> None:
    if image:
        _picture(slide, image, _box(0, 0, _SLIDE_W, _SLIDE_H), transparency=55)
    centred = {"align": "center"}
    _text(
        slide,
        _box(1.5, 1.8, 7, 1.1),
        [spec.get("title", "")],
        {**centred, "size": 34, "bold": True, "color": colors["text_light"]},
    )
    if spec.get("subtitle"):
        _text(
            slide,
            _box(1.5, 3.1, 7, 0.7),
            [spec["subtitle"]],
            {
                **centred,
                "size": 16,
                "italic": True,
                "color": colors["accent"],
                "font": "Calibri Light",
            },
        )


def _content_slide(slide, spec, image, colors) -> None:
    _text(
        slide,
        _box(0.25, 0.2, 9.5, 0.72),
        [spec.get("title", "")],
        {"size": 25, "bold": True, "color": colors["text_dark"], "tight": True},
    )
    # A thin rule under the title.
    _rect(slide, _box(0.25, 0.98, 9.5, 0.03), colors["accent"])
    pictured = bool(image) and _picture(slide, image, _box(5.1, 1.1, 4.65, 4.2))
    bullets = spec.get("bullets") or []
    if bullets:
        textbox = _text(
            slide,
            _box(0.25, 1.1, 4.7 if pictured else 9.3, 4.2),
            list(bullets)[:5],
            {"size": 15, "color": colors["text_dark"], "space_after": 9},
        )
        _bulleted(textbox)


_LAYOUTS = {"title": _title_slide, "closing": _closing_slide}


def _build_deck(outline, image_assignment, output_path: str) -> None:
    """Write the deck `outline` describes to `output_path`, pictures placed by slide index.

    Blocking: it reads every picture and writes the file, so it is run in a
    thread rather than on the event loop.
    """
    from pptx import Presentation  # optional dependency: installed with the recipe
    from pptx.util import Emu  # optional dependency: installed with the recipe

    colors = _palette(outline)
    deck = Presentation()
    deck.slide_width, deck.slide_height = Emu(_emu(_SLIDE_W)), Emu(_emu(_SLIDE_H))
    deck.core_properties.title = str(outline.get("title", "Presentation"))[:255]
    blank = deck.slide_layouts[6]
    for spec in outline.get("slides", []):
        kind = spec.get("type", "content")
        slide = deck.slides.add_slide(blank)
        dark = kind in _LAYOUTS
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = _rgb(colors["bg_dark" if dark else "bg_light"])
        # The accent bar down the left edge of every slide.
        _rect(slide, _box(0, 0, 0.07, _SLIDE_H), colors["accent"])
        _LAYOUTS.get(kind, _content_slide)(slide, spec, image_assignment.get(spec["index"]), colors)
    deck.save(output_path)


# ─────────────────────────────────────────────────────────────────────────────
# 6. AGENT LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────


async def setup(agent):
    errors = []

    for pkg, imp in [
        ("python-pptx", "pptx"),
        ("pymupdf", "fitz"),
        ("pdfplumber", "pdfplumber"),
        ("Pillow", "PIL"),
    ]:
        try:
            __import__(imp)
        except ImportError:
            errors.append(f"pip install {pkg}")

    if errors:
        await agent.alert(f"doc-to-pptx-agent: missing deps — {'; '.join(errors)}", "warning")
    else:
        await agent.log("doc-to-pptx-agent ready (python-pptx + PyMuPDF + pdfplumber)")


def _failed(error: str, **progress: Any) -> dict[str, Any]:
    """The answer to a conversion that produced no deck.

    ``progress`` is how far it got -- ``slide_count``, ``title``,
    ``images_extracted``, ``images_generated`` -- when it failed at the build.
    """
    return {
        "pptx_path": None,
        "slide_count": 0,
        "title": "",
        "images_extracted": 0,
        "images_generated": 0,
        **progress,
        "error": error,
    }


async def handle_task(agent, payload: Any) -> Any:
    # Normalise payload — the CLI wraps the user's message as {"text": "..."}
    # so if the payload is a string or contains a JSON string in "text", parse it.
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass  # not JSON: the text is the request
    if isinstance(payload, dict):
        for key in ("text", "message", "query"):
            raw = payload.get(key, "")
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    payload = json.loads(raw)
                    break
                except ValueError:
                    pass  # looked like JSON and was not; try the next field
    if not isinstance(payload, dict):
        payload = {}
    numbers = {}
    for field, default in (("slide_count", 8), ("min_img_width", 200), ("min_img_height", 150)):
        try:
            numbers[field] = int(payload.get(field, default))
        except (TypeError, ValueError):
            return _failed(f"{field} must be a whole number")
    slide_count = numbers["slide_count"]
    min_w = numbers["min_img_width"]
    min_h = numbers["min_img_height"]
    nim_fallback = bool(payload.get("nim_fallback", True))

    # Read and written only where `wactorz.documents` allows: the request is
    # written by a model, from whatever it was reading.
    try:
        source = readable_document(
            str(payload.get("file_path") or ""), str(payload.get("file_id") or "")
        )
        requested = str(payload.get("output_path") or "")
        target = writable_document(requested, ".pptx") if requested else None
    except DocumentRefused as refused:
        return _failed(str(refused))
    file_path = str(source)

    # Unnamed, the deck goes in a private temp dir, not a fixed
    # `/tmp/presentation.pptx`: that name was predictable in a world-writable
    # directory, so two conversions at once clobbered each other, and anything
    # that got there first — a symlink, say — decided where the file was
    # actually written. `mkdtemp` is 0700 and honours TMPDIR, so it is also
    # correct off Linux. The path goes back to the caller as `pptx_path`.
    output_path = (
        str(target)
        if target
        else str(Path(tempfile.mkdtemp(prefix="wactorz-pptx-")) / "presentation.pptx")
    )

    is_pdf = _is_pdf(file_path)
    work_dir = tempfile.mkdtemp(prefix="doc2pptx_")

    await agent.log(f"Processing: {Path(file_path).name}")

    try:
        # ── Step 1: Read document text ──────────────────────────────────────
        await agent.log("Step 1/4 — Reading document text...")
        doc_text = _read_document(file_path)
        if not doc_text.strip():
            return _failed("Document appears empty or unreadable")
        await agent.log(f"Extracted {len(doc_text):,} characters of text")

        # ── Step 2: Extract PDF images ──────────────────────────────────────
        pdf_images = []
        pdf_page_count = 0

        if is_pdf:
            await agent.log(f"Step 2/4 — Extracting embedded images (min {min_w}×{min_h}px)...")
            try:
                import fitz  # pyright: ignore[reportMissingImports]  # optional: installed with the recipe (pymupdf)

                # Count pages for later page-to-slide mapping
                doc_tmp = fitz.open(file_path)
                pdf_page_count = len(doc_tmp)
                doc_tmp.close()

                pdf_images = _extract_pdf_images(file_path, work_dir, min_w, min_h)
                await agent.log(
                    f"Found {len(pdf_images)} usable image(s) in {pdf_page_count} pages"
                )
            except ImportError:
                await agent.log(
                    "PyMuPDF not installed — skipping PDF image extraction. "
                    "Install with: pip install pymupdf"
                )
        else:
            await agent.log("Step 2/4 — TXT file, skipping PDF image extraction")

        # ── Step 3: LLM outline ─────────────────────────────────────────────
        await agent.log("Step 3/4 — Extracting slide outline via LLM...")
        outline = await _extract_outline(agent, doc_text, slide_count)
        slides = outline.get("slides", [])
        title = outline.get("title", "Presentation")
        await agent.log(f'Outline ready: "{title}" — {len(slides)} slides')

        # ── Assign PDF images to slides ─────────────────────────────────────
        if pdf_images:
            assignment = _assign_images_to_slides(pdf_images, slides, pdf_page_count)
            n_extracted = sum(1 for p in assignment.values() if p)
            await agent.log(f"Assigned {n_extracted}/{len(slides)} slides from PDF images")
        else:
            assignment = {s["index"]: None for s in slides}
            n_extracted = 0

        # ── NIM fallback for slides without a real image ────────────────────
        n_generated = 0
        unassigned = sum(1 for p in assignment.values() if p is None)

        if unassigned > 0 and nim_fallback:
            await agent.log(f"  {unassigned} slide(s) without image — requesting NIM generation...")
            n_generated = await _nim_generate_missing(agent, slides, assignment, work_dir)
            await agent.log(f"  NIM generated {n_generated} image(s)")
        elif unassigned > 0:
            await agent.log(f"  {unassigned} slide(s) will be text-only (nim_fallback=false)")

        # ── Step 4: Build PPTX ──────────────────────────────────────────────
        await agent.log("Step 4/4 — Building .pptx...")
        built = {
            "slide_count": len(slides),
            "title": title,
            "images_extracted": n_extracted,
            "images_generated": n_generated,
        }
        try:
            await asyncio.to_thread(_build_deck, outline, assignment, output_path)
        except ImportError:
            return _failed("python-pptx is not installed: pip install python-pptx", **built)
        except Exception as e:
            return _failed(f"Building the deck failed: {e}", **built)

        size_kb = Path(output_path).stat().st_size // 1024
        await agent.log(
            f"Done! {output_path} "
            f"({size_kb} KB, {len(slides)} slides, "
            f"{n_extracted} PDF images, {n_generated} NIM images)"
        )

    except json.JSONDecodeError as e:
        msg = f"LLM outline JSON parse failed: {e}"
        await agent.alert(msg, "error")
        return _failed(msg)

    except Exception as e:
        msg = f"doc-to-pptx failed: {e}"
        await agent.alert(msg, "error")
        return _failed(msg)

    else:
        return {"pptx_path": output_path, **built, "error": None}

    finally:
        # Extracted and generated images; the deck has them embedded and lives
        # elsewhere, so none of it is needed.
        shutil.rmtree(work_dir, ignore_errors=True)


async def process(agent) -> None:
    # Task-driven only — no polling loop needed
    await asyncio.sleep(3600)

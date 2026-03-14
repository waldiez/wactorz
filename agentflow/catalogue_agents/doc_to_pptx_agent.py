"""
DEMO AGENT — doc-to-pptx-agent  (v2 — PDF image extraction + NIM fallback)
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
    pymupdf          → import fitz   (PDF image extraction + text)
    pdfplumber                        (PDF text extraction — more accurate for text)
    Pillow           → import PIL     (image resize / convert)

Node.js (npm install -g):
    pptxgenjs

Optional (for NIM fallback):
    image-gen-agent must be spawned and running
    NIM API key persisted:  @main remember nim_api_key = nvapi-...


SPAWN CONFIG
────────────
{
  "name":        "doc-to-pptx-agent",
  "type":        "dynamic",
  "description": "Converts PDF or TXT documents into PowerPoint presentations. Extracts real embedded images from PDF first; falls back to NIM FLUX image generation for slides without images.",
  "capabilities": ["document_to_pptx", "pdf_to_presentation", "pptx_generation", "document_conversion"],
  "input_schema": {
    "file_path":       "str  — absolute path to source PDF or TXT file",
    "output_path":     "str  — where to save the .pptx, e.g. /tmp/output.pptx",
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
  "code": "<copy AGENT_CODE string from the bottom of this file>"
}


TASK PAYLOAD EXAMPLES
──────────────────────
Minimal:
  { "file_path": "/home/user/report.pdf", "output_path": "/tmp/report.pptx" }

Full:
  {
    "file_path":      "/home/user/report.pdf",
    "output_path":    "/tmp/report.pptx",
    "slide_count":    10,
    "theme":          "minimal light",
    "nim_fallback":   true,
    "min_img_width":  300,
    "min_img_height": 200
  }

No NIM (extracted images only, text-only for slides without one):
  {
    "file_path":    "/home/user/report.pdf",
    "output_path":  "/tmp/report.pptx",
    "nim_fallback": false
  }
"""

# ──────────────────────────────────────────────────────────────────────────────
# AGENT_CODE — copy this string into the "code" field of the spawn config
# ──────────────────────────────────────────────────────────────────────────────

AGENT_CODE = r'''
import asyncio
import json
import os
import subprocess
import tempfile
import time


# ─────────────────────────────────────────────────────────────────────────────
# 1. DOCUMENT READING
# ─────────────────────────────────────────────────────────────────────────────

def _read_pdf_text(path):
    """Extract text from PDF using pdfplumber (best for clean text)."""
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
    return "\n\n".join(pages)


def _read_txt(path):
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode file: {path}")


def _read_document(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _read_pdf_text(path)
    return _read_txt(path)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PDF IMAGE EXTRACTION  (PyMuPDF)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf_images(pdf_path, work_dir, min_w=200, min_h=150):
    """
    Extract embedded images from a PDF using PyMuPDF (fitz).

    Returns a list of dicts:
      { "path": "/tmp/.../img_p0_x7.png",
        "page": 0,           ← 0-based source page index
        "width": 1024,
        "height": 768 }

    Images smaller than min_w × min_h are skipped (logos, bullets, decorations).
    CMYK images are converted to RGB so they save cleanly as PNG.
    """
    import fitz  # PyMuPDF  (pip install pymupdf)

    results = []
    doc     = fitz.open(pdf_path)
    seen    = set()   # deduplicate by xref across pages

    for page_idx in range(len(doc)):
        page       = doc[page_idx]
        image_list = page.get_images(full=True)

        for img_idx, img_info in enumerate(image_list):
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

                out_path = os.path.join(work_dir, f"pdf_img_p{page_idx}_x{xref}.png")
                pix.save(out_path)
                results.append({
                    "path":   out_path,
                    "page":   page_idx,
                    "width":  pix.width,
                    "height": pix.height,
                })
                pix = None

            except Exception:
                # Corrupt / unsupported image format — skip silently
                continue

    doc.close()
    return results


def _assign_images_to_slides(pdf_images, slides, pdf_page_count):
    """
    Assign extracted PDF images to slides by page proximity.

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

    n_slides     = len(slides)
    n_pages      = max(pdf_page_count, 1)
    assignment   = {}
    used_paths   = set()

    # Sort images by size descending (prefer larger/more prominent images)
    sorted_imgs  = sorted(pdf_images, key=lambda i: i["width"] * i["height"], reverse=True)

    for slide in slides:
        idx        = slide["index"]
        stype      = slide.get("type", "content")

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
        frac_end   = ((idx + 1) / n_slides) * n_pages
        page_start = int(frac_start)
        page_end   = max(int(frac_end), page_start + 1)

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
            assignment[idx] = None   # will trigger NIM fallback

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

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("```").strip()

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 4. NIM FALLBACK — generate images for slides that got no PDF image
# ─────────────────────────────────────────────────────────────────────────────

async def _nim_generate_missing(agent, slides, assignment, work_dir):
    """
    For every slide whose assignment[idx] is None, request image generation
    from image-gen-agent (NIM FLUX.1-dev).  Updates assignment in-place.
    Returns count of successfully generated images.
    """
    missing = [s for s in slides if assignment.get(s["index"]) is None]
    if not missing:
        return 0

    async def _request(slide):
        idx    = slide["index"]
        prompt = slide.get("image_prompt", "")
        if not prompt:
            return idx, None
        out_path = os.path.join(work_dir, f"nim_img_{idx}.png")
        try:
            result = await agent.send_to("image-gen-agent", {
                "prompt":      prompt,
                "output_path": out_path,
                "width":       1024,
                "height":      576,
                "steps":       20,
            })
            if result and result.get("image_path") and os.path.exists(result["image_path"]):
                return idx, result["image_path"]
            return idx, None
        except Exception as e:
            await agent.log(f"NIM fallback for slide {idx} failed: {e}")
            return idx, None

    results = await asyncio.gather(*[_request(s) for s in missing])
    generated = 0
    for idx, path in results:
        assignment[idx] = path
        if path:
            generated += 1
    return generated


# ─────────────────────────────────────────────────────────────────────────────
# 5. PPTXGENJS SCRIPT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_js(outline, image_assignment, output_path):
    """Generate a Node.js / pptxgenjs script as a string."""
    title  = outline.get("title", "Presentation")
    slides = outline.get("slides", [])
    colors = outline.get("theme_colors", {})

    C = {
        "bg_dark":    colors.get("bg_dark",    "1E2761"),
        "bg_light":   colors.get("bg_light",   "F5F7FA"),
        "accent":     colors.get("accent",     "4A90D9"),
        "text_dark":  colors.get("text_dark",  "1A1A2E"),
        "text_light": colors.get("text_light", "FFFFFF"),
    }

    L = [
        "const pptxgen = require('pptxgenjs');",
        "const pres = new pptxgen();",
        "pres.layout = 'LAYOUT_16x9';",
        f"pres.title = {json.dumps(title)};",
        "",
    ]

    for slide in slides:
        idx      = slide["index"]
        stype    = slide.get("type", "content")
        stitle   = slide.get("title", "")
        subtitle = slide.get("subtitle", "")
        bullets  = slide.get("bullets", [])
        img      = image_assignment.get(idx)
        is_dark  = stype in ("title", "closing")
        bg       = C["bg_dark"] if is_dark else C["bg_light"]
        tc       = C["text_light"] if is_dark else C["text_dark"]

        L.append(f"// ── Slide {idx}: {stype} ──────────────────────────")
        L.append("{ const s = pres.addSlide();")
        L.append(f"  s.background = {{ color: '{bg}' }};")

        # Left accent bar
        L.append(
            f"  s.addShape(pres.shapes.RECTANGLE, "
            f"{{ x:0, y:0, w:0.07, h:5.625, "
            f"fill:{{color:'{C['accent']}'}}, line:{{color:'{C['accent']}'}} }});"
        )

        if stype == "title":
            if img:
                L.append(
                    f"  s.addImage({{ path:{json.dumps(img)}, x:4.6, y:0, w:5.4, h:5.625, "
                    f"sizing:{{type:'cover',w:5.4,h:5.625}} }});"
                )
                # Gradient-style dark overlay so text is readable over the image
                L.append(
                    f"  s.addShape(pres.shapes.RECTANGLE, "
                    f"{{ x:4.6, y:0, w:5.4, h:5.625, "
                    f"fill:{{color:'{C['bg_dark']}',transparency:45}}, "
                    f"line:{{color:'{C['bg_dark']}'}} }});"
                )
            L.append(
                f"  s.addText({json.dumps(stitle)}, "
                f"{{ x:0.3, y:1.7, w:5.5, h:1.2, "
                f"fontSize:38, bold:true, color:'{C['text_light']}', "
                f"fontFace:'Calibri', align:'left' }});"
            )
            if subtitle:
                L.append(
                    f"  s.addText({json.dumps(subtitle)}, "
                    f"{{ x:0.3, y:3.05, w:5.5, h:0.7, "
                    f"fontSize:17, italic:true, color:'{C['accent']}', "
                    f"fontFace:'Calibri Light', align:'left' }});"
                )

        elif stype == "closing":
            if img:
                L.append(
                    f"  s.addImage({{ path:{json.dumps(img)}, x:0, y:0, w:10, h:5.625, "
                    f"sizing:{{type:'cover',w:10,h:5.625}}, transparency:55 }});"
                )
            L.append(
                f"  s.addText({json.dumps(stitle)}, "
                f"{{ x:1.5, y:1.8, w:7, h:1.1, "
                f"fontSize:34, bold:true, color:'{C['text_light']}', "
                f"fontFace:'Calibri', align:'center' }});"
            )
            if subtitle:
                L.append(
                    f"  s.addText({json.dumps(subtitle)}, "
                    f"{{ x:1.5, y:3.1, w:7, h:0.7, "
                    f"fontSize:16, italic:true, color:'{C['accent']}', "
                    f"fontFace:'Calibri Light', align:'center' }});"
                )

        else:  # content
            # Slide title
            L.append(
                f"  s.addText({json.dumps(stitle)}, "
                f"{{ x:0.25, y:0.2, w:9.5, h:0.72, "
                f"fontSize:25, bold:true, color:'{tc}', "
                f"fontFace:'Calibri', align:'left', margin:0 }});"
            )
            # Thin divider under title
            L.append(
                f"  s.addShape(pres.shapes.RECTANGLE, "
                f"{{ x:0.25, y:0.98, w:9.5, h:0.03, "
                f"fill:{{color:'{C['accent']}'}}, line:{{color:'{C['accent']}'}} }});"
            )

            text_w = 4.7 if img else 9.3

            if img:
                L.append(
                    f"  s.addImage({{ path:{json.dumps(img)}, x:5.1, y:1.1, w:4.65, h:4.2, "
                    f"sizing:{{type:'cover',w:4.65,h:4.2}} }});"
                )

            if bullets:
                items = []
                for i, b in enumerate(bullets[:5]):
                    last = (i == len(bullets[:5]) - 1)
                    items.append(
                        f"    {{ text:{json.dumps(b)}, options:{{"
                        f"bullet:true, breakLine:{'false' if last else 'true'}, "
                        f"fontSize:15, color:'{tc}', fontFace:'Calibri', paraSpaceAfter:9 }} }}"
                    )
                L.append(
                    f"  s.addText([\n" + ",\n".join(items) +
                    f"\n  ], {{ x:0.25, y:1.1, w:{text_w}, h:4.2, valign:'top' }});"
                )

        L.append("}")
        L.append("")

    L.append(f"pres.writeFile({{ fileName:{json.dumps(output_path)} }})")
    L.append(f"  .then(() => console.log('OK:' + {json.dumps(output_path)}))")
    L.append(f"  .catch(e => {{ console.error('ERR:' + e.message); process.exit(1); }});")

    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# 6. AGENT LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

async def setup(agent):
    errors = []

    # Check Python deps
    for pkg, imp in [("pymupdf", "fitz"), ("pdfplumber", "pdfplumber"), ("Pillow", "PIL")]:
        try:
            __import__(imp)
        except ImportError:
            errors.append(f"pip install {pkg}")

    # Check pptxgenjs
    r = subprocess.run(["node", "-e", "require('pptxgenjs')"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        errors.append("npm install -g pptxgenjs")

    if errors:
        await agent.alert(
            f"doc-to-pptx-agent: missing deps — run: {'; '.join(errors)}", "warning"
        )
    else:
        await agent.log("doc-to-pptx-agent ready (PyMuPDF + pdfplumber + pptxgenjs)")


async def handle_task(agent, payload):
    file_path    = payload.get("file_path", "")
    output_path  = payload.get("output_path", "/tmp/presentation.pptx")
    slide_count  = int(payload.get("slide_count", 8))
    nim_fallback = bool(payload.get("nim_fallback", True))
    min_w        = int(payload.get("min_img_width",  200))
    min_h        = int(payload.get("min_img_height", 150))

    if not file_path or not os.path.exists(file_path):
        return {"pptx_path": None, "slide_count": 0, "title": "",
                "images_extracted": 0, "images_generated": 0,
                "error": f"File not found: {file_path}"}

    is_pdf   = os.path.splitext(file_path)[1].lower() == ".pdf"
    work_dir = tempfile.mkdtemp(prefix="doc2pptx_")

    await agent.log(f"Processing: {os.path.basename(file_path)}")

    try:
        # ── Step 1: Read document text ──────────────────────────────────────
        await agent.log("Step 1/4 — Reading document text...")
        doc_text = _read_document(file_path)
        if not doc_text.strip():
            return {"pptx_path": None, "slide_count": 0, "title": "",
                    "images_extracted": 0, "images_generated": 0,
                    "error": "Document appears empty or unreadable"}
        await agent.log(f"Extracted {len(doc_text):,} characters of text")

        # ── Step 2: Extract PDF images ──────────────────────────────────────
        pdf_images = []
        pdf_page_count = 0

        if is_pdf:
            await agent.log(f"Step 2/4 — Extracting embedded images (min {min_w}×{min_h}px)...")
            try:
                import fitz
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
        outline     = await _extract_outline(agent, doc_text, slide_count)
        slides      = outline.get("slides", [])
        title       = outline.get("title", "Presentation")
        await agent.log(f'Outline ready: "{title}" — {len(slides)} slides')

        # ── Assign PDF images to slides ─────────────────────────────────────
        if pdf_images:
            assignment = _assign_images_to_slides(pdf_images, slides, pdf_page_count)
            n_extracted = sum(1 for p in assignment.values() if p)
            await agent.log(f"Assigned {n_extracted}/{len(slides)} slides from PDF images")
        else:
            assignment  = {s["index"]: None for s in slides}
            n_extracted = 0

        # ── NIM fallback for slides without a real image ────────────────────
        n_generated = 0
        unassigned  = sum(1 for p in assignment.values() if p is None)

        if unassigned > 0 and nim_fallback:
            await agent.log(
                f"  {unassigned} slide(s) without image — requesting NIM generation..."
            )
            n_generated = await _nim_generate_missing(agent, slides, assignment, work_dir)
            await agent.log(f"  NIM generated {n_generated} image(s)")
        elif unassigned > 0:
            await agent.log(
                f"  {unassigned} slide(s) will be text-only (nim_fallback=false)"
            )

        # ── Step 4: Build PPTX ──────────────────────────────────────────────
        await agent.log("Step 4/4 — Building .pptx with pptxgenjs...")
        js_script = _build_js(outline, assignment, output_path)
        js_path   = os.path.join(work_dir, "build.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js_script)

        result = subprocess.run(
            ["node", js_path],
            capture_output=True, text=True, cwd=work_dir, timeout=60
        )
        if result.returncode != 0 or not os.path.exists(output_path):
            err = (result.stderr or result.stdout or "Unknown error").strip()
            return {"pptx_path": None, "slide_count": len(slides), "title": title,
                    "images_extracted": n_extracted, "images_generated": n_generated,
                    "error": f"pptxgenjs failed: {err[:400]}"}

        size_kb = os.path.getsize(output_path) // 1024
        await agent.log(
            f"Done! {output_path} "
            f"({size_kb} KB, {len(slides)} slides, "
            f"{n_extracted} PDF images, {n_generated} NIM images)"
        )
        return {
            "pptx_path":        output_path,
            "slide_count":      len(slides),
            "title":            title,
            "images_extracted": n_extracted,
            "images_generated": n_generated,
            "error":            None,
        }

    except json.JSONDecodeError as e:
        msg = f"LLM outline JSON parse failed: {e}"
        await agent.alert(msg, "error")
        return {"pptx_path": None, "slide_count": 0, "title": "",
                "images_extracted": 0, "images_generated": 0, "error": msg}

    except Exception as e:
        msg = f"doc-to-pptx failed: {e}"
        await agent.alert(msg, "error")
        return {"pptx_path": None, "slide_count": 0, "title": "",
                "images_extracted": 0, "images_generated": 0, "error": msg}


async def process(agent):
    # Task-driven only — no polling loop needed
    await asyncio.sleep(3600)
'''

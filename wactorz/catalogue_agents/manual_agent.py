"""
CATALOG AGENT — manual-agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Searches the internet for device manuals, downloads PDFs, extracts text,
and answers questions about the device using the agent's LLM.

Front-ended by an LLM router: users send plain natural language and the
router decides which internal tool to call (load_manual, ask, status, clear).
The legacy {"action": "...", ...} dict form is still accepted for programmatic
callers (catalog/planner routing).

EXAMPLES (natural language)
───────────────────────────
  "load the manual for my Philips 2200"
  "Philips EP2200 manual please"
  "how do I descale it?"
  "what's the cleaning procedure"
  "what manual is loaded?"
  "forget it" / "clear"

SPAWN CONFIG
────────────
{
  "name":        "manual-agent",
  "type":        "dynamic",
  "description": "Finds device manuals on the web, downloads the PDF, extracts text, and answers questions about the device in natural language using the agent's LLM.",
  "capabilities": ["web_search", "pdf_extraction", "qa_assistant", "device_manuals", "natural_language"],
  "install":     ["httpx", "pymupdf", "pdfplumber", "duckduckgo-search"],
  "input_schema": {
    "text": "str — natural-language request (e.g. 'load the Philips 2200 manual', 'how do I descale it?'). The legacy {action, device, question} dict form is still accepted."
  },
  "output_schema": {
    "result":  "str  — Human-readable response (loaded confirmation, answer, status, etc.)",
    "answer":  "str  — LLM-generated answer to a question (when applicable)",
    "device":  "str  — Device model name (when applicable)",
    "url":     "str  — URL of the downloaded manual PDF (when applicable)",
    "pages":   "int  — Number of pages in the PDF (when applicable)",
    "error":   "str  — Error message if something went wrong"
  },
  "poll_interval": 3600
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
"""
manual-agent — searches the internet for device manuals, downloads PDFs,
extracts text, and answers questions using the agent's LLM.

Recipe-style module: state lives in `agent.state`, the framework injects
`agent` into setup() / handle_task() / process().
"""

import asyncio
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("manual-agent")

TRUSTED_SITES = [
    'manualslib.com', 'manualzz.com', 'manuals.plus',
    'documents.philips.com', 'download.p4c.philips.com',
    'support.brother.com', 'docs.brother.com',
    'support.hp.com', 'support.epson.net',
    'support.canon.com', 'dl.owneriq.net',
]

_SEARCH_ENGINE_DOMAINS = {
    'bing.com', 'microsoft.com', 'google.com', 'googleapis.com',
    'gstatic.com', 'youtube.com', 'schema.org', 'w3.org',
    'microsofttranslator.com', 'bingapis.com',
}

_STOPWORDS = {
    'how','do','i','the','a','an','is','are','what','where','when','why',
    'can','does','to','for','of','in','on','at','my','this','that','it',
    'its','with','and','or','be','was','will','has','have','use','using',
    'get','me','please','tell','about','there','their','they','we','you',
    'your','which','make','need',
}


# ══════════════════════════════════════════════════════════════════════════════
# setup — initialise state slots
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    agent.state.setdefault("manual_text",   None)
    agent.state.setdefault("manual_device", None)
    agent.state.setdefault("manual_url",    None)
    agent.state.setdefault("manual_pages",  0)
    # Persistent cache: device-name → list of known-good PDF URLs
    # Survives across restarts because agent.state is persisted.
    agent.state.setdefault("url_cache",     {})
    # Per-device conversation history (so follow-up questions can use context)
    agent.state.setdefault("_chat_history", [])
    await agent.log(
        "Manual agent ready. Talk to me in plain English — e.g. "
        "'load the Philips 2200 manual' or 'how do I descale it?'"
    )


# ══════════════════════════════════════════════════════════════════════════════
# handle_task — main entry point for @manual-agent messages
# ══════════════════════════════════════════════════════════════════════════════

async def handle_task(agent, payload):
    """
    Two entry modes:

      1. Natural language (preferred):
         payload = "how do I descale my Philips 2200?"
         payload = {"text": "load the manual for HP LaserJet M404"}
         → routed through the LLM tool-router, which picks load_manual / ask /
           status / clear automatically and extracts the device or question.

      2. Legacy dict (for catalog/planner programmatic calls):
         payload = {"action": "load_manual", "device": "Philips 2200"}
         payload = {"action": "ask", "question": "how do I descale?"}
         → handled directly, no LLM round-trip.
    """
    # ── Normalise payload to {text, action?, device?, question?, url?} ──────
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"text": payload}

    if not isinstance(payload, dict):
        return {"error": "Invalid payload — expected str or dict"}

    # If the inner "text" field itself looks like JSON, unwrap one level
    # (this happens when messages come through @mention routing).
    raw_text = payload.get("text") or payload.get("message") or payload.get("query") or ""
    if isinstance(raw_text, str) and raw_text.strip().startswith("{"):
        try:
            inner = json.loads(raw_text)
            if isinstance(inner, dict):
                payload = {**payload, **inner}
        except Exception:
            pass

    # ── Mode 1: explicit action field → legacy direct dispatch ──────────────
    action = str(payload.get("action") or "").strip().lower()
    if action:
        return await _dispatch_action(agent, action, payload)

    # ── Mode 2: free-text → LLM router ──────────────────────────────────────
    text = (
        payload.get("text")
        or payload.get("query")
        or payload.get("question")
        or payload.get("message")
        or ""
    )
    if isinstance(text, str):
        text = text.strip()

    if not text:
        return {
            "error": "Empty request.",
            "hint":  "Send a natural-language message like 'load the Philips 2200 manual' "
                     "or 'how do I descale it?'",
        }

    if not agent.llm:
        # No LLM available — fall back to a best-effort heuristic so the agent
        # is still useful in pure-programmatic deployments.
        return await _heuristic_route(agent, text)

    return await _llm_route(agent, text)


# ══════════════════════════════════════════════════════════════════════════════
# Action dispatcher (shared by legacy dict mode and LLM router)
# ══════════════════════════════════════════════════════════════════════════════

async def _dispatch_action(agent, action: str, payload: dict) -> dict:
    if action == "load_manual":
        device = payload.get("device") or payload.get("query") or payload.get("text", "")
        if not device:
            return {"error": "Missing 'device' field"}
        explicit_url = payload.get("url")
        return await _load_manual(agent, device, explicit_url=explicit_url)

    if action == "ask":
        question = payload.get("question") or payload.get("query") or payload.get("text", "")
        if not question:
            return {"error": "Missing 'question' field"}
        return await _ask(agent, question)

    if action == "status":
        return _status(agent)

    if action == "clear":
        agent.state["manual_text"]   = None
        agent.state["manual_device"] = None
        agent.state["manual_url"]    = None
        agent.state["manual_pages"]  = 0
        agent.state["_chat_history"] = []
        return {"status": "cleared", "result": "Manual cleared."}

    return {
        "error": f"Unknown action: '{action}'",
        "supported": ["load_manual", "ask", "status", "clear"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM router — turns natural language into a tool call
# ══════════════════════════════════════════════════════════════════════════════

_ROUTER_SYSTEM = """\
You are the dispatcher for a device-manual assistant. Your ONLY job is to
classify the user's message into one of four tools and return a single JSON
object — nothing else, no prose, no markdown fences.

Tools:

  load_manual  — Use when the user wants to fetch/load/find a manual for a
                 specific device. Extract the device model name and NORMALIZE
                 it: correct obvious brand-name misspellings (e.g. "Phillips"
                 → "Philips", "Cannon" → "Canon", "Sumsung" → "Samsung") and
                 use the canonical form. Drop conversational filler ("please",
                 "my", "the", "its a coffee machine"). Keep model numbers
                 exactly as given.
                 Example: "load the Phillips 2200 manual its a coffee machine"
                          → {"tool": "load_manual", "device": "Philips 2200"}

  ask          — Use when the user asks a question ABOUT the currently-loaded
                 manual (how-to, troubleshooting, specs, settings). Pass the
                 question through verbatim.
                 Example: "how do I descale it?" → {"tool": "ask",
                                                    "question": "how do I descale it?"}

  status       — Use when the user asks what manual is currently loaded, or
                 wants to see the current state. Use this for short greetings
                 ("hi", "hello") when nothing is loaded yet, so the user sees
                 the agent is idle.
                 Example: "what's loaded?" → {"tool": "status"}

  clear        — Use when the user wants to forget/reset/clear the current
                 manual.
                 Example: "forget it" → {"tool": "clear"}

Disambiguation rules:
  • If a device model is mentioned AND no manual is loaded yet → load_manual.
  • If a device model is mentioned AND it MATCHES the loaded device → ask.
  • If a device model is mentioned AND it is DIFFERENT from the loaded
    device → load_manual (the user is switching devices).
  • If the message is a question with no device name and a manual is loaded
    → ask.
  • If the message is a question with no device name and NO manual is
    loaded → load_manual with the best guess at the device (or, if no device
    is recoverable, ask anyway and let the downstream layer report
    "no manual loaded").

Return EXACTLY one JSON object with a "tool" key. No code fences, no
explanation, no extra keys beyond {tool, device, question}.
"""


async def _llm_route(agent, text: str) -> dict:
    """Ask the LLM which tool to call, then call it."""
    loaded_device = agent.state.get("manual_device")
    loaded_pages  = agent.state.get("manual_pages", 0)

    state_line = (
        f"Currently loaded manual: {loaded_device} ({loaded_pages} pages)"
        if loaded_device else
        "Currently loaded manual: (none)"
    )

    prompt = (
        f"{state_line}\n\n"
        f"User message: {text!r}\n\n"
        f"Return the JSON tool call now."
    )

    await agent.log(f"Routing via LLM: {text!r}")

    try:
        raw = await agent.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=_ROUTER_SYSTEM,
        )
    except Exception as e:
        await agent.log(f"LLM router failed: {e} — falling back to heuristic")
        return await _heuristic_route(agent, text)

    decision = _parse_router_json(raw)
    if not decision:
        await agent.log(f"LLM returned un-parseable router output: {raw!r} — using heuristic")
        return await _heuristic_route(agent, text)

    tool = str(decision.get("tool") or "").strip().lower()
    await agent.log(f"Router decision: tool={tool!r} args={ {k:v for k,v in decision.items() if k != 'tool'} }")

    if tool == "load_manual":
        device = (decision.get("device") or "").strip()
        if not device:
            return {
                "error": "I couldn't figure out which device manual to load.",
                "hint":  "Try: 'load the manual for <device model>'.",
            }
        return await _load_manual(agent, device)

    if tool == "ask":
        question = (decision.get("question") or text).strip()
        return await _ask(agent, question)

    if tool == "status":
        return _status(agent)

    if tool == "clear":
        return await _dispatch_action(agent, "clear", {})

    # Unknown tool from the LLM — fall back
    await agent.log(f"Router returned unknown tool {tool!r} — using heuristic")
    return await _heuristic_route(agent, text)


def _parse_router_json(raw: str) -> Optional[dict]:
    """
    Extract a JSON object from the LLM's response. Tolerates:
      - Bare JSON
      - JSON inside ```json ... ``` fences
      - Leading/trailing prose around a single {...} block
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()

    # Strip markdown fences if present
    if s.startswith("```"):
        # remove leading ``` (possibly ```json) and trailing ```
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()

    # Try whole-string parse first
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Fall back: greedy brace-matched substring
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Heuristic router — fallback used when no LLM is available or the LLM fails
# ══════════════════════════════════════════════════════════════════════════════

# Cheap keyword detector — only used as a last resort. The LLM router above
# is the primary path.
_CLEAR_RE  = re.compile(r"\b(clear|reset|forget|unload|drop)\b", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b(status|what(?:'s| is) loaded|which manual|current manual)\b",
                        re.IGNORECASE)
_LOAD_RE   = re.compile(r"\b(load|fetch|get|find|download|search for)\b.*\b(manual|guide|instructions?)\b",
                        re.IGNORECASE)


async def _heuristic_route(agent, text: str) -> dict:
    """No-LLM fallback. Tries to do the right thing with regex/keywords."""
    if _CLEAR_RE.search(text) and len(text) < 40:
        return await _dispatch_action(agent, "clear", {})

    if _STATUS_RE.search(text):
        return _status(agent)

    # Looks like a load request
    if _LOAD_RE.search(text):
        # Strip command verbs to recover the device name
        device = re.sub(
            r"\b(please|can you|could you|load|fetch|get|find|download|search for|the|a|an|manual|guide|instructions?|for|my|of)\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" ,.?!\"'")
        if device:
            return await _load_manual(agent, device)

    # Default: if a manual is loaded, treat as a question; else ask to load
    if agent.state.get("manual_text"):
        return await _ask(agent, text)

    return {
        "error": "No manual loaded yet, and I couldn't tell which device you mean.",
        "hint":  "Try: 'load the manual for <device model>'.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Load manual
# ══════════════════════════════════════════════════════════════════════════════

async def _load_manual(agent, device: str, explicit_url: Optional[str] = None) -> dict:
    """
    Try to load a manual for ``device``. On failure, ask the LLM for spelling/
    naming variants and retry up to 2 more times. This catches misspellings
    (Phillips → Philips, Cannon → Canon) and brand-omission cases (just "2200"
    → "Philips 2200") that survived the router's normalization.

    If an explicit URL is given, no retry is done — the caller has chosen.
    """
    # ── Attempt 1 — as given ─────────────────────────────────────────────
    result = await _load_manual_once(agent, device, explicit_url=explicit_url)
    if explicit_url or "error" not in result:
        return result

    # ── Retry only if the failure was "couldn't find" (not a download error)
    # and we have an LLM to consult.
    err = (result.get("error") or "").lower()
    no_candidates = "could not find" in err  # see _load_manual_once's wording
    if not no_candidates or not agent.llm:
        return result

    variants = await _suggest_device_variants(agent, device)
    if not variants:
        return result

    await agent.log(f"Retrying with LLM-suggested variants: {variants}")

    tried = {device.lower().strip()}
    last_result = result
    for v in variants[:2]:   # cap at 2 retries
        if not v or v.lower().strip() in tried:
            continue
        tried.add(v.lower().strip())
        await agent.log(f"Trying variant: {v!r}")
        r2 = await _load_manual_once(agent, v)
        if "error" not in r2:
            # Success — but tag the result so the caller knows we corrected
            r2 = dict(r2)
            r2["corrected_from"] = device
            r2["result"] = f"(Resolved '{device}' → '{v}')\n" + (r2.get("result") or "")
            return r2
        last_result = r2

    # All variants failed — return the most recent failure (best diagnostics)
    return last_result


async def _suggest_device_variants(agent, device: str) -> list:
    """
    Ask the LLM for up to 3 alternate spellings/forms of the device name,
    ordered most-likely first. Returns [] on any failure so the caller can
    fall back gracefully.
    """
    prompt = (
        f"The user asked for the manual of: {device!r}\n\n"
        f"Web search found no results. Suggest up to 3 alternate forms of "
        f"this device name that a manufacturer would actually use, ordered "
        f"most-likely first. Apply these fixes:\n"
        f"  • Correct misspellings of brand names "
        f"(Phillips→Philips, Cannon→Canon, Sumsung→Samsung, etc.)\n"
        f"  • If the brand is missing but inferrable, prepend it.\n"
        f"  • If the model number has a common prefix family "
        f"(Philips espresso 2200 → EP2200), include the prefixed form.\n"
        f"  • If a model number was given without the brand, include both.\n\n"
        f"Return ONLY a JSON array of strings — no prose, no markdown.\n"
        f"Example output: [\"Philips EP2200\", \"Philips 2200 series\"]"
    )
    try:
        raw = await agent.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system="You are a device-naming expert. Return only valid JSON.",
        )
    except Exception as e:
        await agent.log(f"Variant suggestion LLM call failed: {e}")
        return []

    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(x) for x in arr if isinstance(x, (str,))]
        except Exception:
            pass
    # Try greedy array match
    m = re.search(r"\[[\s\S]*?\]", s)
    if m:
        try:
            arr = json.loads(m.group(0))
            return [str(x) for x in arr if isinstance(x, (str,))]
        except Exception:
            return []
    return []


async def _load_manual_once(agent, device: str, explicit_url: Optional[str] = None) -> dict:
    await agent.log(f"Searching for manual: {device}")

    loop = asyncio.get_event_loop()

    # ── Explicit URL: skip search entirely (escape hatch) ───────────────
    if explicit_url:
        await agent.log(f"Using explicit URL (skipping search): {explicit_url}")
        candidates = [explicit_url]
        cached_urls = []
        cache_key = _cache_key(device)
        url_cache = agent.state.get("url_cache") or {}
    else:
        # ── Cache check: if we've successfully loaded this device before,
        # try the remembered URL(s) first. Saves a full search round-trip and
        # rescues us when search engines are rate-limiting.
        cache_key = _cache_key(device)
        url_cache = agent.state.get("url_cache") or {}
        cached_urls = url_cache.get(cache_key, [])
        if cached_urls:
            await agent.log(f"Cache hit: {len(cached_urls)} remembered URL(s) for '{device}'")

        # Fresh search — even if we have cached URLs, we still search so the
        # cache stays warm and we get new candidates if cached ones rot.
        # (Cached candidates come FIRST in priority.)
        fresh = await loop.run_in_executor(
            None, lambda: _find_manual_candidates(agent, device)
        )

        # Cache first, fresh second (dedupe preserving order)
        candidates = []
        seen = set()
        for u in cached_urls + fresh:
            if u and u not in seen:
                seen.add(u)
                candidates.append(u)

    if not candidates:
        await agent.alert(f"No PDF manual found for: {device}", "warning")
        return {
            "error":  f"Could not find a PDF manual for: {device}",
            "result": (
                f"I couldn't find a PDF manual for '{device}'. "
                f"Search engines may be rate-limiting — try again in a few minutes, "
                f"or send the URL directly: "
                f'{{"action": "load_manual", "device": "{device}", "url": "https://..."}}'
            ),
            "hint":  "Search engines may be rate-limiting. Try again in a few minutes, or "
                     "pass an explicit url field with the manual URL.",
        }

    await agent.log(f"Got {len(candidates)} candidate URLs — trying them in order")

    for i, pdf_url in enumerate(candidates, 1):
        await agent.log(f"[{i}/{len(candidates)}] Trying: {pdf_url}")

        pdf_bytes = await _download_pdf(agent, pdf_url)
        if not pdf_bytes:
            await agent.log(f"[{i}/{len(candidates)}] Download failed — next")
            continue

        size_kb = len(pdf_bytes) // 1024
        await agent.log(f"[{i}/{len(candidates)}] Downloaded {size_kb} KB — extracting...")

        text, pages = await loop.run_in_executor(
            None, lambda b=pdf_bytes: _extract_text(agent, b)
        )
        if not text:
            await agent.log(f"[{i}/{len(candidates)}] No extractable text — next")
            continue

        agent.state["manual_text"]   = text
        agent.state["manual_device"] = device
        agent.state["manual_url"]    = pdf_url
        agent.state["manual_pages"]  = pages

        # Update cache: put winning URL at the front, keep up to 5 backups
        existing = [u for u in (url_cache.get(cache_key) or []) if u != pdf_url]
        url_cache[cache_key] = [pdf_url] + existing[:4]
        agent.state["url_cache"] = url_cache

        await agent.log(f"✓ Manual loaded: {device} — {pages} pages, {len(text):,} chars")

        return {
            "success": True,
            "device":  device,
            "url":     pdf_url,
            "pages":   pages,
            "chars":   len(text),
            "preview": text[:300].replace("\n", " ").strip(),
            "result":  (
                f"Manual loaded: {device}\n"
                f"  URL:   {pdf_url}\n"
                f"  Pages: {pages}\n"
                f"  Chars: {len(text):,}"
            ),
        }

    await agent.alert(f"All {len(candidates)} candidates failed for: {device}", "warning")
    return {
        "error": f"Could not load any of {len(candidates)} candidate manuals for: {device}",
        "result": (
            f"I found {len(candidates)} possible manual URLs for '{device}' but "
            f"couldn't successfully download and extract any of them. The PDFs may be "
            f"behind a paywall, redirecting to a viewer page, or simply offline. "
            f"You can supply a direct URL by sending: "
            f'{{"action": "load_manual", "device": "{device}", "url": "https://..."}}'
        ),
        "candidates_tried": candidates,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Search — collect candidates (don't pick just one)
# ══════════════════════════════════════════════════════════════════════════════

def _find_manual_candidates(agent, device: str) -> list:
    """
    Returns an ORDERED list of candidate URLs (best first). The loader tries
    them one by one, so a 404 or non-PDF HTML page doesn't end the search.
    De-duplicates while preserving order.
    """
    try:
        import httpx
    except ImportError:
        return []

    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    candidates: list = []

    def add(url: str):
        if url and url.startswith("http") and url not in candidates:
            candidates.append(url)

    # ── Pass 1: direct vendor patterns (Philips EPxxxx model numbers) ────
    model_m = re.search(r'EP\d{4}', device, re.IGNORECASE)
    if model_m:
        model = model_m.group(0).upper()
        ml    = model.lower()
        logger.info(f"Pass 1: trying direct Philips URLs for model {model}")
        direct_urls = [
            f"https://www.download.p4c.philips.com/files/e/{ml}/{ml}_pss_aenghk.pdf",
            f"https://www.download.p4c.philips.com/files/e/{ml}_31/{ml}_31_pss_aenghk.pdf",
            f"https://www.download.p4c.philips.com/files/e/{ml}/{ml}_user_manual_en.pdf",
        ]
        try:
            with httpx.Client(follow_redirects=True, timeout=10, headers=headers) as client:
                for url in direct_urls:
                    try:
                        r = client.head(url)
                        ct = r.headers.get("content-type", "")
                        if r.status_code == 200 and ("pdf" in ct or url.endswith(".pdf")):
                            logger.info(f"  ✓ direct URL works: {url}")
                            add(url)
                    except Exception:
                        continue
        except Exception as e:
            logger.info(f"  Philips direct check failed: {e}")

    # ── Pass 2: DuckDuckGo HTML scrape (THIS is what works — your logs ───
    #     showed 40 hits / 10 URLs from this pass).  We promote it before
    #     DDGS-package queries because:
    #       - it's faster (one HTTP request per query, no multi-page paginate)
    #       - it gives us direct .pdf URLs more reliably
    ddg_urls = _ddg_html_scrape(agent, device, headers)
    for u in ddg_urls:
        add(u)

    # ── Pass 3: DDGS library — only if we still need more candidates ─────
    if len(candidates) < 3:
        ddgs_urls = _ddgs_collect(agent, device)
        for u in ddgs_urls:
            add(u)
    else:
        logger.info(f"Skipping DDGS library: already have {len(candidates)} candidates")

    logger.info(f"Total unique candidates collected: {len(candidates)}")
    for i, u in enumerate(candidates[:10], 1):
        logger.info(f"  [{i}] {u}")

    return candidates


def _ddgs_collect(agent, device: str) -> list:
    """Run DDGS queries, return ALL plausible manual URLs (best-tier first)."""
    queries = [
        f"{device} user manual filetype:pdf",
        f'"{device}" manual site:manualslib.com',
        f"{device} owner manual PDF download",
    ]

    def get_url(r):
        return r.get("href") or r.get("url") or r.get("link") or ""

    out: list = []
    try:
        try:
            from ddgs import DDGS
            logger.info("Pass 2: using ddgs package")
        except ImportError:
            from duckduckgo_search import DDGS
            logger.info("Pass 2: using legacy duckduckgo_search")

        # DDGS supports a comma-separated backend string for ordered fallback.
        # Prefer duckduckgo (real destination URLs) and google/brave (good
        # quality), with bing last because bing has been returning brand
        # homepages instead of manual pages.
        BACKENDS = "duckduckgo, brave, google, yahoo, bing"

        with DDGS() as ddgs:
            for query in queries:
                try:
                    try:
                        results = list(ddgs.text(
                            query, max_results=8, backend=BACKENDS
                        ))
                    except TypeError:
                        # very old API — no backend param
                        results = list(ddgs.text(query, max_results=8))

                    logger.info(f"  query={query!r} → {len(results)} results")
                    if results:
                        # log up to 3 URLs so you can see what we're getting
                        for i, r in enumerate(results[:3]):
                            logger.info(f"    [{i}] {get_url(r)!r}  title={r.get('title','')[:50]!r}")

                    ranked = _rank_manual_urls(results, get_url)
                    logger.info(f"    → {len(ranked)} URL(s) passed the manual filter")
                    out.extend(ranked)
                except Exception as e:
                    logger.info(f"  DDGS query failed ({query!r}): {e}")
                    continue
    except Exception as e:
        logger.info(f"Pass 2: DDGS unavailable ({e})")
    return out


def _ddg_html_scrape(agent, device: str, headers: dict) -> list:
    """
    HTML-search-engine scraping with rate-limit handling.

    Strategy:
      1. Try DuckDuckGo HTML  (html.duckduckgo.com)
      2. If DDG returns 202 (rate-limited) on the first query, skip the rest
         and try Mojeek (independent index, very rarely rate-limits)
      3. Between queries, sleep with jitter to avoid tripping anti-bot
      4. Rotate User-Agent across queries

    Your previous log showed DDG returning 202 (Accepted but no body) — that's
    its "soft block" response. Hammering it harder makes it worse, so we back
    off and switch engines instead.
    """
    try:
        import httpx
        import urllib.parse
        import random
        import time
    except ImportError:
        return []

    logger.info("Pass 3: HTML-scrape search engines (DDG → Mojeek)")

    queries = [
        f"{device} user manual filetype:pdf",
        f"{device} manual site:manualslib.com",
        f"{device} owner manual PDF",
    ]

    user_agents = [
        "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    ]

    out: list = []

    def _harvest(html: str, source: str) -> list:
        """Pull manual-shaped URLs out of an HTML response."""
        page_urls: list = []

        # Direct anchor URLs
        for u in re.findall(r'href="(https?://[^"]+)"', html):
            if any(d in u for d in _SEARCH_ENGINE_DOMAINS):
                continue
            if u.lower().endswith(".pdf") or "manualslib.com" in u:
                page_urls.append(u)

        # DDG redirect wrapper /l/?uddg=<urlencoded>
        for m in re.finditer(r'/l/\?(?:kh=[^&]*&)?uddg=([^"&]+)', html):
            try:
                decoded = urllib.parse.unquote(m.group(1))
                if decoded.startswith("http") and not any(d in decoded for d in _SEARCH_ENGINE_DOMAINS):
                    page_urls.append(decoded)
            except Exception:
                continue

        cleaned: list = []
        for u in page_urls:
            # Rewrite ManualsLib viewer pages → direct PDF download
            if "manualslib.com/manual/" in u and not u.endswith(".pdf"):
                u = u.split("?")[0].rstrip("/")
                if u.endswith(".html"):
                    u = u[:-5] + "/download.pdf"
                else:
                    u = u + "/download.pdf"
            cleaned.append(u)

        logger.info(f"  [{source}] harvested {len(cleaned)} URLs")
        return cleaned

    # ── Engine 1: DuckDuckGo HTML ────────────────────────────────────────
    ddg_blocked = False
    ddg_headers = dict(headers)

    with httpx.Client(follow_redirects=True, timeout=15) as client:
        for i, query in enumerate(queries):
            ddg_headers["User-Agent"] = random.choice(user_agents)
            q = urllib.parse.quote_plus(query)
            url = f"https://html.duckduckgo.com/html/?q={q}"
            try:
                r = client.get(url, headers=ddg_headers)
            except Exception as e:
                logger.info(f"  [DDG] query={query!r}: request failed ({e})")
                continue

            if r.status_code == 202 or not r.text or len(r.text) < 500:
                # 202 = rate-limited / no body
                logger.info(
                    f"  [DDG] query={query!r}: status={r.status_code} "
                    f"body_len={len(r.text)} — likely rate-limited"
                )
                ddg_blocked = True
                # Don't keep hammering — break out and try Mojeek
                break
            if r.status_code != 200:
                logger.info(f"  [DDG] query={query!r}: status {r.status_code}")
                continue

            out.extend(_harvest(r.text, "DDG"))

            # Jittered delay between queries (1.5–3.0s) to look human
            if i < len(queries) - 1:
                time.sleep(1.5 + random.random() * 1.5)

    # ── Engine 2: Mojeek (independent index, fallback when DDG is blocked) ─
    if ddg_blocked or len(out) < 3:
        logger.info("  [DDG] blocked or sparse — trying Mojeek")
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            mojeek_headers = dict(headers)
            for i, query in enumerate(queries):
                mojeek_headers["User-Agent"] = random.choice(user_agents)
                q = urllib.parse.quote_plus(query)
                url = f"https://www.mojeek.com/search?q={q}"
                try:
                    r = client.get(url, headers=mojeek_headers)
                except Exception as e:
                    logger.info(f"  [Mojeek] query={query!r}: request failed ({e})")
                    continue

                if r.status_code != 200 or len(r.text) < 500:
                    logger.info(
                        f"  [Mojeek] query={query!r}: status={r.status_code} "
                        f"body_len={len(r.text)}"
                    )
                    continue

                out.extend(_harvest(r.text, "Mojeek"))

                if i < len(queries) - 1:
                    time.sleep(1.0 + random.random() * 1.0)

    # Dedupe preserving order
    seen = set()
    deduped = []
    for u in out:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    logger.info(f"  HTML scrape total: {len(deduped)} unique URLs")
    return deduped


def _rank_manual_urls(results, get_url_fn) -> list:
    """
    Take a list of search-result dicts and return URLs in priority order:
      Tier 1: direct .pdf
      Tier 2: trusted manual host
      Tier 3: URL or body mentions 'manual' or 'pdf', not a search engine
    """
    t1, t2, t3 = [], [], []
    for r in results:
        u = get_url_fn(r)
        if not u or not u.startswith("http"):
            continue
        u_lower = u.lower()

        # Skip search-engine self-links
        if any(d in u for d in _SEARCH_ENGINE_DOMAINS):
            continue

        if u_lower.endswith(".pdf"):
            t1.append(u)
            continue

        if any(t in u for t in TRUSTED_SITES):
            # ManualsLib viewer pages → append /download.pdf
            if "manualslib.com" in u and not u_lower.endswith(".pdf"):
                t2.append(u.rstrip("/") + "/download.pdf")
            else:
                t2.append(u)
            continue

        body = (r.get("body", "") + " " + r.get("title", "")).lower()
        if "manual" in u_lower or "pdf" in u_lower or "manual" in body or "pdf" in body:
            t3.append(u)

    return t1 + t2 + t3


# ══════════════════════════════════════════════════════════════════════════════
# Download
# ══════════════════════════════════════════════════════════════════════════════

async def _download_pdf(agent, url: str) -> Optional[bytes]:
    try:
        import httpx
    except ImportError:
        await agent.log("httpx is not installed — cannot download PDF")
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct or resp.content[:4] == b"%PDF":
                return resp.content
            # Hunt for embedded PDF link in HTML
            links = re.findall(r'https?://[^\s"\'<>]+\.pdf', resp.text, re.IGNORECASE)
            if links:
                r2 = await client.get(links[0])
                if r2.status_code == 200 and r2.content[:4] == b"%PDF":
                    return r2.content
    except Exception as e:
        await agent.log(f"Download failed for {url}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Extract text
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Extract text — PyMuPDF first (10-20x faster than pdfplumber)
# ══════════════════════════════════════════════════════════════════════════════

# Cap pages we extract. User manuals are often 100+ pages with multi-language
# sections we don't need. Anything beyond this would push us past handle_task's
# 60s timeout, especially on slower CPUs.
_MAX_PAGES_EXTRACTED = 80


def _extract_text(agent, pdf_bytes: bytes) -> tuple:
    import io
    import time

    # ── Strategy 1: PyMuPDF (fitz) — fast, used by the doc-to-pptx agent too ─
    try:
        import fitz   # pymupdf
        t0 = time.time()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)
        max_pages = min(total_pages, _MAX_PAGES_EXTRACTED)
        parts = []
        for i in range(max_pages):
            try:
                t = doc[i].get_text()
                if t:
                    parts.append(t)
            except Exception:
                continue
        doc.close()
        elapsed = time.time() - t0
        logger.info(
            f"  PyMuPDF extracted {max_pages}/{total_pages} pages in {elapsed:.1f}s"
        )
        if parts:
            return "\n".join(parts), total_pages
    except ImportError:
        logger.info("  PyMuPDF (fitz) not available — falling back to pdfplumber")
    except Exception as e:
        logger.info(f"  PyMuPDF failed ({e}) — falling back to pdfplumber")

    # ── Strategy 2: pdfplumber fallback (slow but accurate) ──
    try:
        import pdfplumber
        t0 = time.time()
        parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages)
            max_pages = min(total_pages, _MAX_PAGES_EXTRACTED)
            for i in range(max_pages):
                # Time-bound: if pdfplumber is taking too long, bail early
                if time.time() - t0 > 45:
                    logger.info(
                        f"  pdfplumber 45s budget exceeded at page {i}/{max_pages} — stopping"
                    )
                    break
                try:
                    t = pdf.pages[i].extract_text()
                    if t:
                        parts.append(t)
                except Exception:
                    continue
        elapsed = time.time() - t0
        logger.info(
            f"  pdfplumber extracted {len(parts)} pages in {elapsed:.1f}s"
        )
        if parts:
            return "\n".join(parts), total_pages
    except ImportError:
        logger.info("  pdfplumber not available either")
    except Exception as e:
        logger.info(f"  pdfplumber failed: {e}")

    return "", 0


# ══════════════════════════════════════════════════════════════════════════════
# Ask
# ══════════════════════════════════════════════════════════════════════════════

async def _ask(agent, question: str) -> dict:
    manual_text = agent.state.get("manual_text")
    if not manual_text:
        return {
            "error": "No manual loaded yet.",
            "hint":  "Tell me which device's manual to load first — "
                     "e.g. 'load the Philips 2200 manual'.",
        }
    if not agent.llm:
        return {"error": "No LLM configured on this agent."}

    await agent.log(f"Answering: {question}")

    chunks  = _chunk_text(manual_text, 600, 100)
    ranked  = _rank_chunks(chunks, question)[:6]
    context = "\n\n---\n\n".join(ranked)

    prompt = (
        f"You are a helpful assistant. Answer the question below using ONLY the provided manual excerpt.\n\n"
        f"Device: {agent.state.get('manual_device')}\n\n"
        f"Manual excerpt:\n{context[:6000]}\n\n"
        f"Question: {question}\n\n"
        f"Give a clear, step-by-step answer based on the manual. "
        f"If the manual doesn't contain the answer, say so."
    )

    # agent.llm is a _LLMInterface wrapper around the underlying provider.
    # Both .complete() and .chat() return just a string — the wrapper handles
    # the (response, usage) tuple unpacking internally and tracks tokens/cost.
    try:
        if hasattr(agent.llm, "complete"):
            response = await agent.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You answer questions strictly based on provided manual content.",
            )
        elif hasattr(agent.llm, "chat"):
            response = await agent.llm.chat(
                prompt,
                system="You answer questions strictly based on provided manual content.",
            )
        else:
            return {
                "error":  "LLM provider has no recognised interface (complete/chat).",
                "result": "LLM provider has no recognised interface (complete/chat).",
            }
    except Exception as e:
        await agent.log(f"_ask: LLM call raised: {e}")
        return {
            "error":  f"LLM call failed: {e}",
            "result": f"Sorry — the LLM call failed: {e}",
        }

    # The _LLMInterface returns "[LLM error: ...]" as the response string on
    # provider failure rather than raising. Treat that as an error too so the
    # caller sees a proper error field instead of a fake answer.
    if isinstance(response, str) and response.startswith("[") and response.endswith("]"):
        await agent.log(f"_ask: LLM returned sentinel: {response}")
        return {
            "error":  response.strip("[]"),
            "result": response,
        }

    return {
        "device":   agent.state.get("manual_device"),
        "question": question,
        "answer":   response,
        "result":   response,   # so the chat panel renders the text directly
    }


# ══════════════════════════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════════════════════════

def _status(agent) -> dict:
    device = agent.state.get("manual_device")
    if not device:
        return {"status": "idle", "result": "No manual loaded."}
    return {
        "status":  "loaded",
        "device":  device,
        "url":     agent.state.get("manual_url"),
        "pages":   agent.state.get("manual_pages", 0),
        "chars":   len(agent.state.get("manual_text") or ""),
        "result":  (
            f"Loaded: {device} "
            f"({agent.state.get('manual_pages', 0)} pages, "
            f"{len(agent.state.get('manual_text') or ''):,} chars)"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _cache_key(device: str) -> str:
    """
    Normalise a device name into a cache key so that the LLM router's
    rephrasings still hit the same cache entry. Examples:

      "Philips 2200"       → "philips 2200"
      "philips ep2200"     → "philips ep2200"
      "Philips EP2200/10"  → "philips ep2200"
      "  Philips  2200  "  → "philips 2200"
    """
    s = (device or "").lower().strip()
    # Strip variant suffix after a slash ("EP2200/10" → "EP2200")
    s = s.split("/")[0]
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _keywords(text: str) -> list:
    words = re.findall(r'[a-z]+', text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _chunk_text(text: str, chunk_size=600, overlap=100) -> list:
    words  = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + chunk_size]))
        i += chunk_size - overlap
    return chunks


def _rank_chunks(chunks, question: str) -> list:
    kws    = _keywords(question)
    scored = [(sum(c.lower().count(kw) for kw in kws), c) for c in chunks]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]
'''
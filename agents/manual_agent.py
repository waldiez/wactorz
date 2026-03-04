"""
ManualAgent — searches the internet for device manuals, downloads PDFs,
extracts text, and answers questions using the agent's LLM.
"""

import asyncio
import base64
import importlib
import json
import logging
import re
import time
import urllib.parse
from typing import Optional

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

TRUSTED_SITES = [
    'manualslib.com', 'manualzz.com', 'manuals.plus',
    'documents.philips.com', 'download.p4c.philips.com',
    'support.brother.com', 'docs.brother.com',
    'support.hp.com', 'support.epson.net',
    'support.canon.com', 'dl.owneriq.net',
]

# Domains to ignore when extracting links from search engine HTML
_SEARCH_ENGINE_DOMAINS = {
    'bing.com', 'microsoft.com', 'google.com', 'googleapis.com',
    'gstatic.com', 'youtube.com', 'schema.org', 'w3.org',
    'microsofttranslator.com', 'bingapis.com',
}


class ManualAgent(Actor):
    """
    Pre-defined agent that finds, downloads, and answers questions from device manuals.
    Requires: httpx  (+ pdfplumber or pymupdf for PDF extraction)
    """

    def __init__(self, llm_provider=None, **kwargs):
        kwargs.setdefault("name", "manual-agent")
        super().__init__(**kwargs)
        self.llm              = llm_provider
        self._manual_text:    Optional[str]  = None
        self._manual_device:  Optional[str]  = None
        self._manual_url:     Optional[str]  = None
        self._manual_pages:   int            = 0

    def _current_task_description(self) -> str:
        if self._manual_device:
            return f"loaded: {self._manual_device}"
        return "idle — no manual loaded"

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": "Manual agent ready. Send {action: load_manual, device: ...} to begin.", "timestamp": time.time()},
        )
        logger.info(f"[{self.name}] Ready.")

    # ── Direct chat() entry point (used by CLIInterface) ───────────────────

    async def chat(self, message: str) -> str:
        """
        Synchronous-style entry point for CLIInterface and other direct callers.
        Parses the message as JSON payload or plain-text question, executes the
        action, and returns a human-readable string response.
        """
        payload = None
        stripped = message.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                pass

        if payload and isinstance(payload, dict):
            result = await self._handle_task_payload(payload)
        else:
            if self._manual_text:
                result = await self._ask(stripped)
            else:
                result = {
                    "error": "No manual loaded yet.",
                    "hint": 'Send: {"action": "load_manual", "device": "Your Device Model"}',
                }

        return self._format_result(result)

    def _format_result(self, result: dict) -> str:
        """Turn a result dict into a readable string for chat output."""
        if "error" in result:
            msg = result["error"]
            hint = result.get("hint", "")
            return f"[error] {msg}\n{hint}".strip()

        if "answer" in result:
            return result["answer"]

        if result.get("success"):
            return (
                f"Manual loaded: {result.get('device', '?')}\n"
                f"  URL:   {result.get('url', '?')}\n"
                f"  Pages: {result.get('pages', '?')}\n"
                f"  Chars: {result.get('chars', '?'):,}\n"
                f"  Preview: {result.get('preview', '')[:200]}"
            )

        if "status" in result:
            if result["status"] == "cleared":
                return "Manual cleared."
            if result["status"] == "loaded":
                return (
                    f"Loaded: {result.get('device', '?')} "
                    f"({result.get('pages', '?')} pages, {result.get('chars', '?'):,} chars)"
                )
            return result.get("message", str(result))

        return str(result)

    # ── Message-based entry point (actor mailbox) ──────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            try:
                result = await self._handle_task(msg)
            except Exception as e:
                logger.error(f"[{self.name}] Task handling failed: {e}", exc_info=True)
                result = {"error": f"Internal error: {e}"}

            target = msg.reply_to or msg.sender_id
            if target:
                await self.send(target, MessageType.RESULT, result)
            else:
                logger.warning(
                    f"[{self.name}] No reply target (reply_to={msg.reply_to!r}, "
                    f"sender_id={msg.sender_id!r}). Result discarded: {result}"
                )

    async def _handle_task(self, msg: Message) -> dict:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if not isinstance(msg.payload, dict):
            text = str(msg.payload).strip()
            if text:
                return await self._ask(text)
            return {"error": "Send a dict payload with 'action' key"}

        return await self._handle_task_payload(payload)

    async def _handle_task_payload(self, payload: dict) -> dict:
        """Core task dispatcher — shared by both chat() and handle_message()."""
        action = payload.get("action", "").lower()

        if action == "load_manual":
            device = payload.get("device") or payload.get("query", "")
            if not device:
                return {"error": "Missing 'device' field"}
            return await self._load_manual(device)

        if action == "ask":
            question = payload.get("question") or payload.get("query") or payload.get("text", "")
            if not question:
                return {"error": "Missing 'question' field"}
            return await self._ask(question)

        if action == "status":
            return self._status()

        if action == "clear":
            self._manual_text   = None
            self._manual_device = None
            self._manual_url    = None
            self._manual_pages  = 0
            return {"status": "cleared"}

        if "question" in payload or "query" in payload:
            return await self._ask(payload.get("question") or payload.get("query", ""))

        return {
            "error": f"Unknown action: '{action}'",
            "supported": ["load_manual", "ask", "status", "clear"],
        }

    # ── Load manual ────────────────────────────────────────────────────────

    async def _load_manual(self, device: str) -> dict:
        await self._log(f"Searching for manual: {device}")

        loop    = asyncio.get_event_loop()
        pdf_url = await loop.run_in_executor(None, lambda: self._search_for_manual(device))

        if not pdf_url:
            await self._alert(f"No PDF manual found for: {device}", "warning")
            return {"error": f"Could not find a PDF manual for: {device}"}

        await self._log(f"Found: {pdf_url}")

        pdf_bytes = await self._download_pdf(pdf_url)
        if not pdf_bytes:
            return {"error": f"Failed to download PDF from: {pdf_url}"}

        size_kb = len(pdf_bytes) // 1024
        await self._log(f"Downloaded {size_kb} KB — extracting text...")

        text, pages = await loop.run_in_executor(None, lambda: self._extract_text(pdf_bytes))
        if not text:
            return {"error": "PDF has no extractable text (may be a scanned image PDF)."}

        self._manual_text   = text
        self._manual_device = device
        self._manual_url    = pdf_url
        self._manual_pages  = pages

        await self._log(f"Manual loaded: {device} — {pages} pages, {len(text):,} chars")
        await self._publish_status()

        return {
            "success": True,
            "device":  device,
            "url":     pdf_url,
            "pages":   pages,
            "chars":   len(text),
            "preview": text[:300].replace("\n", " ").strip(),
        }

    # ── Search ─────────────────────────────────────────────────────────────

    def _search_for_manual(self, device: str) -> Optional[str]:
        try:
            import httpx
        except ImportError:
            logger.error(f"[{self.name}] httpx is not installed — cannot search for manuals")
            return None

        headers = {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        # ── Pass 1: direct Philips document server (model number pattern) ──
        model_m = re.search(r'EP\d{4}', device, re.IGNORECASE)
        if model_m:
            model = model_m.group(0).upper()
            ml    = model.lower()
            direct_urls = [
                f"https://www.download.p4c.philips.com/files/e/{ml}/{ml}_pss_aenghk.pdf",
                f"https://www.download.p4c.philips.com/files/e/{ml}_31/{ml}_31_pss_aenghk.pdf",
                f"https://www.download.p4c.philips.com/files/e/{ml}/{ml}_user_manual_en.pdf",
                f"https://www.documents.philips.com/doclib/enc/fetch/2000/4504/261257/261271/User_Manual_{model}.pdf",
            ]
            try:
                with httpx.Client(follow_redirects=True, timeout=10, headers=headers) as client:
                    for url in direct_urls:
                        try:
                            r = client.head(url)
                            ct = r.headers.get("content-type", "")
                            if r.status_code == 200 and ("pdf" in ct or url.endswith(".pdf")):
                                logger.info(f"[{self.name}] Direct URL works: {url}")
                                return url
                        except Exception as e:
                            logger.debug(f"[{self.name}] Direct URL failed ({url}): {e}")
                            continue
            except Exception as e:
                logger.warning(f"[{self.name}] Philips direct check failed: {e}")

        # ── Pass 2: DDGS search ────────────────────────────────────────────
        result = self._search_ddgs(device)
        if result:
            return result

        # ── Pass 3: Bing scrape (with redirect URL decoding) ───────────────
        result = self._search_bing_scrape(device, headers)
        if result:
            return result

        # ── Pass 4: Google scrape fallback ─────────────────────────────────
        result = self._search_google_scrape(device, headers)
        if result:
            return result

        logger.warning(f"[{self.name}] All search passes exhausted — no manual found for: {device}")
        return None

    # ── Pass 2: DDGS ──────────────────────────────────────────────────────

    def _search_ddgs(self, device: str) -> Optional[str]:
        queries = [
            f"{device} user manual filetype:pdf",
            f"{device} user manual PDF manualslib OR manualzz",
            f"{device} owner manual PDF download",
        ]

        def get_url(r):
            return r.get("href") or r.get("url") or r.get("link") or ""

        try:
            try:
                from ddgs import DDGS
                logger.info(f"[{self.name}] Pass 2: using ddgs package")
            except ImportError:
                from duckduckgo_search import DDGS
                logger.info(f"[{self.name}] Pass 2: using duckduckgo_search (deprecated)")

            with DDGS() as ddgs:
                for query in queries:
                    try:
                        results = list(ddgs.text(query, max_results=15))
                        logger.info(f"[{self.name}] Pass 2 query: {query!r} → {len(results)} results")

                        for i, r in enumerate(results[:5]):
                            logger.info(
                                f"[{self.name}]   [{i}] url={get_url(r)!r} "
                                f"title={r.get('title', '')[:60]!r}"
                            )

                        match = self._pick_best_url(results, get_url)
                        if match:
                            logger.info(f"[{self.name}] Pass 2 HIT: {match}")
                            return match

                    except Exception as e:
                        logger.warning(f"[{self.name}] DDGS query failed ({query}): {e}")
                        continue
        except ImportError:
            logger.warning(f"[{self.name}] Neither ddgs nor duckduckgo_search installed — skipping")

        return None

    # ── Pass 3: Bing scrape ───────────────────────────────────────────────

    def _search_bing_scrape(self, device: str, headers: dict) -> Optional[str]:
        import httpx

        queries = [
            f"{device} user manual PDF",
            f"{device} manual PDF manualslib OR manualzz",
        ]

        try:
            with httpx.Client(follow_redirects=True, timeout=15, headers=headers) as client:
                for query in queries:
                    try:
                        url  = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
                        r    = client.get(url)
                        urls = self._extract_bing_urls(r.text)

                        logger.info(f"[{self.name}] Pass 3 query: {query!r} → {len(urls)} real URLs")
                        for i, u in enumerate(urls[:10]):
                            logger.info(f"[{self.name}]   [{i}] {u}")

                        # Build fake result dicts so we can reuse _pick_best_url
                        results = [{"href": u, "title": "", "body": ""} for u in urls]
                        match   = self._pick_best_url(results, lambda r: r["href"])
                        if match:
                            logger.info(f"[{self.name}] Pass 3 HIT: {match}")
                            return match

                    except Exception as e:
                        logger.warning(f"[{self.name}] Bing query failed ({query}): {e}")
                        continue
        except Exception as e:
            logger.warning(f"[{self.name}] Bing scrape failed entirely: {e}")

        return None

    # ── Pass 4: Google scrape ─────────────────────────────────────────────

    def _search_google_scrape(self, device: str, headers: dict) -> Optional[str]:
        import httpx

        queries = [
            f"{device} user manual PDF",
            f"{device} manual filetype:pdf",
        ]

        try:
            with httpx.Client(follow_redirects=True, timeout=15, headers=headers) as client:
                for query in queries:
                    try:
                        url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
                        r   = client.get(url)
                        urls = self._extract_google_urls(r.text)

                        logger.info(f"[{self.name}] Pass 4 query: {query!r} → {len(urls)} real URLs")
                        for i, u in enumerate(urls[:10]):
                            logger.info(f"[{self.name}]   [{i}] {u}")

                        results = [{"href": u, "title": "", "body": ""} for u in urls]
                        match   = self._pick_best_url(results, lambda r: r["href"])
                        if match:
                            logger.info(f"[{self.name}] Pass 4 HIT: {match}")
                            return match

                    except Exception as e:
                        logger.warning(f"[{self.name}] Google query failed ({query}): {e}")
                        continue
        except Exception as e:
            logger.warning(f"[{self.name}] Google scrape failed entirely: {e}")

        return None

    # ── URL extraction helpers ─────────────────────────────────────────────

    @staticmethod
    def _extract_bing_urls(html: str) -> list[str]:
        """
        Extract real destination URLs from Bing search results HTML.
        Bing wraps links as /ck/a?...&u=a1<base64url>...  — we decode those.
        Also picks up any direct href links that aren't bing/microsoft.
        """
        urls = []
        seen = set()

        # Method 1: decode Bing redirect URLs  (/ck/a?...u=a1<base64>...)
        for m in re.finditer(r'href="https?://www\.bing\.com/ck/a\?[^"]*?u=a1([A-Za-z0-9_-]+)[^"]*"', html):
            try:
                encoded = m.group(1)
                # Fix base64url padding
                padded  = encoded + "=" * (4 - len(encoded) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
                if decoded.startswith("http") and decoded not in seen:
                    seen.add(decoded)
                    urls.append(decoded)
            except Exception:
                continue

        # Method 2: direct hrefs that aren't search engine domains
        for m in re.finditer(r'href=["\'](https?://[^"\'<>\s]+)', html):
            link = m.group(1)
            if not any(d in link for d in _SEARCH_ENGINE_DOMAINS) and link not in seen:
                seen.add(link)
                urls.append(link)

        return urls

    @staticmethod
    def _extract_google_urls(html: str) -> list[str]:
        """
        Extract real destination URLs from Google search results HTML.
        Google wraps links as /url?q=<url>&... — we extract the q parameter.
        """
        urls = []
        seen = set()

        # Method 1: Google redirect links
        for m in re.finditer(r'/url\?q=(https?://[^&"]+)', html):
            try:
                decoded = urllib.parse.unquote(m.group(1))
                if not any(d in decoded for d in _SEARCH_ENGINE_DOMAINS) and decoded not in seen:
                    seen.add(decoded)
                    urls.append(decoded)
            except Exception:
                continue

        # Method 2: direct hrefs
        for m in re.finditer(r'href=["\'](https?://[^"\'<>\s]+)', html):
            link = m.group(1)
            if not any(d in link for d in _SEARCH_ENGINE_DOMAINS) and link not in seen:
                seen.add(link)
                urls.append(link)

        return urls

    # ── Shared URL ranking ─────────────────────────────────────────────────

    def _pick_best_url(self, results: list[dict], get_url_fn) -> Optional[str]:
        """
        From a list of search results, pick the best manual URL.
        Priority: direct .pdf link > trusted site > any link with 'manual' + 'pdf' signals.
        """
        # Tier 1: direct .pdf link
        for r in results:
            u = get_url_fn(r)
            if u.lower().endswith(".pdf"):
                return u

        # Tier 2: trusted manual site
        for r in results:
            u = get_url_fn(r)
            if any(t in u for t in TRUSTED_SITES):
                # ManualsLib pages need /download.pdf appended
                if "manualslib.com" in u and not u.endswith(".pdf"):
                    return u.rstrip("/") + "/download.pdf"
                return u

        # Tier 3: URL contains 'manual' or 'pdf' (but not a search engine)
        for r in results:
            u = get_url_fn(r)
            u_lower = u.lower()
            if u.startswith("http") and ("manual" in u_lower or "pdf" in u_lower):
                if not any(d in u for d in _SEARCH_ENGINE_DOMAINS):
                    return u

        # Tier 4: body/title mentions 'pdf' or 'manual'
        for r in results:
            u = get_url_fn(r)
            text = (r.get("body", "") + r.get("title", "")).lower()
            if ("pdf" in text or "manual" in text) and u.startswith("http"):
                if not any(d in u for d in _SEARCH_ENGINE_DOMAINS):
                    return u

        return None

    # ── Download ───────────────────────────────────────────────────────────

    async def _download_pdf(self, url: str) -> Optional[bytes]:
        try:
            import httpx
        except ImportError:
            logger.error(f"[{self.name}] httpx is not installed — cannot download PDF")
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=headers) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning(f"[{self.name}] Download returned status {resp.status_code} for: {url}")
                    return None
                ct = resp.headers.get("content-type", "")
                if "pdf" in ct or resp.content[:4] == b"%PDF":
                    return resp.content
                # HTML — hunt for embedded PDF link
                links = re.findall(r'https?://[^\s"\'<>]+\.pdf', resp.text, re.IGNORECASE)
                if links:
                    logger.info(f"[{self.name}] Following embedded PDF link: {links[0]}")
                    r2 = await client.get(links[0])
                    if r2.status_code == 200 and r2.content[:4] == b"%PDF":
                        return r2.content
                logger.warning(f"[{self.name}] URL did not return a PDF: {url} (content-type: {ct})")
        except Exception as e:
            logger.warning(f"[{self.name}] Download failed for {url}: {e}")
        return None

    # ── Extract text ───────────────────────────────────────────────────────

    def _extract_text(self, pdf_bytes: bytes) -> tuple[str, int]:
        import io
        try:
            import pdfplumber
            parts = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = len(pdf.pages)
                for p in pdf.pages:
                    t = p.extract_text()
                    if t:
                        parts.append(t)
            if parts:
                return "\n".join(parts), pages
        except ImportError:
            logger.warning(f"[{self.name}] pdfplumber not installed — trying pymupdf")
        except Exception as e:
            logger.warning(f"[{self.name}] pdfplumber extraction failed: {e}")

        try:
            import fitz
            doc   = fitz.open(stream=pdf_bytes, filetype="pdf")
            parts = [p.get_text() for p in doc]
            return "\n".join(t for t in parts if t), len(doc)
        except ImportError:
            logger.error(f"[{self.name}] Neither pdfplumber nor pymupdf (fitz) installed — cannot extract text")
        except Exception as e:
            logger.warning(f"[{self.name}] pymupdf extraction failed: {e}")

        return "", 0

    # ── Ask ────────────────────────────────────────────────────────────────

    async def _ask(self, question: str) -> dict:
        if not self._manual_text:
            return {
                "error":  "No manual loaded yet.",
                "hint":   'Send: {"action": "load_manual", "device": "Your Device Model"}',
            }
        if not self.llm:
            return {"error": "No LLM configured on this agent."}

        await self._log(f"Answering: {question}")

        chunks  = self._chunk_text(self._manual_text, 600, 100)
        ranked  = self._rank_chunks(chunks, question)[:6]
        context = "\n\n---\n\n".join(ranked)

        prompt = (
            f"You are a helpful assistant. Answer the question below using ONLY the provided manual excerpt.\n\n"
            f"Device: {self._manual_device}\n\n"
            f"Manual excerpt:\n{context[:6000]}\n\n"
            f"Question: {question}\n\n"
            f"Give a clear, step-by-step answer based on the manual. "
            f"If the manual doesn't contain the answer, say so."
        )

        if hasattr(self.llm, "complete"):
            response, _ = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You answer questions strictly based on provided manual content.",
            )
        else:
            response = str(self.llm)

        return {
            "device":   self._manual_device,
            "question": question,
            "answer":   response,
        }

    # ── Status ─────────────────────────────────────────────────────────────

    def _status(self) -> dict:
        if not self._manual_device:
            return {"status": "idle", "message": "No manual loaded."}
        return {
            "status":  "loaded",
            "device":  self._manual_device,
            "url":     self._manual_url,
            "pages":   self._manual_pages,
            "chars":   len(self._manual_text or ""),
        }

    # ── Helpers ────────────────────────────────────────────────────────────

    _STOPWORDS = {
        'how','do','i','the','a','an','is','are','what','where','when','why',
        'can','does','to','for','of','in','on','at','my','this','that','it',
        'its','with','and','or','be','was','will','has','have','use','using',
        'get','me','please','tell','about','there','their','they','we','you',
        'your','which','make','need',
    }

    def _keywords(self, text: str) -> list[str]:
        words = re.findall(r'[a-z]+', text.lower())
        return [w for w in words if w not in self._STOPWORDS and len(w) > 2]

    def _chunk_text(self, text: str, chunk_size=600, overlap=100) -> list[str]:
        words  = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunks.append(" ".join(words[i:i + chunk_size]))
            i += chunk_size - overlap
        return chunks

    def _rank_chunks(self, chunks: list[str], question: str) -> list[str]:
        kws    = self._keywords(question)
        scored = [(sum(c.lower().count(kw) for kw in kws), c) for c in chunks]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored]

    # ── MQTT helpers ───────────────────────────────────────────────────────

    async def _log(self, msg: str):
        logger.info(f"[{self.name}] {msg}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": msg, "timestamp": time.time()},
        )

    async def _alert(self, msg: str, severity: str = "warning"):
        logger.warning(f"[{self.name}] ALERT: {msg}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/alerts",
            {"message": msg, "severity": severity, "timestamp": time.time()},
        )
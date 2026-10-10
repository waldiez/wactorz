"""The manual agent: routing a request, finding a manual, reading it, and answering from it.

`test_manual_agent_downloads.py` holds the fetch itself to public addresses and
a size; this covers the rest. The program is exec'd as it is at spawn and driven
through a stand-in `agent` and a scripted model. The libraries it imports inside
its functions -- `httpx`, `ddgs`, `fitz`, `pdfplumber` -- are stand-ins put in
`sys.modules`, and its clock's `sleep` returns at once, so nothing here touches
the network or waits.
"""

import sys
import types
from collections.abc import Coroutine
from typing import Any, ClassVar

import pytest

from tests.programs import program_namespace

NS = program_namespace("manual_agent.py")

PDF = b"%PDF-1.7 a manual"


class _Llm:
    """A model that answers each call with the next scripted reply, or raises it."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, messages: list[dict[str, str]], system: str = "") -> Any:
        self.prompts.append(messages[0]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _ChatLlm:
    """A model that offers only `chat`."""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, prompt: str, system: str = "") -> str:
        return self.reply


class _Agent:
    """What the program uses of `agent`, on a runtime without background tasks or a chat."""

    def __init__(self, llm: Any = None) -> None:
        self.state: dict[str, Any] = {}
        self.llm = llm
        self.logs: list[str] = []
        self.alerts: list[tuple[str, str]] = []
        self.notified: list[str] = []
        self.background: list[Coroutine[Any, Any, Any]] = []
        self.fail_notify = False

    async def log(self, text: str) -> None:
        self.logs.append(text)

    async def alert(self, message: str, severity: str = "warning") -> None:
        self.alerts.append((severity, message))

    async def finish_background(self) -> None:
        while self.background:
            await self.background.pop(0)


class _BackgroundAgent(_Agent):
    """A runtime that runs work in the background, with no chat to report to."""

    def run_in_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        self.background.append(coro)


class _ChatAgent(_BackgroundAgent):
    """A runtime as main and nodes give it: background work, and a chat to report to."""

    async def notify_user(self, message: str) -> None:
        if self.fail_notify:
            raise RuntimeError("no chat")
        self.notified.append(message)


async def _ready(llm: Any = None, *, background: bool = True, notify: bool = True) -> _Agent:
    kind = _ChatAgent if notify else _BackgroundAgent if background else _Agent
    agent = kind(llm)
    await NS["setup"](agent)
    return agent


def _loaded(agent: _Agent, text: str = "To descale, fill the tank. " * 40) -> None:
    agent.state.update(
        manual_text=text, manual_device="Philips 2200", manual_url="https://m.example/a.pdf"
    )
    agent.state["manual_pages"] = 12


class _Clock:
    """`time` as the program sees it: a clock the test moves, and sleeps that return."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.step = 0.0

    def time(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setitem(NS, "time", fake)
    return fake


# ── Stand-ins for the libraries it imports where it uses them ─────────────────


class _SyncResponse:
    def __init__(self, status: int = 200, text: str = "", content_type: str = "") -> None:
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}


class _SyncClient:
    """`httpx.Client`: each URL answered from the web the test sets up."""

    web: ClassVar[dict[str, Any]] = {}
    requested: ClassVar[list[str]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_SyncClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def _answer(self, url: str) -> _SyncResponse:
        _SyncClient.requested.append(url)
        for prefix, answer in _SyncClient.web.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return _SyncResponse(404)

    def head(self, url: str) -> _SyncResponse:
        return self._answer(url)

    def get(self, url: str, headers: Any = None) -> _SyncResponse:
        return self._answer(url)


class _AsyncResponse:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status_code = status
        self.is_redirect = False
        self.headers = {"content-type": content_type}
        self._body = body

    async def aiter_bytes(self) -> Any:
        yield self._body


class _AsyncStream:
    def __init__(self, response: _AsyncResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _AsyncResponse:
        return self._response

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _AsyncClient:
    """`httpx.AsyncClient`: downloads answered from the files the test sets up."""

    files: ClassVar[dict[str, Any]] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(self, _method: str, url: str) -> _AsyncStream:
        answer = _AsyncClient.files.get(url, (404, b"", "text/html"))
        if isinstance(answer, Exception):
            raise answer
        return _AsyncStream(_AsyncResponse(*answer))


@pytest.fixture(name="web")
def web_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    httpx = types.ModuleType("httpx")
    httpx.Client = _SyncClient  # pyright: ignore[reportAttributeAccessIssue]
    httpx.AsyncClient = _AsyncClient  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    _SyncClient.web = {}
    _SyncClient.requested = []
    _AsyncClient.files = {}
    # Every host in these tests is public unless its name says otherwise.
    monkeypatch.setitem(NS, "_public_address", lambda url: "private" not in url)
    return _SyncClient.web


def _page(*urls: str) -> _SyncResponse:
    """A search results page long enough to be read, linking to `urls`."""
    links = "".join(f'<a href="{u}">result</a>' for u in urls)
    return _SyncResponse(200, links + " " * 600)


class _Ddgs:
    """`ddgs.DDGS`: results per query, from the table the test sets."""

    results: ClassVar[dict[str, Any]] = {}
    old_api = False

    def __enter__(self) -> "_Ddgs":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def text(self, query: str, max_results: int, backend: str | None = None) -> list[Any]:
        if backend is not None and _Ddgs.old_api:
            raise TypeError("unexpected keyword argument 'backend'")
        for start, answer in _Ddgs.results.items():
            if query.startswith(start):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return []


@pytest.fixture(name="ddgs")
def ddgs_fixture(monkeypatch: pytest.MonkeyPatch) -> type[_Ddgs]:
    module = types.ModuleType("ddgs")
    module.DDGS = _Ddgs  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "ddgs", module)
    _Ddgs.results = {}
    _Ddgs.old_api = False
    return _Ddgs


class _FitzPage:
    def __init__(self, text: Any) -> None:
        self._text = text

    def get_text(self) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class _FitzDoc:
    def __init__(self, pages: list[Any]) -> None:
        self._pages = [_FitzPage(p) for p in pages]
        self.closed = False

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, i: int) -> _FitzPage:
        return self._pages[i]

    def close(self) -> None:
        self.closed = True


def _fitz(pages: list[Any] | Exception) -> types.ModuleType:
    module = types.ModuleType("fitz")

    def open_(stream: bytes, filetype: str) -> _FitzDoc:
        if isinstance(pages, Exception):
            raise pages
        return _FitzDoc(pages)

    module.open = open_  # pyright: ignore[reportAttributeAccessIssue]
    return module


class _PlumberPage:
    def __init__(self, text: Any) -> None:
        self._text = text

    def extract_text(self) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class _PlumberPdf:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = [_PlumberPage(p) for p in pages]

    def __enter__(self) -> "_PlumberPdf":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _pdfplumber(pages: list[Any] | Exception) -> types.ModuleType:
    module = types.ModuleType("pdfplumber")

    def open_(_stream: Any) -> _PlumberPdf:
        if isinstance(pages, Exception):
            raise pages
        return _PlumberPdf(pages)

    module.open = open_  # pyright: ignore[reportAttributeAccessIssue]
    return module


# ── Reading a request ──────────────────────────────────────────────────────────


class TestReadingARequest:
    async def test_json_as_a_string_or_inside_text_is_a_command(self) -> None:
        agent = await _ready()
        _loaded(agent)

        as_string = await NS["handle_task"](agent, '{"action": "status"}')
        inside_text = await NS["handle_task"](agent, {"text": '{"action": "status"}'})

        assert as_string["status"] == inside_text["status"] == "loaded"

    async def test_plain_words_go_to_the_router(self) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, "what's loaded?")

        assert result == {"status": "idle", "result": "No manual loaded."}

    async def test_what_is_not_a_request_is_refused(self) -> None:
        agent = await _ready()

        not_object = await NS["handle_task"](agent, "[1, 2]")
        empty = await NS["handle_task"](agent, {"text": "   "})
        broken = await NS["handle_task"](agent, {"text": "{not json"})

        assert not_object["error"].startswith("Invalid payload")
        assert empty["error"] == "Empty request."
        assert "couldn't tell which device" in broken["error"]

    async def test_commands_and_what_they_need(self) -> None:
        agent = await _ready()
        _loaded(agent)

        no_device = await NS["handle_task"](agent, {"action": "load_manual"})
        no_question = await NS["handle_task"](agent, {"action": "ask"})
        cleared = await NS["handle_task"](agent, {"action": "clear"})
        unknown = await NS["handle_task"](agent, {"action": "dance"})

        assert no_device == {"error": "Missing 'device' field"}
        assert no_question == {"error": "Missing 'question' field"}
        assert cleared["status"] == "cleared"
        assert agent.state["manual_text"] is None
        assert "load_manual" in unknown["supported"]


class TestTheModelRoutes:
    @pytest.mark.parametrize(
        "reply",
        [
            '{"tool": "status"}',
            '```json\n{"tool": "status"}\n```',
            'Sure! {"tool": "status"} is the call.',
        ],
    )
    async def test_its_answer_is_read_however_it_is_wrapped(self, reply: str) -> None:
        agent = await _ready(_Llm(reply))

        result = await NS["handle_task"](agent, "what is loaded right now")

        assert result["status"] == "idle"

    async def test_a_load_starts_in_the_background_and_answers_at_once(self) -> None:
        llm = _Llm('{"tool": "load_manual", "device": "Philips 2200"}')
        agent = await _ready(llm)

        result = await NS["handle_task"](agent, "load the Phillips 2200 manual")

        assert result["status"] == "loading"
        assert len(agent.background) == 1
        assert "User message: 'load the Phillips 2200 manual'" in llm.prompts[0]
        assert "Currently loaded manual: (none)" in llm.prompts[0]
        agent.background.pop().close()

    async def test_ask_clear_and_a_load_with_no_device(self) -> None:
        llm = _Llm(
            '{"tool": "ask", "question": "how do I descale?"}',
            "Fill the tank and run the descale cycle.",
            '{"tool": "load_manual"}',
            '{"tool": "clear"}',
        )
        agent = await _ready(llm)
        _loaded(agent)

        asked = await NS["handle_task"](agent, "descale?")
        no_device = await NS["handle_task"](agent, "load one")
        cleared = await NS["handle_task"](agent, "forget it")

        assert asked["answer"] == "Fill the tank and run the descale cycle."
        assert "couldn't figure out which device" in no_device["error"]
        assert cleared["status"] == "cleared"

    @pytest.mark.parametrize(
        "reply", [RuntimeError("down"), "I think you want the status.", '{"tool": "dance"}']
    )
    async def test_a_failed_or_strange_answer_falls_back_to_the_keywords(self, reply: Any) -> None:
        agent = await _ready(_Llm(reply))

        result = await NS["handle_task"](agent, "status")

        assert result["status"] == "idle"


class TestTheKeywordRouter:
    """Used when there is no model, or the model's answer cannot be used."""

    @pytest.mark.parametrize("text", ["forget it", "clear", "please reset"])
    async def test_a_short_request_to_clear_clears(self, text: str) -> None:
        agent = await _ready()
        _loaded(agent)

        result = await NS["handle_task"](agent, text)

        assert result["status"] == "cleared"

    @pytest.mark.parametrize(
        "question",
        [
            "how do I reset the filter counter?",
            "how do I drop the drip tray?",
            "how do I get the instructions for descaling?",
            "what does the manual say about cleaning?",
        ],
    )
    async def test_a_question_about_the_loaded_manual_is_answered_not_acted_on(
        self, question: str
    ) -> None:
        agent = await _ready()
        _loaded(agent)

        result = await NS["handle_task"](agent, question)

        assert result == {"error": "No LLM configured on this agent."}
        assert agent.state["manual_device"] == "Philips 2200"
        assert agent.background == []

    async def test_a_request_to_load_names_the_device(self) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, "please load the manual for my HP LaserJet M404")

        assert result["status"] == "loading"
        assert "HP LaserJet M404" in result["result"]
        agent.background.pop().close()

    async def test_with_nothing_loaded_and_nothing_named_it_says_so(self) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, "how does it work?")

        assert "No manual loaded yet" in result["error"]


# ── Loading ────────────────────────────────────────────────────────────────────


def _found(*urls: str) -> None:
    """Search finds `urls`, each a PDF that downloads and reads."""
    NS_search.extend(urls)
    for url in urls:
        _AsyncClient.files[url] = (200, PDF, "application/pdf")


NS_search: list[str] = []


@pytest.fixture(name="search")
def search_fixture(web: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The search itself stubbed, answering with whatever `_found` adds."""
    NS_search.clear()
    monkeypatch.setitem(NS, "_find_manual_candidates", lambda agent, device: list(NS_search))
    monkeypatch.setitem(sys.modules, "fitz", _fitz(["Descaling: fill the tank."]))
    return NS_search


class TestLoading:
    async def test_the_first_candidate_that_reads_is_loaded_and_remembered(
        self, search: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _AsyncClient.files["https://a.example/broken.pdf"] = (500, b"", "")
        _AsyncClient.files["https://a.example/scan.pdf"] = (200, PDF, "application/pdf")
        search.extend(["https://a.example/broken.pdf", "https://a.example/scan.pdf"])
        _found("https://a.example/good.pdf")
        agent = await _ready()
        reads = iter([("", 0), ("Descaling: fill the tank.", 30)])
        monkeypatch.setitem(NS, "_extract_text", lambda _a, _b: next(reads))

        result = await NS["_load_manual"](agent, "Philips EP2200/10")

        assert result["success"] is True
        assert result["url"] == "https://a.example/good.pdf"
        assert result["pages"] == 30
        assert agent.state["url_cache"] == {"philips ep2200": ["https://a.example/good.pdf"]}

    async def test_a_remembered_address_is_tried_first(self, search: list[str]) -> None:
        _found("https://fresh.example/m.pdf")
        _AsyncClient.files["https://cached.example/m.pdf"] = (200, PDF, "application/pdf")
        agent = await _ready()
        agent.state["url_cache"] = {
            "philips 2200": [
                "https://cached.example/m.pdf",
                *(f"https://old{i}.example" for i in range(6)),
            ]
        }

        result = await NS["_load_manual"](agent, "Philips  2200")

        assert result["url"] == "https://cached.example/m.pdf"
        assert len(agent.state["url_cache"]["philips 2200"]) == 5

    async def test_nothing_found_alerts_and_says_how_to_give_an_address(
        self, search: list[str]
    ) -> None:
        agent = await _ready()

        result = await NS["_load_manual"](agent, "Nothing 9000")

        assert result["error"] == "Could not find a PDF manual for: Nothing 9000"
        assert '"url": "https://..."' in result["result"]
        assert agent.alerts[0][0] == "warning"

    async def test_every_candidate_failing_says_what_was_tried(self, search: list[str]) -> None:
        search.append("https://a.example/gone.pdf")
        agent = await _ready()

        result = await NS["_load_manual"](agent, "Philips 2200")

        assert result["candidates_tried"] == ["https://a.example/gone.pdf"]
        assert "couldn't successfully download" in result["result"]

    async def test_an_address_given_is_the_only_one_tried(self, search: list[str]) -> None:
        _found("https://search.example/m.pdf")
        _AsyncClient.files["https://given.example/m.pdf"] = (200, PDF, "application/pdf")
        agent = await _ready(_Llm())

        result = await NS["handle_task"](
            agent,
            {
                "action": "load_manual",
                "device": "Philips 2200",
                "url": "https://given.example/m.pdf",
            },
        )

        assert result["url"] == "https://given.example/m.pdf"


class TestRetryingWithBetterNames:
    async def test_a_corrected_name_is_tried_and_said(
        self, search: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _Llm('["Phillips 2200", "Philips EP2200", "Philips 2200 series"]')
        agent = await _ready(llm)
        names: list[str] = []

        def find(_agent: Any, device: str) -> list[str]:
            names.append(device)
            return ["https://a.example/m.pdf"] if device == "Philips EP2200" else []

        monkeypatch.setitem(NS, "_find_manual_candidates", find)
        _AsyncClient.files["https://a.example/m.pdf"] = (200, PDF, "application/pdf")

        result = await NS["_load_manual"](agent, "Phillips 2200")

        assert names == ["Phillips 2200", "Philips EP2200"]
        assert result["corrected_from"] == "Phillips 2200"
        assert result["result"].startswith("(Resolved 'Phillips 2200' → 'Philips EP2200')")

    async def test_when_no_variant_works_the_last_failure_is_returned(
        self, search: list[str]
    ) -> None:
        agent = await _ready(_Llm('Try ["A 1", "B 2"] maybe'))

        result = await NS["_load_manual"](agent, "Z 9")

        assert result["error"] == "Could not find a PDF manual for: B 2"

    @pytest.mark.parametrize("reply", ["no idea", RuntimeError("down"), 42, "[1, 2"])
    async def test_no_usable_suggestion_keeps_the_first_failure(
        self, reply: Any, search: list[str]
    ) -> None:
        agent = await _ready(_Llm(reply))

        result = await NS["_load_manual"](agent, "Z 9")

        assert result["error"] == "Could not find a PDF manual for: Z 9"

    async def test_without_a_model_or_after_a_download_failure_there_is_no_retry(
        self, search: list[str]
    ) -> None:
        search.append("https://a.example/gone.pdf")
        with_model = await _ready(_Llm())
        without = await _ready()

        failed_download = await NS["_load_manual"](with_model, "Philips 2200")
        search.clear()
        nothing_found = await NS["_load_manual"](without, "Philips 2200")

        assert "candidate manuals" in failed_download["error"]
        assert "Could not find" in nothing_found["error"]


class TestLoadingInTheBackground:
    async def test_the_outcome_is_sent_to_the_chat(self, search: list[str]) -> None:
        _found("https://a.example/m.pdf")
        agent = await _ready()

        await NS["_load_manual_async"](agent, "Philips 2200")
        await agent.finish_background()

        assert agent.notified[0].startswith("Manual loaded: Philips 2200")

    async def test_without_a_chat_the_outcome_is_logged(self, search: list[str]) -> None:
        agent = await _ready(notify=False)
        failing = await _ready()
        failing.fail_notify = True

        for each in (agent, failing):
            await NS["_load_manual_async"](each, "Nothing 1")
            await each.finish_background()

        assert any("couldn't find a PDF manual" in line for line in agent.logs)
        assert any("notify_user failed" in line for line in failing.logs)

    async def test_a_crash_is_still_reported(
        self, search: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def crash(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("out of memory")

        monkeypatch.setitem(NS, "_load_manual", crash)
        agent = await _ready()

        await NS["_load_manual_async"](agent, "Philips 2200")
        await agent.finish_background()

        assert agent.notified == ["out of memory"]

    async def test_without_background_tasks_it_loads_while_asked(self, search: list[str]) -> None:
        _found("https://a.example/m.pdf")
        agent = await _ready(background=False, notify=False)

        result = await NS["_load_manual_async"](agent, "Philips 2200")

        assert result["success"] is True


# ── Searching ──────────────────────────────────────────────────────────────────


class TestSearching:
    def test_a_philips_model_is_tried_at_its_known_addresses_first(
        self, web: dict[str, Any], ddgs: type[_Ddgs], clock: _Clock
    ) -> None:
        web["https://www.download.p4c.philips.com/files/e/ep2200/ep2200_pss_aenghk.pdf"] = (
            _SyncResponse(200, content_type="application/pdf")
        )
        web["https://www.download.p4c.philips.com/files/e/ep2200_31/"] = OSError("reset")
        web["https://html.duckduckgo.com/"] = _page("https://manuals.example/ep2200.pdf")

        found = NS["_find_manual_candidates"](_Agent(), "Philips EP2200")

        assert found[0].endswith("ep2200_pss_aenghk.pdf")
        assert "https://manuals.example/ep2200.pdf" in found

    def test_the_library_is_asked_only_when_few_were_found(
        self, web: dict[str, Any], ddgs: type[_Ddgs], clock: _Clock
    ) -> None:
        web["https://html.duckduckgo.com/"] = _page(
            *(f"https://m{i}.example/a.pdf" for i in range(3))
        )
        ddgs.results["Philips"] = [{"href": "https://library.example/m.pdf"}]

        plenty = NS["_find_manual_candidates"](_Agent(), "Philips 2200")
        web["https://html.duckduckgo.com/"] = _page()
        web["https://www.mojeek.com/"] = _page()
        few = NS["_find_manual_candidates"](_Agent(), "Philips 2200")

        assert "https://library.example/m.pdf" not in plenty
        assert few == ["https://library.example/m.pdf"]

    def test_without_httpx_there_is_no_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "httpx", None)

        assert NS["_find_manual_candidates"](_Agent(), "Philips 2200") == []
        assert NS["_ddg_html_scrape"](_Agent(), "Philips 2200", {}) == []


class TestScrapingSearchPages:
    def test_manual_links_are_kept_and_viewer_pages_made_downloads(
        self, web: dict[str, Any], clock: _Clock
    ) -> None:
        web["https://html.duckduckgo.com/"] = _page(
            "https://a.example/m.pdf",
            "https://www.manualslib.com/manual/123/Philips-2200.html?page=2",
            "https://www.manualslib.com/manual/456/Philips-3200",
            "https://www.bing.com/a.pdf",
            "https://a.example/page.html",
        )
        web["https://www.mojeek.com/"] = _SyncResponse(200, "short")

        found = NS["_ddg_html_scrape"](_Agent(), "Philips 2200", {})

        assert found == [
            "https://a.example/m.pdf",
            "https://www.manualslib.com/manual/123/Philips-2200/download.pdf",
            "https://www.manualslib.com/manual/456/Philips-3200/download.pdf",
        ]
        assert clock.slept and all(1.5 <= s <= 3.0 for s in clock.slept)

    def test_duckduckgo_redirect_links_are_unwrapped(
        self, web: dict[str, Any], clock: _Clock
    ) -> None:
        wrapped = "https%3A%2F%2Fa.example%2Fm.pdf"
        page = f'<a href="/l/?kh=-1&uddg={wrapped}">x</a> <a href="/l/?uddg=https%3A%2F%2Fwww.google.com%2F">y</a>'
        web["https://html.duckduckgo.com/"] = _SyncResponse(200, page + " " * 600)
        web["https://www.mojeek.com/"] = _SyncResponse(503, "")

        assert NS["_ddg_html_scrape"](_Agent(), "Philips 2200", {}) == ["https://a.example/m.pdf"]

    def test_a_rate_limited_duckduckgo_hands_over_to_mojeek(
        self, web: dict[str, Any], clock: _Clock
    ) -> None:
        web["https://html.duckduckgo.com/"] = _SyncResponse(202, "")
        web["https://www.mojeek.com/search?q=Philips+2200+user"] = OSError("timeout")
        web["https://www.mojeek.com/"] = _page("https://mojeek-found.example/m.pdf")

        found = NS["_ddg_html_scrape"](_Agent(), "Philips 2200", {})

        assert found == ["https://mojeek-found.example/m.pdf"]
        duck = [u for u in _SyncClient.requested if "duckduckgo" in u]
        assert len(duck) == 1  # not hammered once it said no

    def test_duckduckgo_failing_or_refusing_each_query(
        self, web: dict[str, Any], clock: _Clock
    ) -> None:
        web["https://html.duckduckgo.com/html/?q=Philips+2200+user"] = OSError("timeout")
        web["https://html.duckduckgo.com/"] = _SyncResponse(403, "x" * 600)
        web["https://www.mojeek.com/"] = _page()

        assert NS["_ddg_html_scrape"](_Agent(), "Philips 2200", {}) == []


class TestTheSearchLibrary:
    def test_results_are_ranked_and_a_failed_query_skipped(self, ddgs: type[_Ddgs]) -> None:
        ddgs.results = {
            "Philips 2200 user": [
                {"url": "https://m.example/page", "title": "Owner manual", "body": ""},
                {"href": "https://m.example/a.pdf"},
                {"link": "https://www.manualslib.com/manual/1/x/"},
            ],
            '"Philips 2200"': RuntimeError("rate limited"),
        }

        found = NS["_ddgs_collect"](_Agent(), "Philips 2200")

        assert found == [
            "https://m.example/a.pdf",
            "https://www.manualslib.com/manual/1/x/download.pdf",
            "https://m.example/page",
        ]

    def test_an_old_library_without_backends_still_answers(self, ddgs: type[_Ddgs]) -> None:
        ddgs.old_api = True
        ddgs.results["Philips"] = [{"href": "https://m.example/a.pdf"}]

        assert "https://m.example/a.pdf" in NS["_ddgs_collect"](_Agent(), "Philips 2200")

    def test_the_older_package_name_is_used_when_the_new_one_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = types.ModuleType("duckduckgo_search")
        legacy.DDGS = _Ddgs  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "ddgs", None)
        monkeypatch.setitem(sys.modules, "duckduckgo_search", legacy)
        _Ddgs.results = {"Philips": [{"href": "https://m.example/a.pdf"}]}
        _Ddgs.old_api = False

        assert "https://m.example/a.pdf" in NS["_ddgs_collect"](_Agent(), "Philips 2200")

    def test_with_neither_package_there_is_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "ddgs", None)
        monkeypatch.setitem(sys.modules, "duckduckgo_search", None)

        assert NS["_ddgs_collect"](_Agent(), "Philips 2200") == []

    def test_ranking_puts_files_then_trusted_hosts_then_the_rest(self) -> None:
        results = [
            {"href": "ftp://a.example/m.pdf"},
            {"href": "https://www.google.com/x.pdf"},
            {"href": "https://blog.example/post", "title": "", "body": "nothing"},
            {"href": "https://shop.example/x", "title": "PDF download", "body": ""},
            {"href": "https://support.hp.com/m404"},
            {"href": "https://a.example/m.PDF"},
        ]

        ranked = NS["_rank_manual_urls"](results, lambda r: r["href"])

        assert ranked == [
            "https://a.example/m.PDF",
            "https://support.hp.com/m404",
            "https://shop.example/x",
        ]


# ── Downloading and reading ────────────────────────────────────────────────────


class TestDownloading:
    async def test_a_page_that_links_to_the_pdf_is_followed_to_it(
        self, web: dict[str, Any]
    ) -> None:
        _AsyncClient.files["https://a.example/viewer"] = (
            200,
            b'<a href="https://a.example/files/m.pdf">get</a>',
            "text/html",
        )
        _AsyncClient.files["https://a.example/files/m.pdf"] = (200, PDF, "application/octet-stream")

        assert await NS["_download_pdf"](_Agent(), "https://a.example/viewer") == PDF

    @pytest.mark.parametrize(
        ("url", "answer"),
        [
            ("https://a.example/missing", (404, b"", "text/html")),
            ("https://a.example/page", (200, b"<p>no links</p>", "text/html")),
            ("https://private.example/m.pdf", (200, PDF, "application/pdf")),
        ],
    )
    async def test_nothing_that_is_not_a_reachable_pdf_comes_back(
        self, url: str, answer: Any, web: dict[str, Any]
    ) -> None:
        _AsyncClient.files[url] = answer

        assert await NS["_download_pdf"](_Agent(), url) is None

    async def test_a_failed_download_is_logged(self, web: dict[str, Any]) -> None:
        _AsyncClient.files["https://a.example/m.pdf"] = OSError("reset by peer")
        agent = _Agent()

        assert await NS["_download_pdf"](agent, "https://a.example/m.pdf") is None
        assert any("reset by peer" in line for line in agent.logs)

    async def test_without_httpx_nothing_is_downloaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "httpx", None)
        agent = _Agent()

        assert await NS["_download_pdf"](agent, "https://a.example/m.pdf") is None
        assert "httpx is not installed" in agent.logs[0]


class TestReadingThePdf:
    def test_pymupdf_reads_the_pages_it_can(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules, "fitz", _fitz(["Page one", ValueError("bad"), "", "Page four"])
        )

        assert NS["_extract_text"](_Agent(), PDF) == ("Page one\nPage four", 4)

    def test_only_so_many_pages_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(NS, "_MAX_PAGES_EXTRACTED", 2)
        monkeypatch.setitem(sys.modules, "fitz", _fitz(["one", "two", "three"]))

        assert NS["_extract_text"](_Agent(), PDF) == ("one\ntwo", 3)

    @pytest.mark.parametrize("fitz", [None, _fitz(RuntimeError("encrypted")), _fitz([""])])
    def test_pdfplumber_reads_what_pymupdf_cannot(
        self, fitz: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "fitz", fitz)
        monkeypatch.setitem(
            sys.modules, "pdfplumber", _pdfplumber(["one", ValueError("x"), "three"])
        )

        assert NS["_extract_text"](_Agent(), PDF) == ("one\nthree", 3)

    def test_a_slow_pdfplumber_stops_at_its_budget(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        monkeypatch.setitem(sys.modules, "fitz", None)
        monkeypatch.setitem(sys.modules, "pdfplumber", _pdfplumber(["one", "two", "three"]))
        clock.step = 20.0  # each look at the clock is twenty seconds on

        # Read at 20s and 40s; the third look, at 60s, is past the 45s budget.
        assert NS["_extract_text"](_Agent(), PDF) == ("one\ntwo", 3)

    @pytest.mark.parametrize(
        "pdfplumber", [None, _pdfplumber(RuntimeError("broken")), _pdfplumber([""])]
    )
    def test_when_neither_reads_it_there_is_no_text(
        self, pdfplumber: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "fitz", None)
        monkeypatch.setitem(sys.modules, "pdfplumber", pdfplumber)

        assert NS["_extract_text"](_Agent(), PDF) == ("", 0)


# ── Answering ──────────────────────────────────────────────────────────────────


class TestAnswering:
    async def test_the_answer_comes_from_the_parts_that_match_the_question(self) -> None:
        llm = _Llm("Fill the tank, then run the descale cycle.")
        agent = await _ready(llm)
        filler = "Welcome to your new machine. " * 200
        _loaded(agent, filler + "To descale the machine, fill the tank with descaler. " + filler)

        result = await NS["_ask"](agent, "How do I descale it?")

        assert result["answer"] == "Fill the tank, then run the descale cycle."
        assert result["device"] == "Philips 2200"
        excerpt = llm.prompts[0].split("Manual excerpt:\n", 1)[1]
        assert excerpt.lstrip().startswith("Welcome") is False or "descale" in excerpt[:3000]

    async def test_a_model_with_chat_only(self) -> None:
        agent = await _ready(_ChatLlm("Use the descale button."))
        _loaded(agent)

        assert (await NS["_ask"](agent, "descale?"))["answer"] == "Use the descale button."

    async def test_what_stops_an_answer(self) -> None:
        nothing_loaded = await _ready(_Llm("x"))
        no_model = await _ready()
        _loaded(no_model)
        odd_model = await _ready(object())
        _loaded(odd_model)
        failing = await _ready(_Llm(RuntimeError("quota")))
        _loaded(failing)
        sentinel = await _ready(_Llm("[LLM error: quota exceeded]"))
        _loaded(sentinel)

        assert (await NS["_ask"](nothing_loaded, "q"))["error"] == "No manual loaded yet."
        assert (await NS["_ask"](no_model, "q"))["error"] == "No LLM configured on this agent."
        assert "no recognised interface" in (await NS["_ask"](odd_model, "q"))["error"]
        assert (await NS["_ask"](failing, "q"))["error"] == "LLM call failed: quota"
        assert (await NS["_ask"](sentinel, "q"))["error"] == "LLM error: quota exceeded"

    async def test_status_once_loaded(self) -> None:
        agent = await _ready()
        _loaded(agent, "x" * 1234)

        status = NS["_status"](agent)

        assert status["result"] == "Loaded: Philips 2200 (12 pages, 1,234 chars)"


class TestHelpers:
    @pytest.mark.parametrize(
        ("device", "key"),
        [
            ("Philips 2200", "philips 2200"),
            ("Philips EP2200/10", "philips ep2200"),
            ("  A   B ", "a b"),
            ("", ""),
        ],
    )
    def test_cache_keys(self, device: str, key: str) -> None:
        assert NS["_cache_key"](device) == key

    def test_chunks_overlap_and_keywords_skip_filler(self) -> None:
        chunks = NS["_chunk_text"](" ".join(str(i) for i in range(10)), 4, 1)

        assert chunks == ["0 1 2 3", "3 4 5 6", "6 7 8 9", "9"]
        assert NS["_keywords"]("How do I descale my Philips machine?") == [
            "descale",
            "philips",
            "machine",
        ]

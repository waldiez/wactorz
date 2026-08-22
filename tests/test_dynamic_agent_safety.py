"""The pre-exec safety scan over generated agent code, and the body extractor.

DynamicAgent runs code an LLM wrote, so this scan is a mistake-catcher in front
of an `exec` that happens by design -- not a sandbox. The tests below pin what
it stops and, just as deliberately, what it does not, so nobody later reads a
passing scan as containment.
"""

from pathlib import Path

import pytest

from wactorz.agents.dynamic_agent import DynamicAgent

BLOCKED = [
    ("os.system('ls')", "os.system"),
    ("os.popen('ls')", "os.popen"),
    ("os.execv('/bin/sh', [])", "os.exec*"),
    ("os.remove('/etc/passwd')", "os.remove"),
    ("os.rmdir('/tmp/x')", "os.rmdir"),
    ("shutil.rmtree('/')", "shutil.rmtree"),
    ("socket.socket()", "raw socket"),
    ("eval('1 + 1')", "eval"),
    ("__import__('os')", "__import__"),
    ("open('/tmp/x', 'w')", "open in write mode"),
]

ALLOWED_WITH_A_WARNING = [
    ("subprocess.run(['ls'])", "subprocess"),
    ("import ctypes", "ctypes"),
    ("pickle.loads(data)", "pickle"),
]

# Recorded, not endorsed: each of these reaches exec untouched. The scan is
# pattern-based over source text, so anything that spells the call differently
# passes. Listed so the gap is visible rather than assumed closed.
NOT_CAUGHT = [
    ("os . system('ls')", "whitespace around the dot defeats the literal pattern"),
    ("getattr(os, 'system')('ls')", "attribute lookup by name is not a pattern match"),
    ("exec('import os')", "exec() is absent from the list while eval() is on it"),
]


@pytest.fixture(name="agent")
def agent_fixture(tmp_path: Path) -> DynamicAgent:
    return DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path))


class TestBlockedPatterns:
    @pytest.mark.parametrize(("code", "label"), BLOCKED)
    def test_dangerous_code_is_refused(self, agent: DynamicAgent, code: str, label: str) -> None:
        verdict = agent._validate_code_safety(code)

        assert verdict is not None, label
        assert verdict.startswith("Code blocked for safety:")

    def test_the_refusal_names_the_reason(self, agent: DynamicAgent) -> None:
        """The message goes back to whoever asked for the agent, so it must say why."""
        verdict = agent._validate_code_safety("os.system('ls')")

        assert verdict is not None
        assert "os.system()" in verdict

    def test_ordinary_agent_code_passes(self, agent: DynamicAgent) -> None:
        code = "async def setup(agent):\n    agent.subscribe('t', cb)\n"

        assert agent._validate_code_safety(code) is None


class TestWarningsDoNotBlock:
    @pytest.mark.parametrize(("code", "label"), ALLOWED_WITH_A_WARNING)
    def test_suspicious_but_permitted_code_runs(
        self, agent: DynamicAgent, code: str, label: str
    ) -> None:
        assert agent._validate_code_safety(code) is None, label


class TestKnownGaps:
    @pytest.mark.parametrize(("code", "why"), NOT_CAUGHT)
    def test_these_reach_exec_untouched(self, agent: DynamicAgent, code: str, why: str) -> None:
        """Recorded so the scan is not mistaken for containment.

        Closing any of these means changing this assertion on purpose, which is
        the moment to decide whether a pattern list is the right mechanism at
        all -- a source-text filter cannot see through indirection.
        """
        assert agent._validate_code_safety(code) is None, why


class TestProcessAntiPatterns:
    """Flagged inside process() only, and advisory -- they cause hangs, not harm."""

    def test_sleeping_inside_process_is_warned_not_blocked(self, agent: DynamicAgent) -> None:
        code = "async def process(agent):\n    await asyncio.sleep(5)\n"

        assert agent._validate_code_safety(code) is None

    def test_the_same_call_in_setup_is_not_a_process_antipattern(self, agent: DynamicAgent) -> None:
        """setup() runs once, so sleeping there is legitimate."""
        code = "async def setup(agent):\n    await asyncio.sleep(5)\n"

        assert agent._validate_code_safety(code) is None


class TestExtractFunctionBody:
    def test_it_returns_only_the_named_function(self) -> None:
        code = "async def setup(agent):\n    first = 1\nasync def process(agent):\n    second = 2\n"

        body = DynamicAgent._extract_function_body(code, "process")

        assert body is not None
        assert "second" in body
        assert "first" not in body

    def test_a_missing_function_yields_nothing(self) -> None:
        assert DynamicAgent._extract_function_body("x = 1\n", "process") is None

    def test_a_sync_def_is_found_too(self) -> None:
        body = DynamicAgent._extract_function_body("def process(agent):\n    y = 2\n", "process")

        assert body is not None
        assert "y = 2" in body

    def test_blank_lines_inside_the_body_are_kept(self) -> None:
        """Dedent-based scanning must not treat a blank line as the end."""
        code = "def process(agent):\n    a = 1\n\n    b = 2\n"

        body = DynamicAgent._extract_function_body(code, "process")

        assert body is not None
        assert "a = 1" in body
        assert "b = 2" in body

    def test_the_body_ends_where_the_indentation_does(self) -> None:
        code = "def process(agent):\n    inside = 1\nafter = 2\n"

        body = DynamicAgent._extract_function_body(code, "process")

        assert body is not None
        assert "inside" in body
        assert "after" not in body

    def test_an_empty_body_reads_as_nothing_found(self) -> None:
        """A def with nothing under it is indistinguishable from absent here."""
        assert DynamicAgent._extract_function_body("def process(agent):\n", "process") is None


class TestSanitizeCode:
    """Stripping the LLM self-setup a model writes out of habit.

    Generated code often reaches for `openai` and an API key of its own. The
    agent already has a provider, so that setup is removed rather than allowed
    to fail at import time or, worse, succeed against someone else's key.
    """

    def test_an_llm_import_becomes_an_inert_line(self) -> None:
        """Replaced rather than deleted, so line numbers in tracebacks still line up."""
        out = DynamicAgent._sanitize_code("import openai\nx = 1\n")

        assert "import openai" not in out.replace("# sanitized: import openai", "")
        assert out.startswith("pass  # sanitized:")
        assert "x = 1" in out

    @pytest.mark.parametrize(
        "line",
        [
            "import openai",
            "from anthropic import Anthropic",
            "api_key = 'sk-123'",
            "key = os.environ['OPENAI_API_KEY']",
            "c = openai.OpenAI()",
        ],
    )
    def test_each_self_setup_shape_is_removed(self, line: str) -> None:
        out = DynamicAgent._sanitize_code(f"{line}\nkeep = 1\n")

        assert out.splitlines()[0].startswith("pass  # sanitized:")
        assert "keep = 1" in out

    def test_a_whole_try_block_of_llm_setup_collapses_to_one_line(self) -> None:
        """Removing only the import would leave an except clause with no try."""
        code = (
            "try:\n"
            "    import openai\n"
            "    c = openai.OpenAI()\n"
            "except ImportError:\n"
            "    c = None\n"
            "keep = 1\n"
        )

        out = DynamicAgent._sanitize_code(code)

        assert "pass  # sanitized: LLM setup block" in out
        assert "except" not in out
        assert "keep = 1" in out

    def test_a_call_llm_helper_is_replaced_by_the_agent_shim(self) -> None:
        code = "def call_llm(p):\n    return openai.chat(p)\nx = 1\n"

        out = DynamicAgent._sanitize_code(code)

        assert "async def call_llm(agent" in out
        assert "openai.chat" not in out

    def test_api_key_as_a_dict_key_is_left_alone(self) -> None:
        """Only an assignment is self-setup; a dict entry is ordinary config."""
        code = "cfg = {'api_key': 'x'}\ny = 2\n"

        assert DynamicAgent._sanitize_code(code) == code

    def test_ordinary_agent_code_is_returned_unchanged(self) -> None:
        code = "async def setup(agent):\n    agent.subscribe('t', cb)\n"

        assert DynamicAgent._sanitize_code(code) == code

    def test_blank_lines_and_layout_survive(self) -> None:
        code = "a = 1\n\n\nb = 2\n"

        assert DynamicAgent._sanitize_code(code) == code


class TestCompileCode:
    """Sanitize, check, then exec into the agent's namespace."""

    @staticmethod
    def _agent(tmp_path: Path, code: str, *, trusted: bool = False) -> DynamicAgent:
        return DynamicAgent(name="p", code=code, persistence_dir=str(tmp_path), trusted=trusted)

    def test_good_code_compiles_and_reports_no_error(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path, "async def setup(agent):\n    pass\n")

        assert agent._compile_code() is None
        assert "setup" in agent._ns

    def test_a_syntax_error_is_returned_rather_than_raised(self, tmp_path: Path) -> None:
        """The caller feeds this string back to the model to ask for a fix."""
        agent = self._agent(tmp_path, "def broken(:\n")

        error = agent._compile_code()

        assert error is not None
        assert error.startswith("SyntaxError")

    def test_unsafe_code_is_refused_before_it_runs(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path, "os.system('ls')")

        error = agent._compile_code()

        assert error is not None
        assert error.startswith("Code blocked for safety:")

    def test_a_trusted_agent_skips_the_safety_scan_entirely(self, tmp_path: Path) -> None:
        """Catalogue code is pre-built and may legitimately use blocked calls.

        Recorded because it is the one path where the scan does not run at all:
        marking an agent trusted is the whole decision, and nothing downstream
        re-checks it.
        """
        agent = self._agent(tmp_path, "os.system('ls')", trusted=True)

        error = agent._compile_code()

        assert error is None or not error.startswith("Code blocked for safety:")

    def test_the_llm_shims_are_available_to_generated_code(self, tmp_path: Path) -> None:
        """Models write `get_llm()` out of habit; the shim points it at the agent."""
        agent = self._agent(tmp_path, "async def setup(agent):\n    pass\n")
        agent._compile_code()

        for name in ("get_llm", "setup_llm", "create_llm"):
            assert name in agent._ns

    def test_the_cv2_shim_is_injected_only_when_the_code_uses_cv2(self, tmp_path: Path) -> None:
        """A chat agent should not pay for a camera shim it never touches."""
        with_cv2 = self._agent(tmp_path, "import cv2\nasync def setup(agent):\n    pass\n")
        with_cv2._compile_code()
        without = self._agent(tmp_path, "async def setup(agent):\n    pass\n")
        without._compile_code()

        assert "cv2" in with_cv2._ns
        assert "cv2" not in without._ns

    def test_explicit_code_overrides_the_constructor_code(self, tmp_path: Path) -> None:
        """The repair path recompiles a fixed version without rebuilding the agent."""
        agent = self._agent(tmp_path, "def broken(:\n")

        assert agent._compile_code("async def setup(agent):\n    pass\n") is None

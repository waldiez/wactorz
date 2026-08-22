"""The pattern check applied to model-written code before it is executed.

⚠ This is a regex scan over source text, not a sandbox. It catches the shapes it
lists and nothing else, and the tests below pin both halves of that: what is
refused, and what walks straight past. The second half is the more important
one — a check that reads as a security boundary and is not one is worse than an
absent check, because it invites trust it cannot carry.

The validator is exercised through the real class so the pattern lists under
test are the ones production uses.
"""

from __future__ import annotations

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent


@pytest.fixture(name="validate")
def validate_fixture():
    """The real method, bound to an instance built without running __init__.

    Construction needs an actor system; the check needs only the class-level
    pattern lists and a name for its log lines.
    """
    agent = DynamicAgent.__new__(DynamicAgent)
    agent.name = "test-agent"

    def _validate(code: str) -> str | None:
        return agent._validate_code_safety(code)

    return _validate


class TestRefused:
    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            ("import os\nos.system('ls')", "os.system"),
            ("os.popen('ls')", "os.popen"),
            ("os.execv('/bin/sh', [])", "os.exec"),
            ("os.remove('/tmp/x')", "os.remove"),
            ("os.rmdir('/tmp/x')", "os.rmdir"),
            ("shutil.rmtree('/tmp')", "shutil.rmtree"),
            ("socket.socket()", "raw socket"),
            ("eval('1+1')", "eval()"),
            ("__import__('os')", "__import__"),
            ("open('/tmp/x', 'w')", "open() in write mode"),
        ],
    )
    def test_the_listed_shapes_are_blocked(self, validate, code: str, fragment: str) -> None:
        result = validate(code)
        assert result is not None
        assert fragment in result

    def test_the_message_says_it_was_blocked(self, validate) -> None:
        assert validate("eval('x')").startswith("Code blocked for safety:")

    def test_whitespace_before_the_call_does_not_help(self, validate) -> None:
        assert validate("eval ('x')") is not None

    @pytest.mark.parametrize("mode", ["w", "a", "b"])
    def test_each_write_mode_is_caught(self, validate, mode: str) -> None:
        assert validate(f"open('/tmp/x', '{mode}')") is not None


class TestAllowed:
    def test_ordinary_agent_code_passes(self, validate) -> None:
        code = (
            "async def setup(agent):\n"
            "    agent.subscribe('sensors/temp', on_msg)\n"
            "async def process(agent):\n"
            "    await agent.publish('out', {'v': 1})\n"
        )
        assert validate(code) is None

    def test_reading_a_file_is_allowed(self, validate) -> None:
        assert validate("open('/etc/hostname')") is None

    def test_a_warned_pattern_does_not_block(self, validate) -> None:
        """Warnings are advisory; only the blocked list stops execution."""
        assert validate("import subprocess\nsubprocess.run(['ls'])") is None

    def test_a_process_antipattern_does_not_block(self, validate) -> None:
        """It predicts a timeout crash, not a safety problem, so it only logs."""
        code = "async def process(agent):\n    await asyncio.sleep(5)\n"
        assert validate(code) is None


class TestKnownEvasions:
    """Shapes that reach the same capabilities and are not matched.

    Each of these is a documented limit of a text scan, not a defect to fix by
    adding another pattern — the list can always be walked around. They are
    pinned so the check's actual reach is visible rather than assumed.
    """

    def test_a_write_mode_held_in_a_variable_is_not_matched(self, validate) -> None:
        """The pattern requires a quoted mode inside the call."""
        assert validate("mode = 'w'\nopen('/tmp/x', mode)") is None

    def test_pathlib_writes_are_not_matched(self, validate) -> None:
        """The list names `open`, so another path to the same capability passes."""
        assert validate("from pathlib import Path\nPath('/tmp/x').write_text('data')") is None

    def test_deletion_through_pathlib_is_not_matched(self, validate) -> None:
        assert validate("from pathlib import Path\nPath('/tmp/x').unlink()") is None

    def test_an_aliased_import_is_not_matched(self, validate) -> None:
        """Patterns match the attribute spelling, not the resolved object."""
        assert validate("import os as o\no.system('ls')") is None

    def test_indirection_through_getattr_is_not_matched(self, validate) -> None:
        assert validate("getattr(__builtins__, 'eval')('1+1')") is None


class TestExtractFunctionBody:
    def test_it_returns_the_indented_body(self) -> None:
        code = "async def process(agent):\n    x = 1\n    return x\n"
        body = DynamicAgent._extract_function_body(code, "process")
        assert body is not None
        assert "x = 1" in body
        assert "return x" in body

    def test_it_stops_at_the_next_top_level_definition(self) -> None:
        code = (
            "async def process(agent):\n    inside = 1\nasync def setup(agent):\n    outside = 2\n"
        )
        body = DynamicAgent._extract_function_body(code, "process")
        assert body is not None
        assert "inside" in body
        assert "outside" not in body

    def test_an_absent_function_gives_nothing(self) -> None:
        assert DynamicAgent._extract_function_body("x = 1\n", "process") is None

    def test_a_plain_def_is_found_as_well_as_async(self) -> None:
        body = DynamicAgent._extract_function_body("def process(agent):\n    y = 2\n", "process")
        assert body is not None
        assert "y = 2" in body

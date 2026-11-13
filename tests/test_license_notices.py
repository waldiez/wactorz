# SPDX-License-Identifier: Apache-2.0
"""The license-line check: what it accepts, what it writes, what it never touches."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "license_notices", ROOT / "scripts" / "license_notices.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ln = _load()


MISSING = "does not open with the license line"


class TestProblem:
    def test_a_file_with_the_line_passes(self) -> None:
        assert ln.problem('# SPDX-License-Identifier: Apache-2.0\n"""Doc."""\n', ".py") is None

    def test_a_file_without_it_fails(self) -> None:
        assert ln.problem('"""Doc."""\n', ".py") == MISSING

    def test_an_empty_file_fails(self) -> None:
        assert ln.problem("", ".py") == MISSING

    def test_another_license_is_a_decision_not_a_miss(self) -> None:
        assert (
            ln.problem("# SPDX-License-Identifier: AGPL-3.0-only\n# uses Ultralytics\n", ".py")
            is None
        )

    def test_after_a_shebang_or_a_doctype_passes(self) -> None:
        assert ln.problem("#!/bin/sh\n# SPDX-License-Identifier: Apache-2.0\n", ".sh") is None
        assert (
            ln.problem("<!doctype html>\n<!-- SPDX-License-Identifier: Apache-2.0 -->\n", ".html")
            is None
        )
        assert ln.problem("/**\n * SPDX-License-Identifier: Apache-2.0\n */\n", ".ts") is None
        assert ln.problem("/*\n * SPDX-License-Identifier: Apache-2.0\n */\n", ".css") is None

    @pytest.mark.parametrize(
        ("suffix", "text"),
        [
            # Below code, however near the top.
            (".py", 'print("already running")\n# SPDX-License-Identifier: Apache-2.0\n'),
            # Inside a string, not a comment.
            (".py", '"""\nSPDX-License-Identifier: Apache-2.0\n"""\n'),
            # Under another comment.
            (".py", "# A script.\n# SPDX-License-Identifier: Apache-2.0\n"),
            # Not in the file's own comment form.
            (".py", "// SPDX-License-Identifier: Apache-2.0\n"),
            (".ts", "// SPDX-License-Identifier: Apache-2.0\n"),
            # In a block that does not start the file.
            (".ts", "export {};\n/**\n * SPDX-License-Identifier: Apache-2.0\n */\n"),
            # Not the first line of its block.
            (".css", "/**\n * Styles.\n * SPDX-License-Identifier: Apache-2.0\n */\n"),
            (".html", "<html>\n<!-- SPDX-License-Identifier: Apache-2.0 -->\n"),
        ],
    )
    def test_anywhere_but_the_opening_does_not_count(self, suffix: str, text: str) -> None:
        assert ln.problem(text, suffix) == MISSING

    def test_a_copyright_line_under_it_is_reported(self) -> None:
        text = "/**\n * SPDX-License-Identifier: Apache-2.0\n * Copyright 2025 - 2026 Waldiez & contributors\n */\n"
        assert ln.problem(text, ".ts") is not None


class TestWithNotice:
    @pytest.mark.parametrize(
        ("suffix", "text", "expected"),
        [
            (".py", '"""Doc."""\n', '# SPDX-License-Identifier: Apache-2.0\n"""Doc."""\n'),
            (".py", "", "# SPDX-License-Identifier: Apache-2.0\n"),
            (
                ".sh",
                "#!/bin/sh\necho\n",
                "#!/bin/sh\n# SPDX-License-Identifier: Apache-2.0\necho\n",
            ),
            (
                ".py",
                "#!/usr/bin/env python3",
                "#!/usr/bin/env python3\n# SPDX-License-Identifier: Apache-2.0\n",
            ),
            (".css", "a {}\n", "/**\n * SPDX-License-Identifier: Apache-2.0\n */\na {}\n"),
            (
                ".js",
                "export {};\n",
                "/**\n * SPDX-License-Identifier: Apache-2.0\n */\nexport {};\n",
            ),
            (
                ".html",
                "<!doctype html>\n<html></html>\n",
                "<!doctype html>\n<!-- SPDX-License-Identifier: Apache-2.0 -->\n<html></html>\n",
            ),
            (".html", "<p>x</p>\n", "<!-- SPDX-License-Identifier: Apache-2.0 -->\n<p>x</p>\n"),
        ],
    )
    def test_writes_the_line_in_the_files_own_syntax(
        self, suffix: str, text: str, expected: str
    ) -> None:
        assert ln.with_notice(text, suffix) == expected
        assert ln.problem(expected, suffix) is None

    def test_drops_the_copyright_line_and_keeps_the_rest(self) -> None:
        text = "/**\n * SPDX-License-Identifier: Apache-2.0\n * Copyright 2025 - 2026 Waldiez & contributors\n */\nx;\n"

        assert (
            ln.with_notice(text, ".ts") == "/**\n * SPDX-License-Identifier: Apache-2.0\n */\nx;\n"
        )

    def test_drops_a_python_copyright_line(self) -> None:
        text = '# SPDX-License-Identifier: Apache-2.0\n# Copyright 2025 Waldiez & contributors\n"""Doc."""\n'

        assert ln.with_notice(text, ".py") == '# SPDX-License-Identifier: Apache-2.0\n"""Doc."""\n'

    def test_leaves_a_file_of_another_license_as_it_is(self) -> None:
        text = "# SPDX-License-Identifier: AGPL-3.0-only\n# Copyright 2025 Waldiez & contributors\n"

        assert ln.with_notice(text, ".py") == text

    def test_puts_the_line_at_the_top_when_it_is_only_further_down(self) -> None:
        text = 'print("x")\n# SPDX-License-Identifier: Apache-2.0\n'

        fixed = ln.with_notice(text, ".py")

        assert fixed == "# SPDX-License-Identifier: Apache-2.0\n" + text
        assert ln.problem(fixed, ".py") is None

    def test_keeps_windows_line_endings(self) -> None:
        assert (
            ln.with_notice("#!/bin/sh\r\necho\r\n", ".sh")
            == "#!/bin/sh\r\n# SPDX-License-Identifier: Apache-2.0\r\necho\r\n"
        )
        assert (
            ln.with_notice("a {}\r\n", ".css")
            == "/**\r\n * SPDX-License-Identifier: Apache-2.0\r\n */\r\na {}\r\n"
        )

    def test_a_windows_file_with_the_line_passes(self) -> None:
        assert (
            ln.problem("/**\r\n * SPDX-License-Identifier: Apache-2.0\r\n */\r\nx;\r\n", ".ts")
            is None
        )
        assert ln.problem("# SPDX-License-Identifier: Apache-2.0\r\n", ".py") is None


class TestRun:
    @pytest.fixture(name="tree")
    def tree_fixture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setattr(ln, "ROOT", tmp_path)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "static" / "app" / "assets").mkdir(parents=True)
        (tmp_path / "static" / "docs").mkdir(parents=True)
        (tmp_path / "pkg" / "good.py").write_text(
            "# SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8"
        )
        (tmp_path / "pkg" / "bad.py").write_text('"""Doc."""\n', encoding="utf-8")
        (tmp_path / "pkg" / "data.json").write_text("{}\n", encoding="utf-8")
        (tmp_path / "static" / "app" / "assets" / "index.js").write_text("x;\n", encoding="utf-8")
        (tmp_path / "static" / "docs" / "page.html").write_text("<p></p>\n", encoding="utf-8")
        monkeypatch.setattr(
            ln,
            "source_files",
            lambda: [
                Path("pkg/good.py"),
                Path("pkg/bad.py"),
                Path("pkg/data.json"),
                Path("static/docs/page.html"),
            ],
        )
        return tmp_path

    def test_check_names_each_file_missing_it_and_fails(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert ln.run([], fix=False) == 1

        out = capsys.readouterr().out
        assert f"pkg/bad.py: {MISSING}" in out
        assert f"static/app/assets/index.js: {MISSING} (built: run `make build-frontend`)" in out
        assert "good.py" not in out
        assert "data.json" not in out
        assert "static/docs" not in out

    def test_fix_writes_sources_and_never_a_built_file(self, tree: Path) -> None:
        assert ln.run([], fix=True) == 1

        assert (
            (tree / "pkg" / "bad.py")
            .read_text(encoding="utf-8")
            .startswith("# SPDX-License-Identifier: Apache-2.0\n")
        )
        assert (tree / "static" / "app" / "assets" / "index.js").read_text(
            encoding="utf-8"
        ) == "x;\n"

    def test_fix_keeps_a_files_windows_line_endings(self, tree: Path) -> None:
        (tree / "pkg" / "bad.py").write_bytes(b'"""Doc."""\r\nx = 1\r\n')

        ln.run(["pkg/bad.py"], fix=True)

        assert (tree / "pkg" / "bad.py").read_bytes() == (
            b'# SPDX-License-Identifier: Apache-2.0\r\n"""Doc."""\r\nx = 1\r\n'
        )

    def test_named_paths_are_the_only_ones_read(self, tree: Path) -> None:
        assert ln.run(["pkg/good.py", "pkg/data.json"], fix=False) == 0
        assert ln.run(["pkg/bad.py"], fix=False) == 1

    def test_a_named_path_that_is_gone_is_skipped(self, tree: Path) -> None:
        assert ln.run(["pkg/deleted.py"], fix=False) == 0


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


def test_source_files_are_the_tracked_and_the_new_but_never_the_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    for name in ("tracked.py", "new.py", "ignored.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.py", ".gitignore")
    monkeypatch.setattr(ln, "ROOT", tmp_path)

    assert set(ln.source_files()) == {Path(".gitignore"), Path("tracked.py"), Path("new.py")}
    assert set(ln.source_files(include_new=False)) == {Path(".gitignore"), Path("tracked.py")}


def test_every_tracked_source_file_has_it() -> None:
    """Tracked sources only, as a commit sees them.

    The commit hook runs this with unstaged changes set aside, so a file not yet
    added, or a `static/app` rebuilt and committed separately, is not what the
    commit holds. `make lint-licenses` reads those too, over the tree as it is.
    """
    sources = [
        p
        for p in ln.source_files(include_new=False)
        if ln.covered(p) and not ln.is_built(p) and (ROOT / p).is_file()
    ]
    missing = [
        str(p) for p in sources if ln.problem((ROOT / p).read_text(encoding="utf-8"), p.suffix)
    ]

    assert missing == []

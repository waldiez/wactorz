# SPDX-License-Identifier: Apache-2.0
"""The committed docs are compared with, and rebuilt from, a clean build.

`static/docs` ships in the wheel. A build that only writes into it never removes
anything, so a page the sources stopped building stays there and ships, and a
check that rebuilds in place and asks git what changed cannot see it. Both the
build and the check therefore start from an empty directory, and treat what is
missing, what is extra and what differs alike.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    """Import `scripts/build_docs.py`, which is not an installed module."""
    spec = importlib.util.spec_from_file_location("build_docs", ROOT / "scripts" / "build_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_docs = _load_script()


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestACleanBuild:
    def test_two_builds_write_the_same_files(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path / "one")
        build_docs.render(tmp_path / "two")

        assert _files(tmp_path / "one") == _files(tmp_path / "two")

    def test_every_page_has_unix_line_endings(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)

        written = _files(tmp_path).items()
        crlf = [
            name for name, data in written if name.endswith((".html", ".json")) and b"\r\n" in data
        ]

        assert crlf == []

    def test_every_page_in_the_nav_is_written(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)

        for subdir, md_name, _path in build_docs.collect_pages():
            assert (tmp_path / subdir / md_name.replace(".md", ".html")).is_file()


class TestTheCheck:
    def test_a_matching_site_passes(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)

        assert build_docs.check(tmp_path) == []

    def test_a_page_left_behind_is_extra(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)
        (tmp_path / "guide" / "quickstart.html").write_text("old", encoding="utf-8")

        assert build_docs.check(tmp_path) == ["extra    guide/quickstart.html"]

    def test_a_page_not_rebuilt_is_missing(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)
        (tmp_path / "guide" / "windows.html").unlink()

        assert build_docs.check(tmp_path) == ["missing  guide/windows.html"]

    def test_a_page_built_from_older_sources_is_stale(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)
        (tmp_path / "guide" / "agents.html").write_text("older", encoding="utf-8")

        assert build_docs.check(tmp_path) == ["stale    guide/agents.html"]

    def test_the_api_reference_is_not_compared(self, tmp_path: Path) -> None:
        build_docs.render(tmp_path)
        (tmp_path / "api" / "python").mkdir(parents=True)
        (tmp_path / "api" / "python" / "index.html").write_text("pdoc", encoding="utf-8")

        assert build_docs.check(tmp_path) == []


class TestTheBuild:
    def test_it_removes_what_a_clean_build_does_not_write(self, tmp_path: Path) -> None:
        site = tmp_path / "site"
        build_docs.render(site)
        (site / "guide" / "quickstart.html").write_text("old", encoding="utf-8")
        (site / "retired").mkdir()
        (site / "retired" / "index.html").write_text("old", encoding="utf-8")

        build_docs.build(site)

        assert not (site / "guide" / "quickstart.html").exists()
        assert not (site / "retired").exists()
        assert build_docs.check(site) == []

    def test_it_leaves_the_api_reference_alone(self, tmp_path: Path) -> None:
        site = tmp_path / "site"
        (site / "api" / "js").mkdir(parents=True)
        (site / "api" / "js" / "index.html").write_text("typedoc", encoding="utf-8")

        build_docs.build(site)

        assert (site / "api" / "js" / "index.html").read_text(encoding="utf-8") == "typedoc"

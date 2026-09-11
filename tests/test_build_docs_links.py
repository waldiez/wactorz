"""Links between docs pages point at what the site serves.

The sources link to each other as `name.md`, which is right on GitHub, where
they render, and wrong on the site, which serves only `.html`. The build left
those links as written, so each one was a 404 on the published site. A page the
site does not build is linked to its source on GitHub instead, which is where
the README sends readers for those pages too.

The README pins the other half: it links to the site's guide pages, and a page
missing from the build's NAV is a 404 there. The deployment guide was one, from
the day it was written.
"""

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script() -> ModuleType:
    """Import `scripts/build_docs.py`, which is not an installed module."""
    spec = importlib.util.spec_from_file_location("build_docs", ROOT / "scripts" / "build_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_docs = _load_script()

BUILT = {"agents.md": "guide", "catalogue-weather.md": "catalogue"}


def _link(href: str) -> str:
    return build_docs.link_docs_pages(f'<a href="{href}">x</a>', "../", BUILT)


class TestALinkToAnotherPage:
    def test_a_built_page_is_linked_to_its_html(self) -> None:
        assert _link("agents.md") == '<a href="../guide/agents.html">x</a>'

    def test_the_fragment_is_kept(self) -> None:
        assert _link("agents.md#spawning") == '<a href="../guide/agents.html#spawning">x</a>'

    def test_a_page_in_another_section_is_reached_through_the_root(self) -> None:
        assert _link("catalogue-weather.md") == (
            '<a href="../catalogue/catalogue-weather.html">x</a>'
        )

    def test_a_page_the_site_does_not_build_is_linked_to_its_source(self) -> None:
        assert _link("api.md#cost-management") == (
            f'<a href="{build_docs.GITHUB_DOCS}api.md#cost-management">x</a>'
        )

    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com/agents.md",
            "../README.md",
            "/docs/agents.md",
            "#section",
            "agents.html",
        ],
    )
    def test_any_other_link_is_left_alone(self, href: str) -> None:
        assert _link(href) == f'<a href="{href}">x</a>'


class TestTheReadme:
    def test_every_link_to_the_site_names_a_page_it_builds(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        linked = re.findall(
            r"https://docs\.waldiez\.io/wactorz/(guide|catalogue)/([A-Za-z0-9_-]+)\.html", readme
        )
        built = {(subdir, md) for subdir, md, _path in build_docs.collect_pages()}

        missing = [f"{s}/{n}.html" for s, n in linked if (s, f"{n}.md") not in built]

        assert linked, "the README no longer links to the site; this test guards nothing"
        assert missing == []

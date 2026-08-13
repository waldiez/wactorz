"""The static and docs handlers may only serve files inside their own root.

⚠ The guard these tests pin used to be `str(candidate).startswith(str(base))`.
`resolve()` collapses `..`, so ordinary traversal was already closed — what was
not is the **sibling whose name shares a prefix**: with a base of `…/static/app`,
`…/static/app-old/secret` starts with it and passed. That is a directory the
operator never published, reachable through a check that looked like it pinned
one.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from wactorz.web import static_site


@pytest.fixture(name="tree")
def tree_fixture(tmp_path: Path) -> Path:
    """A published directory, and a sibling that merely shares its prefix."""
    published = tmp_path / "app"
    (published / "assets").mkdir(parents=True)
    (published / "assets" / "main.js").write_text("console.log(1);", encoding="utf-8")

    sibling = tmp_path / "app-old"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("not published", encoding="utf-8")

    docs = tmp_path / "docs"
    (docs / "guide").mkdir(parents=True)
    (docs / "guide" / "index.html").write_text("<h1>guide</h1>", encoding="utf-8")

    docs_sibling = tmp_path / "docs-private"
    (docs_sibling / "internal").mkdir(parents=True)
    (docs_sibling / "internal" / "index.html").write_text("<h1>internal</h1>", encoding="utf-8")
    return tmp_path


class TestTheContainmentTestItself:
    def test_a_descendant_is_inside(self, tmp_path: Path) -> None:
        assert static_site._within(tmp_path / "app" / "a.js", tmp_path / "app")

    def test_the_base_itself_is_inside(self, tmp_path: Path) -> None:
        assert static_site._within(tmp_path / "app", tmp_path / "app")

    def test_a_sibling_sharing_the_prefix_is_not(self, tmp_path: Path) -> None:
        # ⚠ The whole bug: this passes `startswith` and must not pass here.
        assert not static_site._within(tmp_path / "app-old" / "secret", tmp_path / "app")

    def test_the_parent_is_not(self, tmp_path: Path) -> None:
        assert not static_site._within(tmp_path, tmp_path / "app")


class TestServingBuiltAssets:
    """Driven through `make_mocked_request`, not a real URL.

    ⚠ An earlier version of these tests went through a test client and passed
    against the broken guard: aiohttp normalises `..` out of the path before
    routing, so the escaping value never reached the handler. A test that
    passes either way is worse than no test — the relative path is injected
    into `match_info` directly so the guard is the thing under test.
    """

    @staticmethod
    def _get(rel: str) -> Any:
        return make_mocked_request("GET", "/assets/x", match_info={"path": rel})

    async def test_a_published_file_is_served(self, tree: Path) -> None:
        with (
            patch.object(static_site, "FRONTEND_DIST", tree / "app"),
            patch.object(static_site, "FRONTEND_PUBLIC", tree / "app"),
        ):
            resp = await static_site.static_handler(self._get("assets/main.js"))

        assert resp.status == 200

    async def test_a_prefix_sibling_is_refused(self, tree: Path) -> None:
        with (
            patch.object(static_site, "FRONTEND_DIST", tree / "app"),
            patch.object(static_site, "FRONTEND_PUBLIC", tree / "app"),
            pytest.raises(web.HTTPNotFound),
        ):
            await static_site.static_handler(self._get("../app-old/secret.txt"))

    async def test_plain_traversal_stays_refused(self, tree: Path) -> None:
        with (
            patch.object(static_site, "FRONTEND_DIST", tree / "app"),
            patch.object(static_site, "FRONTEND_PUBLIC", tree / "app"),
            pytest.raises(web.HTTPNotFound),
        ):
            await static_site.static_handler(self._get("../../etc/passwd"))


class TestServingDocs:
    @staticmethod
    def _get(rel: str) -> Any:
        return make_mocked_request("GET", "/docs/x", match_info={"path": rel})

    async def test_a_published_page_is_served(self, tree: Path) -> None:
        with patch.object(static_site, "DOCS_SITE", tree / "docs"):
            resp = await static_site.docs_handler(self._get("guide/index.html"))

        assert resp.status == 200

    async def test_a_prefix_sibling_is_refused(self, tree: Path) -> None:
        with (
            patch.object(static_site, "DOCS_SITE", tree / "docs"),
            pytest.raises(web.HTTPNotFound),
        ):
            await static_site.docs_handler(self._get("../docs-private/internal/index.html"))

    async def test_an_escaped_path_names_nothing_outside_the_root(self, tree: Path) -> None:
        # ⚠ The second half of this fix. The directory-index fallback reads
        # `candidate.parent` and redirects to a name found there — so an escaped
        # path disclosed the contents of a directory nobody published, even
        # while refusing to serve the file itself.
        with patch.object(static_site, "DOCS_SITE", tree / "docs"):
            with pytest.raises(web.HTTPException) as caught:
                await static_site.docs_handler(self._get("../docs-private/index.html"))

        assert caught.value.status == 404
        assert "internal" not in caught.value.headers.get("Location", "")

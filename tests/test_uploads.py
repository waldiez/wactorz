"""Storing and serving chat attachments.

The endpoint writes caller-supplied bytes to disk, and the state directory holds
pickles that are loaded and executed — so a write a caller can steer is a
code-execution primitive, not a disk-space problem. Two rules carry that: no part
of a stored path comes from a request, and the type is decided by the bytes
rather than by what the request claims.
"""

import io
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config
from wactorz.web import api_uploads, uploads
from wactorz.web.app import build_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"\x00" * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> Iterator[Path]:
    """Uploads land under a temporary state directory, not the real one."""
    with patch.object(uploads, "resolve_state_dir", return_value=str(tmp_path)):
        yield tmp_path / "uploads"


@pytest.fixture(name="client")
async def client_fixture(store: Path) -> AsyncIterator[TestClient[Any, Any]]:
    app = web.Application()
    app.router.add_post("/api/upload", api_uploads.upload_handler)
    app.router.add_get("/api/upload/{file_id}", api_uploads.download_handler)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


def _form(data: bytes, name: str = "shot.png", field: str = "file") -> FormData:
    """A multipart body shaped the way a browser sends one — with a filename."""
    form = FormData()
    form.add_field(field, io.BytesIO(data), filename=name, content_type="application/octet-stream")
    return form


async def _put(client: TestClient[Any, Any], data: bytes, name: str = "shot.png") -> Any:
    return await client.post("/api/upload", data=_form(data, name))


class TestTheTypeComesFromTheBytes:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PNG, "image/png"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/jpeg"),
            (b"GIF89a" + b"\x00" * 20, "image/gif"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20, "image/webp"),
            (b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20, "audio/wav"),
            (PDF, "application/pdf"),
            (b"PK\x03\x04" + b"\x00" * 20, "application/zip"),
            (b"OggS" + b"\x00" * 20, "audio/ogg"),
        ],
    )
    def test_a_recognised_signature(self, data: bytes, expected: str) -> None:
        assert uploads.sniff(data) == expected

    def test_anything_unrecognised_is_opaque(self) -> None:
        assert uploads.sniff(b"just some text") == uploads.OPAQUE_TYPE

    def test_an_svg_is_never_an_image(self) -> None:
        # The browser's own accept-list matches the `image/` prefix, which
        # includes SVG — and an SVG is a script container. It has no signature
        # here, so it can only ever come back as a download.
        assert uploads.sniff(SVG) == uploads.OPAQUE_TYPE
        assert uploads.serve_type(uploads.sniff(SVG)) == (uploads.OPAQUE_TYPE, False)


class TestWhatMayRenderInThePage:
    @pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/gif", "image/webp"])
    def test_a_still_image_may(self, mime: str) -> None:
        assert uploads.serve_type(mime) == (mime, True)

    @pytest.mark.parametrize("mime", ["application/pdf", "audio/ogg", "text/html", ""])
    def test_nothing_else_does(self, mime: str) -> None:
        assert uploads.serve_type(mime) == (uploads.OPAQUE_TYPE, False)


class TestNamesAndIds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32", "system32"),
            ("plain.txt", "plain.txt"),
            ("with\x00null.txt", "withnull.txt"),
            # A multipart filename arrives percent-encoded, so the separator has
            # to be decoded before it can be stripped.
            ("..%2F..%2Fetc%2Fpasswd", "passwd"),
            ("%2e%2e%2fsecret.txt", "secret.txt"),
            ("", "attachment"),
            ("   ", "attachment"),
        ],
    )
    def test_a_display_name_cannot_read_as_a_path(self, raw: str, expected: str) -> None:
        # It is echoed into a chip and a Content-Disposition header, never onto
        # the filesystem — but a name that looks like a path invites someone
        # later to use it as one.
        assert uploads.safe_name(raw) == expected

    def test_a_long_name_is_bounded(self) -> None:
        assert len(uploads.safe_name("a" * 500)) == 120

    def test_only_ids_we_generated_are_accepted(self) -> None:
        assert uploads.is_id(uploads.new_id())
        for bad in ["../../etc/passwd", "", "abc", "../" + "a" * 29, uploads.new_id() + "x"]:
            assert not uploads.is_id(bad), bad


class TestStoringAFile:
    async def test_it_answers_with_what_the_thread_needs(
        self, client: TestClient[Any, Any]
    ) -> None:
        resp = await _put(client, PNG)

        assert resp.status == 200
        body = await resp.json()
        assert uploads.is_id(body["id"])
        assert body["mime"] == "image/png"
        assert body["size"] == len(PNG)
        assert body["name"] == "shot.png"

    async def test_a_hostile_filename_is_reduced_before_it_is_echoed(
        self, client: TestClient[Any, Any]
    ) -> None:
        # Goes straight into a chip and a Content-Disposition header.
        resp = await _put(client, PNG, name="../../etc/passwd")

        assert (await resp.json())["name"] == "passwd"

    async def test_nothing_the_caller_sent_names_the_file_on_disk(
        self, client: TestClient[Any, Any], store: Path
    ) -> None:
        # The rule that keeps a write from becoming a chosen write.
        await client.post("/api/upload", data=_form(PNG))

        stored = [p.name for p in store.iterdir() if not p.name.endswith(".json")]
        assert stored and all(uploads.is_id(name) for name in stored)

    async def test_a_file_over_the_limit_is_refused(self, client: TestClient[Any, Any]) -> None:
        with patch.object(config, "UPLOAD_MAX_BYTES", 64):
            resp = await _put(client, b"x" * 500)

        assert resp.status == 413

    async def test_a_refused_file_leaves_nothing_behind(
        self, client: TestClient[Any, Any], store: Path
    ) -> None:
        # Written under a temporary name precisely so a partial upload is not
        # something an id later resolves to.
        with patch.object(config, "UPLOAD_MAX_BYTES", 64):
            await _put(client, b"x" * 500)

        assert not list(store.iterdir())

    async def test_an_empty_file_is_refused(self, client: TestClient[Any, Any]) -> None:
        resp = await _put(client, b"")

        assert resp.status == 400

    async def test_a_part_by_another_name_is_refused(self, client: TestClient[Any, Any]) -> None:
        resp = await client.post("/api/upload", data=_form(PNG, field="notfile"))

        assert resp.status == 400


class TestServingItBack:
    async def _stored(self, client: TestClient[Any, Any], data: bytes) -> str:
        resp = await client.post("/api/upload", data=_form(data))
        return str((await resp.json())["id"])

    async def test_an_image_comes_back_ready_to_render(self, client: TestClient[Any, Any]) -> None:
        file_id = await self._stored(client, PNG)

        resp = await client.get(f"/api/upload/{file_id}")

        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.headers["Content-Disposition"].startswith("inline")
        assert await resp.read() == PNG

    async def test_everything_else_comes_back_as_a_download(
        self, client: TestClient[Any, Any]
    ) -> None:
        # A PDF rendered in the page's origin is a scripting surface; a download
        # is not.
        file_id = await self._stored(client, PDF)

        resp = await client.get(f"/api/upload/{file_id}")

        assert resp.headers["Content-Disposition"].startswith("attachment")
        assert resp.headers["Content-Type"] == uploads.OPAQUE_TYPE

    async def test_an_svg_is_served_as_a_download(self, client: TestClient[Any, Any]) -> None:
        file_id = await self._stored(client, SVG)

        resp = await client.get(f"/api/upload/{file_id}")

        assert resp.headers["Content-Disposition"].startswith("attachment")

    async def test_the_browser_is_told_not_to_guess(self, client: TestClient[Any, Any]) -> None:
        # Without this the download decision is advisory: a browser that sniffs
        # can decide to render what we said to save.
        file_id = await self._stored(client, PDF)

        resp = await client.get(f"/api/upload/{file_id}")

        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize("bad", ["..%2F..%2Fetc%2Fpasswd", "nope", "0" * 31])
    async def test_an_id_we_did_not_issue_is_not_found(
        self, client: TestClient[Any, Any], bad: str
    ) -> None:
        resp = await client.get(f"/api/upload/{bad}")

        assert resp.status == 404


class TestTellingTheBrowser:
    """`/api/config` reports whether uploads are on.

    The routes are only registered when they are, so a browser that assumed
    instead of asking would either offer a drop zone whose every upload 404s or
    hide a feature the deployment has. The field and the routes come from one
    flag, and this is what keeps them from drifting apart.
    """

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_the_field_matches_the_routes(
        self, monkeypatch: pytest.MonkeyPatch, enabled: bool
    ) -> None:
        monkeypatch.setattr(config, "UPLOADS_ENABLED", enabled)
        app = build_app()
        # Asked of the route table rather than of a request: the SPA catch-all
        # answers an unregistered path too, so a status code cannot tell the
        # difference between a missing route and a served page.
        posts = {
            r.resource.canonical for r in app.router.routes() if r.method == "POST" and r.resource
        }

        async with TestClient(TestServer(app)) as client:
            payload = await (await client.get("/api/config")).json()

        assert payload["uploads"]["enabled"] is enabled
        assert ("/api/upload" in posts) is enabled


class TestReadingItBackForTheModel:
    """The send path reads bytes directly rather than over HTTP."""

    def test_a_stored_file_reads_back_verbatim(self, store: Path) -> None:
        store.mkdir(parents=True, exist_ok=True)
        (store / ("a" * 32)).write_bytes(PNG)

        assert uploads.read_bytes("a" * 32) == PNG

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "nope", "", "0" * 31, "A" * 32])
    def test_nothing_but_an_id_we_issued_is_read(self, store: Path, bad: str) -> None:
        # Same shape check the download handler applies, so no caller-supplied
        # byte ever reaches a path.
        assert uploads.read_bytes(bad) is None

    def test_a_missing_file_is_not_an_error(self, store: Path) -> None:
        assert uploads.read_bytes("b" * 32) is None

    def test_a_file_larger_than_the_endpoint_accepts_is_refused(self, store: Path) -> None:
        # Reaching this means the bytes on disk disagree with what was recorded.
        # Refused on the stat rather than measured after being read into memory.
        store.mkdir(parents=True, exist_ok=True)
        (store / ("c" * 32)).write_bytes(b"x" * (config.UPLOAD_MAX_BYTES + 1))

        assert uploads.read_bytes("c" * 32) is None

    def test_a_directory_is_not_a_file(self, store: Path) -> None:
        (store / ("d" * 32)).mkdir(parents=True)

        assert uploads.read_bytes("d" * 32) is None

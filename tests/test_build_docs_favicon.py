"""The docs site carries the dashboard's icon, and follows it when it changes."""

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


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    site, public = tmp_path / "site", tmp_path / "public"
    site.mkdir()
    public.mkdir()
    return site, public


def test_a_favicon_from_an_earlier_build_is_replaced(tmp_path: Path) -> None:
    site, public = _dirs(tmp_path)
    (site / "favicon.ico").write_bytes(b"old icon")
    (public / "icon.ico").write_bytes(b"new icon")

    build_docs.write_favicon(site, public)

    assert (site / "favicon.ico").read_bytes() == b"new icon"


def test_icon_ico_is_preferred_over_favicon_ico(tmp_path: Path) -> None:
    site, public = _dirs(tmp_path)
    (public / "favicon.ico").write_bytes(b"favicon")
    (public / "icon.ico").write_bytes(b"icon")

    build_docs.write_favicon(site, public)

    assert (site / "favicon.ico").read_bytes() == b"icon"


def test_without_a_frontend_icon_the_site_gets_a_placeholder(tmp_path: Path) -> None:
    site, public = _dirs(tmp_path)

    build_docs.write_favicon(site, public)

    icon = (site / "favicon.ico").read_bytes()
    # An ICO header: reserved 0, type 1 (icon), one image.
    assert icon[:6] == b"\x00\x00\x01\x00\x01\x00"


def test_the_checkout_has_the_icon_the_site_copies() -> None:
    assert (ROOT / "frontend" / "public" / "icon.ico").exists()

# SPDX-License-Identifier: Apache-2.0
"""The icons the project ships are the WZ mark that `scripts/gen_icons.py` draws.

The SVGs are compared with what the script writes, so the mark is changed in
one place. The PNGs depend on the renderer and are not compared; that they
exist where the app and the add-ons look for them is. The app's manifest and
its service worker name the same icons, and the public pages a link may be
shared from carry a preview card at an address a crawler can fetch.
"""

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "frontend" / "public"


def _load_script() -> ModuleType:
    """Import `scripts/gen_icons.py`, which is not an installed module."""
    spec = importlib.util.spec_from_file_location("gen_icons", ROOT / "scripts" / "gen_icons.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_icons = _load_script()


def test_the_favicon_is_the_mark_the_script_draws() -> None:
    assert (PUBLIC / "favicon.svg").read_text(encoding="utf-8") == gen_icons.favicon_svg()


def test_the_readme_icon_is_the_tile_the_script_draws() -> None:
    committed = (ROOT / ".github" / "assets" / "icon.svg").read_text(encoding="utf-8")

    assert committed == gen_icons.tile_svg(rounded=True)


def test_the_readme_logo_needs_no_font() -> None:
    logo = (ROOT / ".github" / "assets" / "logo.svg").read_text(encoding="utf-8")

    assert "<text" not in logo


@pytest.mark.parametrize(
    "path",
    [
        "frontend/public/icon.png",
        "frontend/public/icon-maskable.png",
        "frontend/public/icon.ico",
        "ha-addon/wactorz/icon.png",
        "ha-addon/wactorz/logo.png",
        "ha-addon/wactorz-ultra/icon.png",
        "ha-addon/wactorz-ultra/logo.png",
        ".github/assets/icon.png",
    ],
)
def test_each_rendered_icon_is_where_it_is_looked_for(path: str) -> None:
    assert (ROOT / path).stat().st_size > 0


def test_a_tile_keeps_the_mark_inside_the_circle_a_launcher_keeps() -> None:
    # A maskable icon is cut to as little as the central circle of 80% of its side.
    half_diagonal = (gen_icons.MARK_SHARE / 2) * (1 + (96 / 114) ** 2) ** 0.5

    assert half_diagonal <= 0.4


def test_the_manifest_s_icons_exist_and_the_service_worker_keeps_them() -> None:
    manifest = json.loads((PUBLIC / "site.webmanifest").read_text(encoding="utf-8"))
    named = {icon["src"].removeprefix("./") for icon in manifest["icons"]}
    shell = re.search(r"SHELL_FILES = new Set\(\[(.*?)\]\)", (PUBLIC / "sw.js").read_text())
    assert shell is not None
    kept = set(re.findall(r'"([^"]+)"', shell.group(1)))

    assert all((PUBLIC / name).is_file() for name in named)
    assert named <= kept
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])


@pytest.mark.parametrize("page", ["docs/_landing.html", "scripts/build_docs.py"])
def test_a_public_page_carries_the_preview_card(page: str) -> None:
    text = (ROOT / page).read_text(encoding="utf-8")
    image = re.search(r'property="og:image" content="([^"]+)"', text)

    assert image is not None and image.group(1).startswith("https://")
    assert image.group(1).endswith("/og.png")
    assert 'name="twitter:card" content="summary_large_image"' in text

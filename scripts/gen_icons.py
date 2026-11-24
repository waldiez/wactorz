#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Draw the WZ mark and write every icon and logo the project ships from it.

    python scripts/gen_icons.py [--media DIR]

The mark is defined once, here, in a 114x96 box: a W whose last stroke runs into
the Z's top bar, with a stem and a hollow dot rising from the W's middle peak,
painted in a cyan-violet-pink gradient. What is written:

- ``frontend/public/favicon.svg``: the mark alone. The dashboard's header, the
  browser tab and the login page all show this file.
- ``frontend/public/icon.png`` and ``icon.ico``: the mark on a dark rounded
  tile, for the installed app and for whatever reads an ``.ico``.
- ``frontend/public/icon-maskable.png``: the same on a full dark square, for a
  launcher that cuts its own shape out of the icon.
- ``ha-addon/<variant>/icon.png`` and ``logo.png``: the tile, and the mark with
  the word "Wactorz", for the Home Assistant add-on store.
- ``.github/assets/icon.svg``, ``icon.png`` and ``logo.svg``: the same for the
  README, with the word turned into outlines so it looks the same without the
  font.
- with ``--media DIR``: ``DIR/images/wactorz/favicon.svg``, ``icon.ico`` and
  ``og.png``, which the documentation site's pages link to; ``og.png`` is the
  1200x630 card a link to them is previewed with.

Rendering needs ``rsvg-convert`` and ImageMagick's ``magick``; the outlined
word needs ``inkscape``. The SVGs are plain strings, so the test suite compares
the committed ones with what this module draws.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "frontend" / "public"
ADDONS = [ROOT / "ha-addon" / "wactorz", ROOT / "ha-addon" / "wactorz-ultra"]
GITHUB_ASSETS = ROOT / ".github" / "assets"

#: The letters as one stroke: the W, its last stroke into the Z's top bar, the Z.
LETTERS = "M12 40 L27 80 L42 46 L55 80 L68 40 H100 L70 80 H102"
#: The stem from the W's middle peak up to the dot.
STEM = "M42 46 V22"
CYAN, VIOLET, PINK = "#22d3ee", "#8b5cf6", "#ec4899"
#: The tile behind the mark: the dashboard's own background.
INK = "#050818"
#: How much of a tile's side the mark spans, inside the circle a launcher keeps.
MARK_SHARE = 0.6
#: How rounded a tile's corners are, as a share of its side.
CORNER_SHARE = 0.22
ICO_SIZES = "16,24,32,48,64,128,256"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


def _gradient(gradient_id: str, x1: float, x2: float) -> str:
    return (
        f'<linearGradient id="{gradient_id}" x1="{x1:g}" y1="0" x2="{x2:g}" y2="0" '
        'gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{CYAN}"/><stop offset=".55" stop-color="{VIOLET}"/>'
        f'<stop offset="1" stop-color="{PINK}"/></linearGradient>'
    )


def mark_shapes(paint: str) -> str:
    """The mark's three shapes in its 114x96 box, stroked with ``paint``."""
    return (
        f'<path d="{STEM}" stroke="{paint}" stroke-width="5" stroke-linecap="round" fill="none"/>'
        f'<path d="{LETTERS}" stroke="{paint}" stroke-width="12" stroke-linecap="round" '
        'stroke-linejoin="round" fill="none"/>'
        f'<circle cx="42" cy="15" r="7" fill="none" stroke="{paint}" stroke-width="6.6"/>'
    )


def favicon_svg() -> str:
    """The mark alone, centred in a square box so it can stand for a tab or an app."""
    return (
        f'<svg {SVG_NS} viewBox="0 -9 114 114" width="114" height="114" '
        'role="img" aria-label="Wactorz">'
        f"<defs>{_gradient('wz', 8, 106)}</defs>{mark_shapes('url(#wz)')}</svg>\n"
    )


def tile_svg(*, rounded: bool, size: int = 512) -> str:
    """The mark on a dark tile ``size`` wide: rounded, or a full square for a launcher to cut."""
    width = size * MARK_SHARE
    scale = width / 114
    x = (size - width) / 2
    y = (size - 96 * scale) / 2
    corner = f' rx="{size * CORNER_SHARE:g}"' if rounded else ""
    return (
        f'<svg {SVG_NS} viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        'role="img" aria-label="Wactorz">'
        f"<defs>{_gradient('wz', 8, 106)}</defs>"
        f'<rect width="{size}" height="{size}"{corner} fill="{INK}"/>'
        f'<g transform="translate({x:g} {y:g}) scale({scale:g})">{mark_shapes("url(#wz)")}</g>'
        "</svg>\n"
    )


def logo_svg() -> str:
    """The mark and the word "Wactorz" in the same gradient, 250x100, on nothing.

    One image for a light page and a dark one alike: a store or a README shows
    it on either, and the gradient reads on both where a plain colour would not.
    """
    return (
        f'<svg {SVG_NS} viewBox="0 0 250 100" width="250" height="100" '
        'role="img" aria-label="Wactorz">'
        f"<defs>{_gradient('wz', 8, 106)}{_gradient('word', 80, 240)}</defs>"
        f'<g transform="translate(8 22) scale({56 / 96:g})">{mark_shapes("url(#wz)")}</g>'
        '<text x="80" y="66" font-family="Inter" font-weight="700" font-size="40" '
        'letter-spacing="-0.5" fill="url(#word)">Wactorz</text>'
        "</svg>\n"
    )


def og_svg() -> str:
    """The card a shared link to the docs is previewed with: 1200x630, on the dark background.

    Only the mark, the name and what it does: a feed shows the card small and
    prints the link's own address under it, so anything finer would not be read.
    """
    return (
        f'<svg {SVG_NS} viewBox="0 0 1200 630" width="1200" height="630">'
        f"<defs>{_gradient('wz', 8, 106)}{_gradient('word', 380, 1100)}"
        '<radialGradient id="bg" cx="30%" cy="40%" r="80%"><stop offset="0" stop-color="#0d1a38"/>'
        f'<stop offset="1" stop-color="{INK}"/></radialGradient></defs>'
        '<rect width="1200" height="630" fill="url(#bg)"/>'
        f'<g transform="translate(90 175) scale(2.5)">{mark_shapes("url(#wz)")}</g>'
        '<text x="430" y="300" font-family="Inter" font-weight="800" font-size="120" '
        'letter-spacing="-3" fill="url(#word)">Wactorz</text>'
        '<text font-family="Inter" font-weight="500" font-size="36" fill="#cbd5e1">'
        '<tspan x="436" y="370">Spawn, coordinate and monitor AI agents</tspan>'
        '<tspan x="436" y="418">at runtime, over MQTT.</tspan></text>'
        "</svg>\n"
    )


def _run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"{command[0]} failed: {result.stderr.strip()}")


def _png(svg: Path, out: Path, size: tuple[int, int]) -> None:
    _run("rsvg-convert", "-w", str(size[0]), "-h", str(size[1]), "-o", str(out), str(svg))


def _ico(svg: Path, out: Path) -> None:
    _run(
        "magick", "-background", "none", "-density", "384", str(svg),
        "-define", f"icon:auto-resize={ICO_SIZES}", str(out),
    )  # fmt: skip


def _outlined(svg: Path, out: Path) -> None:
    """``svg`` with its text turned into outlines, so it needs no font where it is shown."""
    _run(
        "inkscape", str(svg), "--export-text-to-path", "--export-plain-svg",
        f"--export-filename={out}",
    )  # fmt: skip


def _say(path: Path) -> None:
    print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


def write_all(media: Path | None) -> None:
    """Write every file the module docstring lists."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        sources = {
            "mark": favicon_svg(),
            "tile": tile_svg(rounded=True),
            "square": tile_svg(rounded=False),
            "logo": logo_svg(),
            "og": og_svg(),
        }
        drawn = {name: work / f"{name}.svg" for name in sources}
        for name, text in sources.items():
            drawn[name].write_text(text, encoding="utf-8")

        targets: list[Path] = [PUBLIC / "favicon.svg", GITHUB_ASSETS / "icon.svg"]
        (PUBLIC / "favicon.svg").write_text(sources["mark"], encoding="utf-8")
        (GITHUB_ASSETS / "icon.svg").write_text(sources["tile"], encoding="utf-8")
        _png(drawn["tile"], PUBLIC / "icon.png", (512, 512))
        _png(drawn["square"], PUBLIC / "icon-maskable.png", (512, 512))
        _ico(drawn["tile"], PUBLIC / "icon.ico")
        _png(drawn["tile"], GITHUB_ASSETS / "icon.png", (128, 128))
        _outlined(drawn["logo"], GITHUB_ASSETS / "logo.svg")
        targets += [PUBLIC / "icon.png", PUBLIC / "icon-maskable.png", PUBLIC / "icon.ico"]
        targets += [GITHUB_ASSETS / "icon.png", GITHUB_ASSETS / "logo.svg"]
        for addon in ADDONS:
            _png(drawn["tile"], addon / "icon.png", (128, 128))
            _png(drawn["logo"], addon / "logo.png", (250, 100))
            targets += [addon / "icon.png", addon / "logo.png"]
        if media is not None:
            images = media / "images" / "wactorz"
            images.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PUBLIC / "favicon.svg", images / "favicon.svg")
            shutil.copyfile(PUBLIC / "icon.ico", images / "icon.ico")
            _png(drawn["og"], images / "og.png", (1200, 630))
            targets += [images / "favicon.svg", images / "icon.ico", images / "og.png"]
    for target in targets:
        _say(target)


def main() -> None:
    """Parse the arguments and write the icons."""
    parser = argparse.ArgumentParser(description="Draw the WZ mark and write every icon from it.")
    parser.add_argument(
        "--media", type=Path, help="also write the docs site's icons under this checkout"
    )
    args = parser.parse_args()
    for tool in ("rsvg-convert", "magick", "inkscape"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not installed; it is needed to render the icons")
    write_all(args.media)


if __name__ == "__main__":
    main()

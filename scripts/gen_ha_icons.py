#!/usr/bin/env python3
"""Generate ha-addon/icon.png (128x128) and ha-addon/logo.png (250x100) from the SVG."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "ha-addon"

# Static "hold" state SVG — all dots + lines visible, no animations, no number labels
ICON_SVG = """\
<svg viewBox="0 0 32 32" width="128" height="128" fill="none"
     xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="bg" cx="50%" cy="40%" r="65%">
      <stop offset="0%"   stop-color="#0d1a38"/>
      <stop offset="100%" stop-color="#040810"/>
    </radialGradient>
    <linearGradient id="gl" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%"   stop-color="#6366f1"/>
      <stop offset="100%" stop-color="#a78bfa"/>
    </linearGradient>
    <linearGradient id="gr" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%"   stop-color="#a78bfa"/>
      <stop offset="100%" stop-color="#6366f1"/>
    </linearGradient>
    <filter id="dg" x="-100%" y="-100%" width="300%" height="300%">
      <feGaussianBlur stdDeviation="1.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="lg" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="0.6" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>

  <rect width="32" height="32" rx="7" fill="url(#bg)"
        filter="drop-shadow(0 3px 18px rgba(139,92,246,0.72))"/>

  <!-- W lines -->
  <line x1="3.5"  y1="6"  x2="11"   y2="26" stroke="url(#gl)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
  <line x1="11"   y1="26" x2="16"   y2="13" stroke="url(#gl)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
  <line x1="16"   y1="13" x2="21"   y2="26" stroke="url(#gr)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
  <line x1="21"   y1="26" x2="28.5" y2="6"  stroke="url(#gr)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>

  <!-- Dots -->
  <g filter="url(#dg)">
    <circle cx="3.5"  cy="6"  r="2.6" fill="#22d3ee"/><circle cx="3.5"  cy="6"  r="1.1" fill="white" opacity="0.8"/>
    <circle cx="11"   cy="26" r="2.6" fill="#818cf8"/><circle cx="11"   cy="26" r="1.1" fill="white" opacity="0.8"/>
    <circle cx="16"   cy="13" r="2.6" fill="#f472b6"/><circle cx="16"   cy="13" r="1.1" fill="white" opacity="0.8"/>
    <circle cx="21"   cy="26" r="2.6" fill="#818cf8"/><circle cx="21"   cy="26" r="1.1" fill="white" opacity="0.8"/>
    <circle cx="28.5" cy="6"  r="2.6" fill="#22d3ee"/><circle cx="28.5" cy="6"  r="1.1" fill="white" opacity="0.8"/>
  </g>

  <rect width="32" height="32" rx="7" stroke="#818cf8" stroke-width="0.4" fill="none" opacity="0.22"/>
</svg>
"""

# Landscape logo — icon on the left, "wactorz" wordmark on the right
LOGO_SVG = """\
<svg viewBox="0 0 250 100" width="250" height="100" fill="none"
     xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="bg" cx="50%" cy="40%" r="65%">
      <stop offset="0%"   stop-color="#0d1a38"/>
      <stop offset="100%" stop-color="#040810"/>
    </radialGradient>
    <linearGradient id="gl" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%"   stop-color="#6366f1"/>
      <stop offset="100%" stop-color="#a78bfa"/>
    </linearGradient>
    <linearGradient id="gr" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%"   stop-color="#a78bfa"/>
      <stop offset="100%" stop-color="#6366f1"/>
    </linearGradient>
    <linearGradient id="wg" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%"   stop-color="#a78bfa"/>
      <stop offset="100%" stop-color="#6366f1"/>
    </linearGradient>
    <filter id="dg" x="-100%" y="-100%" width="300%" height="300%">
      <feGaussianBlur stdDeviation="1.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="lg" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="0.6" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>

  <!-- Background -->
  <rect width="250" height="100" rx="14" fill="url(#bg)"/>

  <!-- Icon (32x32 viewBox scaled to ~72px, offset 14,14) -->
  <g transform="translate(14,14) scale(2.25)">
    <rect width="32" height="32" rx="7" fill="none"/>
    <line x1="3.5"  y1="6"  x2="11"   y2="26" stroke="url(#gl)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
    <line x1="11"   y1="26" x2="16"   y2="13" stroke="url(#gl)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
    <line x1="16"   y1="13" x2="21"   y2="26" stroke="url(#gr)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
    <line x1="21"   y1="26" x2="28.5" y2="6"  stroke="url(#gr)" stroke-width="1.5" stroke-linecap="round" filter="url(#lg)"/>
    <g filter="url(#dg)">
      <circle cx="3.5"  cy="6"  r="2.6" fill="#22d3ee"/><circle cx="3.5"  cy="6"  r="1.1" fill="white" opacity="0.8"/>
      <circle cx="11"   cy="26" r="2.6" fill="#818cf8"/><circle cx="11"   cy="26" r="1.1" fill="white" opacity="0.8"/>
      <circle cx="16"   cy="13" r="2.6" fill="#f472b6"/><circle cx="16"   cy="13" r="1.1" fill="white" opacity="0.8"/>
      <circle cx="21"   cy="26" r="2.6" fill="#818cf8"/><circle cx="21"   cy="26" r="1.1" fill="white" opacity="0.8"/>
      <circle cx="28.5" cy="6"  r="2.6" fill="#22d3ee"/><circle cx="28.5" cy="6"  r="1.1" fill="white" opacity="0.8"/>
    </g>
  </g>

  <!-- Wordmark -->
  <text x="108" y="62"
        font-family="'SF Pro Display', 'Segoe UI', system-ui, sans-serif"
        font-size="38" font-weight="700" letter-spacing="-1"
        fill="url(#wg)">wactorz</text>

  <rect width="250" height="100" rx="14" stroke="#818cf8" stroke-width="0.5" fill="none" opacity="0.22"/>
</svg>
"""


def convert(svg_content: str, out_path: Path) -> None:
    result = subprocess.run(
        ["rsvg-convert", "-o", str(out_path)],
        input=svg_content.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"rsvg-convert failed: {result.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    print(f"  wrote {out_path.relative_to(ROOT)}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Generating HA addon icons...")
    convert(ICON_SVG, OUT / "icon.png")
    convert(LOGO_SVG, OUT / "logo.png")
    print("Done.")


if __name__ == "__main__":
    main()

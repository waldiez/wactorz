#!/usr/bin/env python3
"""Generate a root index.html for a rustdoc output directory.

cargo doc --workspace doesn't create a root index; this script scans the
output dir for crate subdirectories and emits a simple redirect/listing page.

Usage:
    python3 scripts/rustdoc_index.py site/api/rust
"""
import sys
from pathlib import Path

CRATE_ORDER = [
    "wactorz",
    "wactorz_core",
    "wactorz_agents",
    "wactorz_interfaces",
    "wactorz_mqtt",
]


def main(out_dir: Path) -> None:
    if not out_dir.is_dir():
        print(f"[rustdoc_index] {out_dir} not found — skipping", file=sys.stderr)
        return

    index = out_dir / "index.html"
    if index.exists():
        return  # already present (cargo generated one)

    # Collect crate dirs that have their own index.html
    crates = sorted(
        [d.name for d in out_dir.iterdir()
         if d.is_dir() and (d / "index.html").exists()],
        key=lambda n: (CRATE_ORDER.index(n) if n in CRATE_ORDER else 999, n),
    )

    if not crates:
        print("[rustdoc_index] no crate dirs found", file=sys.stderr)
        return

    # If there's only one meaningful entry, just redirect to it
    primary = crates[0]

    rows = "\n".join(
        f'      <li><a href="{c}/index.html">{c.replace("_", "-")}</a></li>'
        for c in crates
    )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0; url={primary}/index.html">
  <title>Wactorz Rust API</title>
  <style>
    body {{ font-family: monospace; background: #05080e; color: #dde3f0; padding: 3rem 2rem; }}
    h1   {{ color: #4f8ef7; font-size: 1.4rem; margin-bottom: 1.5rem; }}
    ul   {{ list-style: none; padding: 0; }}
    li   {{ margin: 0.4rem 0; }}
    a    {{ color: #00d4ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    p    {{ color: #5a6890; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  </style>
</head>
<body>
  <h1>Wactorz — Rust API Reference</h1>
  <p>Redirecting to <code>{primary}</code> …</p>
  <ul>
{rows}
  </ul>
</body>
</html>
"""
    index.write_text(html)
    print(f"[rustdoc_index] wrote {index} ({len(crates)} crates, primary → {primary})")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("site/api/rust"))

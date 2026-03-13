#!/usr/bin/env python3
"""
AgentFlow docs builder.

Converts docs/*.md → site/*.html using a custom dark template that matches
the landing page (Chakra Petch + JetBrains Mono, #05080e background).

Usage:
    python3 scripts/build_docs.py               # build → site/
    python3 scripts/build_docs.py --serve       # build + serve on :8001
    python3 scripts/build_docs.py --serve 8002  # custom port
    python3 scripts/build_docs.py --reload      # serve + watch docs/ for changes
"""
import argparse
import http.server
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE = ROOT / "site"

# ── Navigation definition ──────────────────────────────────────────────────────
# Each section maps to a subdirectory under site/
# Format: (label, subdir, [(page_label, md_filename), ...]) or (label, url)
NAV = [
    ("Guide", "guide", [
        ("Installation", "development.md"),
        ("Architecture", "architecture.md"),
        ("Agents",       "agents.md"),
        ("Deployment",   "deployment.md"),
        ("Windows",      "windows.md"),
    ]),
    ("Reference", "reference", [
        ("REST & WebSocket API", "api.md"),
        ("MQTT Topics",          "mqtt_topics.md"),
        ("Python API",           "python-api.md"),
    ]),
    ("Rust Docs ↗",  "https://waldiez.github.io/agentflow/api/rust/"),
    ("JS/TS Docs ↗", "https://waldiez.github.io/agentflow/api/js/"),
]

# ── HTML template ──────────────────────────────────────────────────────────────
TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title} — AgentFlow</title>
  <meta name="description" content="AgentFlow — Actor-model multi-agent AI framework"/>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@300;400;500;600;700&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet"/>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    :root{{
      --bg:#05080e;--bg2:#080d1a;--bg3:#0d1426;
      --border:#1a2140;--border-hi:#2a3560;
      --blue:#4f8ef7;--cyan:#00d4ff;
      --text:#dde3f0;--muted:#5a6890;--muted-hi:#8899bb;
      --mono:'JetBrains Mono',monospace;
      --display:'Chakra Petch',sans-serif;
    }}
    html{{scroll-behavior:smooth}}
    body{{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:.9rem;line-height:1.75;display:flex;flex-direction:column;min-height:100vh}}
    ::-webkit-scrollbar{{width:5px}}
    ::-webkit-scrollbar-thumb{{background:var(--border-hi);border-radius:3px}}

    /* ── Nav bar ── */
    .topbar{{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;padding:0 1.5rem;height:52px;border-bottom:1px solid var(--border);background:rgba(5,8,14,.88);backdrop-filter:blur(10px)}}
    .topbar-logo{{font-family:var(--display);font-weight:600;font-size:.95rem;letter-spacing:.06em;color:var(--text);text-decoration:none;display:flex;align-items:center;gap:.5rem}}
    .logo-mark{{width:20px;height:20px;border:1.5px solid var(--blue);border-radius:3px;display:grid;place-items:center}}
    .logo-mark::before{{content:'';width:5px;height:5px;background:var(--blue);border-radius:50%;box-shadow:0 0 6px var(--blue)}}
    .topbar-links{{display:flex;gap:.1rem}}
    .topbar-links a{{font-family:var(--mono);font-size:.75rem;color:var(--muted-hi);text-decoration:none;padding:.3rem .65rem;border-radius:3px;border:1px solid transparent;transition:all .15s}}
    .topbar-links a:hover,.topbar-links a.active{{color:var(--text);border-color:var(--border-hi);background:rgba(79,142,247,.07)}}

    /* ── Layout ── */
    .layout{{display:flex;flex:1}}

    /* ── Sidebar ── */
    .sidebar{{width:220px;flex-shrink:0;border-right:1px solid var(--border);padding:1.5rem 0;overflow-y:auto;position:sticky;top:52px;height:calc(100vh - 52px);background:var(--bg2)}}
    .sidebar-group{{margin-bottom:1.25rem}}
    .sidebar-label{{font-family:var(--display);font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);padding:.1rem 1.25rem .4rem;font-weight:500}}
    .sidebar a{{display:block;font-size:.8rem;color:var(--muted-hi);text-decoration:none;padding:.32rem 1.25rem;border-left:2px solid transparent;transition:all .15s}}
    .sidebar a:hover{{color:var(--text);border-left-color:var(--border-hi);background:rgba(79,142,247,.04)}}
    .sidebar a.active{{color:var(--blue);border-left-color:var(--blue);background:rgba(79,142,247,.06)}}
    .sidebar a.external{{color:var(--muted);font-size:.76rem}}

    /* ── Content ── */
    .content{{flex:1;padding:3rem 3.5rem 5rem;max-width:860px;min-width:0}}

    /* ── Typography ── */
    .content h1{{font-family:var(--display);font-size:2rem;font-weight:700;letter-spacing:-.02em;color:#fff;margin-bottom:1rem;line-height:1.15}}
    .content h2{{font-family:var(--display);font-size:1.35rem;font-weight:600;color:#fff;margin:2.5rem 0 .75rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}}
    .content h3{{font-family:var(--display);font-size:1.05rem;font-weight:500;color:var(--text);margin:1.75rem 0 .5rem}}
    .content h4{{font-family:var(--display);font-size:.9rem;font-weight:500;color:var(--muted-hi);margin:1.25rem 0 .35rem}}
    .content p{{margin-bottom:1rem;color:var(--text)}}
    .content a{{color:var(--blue);text-decoration:none}}
    .content a:hover{{text-decoration:underline}}
    .content ul,.content ol{{margin:.5rem 0 1rem 1.5rem}}
    .content li{{margin:.25rem 0}}
    .content strong{{color:#fff;font-weight:500}}
    .content em{{color:var(--muted-hi)}}
    .content hr{{border:none;border-top:1px solid var(--border);margin:2rem 0}}

    /* ── Code ── */
    .content code{{font-family:var(--mono);font-size:.82em;background:var(--bg3);color:var(--cyan);padding:.15em .4em;border-radius:3px;border:1px solid var(--border)}}
    .content pre{{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:1.25rem 1.5rem;overflow-x:auto;margin:1rem 0 1.5rem;position:relative}}
    .content pre code{{background:none;border:none;padding:0;color:var(--text);font-size:.83rem;line-height:1.7}}

    /* ── Pygments syntax highlight overrides ── */
    .highlight .hll{{background:#1a2140}}
    .highlight .c,.highlight .ch,.highlight .cm,.highlight .cp,.highlight .cpf,.highlight .cs{{color:#546e7a;font-style:italic}}
    .highlight .k,.highlight .kc,.highlight .kd,.highlight .kn,.highlight .kp,.highlight .kr,.highlight .kt{{color:#c792ea}}
    .highlight .s,.highlight .s1,.highlight .s2,.highlight .sb,.highlight .sc,.highlight .dl,.highlight .sd,.highlight .se,.highlight .sh,.highlight .si,.highlight .sx,.highlight .sr,.highlight .ss{{color:#c3e88d}}
    .highlight .n{{color:var(--text)}}
    .highlight .na,.highlight .nb,.highlight .nc,.highlight .nd,.highlight .ne,.highlight .nf,.highlight .nl,.highlight .nn{{color:#82aaff}}
    .highlight .mi,.highlight .mf,.highlight .mh,.highlight .mo{{color:#f78c6c}}
    .highlight .o,.highlight .ow{{color:#89ddff}}
    .highlight .p{{color:var(--text)}}

    /* ── Blockquote / admonition ── */
    .content blockquote{{border-left:3px solid var(--blue);margin:1rem 0;padding:.75rem 1.25rem;background:rgba(79,142,247,.05);color:var(--muted-hi)}}

    /* ── Tables ── */
    .content table{{width:100%;border-collapse:collapse;margin:1rem 0 1.5rem;font-size:.83rem}}
    .content th{{background:var(--bg3);color:var(--text);font-weight:500;padding:.6rem 1rem;border:1px solid var(--border);text-align:left}}
    .content td{{padding:.5rem 1rem;border:1px solid var(--border);color:var(--muted-hi)}}
    .content tr:hover td{{background:rgba(79,142,247,.03)}}

    /* ── Footer ── */
    footer{{border-top:1px solid var(--border);padding:1.25rem 2rem;font-family:var(--mono);font-size:.75rem;color:var(--muted);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem}}
    footer a{{color:var(--muted-hi);text-decoration:none}}
    footer a:hover{{color:var(--text)}}

    /* ── Responsive ── */
    @media(max-width:768px){{
      .sidebar{{display:none}}
      .content{{padding:2rem 1.25rem 4rem}}
    }}
  </style>
</head>
<body>
<header class="topbar">
  <a href="{root}index.html" class="topbar-logo">
    <div class="logo-mark"></div>AgentFlow
  </a>
  <nav class="topbar-links">
    <a href="https://github.com/waldiez/agentflow" target="_blank" rel="noopener">GitHub</a>
    <a href="https://pypi.org/project/agentflow/" target="_blank" rel="noopener">PyPI</a>
    <a href="{root}api/rust/" target="_blank">Rust Docs</a>
    <a href="{root}api/js/" target="_blank">JS Docs</a>
  </nav>
</header>

<div class="layout">
  <aside class="sidebar">
{sidebar}
  </aside>
  <main class="content">
{body}
  </main>
</div>

<footer>
  <span>AgentFlow &mdash; Apache-2.0 &mdash; <a href="https://github.com/waldiez/agentflow">GitHub</a></span>
  <span>Built with <a href="https://github.com/waldiez/agentflow/blob/main/scripts/build_docs.py">build_docs.py</a></span>
</footer>
</body>
</html>
"""


# ── Markdown renderer ──────────────────────────────────────────────────────────

def _ensure_markdown():
    try:
        import markdown  # noqa: F401
        return True
    except ImportError:
        print("[build_docs] installing markdown + pygments …")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "markdown", "pygments"])
        return True


def render_md(text: str) -> str:
    import markdown
    from markdown.extensions.codehilite import CodeHiliteExtension
    md = markdown.Markdown(extensions=[
        "fenced_code",
        "tables",
        "toc",
        "admonition",
        "attr_list",
        CodeHiliteExtension(css_class="highlight", guess_lang=True, noclasses=False),
    ])
    return md.convert(text)


# ── Nav helpers ───────────────────────────────────────────────────────────────

def _md_to_html_path(md_file: str) -> str:
    """'development.md' → 'development.html'"""
    return re.sub(r"\.md$", ".html", md_file)


def build_sidebar(active_md: str, active_subdir: str, root: str = "../") -> str:
    lines = []
    for item in NAV:
        label = item[0]
        if len(item) == 2:
            url = item[1]
            lines.append(f'    <a href="{url}" class="external" target="_blank" rel="noopener">{label}</a>')
        else:
            subdir, children = item[1], item[2]
            lines.append('    <div class="sidebar-group">')
            lines.append(f'      <div class="sidebar-label">{label}</div>')
            for child_label, child_md in children:
                href = f"{root}{subdir}/{_md_to_html_path(child_md)}"
                cls = "active" if child_md == active_md and subdir == active_subdir else ""
                lines.append(f'      <a href="{href}" class="{cls}">{child_label}</a>')
            lines.append("    </div>")
    return "\n".join(lines)


def extract_title(md_text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


# ── Build ─────────────────────────────────────────────────────────────────────

def collect_pages() -> list[tuple[str, str, Path]]:
    """Return (subdir, md_filename, path) for all pages referenced in NAV."""
    pages = []
    for item in NAV:
        if len(item) == 3:
            subdir, children = item[1], item[2]
            for _, child_md in children:
                pages.append((subdir, child_md, DOCS / child_md))
    return pages


def _redirect(target: str) -> str:
    return f'<!DOCTYPE html><meta http-equiv="refresh" content="0; url={target}"><a href="{target}">{target}</a>\n'


def build(site_dir: Path = SITE) -> None:
    _ensure_markdown()

    site_dir.mkdir(parents=True, exist_ok=True)

    # Copy assets
    assets_src = DOCS / "assets"
    if assets_src.is_dir():
        shutil.copytree(assets_src, site_dir / "assets", dirs_exist_ok=True)

    # Favicon placeholder (1x1 transparent ICO — avoids 404 noise in dev server)
    favicon = site_dir / "favicon.ico"
    if not favicon.exists():
        # Minimal valid ICO file (1x1 transparent)
        favicon.write_bytes(bytes([
            0,0,1,0,1,0,1,1,0,0,1,0,1,0,48,0,0,0,22,0,0,0,
            40,0,0,0,1,0,0,0,2,0,0,0,1,0,1,0,0,0,0,0,8,0,0,0,
            0,0,0,0,0,0,0,0,2,0,0,0,0,0,0,0,0,0,0,0,255,255,255,0,
            0,0,0,0,0,0,0,0,
        ]))

    # Copy landing page
    landing = DOCS / "_landing.html"
    if landing.exists():
        shutil.copy(landing, site_dir / "index.html")
        print(f"  landing  → site/index.html")

    # Render each markdown page into its subdir
    first_per_subdir: dict[str, str] = {}
    for subdir, md_name, md_path in collect_pages():
        out_dir = site_dir / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        first_per_subdir.setdefault(subdir, md_name)

        if not md_path.exists():
            print(f"  [skip]   {md_name} not found")
            continue

        text = md_path.read_text(encoding="utf-8")
        title = extract_title(text, md_name.replace(".md", "").replace("-", " ").title())
        body = render_md(text)
        root = "../"  # all content pages are exactly one level deep
        sidebar = build_sidebar(md_name, subdir, root)

        html = TEMPLATE.format(title=title, sidebar=sidebar, body=body, root=root)
        out = out_dir / _md_to_html_path(md_name)
        out.write_text(html, encoding="utf-8")
        print(f"  {md_name:<30} → site/{subdir}/{out.name}")

    # index.html redirect for each subdir → first page
    for subdir, first_md in first_per_subdir.items():
        idx = site_dir / subdir / "index.html"
        first_html = _md_to_html_path(first_md)
        idx.write_text(_redirect(f"./{first_html}"))
        print(f"  index    → site/{subdir}/index.html → {first_html}")

    # Compat redirect: landing page links to ./api/python/
    py_api_compat = site_dir / "api" / "python"
    py_api_compat.mkdir(parents=True, exist_ok=True)
    compat_idx = py_api_compat / "index.html"
    compat_idx.write_text(_redirect("../../reference/python-api.html"))
    print(f"  compat   → site/api/python/ → ../../reference/python-api.html")

    print(f"\n✓  site built → {site_dir}")


# ── Rust + JS/TS docs ──────────────────────────────────────────────────────────

def build_rust(site_dir: Path = SITE) -> None:
    rust_dir = ROOT / "rust"
    out_dir = site_dir / "api" / "rust"
    if not rust_dir.is_dir():
        print("  [skip] rust/ not found")
        return
    print("  building rustdoc …")
    try:
        r = subprocess.run(["cargo", "doc", "--no-deps", "--workspace"], cwd=rust_dir, check=False)
    except FileNotFoundError:
        print("  [skip] cargo not found")
        return
    if r.returncode != 0:
        print("  [warn] cargo doc failed")
        return
    doc_src = rust_dir / "target" / "doc"
    if doc_src.is_dir():
        shutil.copytree(doc_src, out_dir, dirs_exist_ok=True)
        print(f"  rustdoc  → site/api/rust/")
    index_script = ROOT / "scripts" / "rustdoc_index.py"
    if index_script.exists():
        subprocess.run([sys.executable, str(index_script), str(out_dir)], check=False)


def build_jsdocs(site_dir: Path = SITE) -> None:
    frontend_dir = ROOT / "frontend"
    out_dir = site_dir / "api" / "js"
    if not frontend_dir.is_dir():
        print("  [skip] frontend/ not found")
        return
    print("  building typedoc …")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["bun", "run", "docs"],
            cwd=frontend_dir, check=False,
            env={**os.environ, "FORCE_COLOR": "0"},
        )
    except FileNotFoundError:
        print("  [skip] bun not found")
        return
    if r.returncode != 0:
        print("  [warn] typedoc failed")
    else:
        print(f"  typedoc  → site/api/js/")


# ── Watcher ────────────────────────────────────────────────────────────────────

WATCH_PATTERNS = {".py", ".md", ".html", ".css", ".json", ".yaml", ".yml"}
WATCH_IGNORE   = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
WATCH_DIRS     = [DOCS, ROOT / "agentflow"]


def _start_watcher() -> None:
    """Watch core source + docs/ and rebuild on changes. Uses watchdog when available, falls back to polling."""
    if HAS_WATCHDOG:
        _start_watchdog_watcher()
    else:
        print("  [reload] watchdog not installed, falling back to polling (pip install watchdog)")
        t = threading.Thread(target=_poll_and_rebuild, daemon=True)
        t.start()


def _poll_and_rebuild(interval: float = 1.0) -> None:
    def _snapshot() -> dict:
        result = {}
        for d in WATCH_DIRS:
            if d.exists():
                result.update({p: p.stat().st_mtime for p in d.rglob("*") if p.is_file()})
        return result

    known = _snapshot()
    while True:
        time.sleep(interval)
        try:
            current = _snapshot()
        except OSError:
            continue
        if current != known:
            for p in current:
                if current[p] != known.get(p):
                    print(f"  [reload] {Path(p).relative_to(ROOT)}")
            known = current
            try:
                build()
            except Exception as exc:
                print(f"  [reload] build error: {exc}")


def _start_watchdog_watcher() -> None:
    class _RebuildHandler(FileSystemEventHandler):
        def __init__(self) -> None:
            super().__init__()
            self._timer: threading.Timer | None = None
            self._last = 0.0

        def _should_watch(self, path: str) -> bool:
            p = Path(path)
            if not any(p.name.endswith(ext) for ext in WATCH_PATTERNS):
                return False
            return not any(part in WATCH_IGNORE for part in p.parts)

        def _schedule(self, path: str) -> None:
            if time.time() - self._last < 2.0:
                return
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(0.5, self._rebuild, args=(path,))
            self._timer.start()

        def _rebuild(self, path: str) -> None:
            self._last = time.time()
            try:
                print(f"  [reload] {Path(path).relative_to(ROOT)}")
            except ValueError:
                print(f"  [reload] {path}")
            try:
                build()
            except Exception as exc:
                print(f"  [reload] build error: {exc}")

        def on_modified(self, event: FileSystemEvent) -> None:
            if not event.is_directory and self._should_watch(str(event.src_path)):
                self._schedule(str(event.src_path))

        def on_created(self, event: FileSystemEvent) -> None:
            if not event.is_directory and self._should_watch(str(event.src_path)):
                self._schedule(str(event.src_path))

        def on_deleted(self, event: FileSystemEvent) -> None:
            if not event.is_directory and self._should_watch(str(event.src_path)):
                self._schedule(str(event.src_path))

    handler = _RebuildHandler()
    observer = Observer()
    for d in WATCH_DIRS:
        if d.exists():
            observer.schedule(handler, str(d), recursive=True)
            print(f"  watching {d.relative_to(ROOT)}/")
    observer.daemon = True
    observer.start()


# ── Serve ──────────────────────────────────────────────────────────────────────

def serve(port: int = 8001, full: bool = False, reload: bool = False) -> None:
    build()
    if full:
        build_rust()
        build_jsdocs()

    if reload:
        _start_watcher()

    os.chdir(SITE)
    handler = http.server.SimpleHTTPRequestHandler

    class _Handler(handler):
        def log_message(self, fmt, *args):
            msg = str(args[0]) if args else ""
            if not any(x in msg for x in (".js", ".css", ".woff", ".png", ".ico", ".svg")):
                super().log_message(fmt, *args)

        def translate_path(self, path):
            # strip query string
            path = path.split("?", 1)[0].split("#", 1)[0]
            result = super().translate_path(path)
            # serve index.html for bare directory paths
            from pathlib import Path as P
            p = P(result)
            if p.is_dir():
                idx = p / "index.html"
                if idx.exists():
                    return str(idx)
            return result

    with http.server.HTTPServer(("", port), _Handler) as httpd:
        url = f"http://localhost:{port}/"
        print(f"\n  docs      → {url}")
        print(f"  guide     → {url}guide/")
        print(f"  api/rust  → {url}api/rust/")
        print(f"  api/js    → {url}api/js/")
        print(f"\nPress Ctrl-C to stop.\n")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentFlow docs builder")
    parser.add_argument("--serve", nargs="?", const=8001, type=int, metavar="PORT",
                        help="serve after building (default port 8001)")
    parser.add_argument("--full", action="store_true",
                        help="also build rustdoc and typedoc")
    parser.add_argument("--reload", action="store_true",
                        help="watch docs/ and rebuild on changes (implies --serve)")
    args = parser.parse_args()

    if args.reload:
        args.serve = args.serve or 8001

    if args.serve is not None:
        serve(args.serve, full=args.full, reload=args.reload)
    else:
        build()
        if args.full:
            build_rust()
            build_jsdocs()

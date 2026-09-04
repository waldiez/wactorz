"""Create the files the server's demo-page loader insists on reading at startup.

Nothing serves that page here, so the files are left empty rather than pulling
third-party scripts into the image to satisfy a loader. The list is read from the
loader itself, so it cannot drift from what that expects.
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path("/opt/web")


def literal(source: pathlib.Path) -> list[str]:
    """Read `_static_files` without executing the module it lives in."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "_static_files" for target in node.targets
        ):
            return [path for path, _kind in ast.literal_eval(node.value)]
    return []


if __name__ == "__main__":
    paths = literal(pathlib.Path("/opt/http_server.py"))
    if not paths:
        print("no static file list found; the server layout has changed", file=sys.stderr)
        raise SystemExit(1)
    for name in paths:
        target = ROOT / name.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    print(f"placeholders: {len(paths)}")

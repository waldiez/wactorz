#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


def update_python_version(new_version: str) -> None:
    version_file = ROOT_DIR / "wactorz" / "_version.py"
    if version_file.exists():
        content = version_file.read_text()
        new_content = re.sub(r'__version__ = ".*"', f'__version__ = "{new_version}"', content)
        version_file.write_text(new_content)
        print(f"Updated {version_file}")


def update_package_json(new_version: str) -> None:
    files = [ROOT_DIR / "frontend" / "package.json"]
    for package_file in files:
        if package_file.exists():
            with open(package_file) as f:
                data = json.load(f, strict=False)
            data["version"] = new_version
            with open(package_file, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            print(f"Updated {package_file}")


_ADDON_VERSION = re.compile(r'^version: "(.*)"', re.MULTILINE)


def addon_version_for(current: str, new_version: str) -> str:
    """What the add-on's version should become, given what it already is.

    The add-on may carry a fourth component the library does not have: a rebuild
    that ships no new library code — a run.sh fix, a schema entry — goes from
    0.5.3 to 0.5.3.1 and is published without tagging the repository.

    So a suffix survives a sync against the same library version, and does not
    survive a new one: 0.5.3.1 stays put when syncing to 0.5.3, and becomes
    0.5.4 when syncing to 0.5.4, because the add-on's suffix counts rebuilds of
    one library version and means nothing once that version changes.
    """
    parts = current.split(".")
    if len(parts) == 4 and ".".join(parts[:3]) == new_version:
        return current
    return new_version


def update_ha_addon_config(new_version: str) -> None:
    addon_root = ROOT_DIR / "ha-addon"
    for ha_file in (
        addon_root / "wactorz" / "config.yaml",
        addon_root / "wactorz-ultra" / "config.yaml",
    ):
        if ha_file.exists():
            content = ha_file.read_text()
            found = _ADDON_VERSION.search(content)
            current = found.group(1) if found else ""
            wanted = addon_version_for(current, new_version)
            if wanted != new_version:
                # The only case that is not a write: an add-on rebuild of this
                # same library version, whose suffix would otherwise be lost.
                print(f"Kept {ha_file} at {current} (add-on rebuild of {new_version})")
                continue
            new_content = _ADDON_VERSION.sub(f'version: "{wanted}"', content, count=1)
            ha_file.write_text(new_content)
            print(f"Updated {ha_file}")


def update_docs_landing(new_version: str) -> None:
    landing_file = ROOT_DIR / "docs" / "_landing.html"
    if landing_file.exists():
        content = landing_file.read_text(encoding="utf-8")
        # The stage word is captured and put back rather than written in:
        # hardcoding "Alpha" meant this quietly stopped matching the moment the
        # badge became Beta, so the version stayed at whatever it last said.
        new_content = re.sub(
            r'<div class="hero-badge">v\S* · (\w+)</div>',
            lambda m: f'<div class="hero-badge">v{new_version} · {m.group(1)}</div>',
            content,
        )
        landing_file.write_text(new_content, encoding="utf-8")
        print(f"Updated {landing_file}")


def update_docs_versions_json(new_version: str) -> None:
    v_file = ROOT_DIR / "docs" / "versions.json"
    if v_file.exists():
        try:
            with open(v_file) as f:
                data = json.load(f)
            # Check if version already exists
            exists = any(v.get("version") == new_version for v in data)
            if not exists:
                data.append({"version": new_version, "title": new_version})
                with open(v_file, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                print(f"Updated {v_file}")
        except Exception as e:
            print(f"Warning: Could not update {v_file}: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/sync_versions.py <new_version>")
        sys.exit(1)

    new_version = sys.argv[1]
    new_version = new_version.removeprefix("v")

    # Checked because every writer below takes this string on trust. There are
    # no options to parse, so a mistyped flag arrives here as the version and is
    # written into _version.py, package.json, both add-on manifests and the docs
    # before anything looks wrong.
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-]?\w+)?", new_version):
        print(f"Not a version: {new_version!r}")
        print("Usage: python scripts/sync_versions.py <new_version>")
        sys.exit(1)

    update_python_version(new_version)
    update_package_json(new_version)
    update_ha_addon_config(new_version)
    update_docs_landing(new_version)
    update_docs_versions_json(new_version)

    print(f"\nAll versions synced to {new_version}!")


if __name__ == "__main__":
    main()

"""What Dependabot proposes, and what it is told to leave alone.

A major release can need code changed before it can be merged. Grouped with
the routine updates, it would hold them back with it, so it comes in a PR of
its own; the MCP SDK's next major is a migration of ours and is not proposed
at all. The frontend is not asked for while Dependabot cannot read its lockfile.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))


def _entry(ecosystem: str) -> dict:
    return next(u for u in CONFIG["updates"] if u["package-ecosystem"] == ecosystem)


def test_every_update_goes_to_dev() -> None:
    assert {entry["target-branch"] for entry in CONFIG["updates"]} == {"dev"}


def test_python_majors_come_on_their_own() -> None:
    (group,) = _entry("uv")["groups"].values()

    assert group["update-types"] == ["minor", "patch"]


def test_the_mcp_sdks_next_major_is_left_for_a_change_of_our_own() -> None:
    rule = next(r for r in _entry("uv")["ignore"] if r["dependency-name"] == "mcp")

    assert rule["update-types"] == ["version-update:semver-major"]


def test_bun_is_not_asked_for_until_its_lockfile_can_be_read() -> None:
    assert all(entry["package-ecosystem"] != "bun" for entry in CONFIG["updates"])

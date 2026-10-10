"""CI and the app image use one uv, written in one place.

The image exports its locked dependencies with the uv its Dockerfile pins, and
CI checks the lockfile with the uv it installs. If the two drifted, CI would pass
a lockfile the image's uv then reads differently. So the version is written
once, in the Dockerfile's `AS uv` line, which Dependabot updates; every job sets
uv up through `.github/actions/setup-uv`, which reads it from there. A bump of
that line is then complete by itself, with nothing else to move in step.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / ".github" / "actions" / "setup-uv" / "action.yml"

#: The Dockerfile's uv stage, as the action's `sed` reads it.
UV_STAGE = re.compile(
    r"^FROM ghcr\.io/astral-sh/uv:([^@\s]+)@sha256:[0-9a-f]{64} AS uv$", re.MULTILINE
)


def _steps(workflow: Path) -> list[dict]:
    jobs = (yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}).get("jobs") or {}
    return [step for job in jobs.values() for step in job.get("steps") or []]


def test_the_dockerfile_names_a_uv_by_version_and_digest() -> None:
    found = UV_STAGE.search((ROOT / "Dockerfile").read_text(encoding="utf-8"))

    assert found is not None, "the Dockerfile's uv stage is pinned by tag and digest"


def test_the_action_reads_the_version_from_that_line() -> None:
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    read, install = action["runs"]["steps"]

    assert "Dockerfile" in read["run"]
    assert r"FROM ghcr\.io/astral-sh/uv:([^@[:space:]]+)@sha256:[0-9a-f]{64} AS uv$" in read["run"]
    assert install["uses"].startswith("astral-sh/setup-uv@")
    assert install["with"]["version"] == "${{ steps.uv.outputs.version }}"


def test_every_workflow_sets_uv_up_through_the_action() -> None:
    # A workflow that installed uv itself would name a version of its own,
    # which a Dependabot bump of the Dockerfile leaves behind.
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for step in _steps(path):
            uses = str(step.get("uses", ""))
            assert not uses.startswith("astral-sh/setup-uv@"), (
                f"{path.name} sets up uv directly: use ./.github/actions/setup-uv"
            )
            assert "UV_VERSION" not in str(step.get("with", "")), path.name
        text = path.read_text(encoding="utf-8")
        assert "UV_VERSION" not in text, f"{path.name} names a uv version of its own"


def test_dependabot_keeps_the_actions_pinned_inside_it_up_to_date() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    actions = next(u for u in config["updates"] if u["package-ecosystem"] == "github-actions")

    assert "/.github/actions/setup-uv" in actions["directories"]

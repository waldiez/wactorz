"""CI and the app image use one uv.

The image exports its locked dependencies with the uv pinned in its Dockerfile,
and every CI job installs the uv named by `UV_VERSION` in ci.yml. Dependabot
bumps the first and not the second, so the two could drift, and CI would then
check the lockfile with a different uv than the one the image is built with.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _dockerfile_uv() -> str:
    text = (ROOT / "Dockerfile").read_text()
    found = re.search(
        r"^FROM ghcr\.io/astral-sh/uv:([^@\s]+)@sha256:[0-9a-f]{64} AS uv$", text, re.MULTILINE
    )
    assert found is not None, "the Dockerfile's uv stage is pinned by tag and digest"
    return found.group(1)


def _ci_uv() -> str:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return str(workflow["env"]["UV_VERSION"])


def test_ci_installs_the_uv_the_image_is_built_with() -> None:
    assert _ci_uv() == _dockerfile_uv(), (
        "Dependabot moved one of them: set UV_VERSION in .github/workflows/ci.yml "
        "to the version in the Dockerfile's `AS uv` line"
    )

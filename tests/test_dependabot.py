"""Dependabot proposes updates to `dev`, never to `main`.

`main` takes only releases, and every change reaches it through `dev`. An entry
without `target-branch` makes Dependabot open its pull request against the
default branch, `main`, where merging it would ship an untested update.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def test_every_update_targets_dev() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))

    for entry in config["updates"]:
        assert entry.get("target-branch") == "dev", entry["package-ecosystem"]

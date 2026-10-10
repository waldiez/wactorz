"""The add-ons' base images are named in three places, which must agree.

`ha-addon/bases/Dockerfile` pins each base by digest, and is what the release
workflow builds on and Dependabot updates. Each add-on's `build.yaml` (for a
source build by Home Assistant) and its Dockerfile's `BUILD_FROM` default (for a
plain `docker build`) name the floating tag of the same line: `trixie` for
`trixie-2026.08.0`. So the bot's dated builds change the bases file alone, and
only a new line -- another Python or OS release, which a person decides --
has to reach the other two, which these tests then insist on.
"""

import re
from pathlib import Path

import pytest
import yaml

from wactorz import __version__

ROOT = Path(__file__).resolve().parent.parent
ADDONS = ("wactorz", "wactorz-ultra")
ARCHES = ("aarch64", "amd64")


def _pinned() -> dict[str, str]:
    """Each `<add-on>-<arch>` stage of the bases file, as its full reference."""
    text = (ROOT / "ha-addon" / "bases" / "Dockerfile").read_text()
    return {name: ref for ref, name in re.findall(r"^FROM (\S+) AS (\S+)$", text, re.MULTILINE)}


#: The dated suffix Home Assistant's base tags carry after their line.
_DATED = re.compile(r"-\d{4}\.\d{2}\.\d+$")


def _line(reference: str) -> str:
    """`image:tag` of a pinned reference, without its digest or its date."""
    return _DATED.sub("", reference.split("@", 1)[0])


def _build(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "build.yaml").read_text())


class TestTheBases:
    @pytest.mark.parametrize("addon", ADDONS)
    @pytest.mark.parametrize("arch", ARCHES)
    def test_each_is_pinned_by_digest(self, addon: str, arch: str) -> None:
        assert re.search(r"@sha256:[0-9a-f]{64}$", _pinned()[f"{addon}-{arch}"])

    @pytest.mark.parametrize("addon", ADDONS)
    @pytest.mark.parametrize("arch", ARCHES)
    def test_each_is_a_dated_build_of_a_line(self, addon: str, arch: str) -> None:
        reference = _pinned()[f"{addon}-{arch}"]

        assert _line(reference) != reference.split("@", 1)[0]

    @pytest.mark.parametrize("addon", ADDONS)
    @pytest.mark.parametrize("arch", ARCHES)
    def test_build_yaml_names_the_pinned_line(self, addon: str, arch: str) -> None:
        assert _build(addon)["build_from"][arch] == _line(_pinned()[f"{addon}-{arch}"])

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_dockerfile_default_names_the_pinned_line(self, addon: str) -> None:
        dockerfile = (ROOT / "ha-addon" / addon / "Dockerfile").read_text()
        default = re.search(r"^ARG BUILD_FROM=(\S+)$", dockerfile, re.MULTILINE)

        assert default is not None
        assert default.group(1) == _line(_pinned()[f"{addon}-aarch64"])

    def test_a_new_dated_build_changes_the_bases_file_alone(self) -> None:
        """What Dependabot proposes each week leaves the other two as they are."""
        pinned = _pinned()["wactorz-amd64"]
        bumped = _DATED.sub("-2099.12.1", pinned.split("@", 1)[0]) + "@sha256:" + "0" * 64

        assert _line(bumped) == _line(pinned)
        assert _line(bumped) == _build("wactorz")["build_from"]["amd64"]

    def test_a_new_line_is_one_the_others_must_follow(self) -> None:
        pinned = _pinned()["wactorz-amd64"].split("@", 1)[0]
        moved = pinned.replace("3.14-alpine3.24", "3.15-alpine3.25")

        assert _line(moved) != _build("wactorz")["build_from"]["amd64"]


class TestTheSourceRef:
    @pytest.mark.parametrize("addon", ADDONS)
    def test_build_yaml_installs_this_release(self, addon: str) -> None:
        # The Dockerfile has no default ref, so this is what a source build ships.
        assert _build(addon)["args"]["WACTORZ_REF"] == f"v{__version__}"

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_dockerfile_has_no_default_ref(self, addon: str) -> None:
        dockerfile = (ROOT / "ha-addon" / addon / "Dockerfile").read_text()

        assert re.search(r"^ARG WACTORZ_REF$", dockerfile, re.MULTILINE)

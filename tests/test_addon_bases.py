"""The add-ons' base images are named in three places, which must agree.

`ha-addon/bases/Dockerfile` pins each base by digest, and is what the release
workflow builds on and Dependabot updates. Each add-on's `build.yaml` names the
same bases for a source build by Home Assistant, and its Dockerfile's
`BUILD_FROM` default names the same base line for a plain `docker build` -- a
floating tag such as `trixie` is fine there, as long as the pinned tag is that
line (`trixie-2026.08.0`). A bump that reaches one of them and not the others
would build and test on one base and ship on another.
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


def _build(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "build.yaml").read_text())


class TestTheBases:
    @pytest.mark.parametrize("addon", ADDONS)
    @pytest.mark.parametrize("arch", ARCHES)
    def test_each_is_pinned_by_digest(self, addon: str, arch: str) -> None:
        assert re.search(r"@sha256:[0-9a-f]{64}$", _pinned()[f"{addon}-{arch}"])

    @pytest.mark.parametrize("addon", ADDONS)
    @pytest.mark.parametrize("arch", ARCHES)
    def test_build_yaml_names_the_pinned_tag(self, addon: str, arch: str) -> None:
        tag = _pinned()[f"{addon}-{arch}"].split("@", 1)[0]

        assert _build(addon)["build_from"][arch] == tag

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_dockerfile_default_is_the_pinned_base_line(self, addon: str) -> None:
        # The pinned tag itself, or the floating tag it is a dated build of.
        dockerfile = (ROOT / "ha-addon" / addon / "Dockerfile").read_text()
        default = re.search(r"^ARG BUILD_FROM=(\S+)$", dockerfile, re.MULTILINE)
        pinned = _pinned()[f"{addon}-aarch64"].split("@", 1)[0]

        assert default is not None
        assert pinned == default.group(1) or pinned.startswith(default.group(1) + "-")


class TestTheSourceRef:
    @pytest.mark.parametrize("addon", ADDONS)
    def test_build_yaml_installs_this_release(self, addon: str) -> None:
        # The Dockerfile has no default ref, so this is what a source build ships.
        assert _build(addon)["args"]["WACTORZ_REF"] == f"v{__version__}"

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_dockerfile_has_no_default_ref(self, addon: str) -> None:
        dockerfile = (ROOT / "ha-addon" / addon / "Dockerfile").read_text()

        assert re.search(r"^ARG WACTORZ_REF$", dockerfile, re.MULTILINE)

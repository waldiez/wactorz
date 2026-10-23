"""The app image comes in two flavours, built from one Dockerfile.

`default` is small: Wactorz and its integrations. `ultra` adds what vision
models and the Reachy Mini SDK need, which is gigabytes nobody running a chat
agent wants to pull. What each holds when it runs is checked by running it
(`make image-smoke`); these hold the files that build and publish them to each
other, since they name the flavours separately and would drift apart by hand.
"""

import re
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
FLAVOURS = ["default", "ultra"]

#: What an `ultra` image is for, as the system packages each needs: OpenCV and
#: PyTorch to be imported, a package with no wheel to be built, and the Reachy
#: Mini SDK to reach its robot.
ULTRA_PACKAGES = {
    "libgl1",
    "libglib2.0-0",
    "libgomp1",
    "build-essential",
    "pkg-config",
    "libcairo2-dev",
    "libgirepository1.0-dev",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-nice",
    "gstreamer1.0-libav",
    "gir1.2-gstreamer-1.0",
    "gir1.2-gst-plugins-base-1.0",
    "gir1.2-gst-plugins-bad-1.0",
}


def _base(flavour: str) -> str:
    """The stage a flavour starts from: its FROM line and what it installs."""
    found = re.search(
        rf"^FROM \S+ AS base-{flavour}\n.*?(?=^FROM )", DOCKERFILE, re.MULTILINE | re.DOTALL
    )
    assert found is not None, f"the Dockerfile has no `base-{flavour}` stage"
    return found.group(0)


def _system_packages(flavour: str) -> set[str]:
    installed = re.search(
        r"apt-get install -y --no-install-recommends(.*?)&& rm -rf", _base(flavour), re.DOTALL
    )
    assert installed is not None
    return set(installed.group(1).replace("\\", " ").split())


def _python(flavour: str) -> tuple[int, int]:
    found = re.match(r"FROM python:(\d+)\.(\d+)-slim@sha256:[0-9a-f]{64} AS", _base(flavour))
    assert found is not None, f"`base-{flavour}` is an official Python image, pinned by digest"
    return int(found.group(1)), int(found.group(2))


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


class TestTheDockerfile:
    def test_a_build_that_names_no_flavour_gets_the_small_image(self) -> None:
        assert re.search(r"^ARG FLAVOUR=default$", DOCKERFILE, re.MULTILINE)
        assert re.search(r"^FROM base-\$\{FLAVOUR\} AS app$", DOCKERFILE, re.MULTILINE)

    @pytest.mark.parametrize("flavour", FLAVOURS)
    def test_each_flavour_starts_from_a_pinned_python(self, flavour: str) -> None:
        assert _python(flavour) >= (3, 10)

    def test_the_small_image_stays_small(self) -> None:
        assert _system_packages("default") == {"curl"}

    def test_the_ultra_image_has_what_it_is_for(self) -> None:
        missing = ULTRA_PACKAGES - _system_packages("ultra")

        assert not missing, f"`base-ultra` no longer installs {sorted(missing)}"

    def test_the_ultra_image_stays_on_a_python_the_reachy_sdk_builds_on(self) -> None:
        # The SDK needs a PyGObject that does not build on a later one. Moving
        # this base is a decision about that agent, not a routine update.
        assert _python("ultra") <= (3, 13)

    def test_only_the_ultra_image_installs_the_vision_extras(self) -> None:
        install = DOCKERFILE[DOCKERFILE.index('case "$FLAVOUR" in') :]
        install = install[: install.index("esac")]
        default = re.search(r"default\) extras=\"([^\"]*)\"", install)
        ultra = re.search(r"ultra\) extras=\"([^\"]*)\"", install)
        assert default is not None
        assert ultra is not None

        assert default.group(1).split() == ["--extra", "all"]
        assert ultra.group(1).split() == ["--extra", "all", "--extra", "ml", "--extra", "vision"]

    def test_the_image_says_which_it_is(self) -> None:
        # The smoke test asks, to know whether to look for the extra packages.
        assert "ENV WACTORZ_IMAGE_FLAVOUR=${FLAVOUR}" in DOCKERFILE


class TestTheLockedPyTorch:
    """On Linux the PyPI build is the CUDA one, with every NVIDIA library behind it."""

    # Read as text: Python 3.10 has no TOML reader, and the suite runs there.
    def test_linux_takes_the_cpu_build(self) -> None:
        settings = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        assert (
            '[[tool.uv.index]]\nname = "pytorch-cpu"\n'
            'url = "https://download.pytorch.org/whl/cpu"\nexplicit = true\n'
        ) in settings, "an index only what names it comes from"
        # A source reaches only a package the project names, so both are named.
        assert '"torch>=' in settings
        assert '"torchvision>=' in settings
        for package in ("torch", "torchvision"):
            assert (
                f'{package} = [{{ index = "pytorch-cpu", marker = "sys_platform == \'linux\'" }}]'
                in settings
            )

    def test_the_lockfile_holds_no_cuda_library(self) -> None:
        locked = (ROOT / "uv.lock").read_text(encoding="utf-8")
        names = set(re.findall(r'^\[\[package\]\]\nname = "([^"]+)"', locked, re.MULTILINE))
        cuda = {
            name
            for name in names
            if name.startswith(("nvidia-cu", "nvidia-nv", "cuda-")) or name == "triton"
        }

        assert "torch" in names
        assert not cuda, f"the lockfile pulls in {sorted(cuda)}: is torch back on the PyPI build?"
        # Both, from the one place: they are built against each other, and a
        # torchvision from PyPI beside this torch fails when a model is loaded.
        for package in ("torch", "torchvision"):
            assert re.search(
                rf'^name = "{package}"\nversion = "[^"]+\+cpu"\n'
                r'source = \{ registry = "https://download\.pytorch\.org/whl/cpu" \}$',
                locked,
                re.MULTILINE,
            ), package

    def test_the_ultra_install_is_told_where_that_build_is(self) -> None:
        assert "--extra-index-url https://download.pytorch.org/whl/cpu" in DOCKERFILE
        assert "pip install --no-cache-dir --require-hashes $indexes" in DOCKERFILE


class TestTheLockedReachy:
    def test_reachy_cannot_downgrade_the_shared_starlette_below_security_fixes(self) -> None:
        locked = (ROOT / "uv.lock").read_text(encoding="utf-8")
        versions = re.findall(r'^name = "starlette"\nversion = "([^"]+)"', locked, re.MULTILINE)

        assert versions
        assert all(Version("1.3.1") <= Version(version) < Version("2") for version in versions)

    def test_reachy_is_opt_in_and_its_native_dependencies_resolve_without_building(self) -> None:
        settings = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        locked = (ROOT / "uv.lock").read_text(encoding="utf-8")
        metadata = re.search(
            r'^\[\[tool\.uv\.dependency-metadata\]\]\nname = "pygobject"\n'
            r'version = "3\.46\.0"\nrequires-dist = \["pycairo>=1\.16\.0"\]',
            settings,
            re.MULTILINE,
        )
        assert metadata is not None
        for name in ("reachy-mini", "deepgram-sdk", "pygobject", "pycairo"):
            assert re.search(rf'^name = "{name}"$', locked, re.MULTILINE), name
        assert re.search(
            r'^name = "pygobject"\nversion = "3\.46\.0"\nsource = \{[^\n]*\}\n'
            r'dependencies = \[\n    \{ name = "pycairo",',
            locked,
            re.MULTILINE | re.DOTALL,
        )
        all_extra = re.search(r"^all = \[(.*?)^\]", settings, re.MULTILINE | re.DOTALL)
        assert all_extra is not None
        assert "reachy" not in all_extra.group(1)


class TestBuildingAndPublishing:
    def test_make_builds_the_flavour_it_is_asked_for(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        assert re.search(r"^FLAVOUR \?= default$", makefile, re.MULTILINE)
        assert "docker build --build-arg FLAVOUR=$(FLAVOUR) " in makefile

    @pytest.mark.parametrize(("workflow", "job"), [("ci.yml", "image"), ("image.yml", "publish")])
    def test_both_flavours_are_built_and_checked(self, workflow: str, job: str) -> None:
        steps = _workflow(workflow)["jobs"][job]
        build = next(step for step in steps["steps"] if "build-push-action" in step.get("uses", ""))

        assert steps["strategy"]["matrix"]["flavour"] == FLAVOURS
        assert steps["strategy"]["fail-fast"] is False, (
            "one flavour failing says nothing of the other"
        )
        assert build["with"]["build-args"].strip() == "FLAVOUR=${{ matrix.flavour }}"

    def test_ci_keeps_a_build_cache_for_each(self) -> None:
        steps = _workflow("ci.yml")["jobs"]["image"]["steps"]
        build = next(step for step in steps if "build-push-action" in step.get("uses", ""))

        assert "scope=image-${{ matrix.flavour }}" in build["with"]["cache-from"]
        assert "scope=image-${{ matrix.flavour }}" in build["with"]["cache-to"]

    def test_the_ultra_image_is_published_under_names_of_its_own(self) -> None:
        steps = _workflow("image.yml")["jobs"]["publish"]["steps"]
        script = next(step for step in steps if step.get("id") == "tags")["run"]

        assert 'default) versioned="$TAG"; moving="latest" ;;' in script
        assert '*) versioned="${TAG}-${FLAVOUR}"; moving="$FLAVOUR" ;;' in script

    @pytest.mark.parametrize("compose", ["compose.yaml", "compose.dev.yaml"])
    def test_compose_builds_one_flavour_for_every_service_that_shares_the_image(
        self, compose: str
    ) -> None:
        services = yaml.safe_load((ROOT / compose).read_text(encoding="utf-8"))["services"]
        built = {
            name: service["build"]
            for name, service in services.items()
            if isinstance(service.get("build"), dict)
            and service["build"].get("dockerfile") == "Dockerfile"
        }

        assert built
        for name, build in built.items():
            assert build["args"] == {"FLAVOUR": "${WACTORZ_FLAVOUR:-default}"}, name

    def test_dependabot_leaves_the_python_release_alone(self) -> None:
        config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        docker = next(
            entry for entry in config["updates"] if entry["package-ecosystem"] == "docker"
        )
        python = next(rule for rule in docker["ignore"] if rule["dependency-name"] == "python")

        assert set(python["update-types"]) == {
            "version-update:semver-major",
            "version-update:semver-minor",
        }

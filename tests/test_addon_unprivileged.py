"""The Home Assistant add-ons run Wactorz as an ordinary user, on a read-only `/config`.

Wactorz runs code a model wrote. In the add-on that code ran as root, with Home
Assistant's configuration folder mapped writable: it could rewrite the
configuration and read what Home Assistant keeps private to root. Now the start
script keeps root for what needs it and hands over to an unprivileged user, and
the folder is mapped read-only.

Two things must survive that for an install that already works. Option values
that name a file under `/config` still point at it, so the folder stays at that
path. And a deploy target's SSH key, private to root as a key should be, still
reaches Wactorz, as a copy made for it.

What happens when the image runs is checked by running it; these hold the three
files to what that run depends on. The files are per add-on and drift apart by
hand, which is what a test can see.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]

#: The user the images create and the start script hands over to, and its id:
#: a Dockerfile's USER line names it by number.
USER = "wactorz"
USER_ID = 1000
ROOT_IDS = "0:0"


def _addon(addon: str, name: str) -> str:
    return (ROOT / "ha-addon" / addon / name).read_text(encoding="utf-8")


def _maps(addon: str) -> dict[str, dict]:
    """The add-on's mapped folders, by type."""
    entries = yaml.safe_load(_addon(addon, "config.yaml"))["map"]
    assert all(isinstance(entry, dict) for entry in entries), (
        "every folder is mapped in the Supervisor's current syntax, a mapping with a "
        "`type`, not the deprecated `name:rw` string"
    )
    return {entry["type"]: entry for entry in entries}


def _drop(addon: str) -> str:
    """The part of the start script that hands over to the unprivileged user."""
    script = _addon(addon, "run.sh")
    start = script.index("# ── Run as an unprivileged user")
    return script[start:]


@pytest.mark.parametrize("addon", ADDONS)
class TestTheMappedFolders:
    def test_home_assistants_configuration_is_read_only(self, addon: str) -> None:
        config = _maps(addon)["homeassistant_config"]

        assert config.get("read_only", True) is True

    def test_it_stays_where_option_values_already_point(self, addon: str) -> None:
        # `key: /config/ssh/rpi_kitchen` is in people's options. The current
        # folder type mounts at /homeassistant unless told otherwise.
        assert _maps(addon)["homeassistant_config"]["path"] == "/config"

    def test_the_deprecated_folder_is_not_mapped(self, addon: str) -> None:
        assert "config" not in _maps(addon)
        assert "config:rw" not in _addon(addon, "config.yaml")

    def test_the_two_the_start_script_writes_are_writable(self, addon: str) -> None:
        # It writes /share/wactorz/mosquitto-logins.yaml and the broker
        # certificate into /ssl, as root, before it hands over.
        maps = _maps(addon)

        assert maps["share"]["read_only"] is False
        assert maps["ssl"]["read_only"] is False

    def test_nothing_else_is_mapped(self, addon: str) -> None:
        assert set(_maps(addon)) == {"homeassistant_config", "share", "ssl"}


@pytest.mark.parametrize("addon", ADDONS)
class TestTheImage:
    def test_it_creates_the_user(self, addon: str) -> None:
        dockerfile = _addon(addon, "Dockerfile")

        assert re.search(rf"(adduser|useradd)\b[^\n]*(\\\n[^\n]*)*\b{USER}\b", dockerfile)

    def test_it_ends_as_root_for_the_start_script(self, addon: str) -> None:
        # The start script needs root for its first half, and drops it itself.
        users = re.findall(r"^USER\s+(\S+)", _addon(addon, "Dockerfile"), flags=re.MULTILINE)

        assert not users or users[-1] == ROOT_IDS


def test_the_ultra_images_virtualenv_is_built_by_the_user_that_writes_to_it() -> None:
    # Agents install packages into it at runtime. Handing it over after it was
    # built as root would store every file in it twice.
    dockerfile = _addon("wactorz-ultra", "Dockerfile")
    assert f"--uid {USER_ID} " in dockerfile, "the number the USER line names is that user's"
    as_user = dockerfile.index(f"USER {USER_ID}:{USER_ID}")
    back_to_root = dockerfile.index(f"USER {ROOT_IDS}")

    assert as_user < dockerfile.index('python3 -m venv "$VIRTUAL_ENV"') < back_to_root
    assert as_user < dockerfile.index("wactorz[all,ml] @ git+") < back_to_root
    assert "chown -R" not in dockerfile


@pytest.mark.parametrize("addon", ADDONS)
class TestTheHandOver:
    def test_wactorz_is_started_as_the_user_with_no_way_back(self, addon: str) -> None:
        drop = _drop(addon)

        assert f"run_as={USER}" in drop
        assert 'exec setpriv --no-new-privs s6-setuidgid "$run_as" wactorz' in drop

    def test_nothing_starts_wactorz_before_that(self, addon: str) -> None:
        script = _addon(addon, "run.sh")
        before = script[: script.index("# ── Run as an unprivileged user")]

        assert not re.search(r"^\s*exec\s+wactorz\b", before, flags=re.MULTILINE)

    def test_a_run_that_is_not_root_says_so_and_starts_anyway(self, addon: str) -> None:
        # A local run of the script outside Home Assistant, which has no user
        # to drop to and must not fail for it.
        drop = _drop(addon)

        assert "Not dropping privileges" in drop
        assert drop.rstrip().endswith("exec wactorz")

    def test_ssh_keys_are_copied_for_the_user_and_the_targets_pointed_at_the_copies(
        self, addon: str
    ) -> None:
        drop = _drop(addon)

        assert "'^DEPLOY_[A-Z0-9_]+_KEY$'" in drop
        assert f'install -m 0600 -o {USER} -g {USER} "$key_file"' in drop
        assert 'export "${key_var}=/run/wactorz/keys/${key_var}"' in drop

    def test_the_copies_are_kept_out_of_what_is_mapped_or_backed_up(self, addon: str) -> None:
        # /data is in the add-on's backups; /config and /share are Home Assistant's.
        copies = re.findall(r'"(/[^"$]*)/\$\{key_var\}"', _drop(addon))

        assert copies
        assert all(path.startswith("/run/") for path in copies)

    def test_the_secrets_and_the_brokers_folder_are_not_handed_over(self, addon: str) -> None:
        # The options and the generated key reach the app through its
        # environment; the broker's folder belongs to the broker's user.
        assert "/data/options.json | /data/api_key | /data/mosquitto) continue ;;" in _drop(addon)

    def test_runtime_installs_and_the_home_directory_go_under_the_state_directory(
        self, addon: str
    ) -> None:
        drop = _drop(addon)

        assert 'export HOME="${WACTORZ_STATE_DIR}/home"' in drop
        assert 'export PYTHONUSERBASE="${WACTORZ_STATE_DIR}/.python"' in drop


def test_both_addons_hand_over_the_same_way() -> None:
    assert _drop(ADDONS[0]) == _drop(ADDONS[1])


# ── The Home Assistant probe ───────────────────────────────────────────────────


def _probe(addon: str) -> str:
    """The lines of the start script that ask Home Assistant whether it answers."""
    script = _addon(addon, "run.sh")
    start = script.index("ha_probe=$(curl")
    end = script.index("esac", start) + len("esac")
    return script[start:end]


# Windows has a `bash` on its path that is only the launcher for a Linux
# subsystem, and fails when none is installed: finding the command says nothing
# there. The script it is a fragment of never runs on Windows either.
@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="runs the probe through bash, which Windows lacks",
)
@pytest.mark.parametrize("addon", ADDONS)
class TestSayingWhetherHomeAssistantAnswered:
    """curl prints `000` for no answer and also fails, so a fallback doubled it."""

    @staticmethod
    def _run(addon: str, printed: str, status: int) -> str:
        harness = (
            f"curl() {{ printf '%s' '{printed}'; return {status}; }}\n"
            'bashio::log.info() { echo "INFO $*"; }\n'
            'bashio::log.warning() { echo "WARNING $*"; }\n'
            "HA_TOKEN=t HA_URL=http://ha HA_MODE=custom\n" + _probe(addon)
        )
        done = subprocess.run(  # a fragment of this repository's script
            ["bash", "-c", harness],  # bash is found on PATH on purpose
            check=True,
            capture_output=True,
            text=True,
        )
        return done.stdout.strip()

    def test_no_answer_is_reported_as_unreachable(self, addon: str) -> None:
        said = self._run(addon, printed="000", status=7)

        assert said.startswith("WARNING HA unreachable")
        assert "000000" not in said

    def test_a_curl_that_printed_nothing_is_unreachable_too(self, addon: str) -> None:
        assert self._run(addon, printed="", status=6).startswith("WARNING HA unreachable")

    def test_an_answer_is_reported_as_it_came(self, addon: str) -> None:
        assert self._run(addon, printed="200", status=0).startswith("INFO HA connection OK")
        assert self._run(addon, printed="401", status=0).startswith("WARNING HA auth FAILED (401)")


@pytest.mark.parametrize("addon", ADDONS)
def test_an_install_with_no_nodes_is_not_warned_about_node_accounts(addon: str) -> None:
    # The embedded broker turns node accounts on for itself. With no deploy
    # target there is none to generate, which is not a failure to report.
    script = _addon(addon, "run.sh")
    warning = script.index('"No node accounts were generated;')
    guard = script.rindex('if [ -n "$DEPLOY_TARGETS" ]; then', 0, warning)

    assert script[guard:warning].count("\n") == 1, "the warning is the guarded statement"

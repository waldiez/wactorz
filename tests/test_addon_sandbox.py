"""The add-ons can run a sandbox node beside Wactorz, for the code the model writes.

With ``sandbox_node`` on, the start script runs a node in the same container
under a second user, and Wactorz sends model-written code there
(``WACTORZ_MODEL_CODE_NODE``). It is off by default, so an install that has not
asked for it starts exactly as before. On, it needs a broker account of its
own: without node accounts the node would be handed the add-on's, which
reaches the whole broker, so the start script refuses and says why.

The option block is run for real, with bash and with bashio stubbed out; the
rest holds the images, the options and the script to what that depends on.
What the container does when it runs is checked by running it.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from wactorz import config

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]

#: The sandbox's user and its id, one past the add-on's own.
SANDBOX_USER = "wactorz-sandbox"
SANDBOX_ID = 1001

#: Stand-ins for what the block calls: bashio's logger, and the option reader,
#: answering from the environment as `OPTION_<name>`.
STUBS = """
bashio::log.info() { echo "INFO $*" >&2; }
bashio::log.warning() { echo "WARNING $*" >&2; }
get_config_safe() { local var="OPTION_$1"; echo "${!var:-$2}"; }
"""

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="run.sh is a bash script for a Linux container",
)


def _file(addon: str, name: str) -> str:
    return (ROOT / "ha-addon" / addon / name).read_text(encoding="utf-8")


def _option_block(addon: str = "wactorz") -> str:
    match = re.search(
        r"^# ── Sandbox node ──.*?(?=^# What the embedded broker's access list)",
        _file(addon, "run.sh"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "the sandbox option block in run.sh moved or was renamed"
    return match.group(0)


def _decide(**options: str) -> subprocess.CompletedProcess[str]:
    """Run the option block as a start would, and print what it decided."""
    script = (
        f"{STUBS}\n{_option_block()}\n"
        'printf "%s|%s" "$sandbox_on" "${WACTORZ_MODEL_CODE_NODE:-}"\n'
    )
    env = {"PATH": "/usr/bin:/bin", **{f"OPTION_{k}": v for k, v in options.items()}}
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True, timeout=30
    )


@needs_bash
class TestTheOption:
    def test_off_by_default_nothing_changes(self) -> None:
        assert _decide().stdout == "false|"

    def test_on_with_node_accounts_it_sends_model_code_there(self) -> None:
        script = (
            f"{STUBS}\nWACTORZ_NODE_ACCOUNTS=true\n{_option_block()}\n"
            'printf "%s|%s" "$sandbox_on" "${WACTORZ_MODEL_CODE_NODE:-}"\n'
        )
        run = subprocess.run(
            ["bash", "-c", script],
            env={"PATH": "/usr/bin:/bin", "OPTION_sandbox_node": "true"},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        assert run.stdout == "true|sandbox"

    def test_without_node_accounts_it_refuses_and_says_why(self) -> None:
        # The add-on's own account reaches the whole broker; the sandbox must not hold it.
        script = (
            f"{STUBS}\nWACTORZ_NODE_ACCOUNTS=false\n{_option_block()}\n"
            'printf "%s|%s" "$sandbox_on" "${WACTORZ_MODEL_CODE_NODE:-}"\n'
        )
        run = subprocess.run(
            ["bash", "-c", script],
            env={"PATH": "/usr/bin:/bin", "OPTION_sandbox_node": "true"},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        assert run.stdout == "false|"
        assert "node_accounts is off" in run.stderr


@pytest.mark.parametrize("addon", ADDONS)
class TestTheFiles:
    def test_the_option_is_offered_off(self, addon: str) -> None:
        options = yaml.safe_load(_file(addon, "config.yaml"))

        assert options["options"]["sandbox_node"] is False
        assert options["schema"]["sandbox_node"] == "bool?"

    def test_the_image_has_the_sandbox_s_user(self, addon: str) -> None:
        dockerfile = _file(addon, "Dockerfile")

        assert SANDBOX_USER in dockerfile
        assert str(SANDBOX_ID) in dockerfile

    def test_the_node_starts_with_its_own_settings_alone(self, addon: str) -> None:
        # `env -i`: the add-on's environment holds the model's key and Home
        # Assistant's token, and none of it may reach the node.
        script = _file(addon, "run.sh")

        assert "env -i PATH=" in script
        assert f"setpriv --no-new-privs s6-setuidgid {SANDBOX_USER}" in script

    def test_main_s_hand_over_leaves_the_sandbox_s_directory_alone(self, addon: str) -> None:
        # Handed to the add-on's user on the next start, the node could no
        # longer write its own state.
        script = _file(addon, "run.sh")
        hand_over = script[script.index("# ── Run as an unprivileged user") :]

        assert '| "$SANDBOX_HOME") continue' in hand_over

    def test_the_node_s_name_is_not_one_that_means_main(self, addon: str) -> None:
        name = re.search(r"^SANDBOX_NAME=(\S+)$", _file(addon, "run.sh"), re.MULTILINE)

        assert name is not None
        assert name.group(1) not in config.RESERVED_NODE_NAMES


def test_the_embedded_broker_s_list_names_the_add_on_s_own_account() -> None:
    # The access list gives the whole broker to the accounts it names. Generated
    # before the add-on's account was named, it left the add-on reaching nothing
    # on its own broker as soon as any node -- the sandbox, or a deploy target --
    # had an account.
    script = _file("wactorz", "run.sh")
    named = script.index('export MQTT_USERNAME="wactorz"')
    generated = script.index("python3 -m wactorz.broker_certificates --export")

    assert named < generated


def test_both_addons_run_the_same_script() -> None:
    assert _file("wactorz", "run.sh") == _file("wactorz-ultra", "run.sh")


def test_the_variable_set_is_the_one_wactorz_reads() -> None:
    assert "WACTORZ_MODEL_CODE_NODE" in (ROOT / "wactorz" / "config.py").read_text()
    assert "export WACTORZ_MODEL_CODE_NODE=" in _file("wactorz", "run.sh")


@pytest.mark.parametrize(
    ("addon", "docs"),
    [
        ("wactorz", "ha-addon/DOCS.md"),
        ("wactorz", "ha-addon/wactorz/DOCS.md"),
        ("wactorz-ultra", "ha-addon/DOCS.md"),
        ("wactorz-ultra", "ha-addon/wactorz-ultra/DOCS.md"),
    ],
)
def test_every_option_has_a_row_in_the_docs(addon: str, docs: str) -> None:
    # The options table is where people browsing an add-on find what it can do;
    # an option missing from one copy of it is an option nobody finds.
    options = yaml.safe_load(_file(addon, "config.yaml"))["options"]
    text = (ROOT / docs).read_text(encoding="utf-8")

    assert [name for name in options if f"| `{name}` |" not in text] == []

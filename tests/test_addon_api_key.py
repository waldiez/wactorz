"""The Home Assistant add-on always runs with an API key.

The panel does not need one: ingress requests are recognised as the
Supervisor's. Everything else does, because other add-ons share the network the
ports are reachable on, published or not, and an open API spawns agents. So
`run.sh` uses the configured key, or generates one and keeps it in `/data`.

The key block is run for real, with bash and with bashio stubbed out, because
what matters is shell behaviour: whether the file is private, whether a second
start reads the same key back, and what happens when `/data` cannot be written.
"""

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]

#: Everything the add-on treats as a secret, which the Supervisor masks only
#: when the schema says `password`.
SECRETS = [
    "api_key",
    "llm_api_key",
    "mqtt_password",
    "ha_token",
    "discord_bot_token",
    "telegram_bot_token",
]

#: Stand-ins for what the block calls: bashio's logger, and the option reader,
#: answering from `CONFIGURED_KEY` so a test can set or omit it.
STUBS = """
bashio::log.info() { echo "INFO $*" >&2; }
bashio::log.warning() { echo "WARNING $*" >&2; }
get_config_safe() { echo "${CONFIGURED_KEY:-$2}"; }
"""

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="run.sh is a bash script for a Linux container",
)


def _run_sh(addon: str = "wactorz") -> str:
    return (ROOT / "ha-addon" / addon / "run.sh").read_text(encoding="utf-8")


def _key_block(addon: str = "wactorz") -> str:
    """The API key section of `run.sh`, up to the next section."""
    match = re.search(
        r"^# ── API key.*?(?=^# Other Integrations)", _run_sh(addon), re.MULTILINE | re.DOTALL
    )
    assert match is not None, "the API key block in run.sh moved or was renamed"
    return match.group(0)


def _config(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "config.yaml").read_text(encoding="utf-8"))


def _start(key_file: Path, configured: str = "") -> subprocess.CompletedProcess[str]:
    """Run the block once, as a start of the add-on would, and print the key."""
    script = f'{STUBS}\n{_key_block()}\nprintf "%s" "$API_KEY"\n'
    env = {"PATH": "/usr/bin:/bin", "API_KEY_FILE": str(key_file)}
    if configured:
        env["CONFIGURED_KEY"] = configured
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True, timeout=30
    )


@needs_bash
class TestTheKey:
    def test_a_configured_key_is_used_as_it_is(self, tmp_path: Path) -> None:
        key_file = tmp_path / "api_key"

        started = _start(key_file, configured="the-owners-own-key")

        assert started.stdout == "the-owners-own-key"
        assert not key_file.exists()

    def test_without_one_a_strong_key_is_generated(self, tmp_path: Path) -> None:
        started = _start(tmp_path / "api_key")

        assert re.fullmatch(r"[0-9a-f]{64}", started.stdout)

    def test_it_is_kept_privately(self, tmp_path: Path) -> None:
        key_file = tmp_path / "api_key"

        started = _start(key_file)

        assert key_file.read_text() == started.stdout
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    def test_the_next_start_reads_the_same_key(self, tmp_path: Path) -> None:
        # A new key each start would end every signed-in session on restart.
        key_file = tmp_path / "api_key"

        first = _start(key_file).stdout

        assert _start(key_file).stdout == first

    def test_it_never_reaches_the_log(self, tmp_path: Path) -> None:
        started = _start(tmp_path / "api_key")

        assert started.stdout not in started.stderr

    def test_an_unwritable_data_dir_still_leaves_a_key(self, tmp_path: Path) -> None:
        # Closing the ports matters more than keeping sessions across restarts.
        started = _start(tmp_path / "missing" / "api_key")

        assert re.fullmatch(r"[0-9a-f]{64}", started.stdout)
        assert "for this start only" in started.stderr


class TestTheAddonDeclaresNoExemption:
    @pytest.mark.parametrize("addon", ADDONS)
    def test_it_does_not_switch_off_the_fail_closed_check(self, addon: str) -> None:
        # With a key always set, the wide bind passes on its own. The opt-out
        # would only matter if that broke, which is exactly when it must not apply.
        assert "export WACTORZ_EXPOSED_OK" not in _run_sh(addon)

    def test_both_addons_run_the_same_block(self) -> None:
        assert _key_block(ADDONS[0]) == _key_block(ADDONS[1])


@pytest.mark.parametrize("addon", ADDONS)
@pytest.mark.parametrize("option", SECRETS)
def test_secrets_are_masked_in_the_supervisor_ui(addon: str, option: str) -> None:
    assert _config(addon)["schema"][option] == "password?"

"""The compose stacks always run the app with an API key.

Inside its container the app listens on every interface, so everything on
`wactorz-net` reaches it, and so does the network once a port is published. The
`api-key` service therefore generates a key when `.env` gives none, into a volume
the app and Prometheus both read, and the app no longer declares itself exempt
from the fail-closed check.

The service's command is run for real, with `sh`, against a temporary directory
standing in for the volume: what matters is shell behaviour, such as whether a
second run keeps the first key.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from wactorz import config

ROOT = Path(__file__).resolve().parents[1]

#: Each compose file, and the name of its app service.
APPS = {"compose.yaml": "wactorz-python", "compose.dev.yaml": "wactorz"}

KEY_PATH = "/run/wactorz/api_key"

needs_sh = pytest.mark.skipif(
    sys.platform == "win32", reason="runs through sh, which Windows lacks"
)


def _compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _generate(volume: Path, api_key: str = "") -> subprocess.CompletedProcess[str]:
    """Run the api-key service's command once, with `volume` as its volume."""
    script = _compose("compose.yaml")["services"]["api-key"]["command"][0]
    # `$$` is compose's escape for a literal `$`.
    script = script.replace("$$", "$").replace("/run/wactorz", str(volume))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "API_KEY": api_key}
    # The image's `python` is the interpreter running these tests here.
    script = script.replace("python -c", f"{sys.executable} -c")
    return subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, check=True, timeout=30
    )


@needs_sh
class TestTheGeneratedKey:
    def test_without_one_in_env_a_strong_key_is_generated(self, tmp_path: Path) -> None:
        _generate(tmp_path)

        assert re.fullmatch(r"[0-9a-f]{64}", (tmp_path / "api_key").read_text())

    def test_the_app_and_prometheus_can_both_read_it(self, tmp_path: Path) -> None:
        # They run as different users, and only they mount the volume.
        _generate(tmp_path)

        assert stat.S_IMODE((tmp_path / "api_key").stat().st_mode) == 0o644

    def test_the_next_run_keeps_it(self, tmp_path: Path) -> None:
        # A new key on every `up` would end every signed-in session.
        _generate(tmp_path)
        first = (tmp_path / "api_key").read_text()

        _generate(tmp_path)

        assert (tmp_path / "api_key").read_text() == first

    def test_a_key_in_env_means_none_is_generated(self, tmp_path: Path) -> None:
        _generate(tmp_path, api_key="from-dot-env")

        assert not (tmp_path / "api_key").exists()

    def test_it_never_reaches_the_log(self, tmp_path: Path) -> None:
        ran = _generate(tmp_path)

        assert (tmp_path / "api_key").read_text() not in ran.stdout + ran.stderr


@pytest.mark.parametrize(("name", "app"), APPS.items())
class TestTheStack:
    def test_the_key_is_made_before_the_app_starts(self, name: str, app: str) -> None:
        dependency = _compose(name)["services"][app]["depends_on"]["api-key"]
        assert dependency["condition"] == "service_completed_successfully"

    def test_the_app_reads_the_generated_key(self, name: str, app: str) -> None:
        service = _compose(name)["services"][app]
        assert service["environment"]["API_KEY_FILE"] == KEY_PATH
        assert "api-key-data:/run/wactorz:ro" in service["volumes"]

    def test_the_app_claims_no_exemption(self, name: str, app: str) -> None:
        # With a key always set the wide bind passes on its own; the opt-out
        # would only matter if the key went missing, exactly when it must not.
        assert "WACTORZ_EXPOSED_OK" not in _compose(name)["services"][app]["environment"]

    def test_prometheus_scrapes_with_the_same_key(self, name: str, app: str) -> None:
        service = _compose(name)["services"]["prometheus"]
        assert service["environment"]["API_KEY_FILE"] == KEY_PATH
        assert "api-key-data:/run/wactorz:ro" in service["volumes"]

    def test_the_key_service_reaches_nothing(self, name: str, app: str) -> None:
        assert _compose(name)["services"]["api-key"]["network_mode"] == "none"

    def test_both_stacks_generate_the_same_way(self, name: str, app: str) -> None:
        # The log hint names each file's own service; the rest must not drift.
        def logic(compose: str) -> str:
            script = _compose(compose)["services"]["api-key"]["command"][0]
            return re.sub(r'echo "\[api-key\] Generated.*"', "", script)

        assert logic(name) == logic("compose.yaml")


def test_plain_mqtt_is_published_to_this_host_only() -> None:
    # It carries the broker password in the clear; remote nodes use 8883.
    ports = _compose("compose.yaml")["services"]["mosquitto"]["ports"]
    plain = next(port for port in ports if port.endswith(":1883"))
    assert plain.startswith("${MQTT_EXTERNAL_BIND:-127.0.0.1}:")


class TestApiKeyFile:
    def test_the_variable_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "key").write_text("from-file")
        monkeypatch.setenv("API_KEY", "from-env")
        monkeypatch.setenv("API_KEY_FILE", str(tmp_path / "key"))

        assert config._api_key() == "from-env"  # pyright: ignore[reportPrivateUsage]

    def test_otherwise_the_file_is_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Trailing whitespace is trimmed: `echo key > file` adds a newline.
        (tmp_path / "key").write_text("from-file\n")
        monkeypatch.setenv("API_KEY", "")
        monkeypatch.setenv("API_KEY_FILE", str(tmp_path / "key"))

        assert config._api_key() == "from-file"  # pyright: ignore[reportPrivateUsage]

    def test_neither_means_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("API_KEY_FILE", raising=False)

        assert config._api_key() == ""  # pyright: ignore[reportPrivateUsage]

    def test_an_unreadable_file_is_named_and_counts_as_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No key on a wide bind is then refused at startup, which is the point:
        # a missing secret must not quietly leave the API open.
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.setenv("API_KEY_FILE", str(tmp_path / "absent"))

        with pytest.warns(RuntimeWarning, match="API_KEY_FILE"):
            assert config._api_key() == ""  # pyright: ignore[reportPrivateUsage]


@needs_sh
class TestPrometheusReadsTheKeyFile:
    SCRIPT = ROOT / "infra" / "prometheus" / "render-config.sh"

    def _render(self, **env: str) -> str:
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PROMETHEUS_MONITOR_MOSQUITTO": "0",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "prometheus.yml"
            subprocess.run(
                ["sh", str(self.SCRIPT), str(out)], check=True, cwd=ROOT, env={**base, **env}
            )
            return out.read_text(encoding="utf-8")

    def test_a_generated_key_is_named_not_copied(self, tmp_path: Path) -> None:
        key = tmp_path / "api_key"
        key.write_text("generated-secret")

        rendered = self._render(API_KEY_FILE=str(key))

        assert f"credentials_file: '{key}'" in rendered
        assert "generated-secret" not in rendered

    def test_a_key_from_env_still_wins(self, tmp_path: Path) -> None:
        key = tmp_path / "api_key"
        key.write_text("generated-secret")

        rendered = self._render(API_KEY="from-env", API_KEY_FILE=str(key))

        assert "credentials: 'from-env'" in rendered
        assert "credentials_file" not in rendered

    def test_a_missing_file_adds_nothing(self, tmp_path: Path) -> None:
        rendered = self._render(API_KEY_FILE=str(tmp_path / "absent"))

        assert "authorization" not in rendered

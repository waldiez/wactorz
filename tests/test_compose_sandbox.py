"""The compose stack's sandbox node: told what any node is told, and nothing of main's.

In the compose stack the sandbox node is a container, not a machine ``/deploy``
reaches, so a one-shot container writes it the ``.env`` a deployed node gets
(:mod:`wactorz.node.compose_env`). What matters is what crosses from main to it:
its own name, signing key and broker account, and the CA with TLS -- and
nothing else. The compose files are read too, so that a later edit cannot
quietly hand the sandbox main's state, main's settings, or main's network.
"""

import dataclasses
import stat
import sys
from pathlib import Path

import pytest
import yaml

from wactorz import broker_certificates
from wactorz.config import CONFIG, DeployTarget
from wactorz.core import broker_accounts, node_env, node_signing
from wactorz.node import compose_env

ROOT = Path(__file__).resolve().parents[1]
STACKS = ("compose.yaml", "compose.dev.yaml")

#: Each stack's app service, whose profiles the sandbox runs with.
APP_SERVICES = {"compose.yaml": "wactorz-python", "compose.dev.yaml": "wactorz"}


def _env(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return {key: value.strip("'") for key, value in pairs}


@pytest.fixture(name="main_config")
def main_config_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Main's settings as the compose stack gives them: its broker, with node accounts on."""
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(node_signing, "_secret", None)
    config = dataclasses.replace(
        CONFIG,
        mqtt_host="mosquitto",
        mqtt_port=1883,
        mqtt_tls="",
        mqtt_tls_ca="",
        mqtt_username="wactorz",
        mqtt_password="server-secret",
        node_accounts=True,
    )
    for module in (compose_env, node_env):
        monkeypatch.setattr(module, "CONFIG", config)


@pytest.mark.usefixtures("main_config")
class TestWhatTheSandboxIsTold:
    def test_its_own_name_broker_key_and_account(self, tmp_path: Path) -> None:
        env = _env(compose_env.write(tmp_path / "out", "mosquitto", "sandbox"))

        assert env["WACTORZ_NODE"] == "sandbox"
        assert (env["WACTORZ_BROKER"], env["WACTORZ_PORT"]) == ("mosquitto", "1883")
        assert env["WACTORZ_NODE_KEY"] == node_signing.node_key("sandbox")
        assert env["MQTT_USERNAME"] == "sandbox"
        assert env["MQTT_PASSWORD"] == broker_accounts.password("sandbox")

    def test_nothing_of_main_s_own(self, tmp_path: Path) -> None:
        text = compose_env.write(tmp_path / "out", "mosquitto", "sandbox").read_text()

        assert "server-secret" not in text
        assert "API_KEY" not in text
        assert "OPENAI" not in text

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
    def test_the_file_is_the_node_s_alone(self, tmp_path: Path) -> None:
        env = compose_env.write(tmp_path / "out", "mosquitto", "sandbox")

        assert stat.S_IMODE(env.stat().st_mode) == 0o600

    def test_with_tls_it_gets_the_ca_and_the_tls_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca = tmp_path / "state" / "mqtt_tls" / "ca.crt"
        ca.parent.mkdir(parents=True)
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        tls = dataclasses.replace(compose_env.CONFIG, mqtt_tls="1", mqtt_port=8883)
        monkeypatch.setattr(compose_env, "CONFIG", tls)
        out = tmp_path / "out"

        env = _env(compose_env.write(out, "mosquitto", "sandbox"))

        assert env["WACTORZ_PORT"] == "8883"
        assert env["MQTT_TLS"] == "1"
        assert env["MQTT_TLS_CA"] == str(out / compose_env.CA_NAME)
        assert (out / compose_env.CA_NAME).read_text() == ca.read_text()

    def test_a_missing_ca_is_a_failure_not_plain_mqtt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            compose_env, "CONFIG", dataclasses.replace(compose_env.CONFIG, mqtt_tls="1")
        )

        assert compose_env.main(["--out", str(tmp_path / "out")]) == 1

    def test_kept_on_main_the_sandbox_is_told_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A settings file left from before would start a node with no account.
        out = tmp_path / "out"
        out.mkdir()
        (out / ".env").write_text("WACTORZ_NODE=sandbox\n")
        monkeypatch.setattr(compose_env, "MODEL_CODE_NODE", "main")

        assert compose_env.main(["--out", str(out)]) == 0
        assert not (out / ".env").exists()

    def test_the_node_is_the_one_model_code_is_sent_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose_env, "MODEL_CODE_NODE", "isolated")

        assert compose_env.main(["--out", str(tmp_path / "out")]) == 0
        assert _env(tmp_path / "out" / ".env")["WACTORZ_NODE"] == "isolated"


class TestItsBrokerAccount:
    def test_the_model_code_node_gets_one_without_being_a_deploy_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(broker_certificates, "MODEL_CODE_NODE", "sandbox")
        monkeypatch.setattr(
            broker_certificates,
            "CONFIG",
            dataclasses.replace(CONFIG, deploy_targets=(DeployTarget(name="raspberrypi"),)),
        )

        assert broker_certificates.account_nodes() == ["raspberrypi", "sandbox"]

    def test_none_when_the_code_is_kept_on_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(broker_certificates, "MODEL_CODE_NODE", "main")
        monkeypatch.setattr(
            broker_certificates, "CONFIG", dataclasses.replace(CONFIG, deploy_targets=())
        )

        assert broker_certificates.account_nodes() == []

    def test_once_when_it_is_also_a_deploy_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(broker_certificates, "MODEL_CODE_NODE", "sandbox")
        monkeypatch.setattr(
            broker_certificates,
            "CONFIG",
            dataclasses.replace(CONFIG, deploy_targets=(DeployTarget(name="sandbox"),)),
        )

        assert broker_certificates.account_nodes() == ["sandbox"]

    def test_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(broker_certificates, "MODEL_CODE_NODE", "")
        monkeypatch.setattr(
            broker_certificates, "CONFIG", dataclasses.replace(CONFIG, deploy_targets=())
        )

        assert broker_certificates.account_nodes() == []


def _services(stack: str) -> dict[str, dict]:
    compose = yaml.load((ROOT / stack).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return compose["services"]


@pytest.mark.parametrize("stack", STACKS)
class TestTheContainer:
    def test_mounts_nothing_of_main_s(self, stack: str) -> None:
        sandbox = _services(stack)["wactorz-sandbox"]

        assert "env_file" not in sandbox
        assert sorted(sandbox["volumes"]) == [
            "sandbox-env:/run/wactorz-sandbox:ro",
            "sandbox-state:/app/state",
        ]

    def test_shares_a_network_with_the_broker_alone(self, stack: str) -> None:
        services = _services(stack)
        on_it = sorted(
            name
            for name, service in services.items()
            if "wactorz-sandbox-net" in (service.get("networks") or [])
        )

        assert services["wactorz-sandbox"]["networks"] == ["wactorz-sandbox-net"]
        assert on_it == ["mosquitto", "wactorz-sandbox"]

    def test_runs_as_its_own_hardened_container(self, stack: str) -> None:
        sandbox = _services(stack)["wactorz-sandbox"]

        assert sandbox["read_only"] == "true"
        assert sandbox["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in sandbox["security_opt"]
        assert "ports" not in sandbox

    def test_runs_with_the_app(self, stack: str) -> None:
        services = _services(stack)
        app = APP_SERVICES[stack]

        for name in ("wactorz-sandbox", "sandbox-env"):
            assert set(services[app]["profiles"]) <= set(services[name]["profiles"])

    def test_gets_a_broker_account_unless_the_code_is_kept_on_main(self, stack: str) -> None:
        certs = _services(stack)["mqtt-certs"]["environment"]

        assert certs["WACTORZ_MODEL_CODE_NODE"] == "${WACTORZ_MODEL_CODE_NODE:-sandbox}"

    def test_without_settings_it_waits_rather_than_retrying(self, stack: str) -> None:
        command = "\n".join(_services(stack)["wactorz-sandbox"]["command"])

        assert "exec sleep infinity" in command

    def test_its_settings_are_written_without_a_network(self, stack: str) -> None:
        # The one container that reads main's state reaches nothing.
        assert _services(stack)["sandbox-env"]["network_mode"] == "none"

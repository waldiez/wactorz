"""The brokers Wactorz provides serve TLS once it has issued them a certificate.

Three places package a broker, none able to see the others: the compose stack,
the add-ons' embedded broker, and the official Mosquitto add-on, which Wactorz
can only hand files to. What they share is the rule that keeps an existing
install working: TLS is added beside plain MQTT, never instead of it, and only
when a certificate was issued -- so an install that has none starts as it did.

Read out of the files rather than by running them: `run.sh` needs bashio and the
Supervisor, and the thing that drifts is the text itself.
"""

import re
from pathlib import Path

import pytest
import yaml

from wactorz.agents.installer_agent import tls_mode

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]


#: Each compose file, and the service its certificate step shares profiles with.
COMPOSE_FILES = {"compose.yaml": "wactorz-python", "compose.dev.yaml": "wactorz"}

#: Where each file publishes the broker's TLS port. The dev stack keeps to loopback.
TLS_PORT = {
    "compose.yaml": "${MQTT_TLS_EXTERNAL_PORT:-8883}:8883",
    "compose.dev.yaml": "127.0.0.1:8883:8883",
}


def _compose(name: str = "compose.yaml") -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _addon_config(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "config.yaml").read_text(encoding="utf-8"))


def _run_sh(addon: str) -> str:
    return (ROOT / "ha-addon" / addon / "run.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", COMPOSE_FILES)
class TestTheComposeBroker:
    def test_the_certificate_step_runs_only_with_the_app(self, name: str) -> None:
        services = _compose(name)["services"]
        assert set(services["mqtt-certs"]["profiles"]) == set(
            services[COMPOSE_FILES[name]]["profiles"]
        )

    def test_the_broker_waits_for_it_but_does_not_need_it(self, name: str) -> None:
        # Required, the default profile -- the broker alone -- would refuse to start.
        dependency = _compose(name)["services"]["mosquitto"]["depends_on"]["mqtt-certs"]
        assert dependency == {"condition": "service_completed_successfully", "required": False}

    def test_a_failed_certificate_step_does_not_fail_the_stack(self, name: str) -> None:
        script = _compose(name)["services"]["mqtt-certs"]["command"][0]
        assert "broker_certificates --export /tmp/mqtt-tls" in script
        assert "|| echo" in script

    def test_the_broker_adds_tls_only_when_a_certificate_is_there(self, name: str) -> None:
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        guard = script.index("if [ -s /wactorz-tls/broker.crt ] && [ -s /wactorz-tls/broker.key ]")
        assert guard < script.index("listener 8883") < script.index("fi\n")
        assert 'exec /usr/sbin/mosquitto -c "$$conf"' in script

    def test_plain_mqtt_stays_published_beside_tls(self, name: str) -> None:
        ports = _compose(name)["services"]["mosquitto"]["ports"]
        assert TLS_PORT[name] in ports
        assert any(port.endswith(":1883") for port in ports)

    def test_the_broker_never_sees_the_ca_key(self, name: str) -> None:
        # It reads a folder holding the export alone, not the state directory.
        mounts = _compose(name)["services"]["mosquitto"]["volumes"]
        assert "./infra/mosquitto/tls:/wactorz-tls:ro" in mounts
        assert not any("state" in mount for mount in mounts)

    def test_the_certificate_step_writes_the_folder_the_broker_reads(self, name: str) -> None:
        # One folder, so a certificate from a run on the host serves the same broker.
        certs = _compose(name)["services"]["mqtt-certs"]
        assert "./infra/mosquitto/tls:/mqtt-tls" in certs["volumes"]
        # Handed to the folder's owner, so the checkout keeps its ownership.
        assert 'chown "$$(stat -c %u:%g /mqtt-tls)"' in certs["command"][0]

    def test_the_app_leaves_writing_the_folder_to_the_certificate_step(self, name: str) -> None:
        app = _compose(name)["services"][COMPOSE_FILES[name]]
        assert app["environment"]["MQTT_TLS_EXPORT"] == ""


def test_the_certificate_folder_is_always_in_the_checkout() -> None:
    # Missing, Docker would create it as root, and a run on the host could not write it.
    assert (ROOT / "infra" / "mosquitto" / "tls" / ".gitkeep").is_file()
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "infra/mosquitto/tls/*" in ignored
    assert "!infra/mosquitto/tls/.gitkeep" in ignored


def test_both_compose_files_start_the_broker_the_same_way() -> None:
    # Copies, because a compose file cannot include another's command. The dev
    # stack is where the TLS path is tried first, so it has to be the same path.
    prod, dev = (_compose(name)["services"] for name in COMPOSE_FILES)
    assert prod["mosquitto"]["command"] == dev["mosquitto"]["command"]
    assert prod["mqtt-certs"]["command"] == dev["mqtt-certs"]["command"]
    assert prod["mqtt-certs"]["entrypoint"] == dev["mqtt-certs"]["entrypoint"]


@pytest.mark.parametrize("addon", ADDONS)
class TestTheAddons:
    def test_the_ssl_folder_is_mapped(self, addon: str) -> None:
        assert "ssl:rw" in _addon_config(addon)["map"]

    def test_the_tls_port_is_offered_but_not_published(self, addon: str) -> None:
        assert _addon_config(addon)["ports"]["8883/tcp"] is None

    def test_the_ca_option_is_offered_and_blank_by_default(self, addon: str) -> None:
        config = _addon_config(addon)
        assert config["options"]["mqtt_tls_ca"] == ""
        assert config["schema"]["mqtt_tls_ca"] == "str?"

    def test_run_sh_exports_the_ca_option_with_the_same_default(self, addon: str) -> None:
        assert re.search(
            r"^MQTT_TLS_CA=\$\(get_config_safe 'mqtt_tls_ca' ''\)\nexport MQTT_TLS_CA=\"\$\{MQTT_TLS_CA\}\"$",
            _run_sh(addon),
            flags=re.MULTILINE,
        )

    def test_the_deploy_tls_mode_offers_what_wactorz_accepts(self, addon: str) -> None:
        schema = _addon_config(addon)["schema"]["deploy_targets"][0]["broker_tls"]
        match = re.fullmatch(r"list\(([\w|]+)\)\?", schema)
        assert match, schema
        assert all(tls_mode(mode) == mode for mode in match.group(1).split("|"))

    def test_only_wactorzs_own_names_are_written_to_ssl(self, addon: str) -> None:
        # fullchain.pem and privkey.pem are the Mosquitto add-on's defaults and the
        # usual Let's Encrypt names: a certificate already there must survive.
        written = set(re.findall(r"/ssl/([\w-]+(?:\.[\w-]+)*)", _run_sh(addon)))
        assert written == {"wactorz-mqtt.crt", "wactorz-mqtt.key"}

    def test_the_embedded_broker_adds_tls_only_when_a_certificate_was_issued(
        self, addon: str
    ) -> None:
        source = _run_sh(addon)
        guard = source.index('if [ "$mqtt_tls_ready" = true ]; then\n        chown')
        assert (
            guard
            < source.index("listener 8883")
            < source.index("mosquitto -c /tmp/mosquitto.conf &")
        )

    def test_issuing_the_certificate_is_never_fatal(self, addon: str) -> None:
        source = _run_sh(addon)
        assert re.search(
            r"^if mqtt_tls_log=\$\(python3 -m wactorz\.broker_certificates ", source, re.MULTILINE
        )
        assert "set -e" not in source


def test_both_addons_ship_the_same_run_sh() -> None:
    assert _run_sh("wactorz") == _run_sh("wactorz-ultra")

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
        assert "broker_certificates --export /tmp/broker-files" in script
        assert "|| echo" in script

    def test_the_broker_adds_tls_only_when_a_certificate_is_there(self, name: str) -> None:
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        guard = script.index("if [ -s /mosquitto/config/tls/broker.crt ]")
        assert guard < script.index("listener 8883") < script.index("fi\n", guard)

    def test_the_broker_adds_the_access_list_only_when_one_is_there(self, name: str) -> None:
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        guard = script.index("if [ -s /mosquitto/config/acl ]")
        assert guard < script.index("acl_file /mosquitto/config/acl") < script.index("fi\n", guard)

    def test_the_node_accounts_are_added_to_the_brokers_own(self, name: str) -> None:
        # Appended, never replacing: the broker's own account is written first, and
        # an install with no node accounts keeps exactly that file.
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        assert "cat /wactorz-broker/node_passwd >> /mosquitto/config/passwd" in script
        assert script.index("mosquitto_passwd -b -c") < script.index(
            "cat /wactorz-broker/node_passwd"
        )

    def test_the_broker_is_told_to_read_them_again_when_they_change(self, name: str) -> None:
        # A deploy adds an account while the broker runs, and nothing outside its
        # container can signal it.
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        assert "while sleep" in script
        # Everything the broker takes from the folder is watched, so a certificate
        # arriving on its own -- which changes the config, not just its content --
        # wakes this too.
        stamp = script[script.index("stamp()") : script.index("while sleep")]
        for watched in ("node_passwd", "acl", "broker.crt", "broker.key"):
            assert f"/wactorz-broker/{watched}" in stamp
        watcher = script[script.index("while sleep") :]
        # A reload carries new accounts, but mosquitto does not pick up an acl_file
        # or a listener its config did not already name ("Listeners not valid for
        # reloading", conf.c) -- so the watcher rebuilds the config, compares it,
        # and restarts when it changed. Reloading either way would leave the nodes
        # uncontained with nothing saying so.
        assert watcher.index("write_conf /tmp/next.conf") < watcher.index("cmp -s")
        assert "kill -HUP 1" in watcher
        assert "kill -TERM 1" in watcher
        # Signalling the broker needs the capability: it runs as its own user.
        assert "KILL" in _compose(name)["services"]["mosquitto"]["cap_add"]

    def test_plain_mqtt_stays_published_beside_tls(self, name: str) -> None:
        ports = _compose(name)["services"]["mosquitto"]["ports"]
        assert TLS_PORT[name] in ports
        assert any(port.endswith(":1883") for port in ports)

    def test_the_broker_never_sees_the_ca_key(self, name: str) -> None:
        # It reads a folder holding the export alone, not the state directory.
        mounts = _compose(name)["services"]["mosquitto"]["volumes"]
        assert "./infra/mosquitto/generated:/wactorz-broker:ro" in mounts
        assert not any("state" in mount for mount in mounts)

    def test_the_certificate_step_writes_the_folder_the_broker_reads(self, name: str) -> None:
        # One folder, so a certificate from a run on the host serves the same broker.
        certs = _compose(name)["services"]["mqtt-certs"]
        assert "./infra/mosquitto/generated:/wactorz-broker" in certs["volumes"]
        # Everything it generated, not the certificate alone: the accounts too.
        assert "cp -r /tmp/broker-files/. /wactorz-broker/" in certs["command"][0]
        # Handed to the folder's owner, so the checkout keeps its ownership.
        assert 'chown "$$(stat -c %u:%g /wactorz-broker)"' in certs["command"][0]

    def test_the_rebuilt_config_stays_readable_to_the_broker(self, name: str) -> None:
        # A reload re-reads it as the user the broker dropped to, unlike the start,
        # which happens while it is still root -- and the watcher writes it under a
        # umask meant for the password file.
        script = _compose(name)["services"]["mosquitto"]["command"][-1]
        assert 'chmod 0644 "$$target"' in script

    def test_the_certificate_step_has_a_fixed_hostname(self, name: str) -> None:
        # The certificate names the host it is generated on. Left to Docker that is
        # the container's id, which changes on every run, so the certificate would be
        # reissued -- and the broker restarted -- on every `up`.
        assert _compose(name)["services"]["mqtt-certs"]["hostname"] == "mosquitto"

    def test_the_app_leaves_writing_the_folder_to_the_certificate_step(self, name: str) -> None:
        app = _compose(name)["services"][COMPOSE_FILES[name]]
        assert app["environment"]["MQTT_BROKER_DIR"] == ""


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_app_can_be_told_to_stop(name: str) -> None:
    # `init: true` runs tini as root at pid 1 while the app runs as its own user, so
    # without KILL tini cannot forward a stop signal and the container is killed after
    # the timeout -- no drained outbox, no state written on the way out.
    app = _compose(name)["services"][COMPOSE_FILES[name]]
    assert "KILL" in app["cap_add"]


def test_the_certificate_folder_is_always_in_the_checkout() -> None:
    # Missing, Docker would create it as root, and a run on the host could not write it.
    assert (ROOT / "infra" / "mosquitto" / "generated" / ".gitkeep").is_file()
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "infra/mosquitto/generated/*" in ignored
    assert "!infra/mosquitto/generated/.gitkeep" in ignored


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


class TestTheAddonsNodeAccounts:
    """Each node authenticating as itself, where the add-on's broker has those accounts."""

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_option_is_offered_and_off_by_default(self, addon: str) -> None:
        # On by default it would hand nodes accounts an external broker never had.
        config = _addon_config(addon)
        assert config["options"]["node_accounts"] is False
        assert config["schema"]["node_accounts"] == "bool?"

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_embedded_broker_turns_it_on_for_itself(self, addon: str) -> None:
        # That broker is configured here, and the setting has to be on before the
        # step that generates the accounts, because that is what generates them.
        source = _run_sh(addon)
        assert "WACTORZ_NODE_ACCOUNTS=$(get_config_safe 'node_accounts' 'false')" in source
        forced = source.index('if [ "$MOSQUITTO_EMBEDDED" = "true" ]; then')
        assert (
            forced
            < source.index("WACTORZ_NODE_ACCOUNTS=true")
            < source.index("export WACTORZ_NODE_ACCOUNTS")
        )
        assert source.index("export WACTORZ_NODE_ACCOUNTS") < source.index(
            "python3 -m wactorz.broker_certificates"
        )

    @pytest.mark.parametrize("addon", ADDONS)
    def test_it_goes_back_off_when_no_accounts_were_generated(self, addon: str) -> None:
        # A boot where generation failed would otherwise leave this broker with the
        # shared account while a deploy hands the node a derived one it has never
        # heard of: the node cannot connect, and nothing says why.
        source = _run_sh(addon)
        fallback = source.index('elif [ "$WACTORZ_NODE_ACCOUNTS" = "true" ]; then')
        assert source.index("WACTORZ_NODE_ACCOUNTS=false", fallback) > fallback
        assert source.index("export WACTORZ_NODE_ACCOUNTS", fallback) > fallback

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_embedded_broker_loads_them_before_any_listener(self, addon: str) -> None:
        # acl_file and password_file are settings for the broker, not for a listener.
        source = _run_sh(addon)
        accounts = source.index('cat "$MQTT_BROKER_FILES/node_passwd" >> /tmp/mosquitto.passwd')
        acl = source.index('echo "acl_file ${MQTT_BROKER_FILES}/acl" >> /tmp/mosquitto.conf')
        assert accounts < source.index("# TCP listener only.")
        assert acl < source.index("# TCP listener only.")
        # And the accounts go in before the password file is handed to the broker's
        # user: /tmp is sticky, where a kernel with fs.protected_regular set refuses
        # even root a write to a file owned by someone else -- the broker would then
        # not start at all.
        assert accounts < source.index("chown mosquitto:mosquitto /tmp/mosquitto.passwd")

    @pytest.mark.parametrize("addon", ADDONS)
    def test_the_official_addon_gets_a_file_to_paste(self, addon: str) -> None:
        # Its accounts are its own, and no add-on may edit another's configuration.
        source = _run_sh(addon)
        written = source.index("--logins /share/wactorz/mosquitto-logins.yaml")
        guard = source.index(
            'if [ "$WACTORZ_NODE_ACCOUNTS" = "true" ] && [ "$MOSQUITTO_EMBEDDED" != "true" ]'
        )
        assert guard < written


def test_both_addons_ship_the_same_run_sh() -> None:
    assert _run_sh("wactorz") == _run_sh("wactorz-ultra")

"""TLS for the broker connection: one rule for trusting the broker, and its certificates.

Every connection to the broker follows one rule, written in
`wactorz/core/mqtt_tls.py` and copied into the runner and the catalogue programs
that open their own connection, since those run on nodes without the package. The
first tests hold the copies to the rule. The rest cover the CA and broker
certificate an install issues itself, including a TLS handshake made with them.
"""

import ast
import datetime
import os
import ssl
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from wactorz import broker_certificates, config, remote_runner
from wactorz.config import DeployTarget
from wactorz.core import broker_tls, mqtt_tls
from wactorz.core import mqtt as core_mqtt

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ("anomaly_detector_agent.py", "timeseries_collector_agent.py")
TLS_VARIABLES = ("MQTT_TLS", "MQTT_TLS_CA", "MQTT_TLS_CHECK_HOSTNAME")


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
    for variable in TLS_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return state


@pytest.fixture(name="issued")
def issued_fixture(state: Path) -> broker_tls.BrokerFiles:
    return broker_tls.ensure(["broker.lan", "192.168.1.10"])


def _ca_subjects(context: ssl.SSLContext) -> list[Any]:
    return sorted(str(cert.get("subject")) for cert in context.get_ca_certs())


# ── The rule ───────────────────────────────────────────────────────────────────


class TestTheRule:
    def test_it_is_off_unless_turned_on(self) -> None:
        assert not mqtt_tls.tls_enabled("")
        assert not mqtt_tls.tls_enabled("off")
        assert mqtt_tls.tls_enabled(" On ")

    def test_the_generated_ca_is_trusted_without_a_hostname_check(
        self, issued: broker_tls.BrokerFiles
    ) -> None:
        context = mqtt_tls.client_context("")
        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert "Wactorz MQTT CA" in "".join(_ca_subjects(context))

    def test_a_ca_of_your_own_checks_the_hostname(self, issued: broker_tls.BrokerFiles) -> None:
        assert mqtt_tls.client_context(str(issued.ca)).check_hostname is True

    def test_the_system_store_checks_the_hostname(self) -> None:
        assert mqtt_tls.client_context("system").check_hostname is True

    def test_the_hostname_check_can_be_overridden(self, issued: broker_tls.BrokerFiles) -> None:
        assert mqtt_tls.client_context("", "1").check_hostname is True
        assert mqtt_tls.client_context(str(issued.ca), "no").check_hostname is False

    def test_a_missing_ca_refuses_rather_than_trusting_anything(self, state: Path) -> None:
        with pytest.raises(OSError):
            mqtt_tls.client_context("")


# ── The copies follow it ───────────────────────────────────────────────────────


def _catalogue_helper(module: str) -> Callable[[], dict[str, Any]]:
    """The TLS helper a catalogue program carries, executed on its own."""
    source = next(
        node.value.value
        for node in ast.parse(
            (ROOT / "wactorz" / "catalogue_agents" / module).read_text(encoding="utf-8")
        ).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "AGENT_CODE" for t in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )
    program = ast.parse(source)
    assert any(
        isinstance(node, ast.Import) and any(alias.name == "ssl" for alias in node.names)
        for node in program.body
    ), f"{module} does not import ssl at the top of its program"
    helper = next(
        node
        for node in program.body
        if isinstance(node, ast.FunctionDef) and node.name == "_mqtt_tls_kwargs"
    )
    namespace: dict[str, Any] = {"os": os, "ssl": ssl}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), module, "exec"), namespace)
    return namespace["_mqtt_tls_kwargs"]


CASES = [
    {},
    {"MQTT_TLS": "0"},
    {"MQTT_TLS": "1"},
    {"MQTT_TLS": "on", "MQTT_TLS_CA": "<ca>"},
    {"MQTT_TLS": "yes", "MQTT_TLS_CA": "<ca>", "MQTT_TLS_CHECK_HOSTNAME": "0"},
    {"MQTT_TLS": "true", "MQTT_TLS_CHECK_HOSTNAME": "1"},
    {"MQTT_TLS": "1", "MQTT_TLS_CA": "system"},
]


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: ",".join(f"{k}={v}" for k, v in case.items()) or "unset"
)
def test_every_copy_decides_as_the_rule_does(
    case: dict[str, str], issued: broker_tls.BrokerFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = {key: value.replace("<ca>", str(issued.ca)) for key, value in case.items()}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    expected = (
        mqtt_tls.client_context(
            environment.get("MQTT_TLS_CA", ""), environment.get("MQTT_TLS_CHECK_HOSTNAME", "")
        )
        if mqtt_tls.tls_enabled(environment.get("MQTT_TLS", ""))
        else None
    )
    copies = [remote_runner._tls_context()] + [
        _catalogue_helper(module)().get("tls_context") for module in CATALOGUE
    ]

    for copy in copies:
        if expected is None:
            assert copy is None
        else:
            assert copy is not None
            assert copy.check_hostname == expected.check_hostname
            assert _ca_subjects(copy) == _ca_subjects(expected)


class TestTheRunner:
    def test_it_refuses_to_start_when_its_ca_cannot_be_loaded(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_TLS", "1")
        monkeypatch.setenv("MQTT_TLS_CA", str(state / "not-deployed.crt"))
        monkeypatch.setattr("sys.argv", ["remote_runner.py", "--name", "rpi"])

        def _no_runner(**_kwargs: Any) -> None:
            raise AssertionError("the runner was built without a CA to verify the broker with")

        monkeypatch.setattr(remote_runner, "_RemoteRunner", _no_runner)

        with pytest.raises(SystemExit) as exited:
            remote_runner.main()
        # The status the node's systemd unit does not restart on.
        assert exited.value.code == 2

    def test_its_heartbeat_says_whether_it_uses_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MQTT_TLS", raising=False)
        assert remote_runner._tls_on() is False
        monkeypatch.setenv("MQTT_TLS", "yes")
        assert remote_runner._tls_on() is True


class _Recorder:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, hostname: str, port: int, **kwargs: Any) -> "_Recorder":
        self.kwargs = kwargs
        return self


@pytest.mark.real_mqtt_client
class TestTheServerFactory:
    @pytest.fixture(autouse=True)
    def _real_factory(self, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
        recorder = _Recorder()
        monkeypatch.setattr("aiomqtt.Client", recorder)
        return recorder

    def _configure(self, monkeypatch: pytest.MonkeyPatch, **tls: str) -> None:
        monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, **tls))

    def test_it_connects_as_before_when_tls_is_off(
        self, monkeypatch: pytest.MonkeyPatch, _real_factory: _Recorder
    ) -> None:
        self._configure(monkeypatch, mqtt_tls="", mqtt_tls_ca="", mqtt_tls_check_hostname="")
        core_mqtt.mqtt_client("broker", 1883)
        assert "tls_context" not in _real_factory.kwargs

    def test_it_applies_the_rule_when_tls_is_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _real_factory: _Recorder,
        issued: broker_tls.BrokerFiles,
    ) -> None:
        self._configure(monkeypatch, mqtt_tls="1", mqtt_tls_ca="", mqtt_tls_check_hostname="")
        core_mqtt.mqtt_client("broker", 8883)
        context = _real_factory.kwargs["tls_context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname is False

    def test_a_callers_own_tls_wins(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _real_factory: _Recorder,
        issued: broker_tls.BrokerFiles,
    ) -> None:
        self._configure(monkeypatch, mqtt_tls="1", mqtt_tls_ca="", mqtt_tls_check_hostname="")
        own = ssl.create_default_context()
        core_mqtt.mqtt_client("broker", 8883, tls_context=own)
        assert _real_factory.kwargs["tls_context"] is own


# ── The certificates an install issues ─────────────────────────────────────────


class TestTheCertificates:
    def test_the_broker_certificate_is_issued_by_the_ca(
        self, issued: broker_tls.BrokerFiles
    ) -> None:
        ca = x509.load_pem_x509_certificate(issued.ca.read_bytes())
        cert = x509.load_pem_x509_certificate(issued.cert.read_bytes())
        assert broker_tls._signed_by(cert, ca)
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert ExtendedKeyUsageOID.SERVER_AUTH in list(usage)

    def test_it_names_the_brokers_addresses(self, issued: broker_tls.BrokerFiles) -> None:
        names = broker_tls._names_in(x509.load_pem_x509_certificate(issued.cert.read_bytes()))
        assert {"broker.lan", "192.168.1.10", "localhost", "127.0.0.1", "core-mosquitto"} <= names

    def test_the_keys_are_readable_by_their_owner_only(
        self, issued: broker_tls.BrokerFiles
    ) -> None:
        if os.name == "nt":
            pytest.skip("POSIX permissions")
        for key in (issued.key, issued.ca.with_name(broker_tls.CA_KEY_FILE)):
            assert stat.S_IMODE(key.stat().st_mode) == 0o600

    def test_nothing_is_issued_when_nothing_changed(self, issued: broker_tls.BrokerFiles) -> None:
        before = (issued.ca.read_bytes(), issued.cert.read_bytes())
        again = broker_tls.ensure(["broker.lan", "192.168.1.10"])
        assert again.issued is False
        assert (again.ca.read_bytes(), again.cert.read_bytes()) == before

    def test_a_new_address_reissues_the_certificate_but_keeps_the_ca(
        self, issued: broker_tls.BrokerFiles
    ) -> None:
        ca = issued.ca.read_bytes()
        again = broker_tls.ensure(["broker.lan", "192.168.1.10", "10.0.0.7"])
        assert again.issued is True
        assert again.ca.read_bytes() == ca
        assert "10.0.0.7" in broker_tls._names_in(
            x509.load_pem_x509_certificate(again.cert.read_bytes())
        )

    def test_a_certificate_near_expiry_is_reissued(self, issued: broker_tls.BrokerFiles) -> None:
        later = datetime.datetime.now(datetime.timezone.utc) + broker_tls.BROKER_LIFETIME
        assert broker_tls.ensure(["broker.lan", "192.168.1.10"], now=later).issued is True

    def test_a_damaged_ca_is_an_error_not_a_new_ca(self, issued: broker_tls.BrokerFiles) -> None:
        ca = issued.ca.read_bytes()
        issued.ca.with_name(broker_tls.CA_KEY_FILE).unlink()
        with pytest.raises(broker_tls.UnreadableCAError):
            broker_tls.ensure()
        assert issued.ca.read_bytes() == ca

    def test_a_name_that_is_not_a_host_is_left_out(self, state: Path) -> None:
        files = broker_tls.ensure(["not a host/", "fine.lan"])
        names = broker_tls._names_in(x509.load_pem_x509_certificate(files.cert.read_bytes()))
        assert "fine.lan" in names
        assert not any(" " in name for name in names)

    def test_the_keys_are_elliptic_curve(self, issued: broker_tls.BrokerFiles) -> None:
        cert = x509.load_pem_x509_certificate(issued.cert.read_bytes())
        assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)

    def test_a_client_trusting_the_ca_completes_a_handshake_with_the_broker_certificate(
        self, issued: broker_tls.BrokerFiles
    ) -> None:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(issued.cert, issued.key)
        client = mqtt_tls.client_context("")
        _handshake(client, server, server_hostname="an-address-not-in-the-certificate")

    def test_a_client_trusting_another_ca_is_refused(
        self, issued: broker_tls.BrokerFiles, tmp_path: Path
    ) -> None:
        other = broker_tls.ensure(directory=tmp_path / "another-install")
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(issued.cert, issued.key)
        client = mqtt_tls.client_context(str(other.ca), "0")
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(client, server, server_hostname="broker.lan")


def _handshake(client: ssl.SSLContext, server: ssl.SSLContext, server_hostname: str) -> None:
    """Run a TLS handshake between two contexts in memory, raising if it fails."""
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_side = client.wrap_bio(client_in, client_out, server_hostname=server_hostname)
    server_side = server.wrap_bio(server_in, server_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(20):
        for name, side in (("client", client_side), ("server", server_side)):
            if done[name]:
                continue
            try:
                side.do_handshake()
                done[name] = True
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if all(done.values()):
            return
    raise AssertionError("the handshake did not finish")


class TestTheExport:
    def test_the_certificate_is_written_with_its_chain(
        self, issued: broker_tls.BrokerFiles, tmp_path: Path
    ) -> None:
        target = tmp_path / "ssl"
        broker_tls.export(issued, target, cert_name="wactorz-mqtt.crt", key_name="wactorz-mqtt.key")
        chain = (target / "wactorz-mqtt.crt").read_bytes()
        assert chain.count(b"BEGIN CERTIFICATE") == 2
        assert (target / "wactorz-mqtt.key").read_bytes() == issued.key.read_bytes()
        assert not (target / "ca.crt").exists()
        if os.name != "nt":
            assert stat.S_IMODE((target / "wactorz-mqtt.key").stat().st_mode) == 0o600

    def test_the_command_issues_and_exports(
        self, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            config,
            "CONFIG",
            replace(
                config.CONFIG,
                mqtt_host="core-mosquitto",
                deploy_targets=(DeployTarget(name="rpi", host="10.0.0.5", broker="192.168.1.20"),),
            ),
        )
        monkeypatch.setattr(broker_certificates, "CONFIG", config.CONFIG)
        out = tmp_path / "export"

        assert (
            broker_certificates.main(
                ["--name", "extra.lan", "--export", str(out), "--ca-name", "ca.crt"]
            )
            == 0
        )

        cert = x509.load_pem_x509_certificate((out / "broker.crt").read_bytes())
        assert {"192.168.1.20", "extra.lan", "core-mosquitto"} <= broker_tls._names_in(cert)
        assert (out / "ca.crt").exists()

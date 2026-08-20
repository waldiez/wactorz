"""Deploy targets: SSH credentials come from the environment, never from chat.

``/deploy <node> <host> <user> <password>`` handed the monitor a live SSH
credential over an unauthenticated chat API, echoed it into a reply that is
persisted to conversation history, and — with no host given — port-scanned the
local /24 for open SSH ports first. The installer then wrote the password into
its own kv state and connected with ``known_hosts=None``, so anything that could
answer for the target's address collected it.

Each of those is pinned separately below, because each fails silently: a
credential that reaches a log, a scan that runs, or a host key that is never
checked all look exactly like success from the caller's side.
"""

import asyncio
import dataclasses
from pathlib import Path

import asyncssh
import pytest

from wactorz import config as config_module
from wactorz.agents import installer_agent as installer_module
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.config import (
    DeployTarget,
    deploy_name_error,
    deploy_target,
    deploy_target_for_host,
)
from wactorz.web import chat

SECRET = "hunter2-should-never-appear"


@pytest.fixture
def targets(monkeypatch: pytest.MonkeyPatch):
    """Install the configured deploy targets (and any host-key settings) for a test.

    Both modules are patched: the lookup helpers read ``config.CONFIG``, while
    the installer holds its own imported binding. In production they are the
    same object — here they have to be kept in step by hand.
    """

    def _install(*entries: DeployTarget, **overrides) -> tuple[DeployTarget, ...]:
        patched = dataclasses.replace(config_module.CONFIG, deploy_targets=entries, **overrides)
        monkeypatch.setattr(config_module, "CONFIG", patched)
        monkeypatch.setattr(installer_module, "CONFIG", patched)
        return entries

    return _install


# ── Parsing ────────────────────────────────────────────────────────────────


def test_targets_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_TARGETS", "rpi-kitchen, rpi-garage")
    monkeypatch.setenv("DEPLOY_RPI_KITCHEN_HOST", "192.168.1.50")
    monkeypatch.setenv("DEPLOY_RPI_KITCHEN_USER", "pi")
    monkeypatch.setenv("DEPLOY_RPI_KITCHEN_KEY", "/keys/kitchen")
    monkeypatch.setenv("DEPLOY_RPI_KITCHEN_BROKER", "192.168.1.10")
    monkeypatch.setenv("DEPLOY_RPI_GARAGE_HOST", "192.168.1.51")
    monkeypatch.setenv("DEPLOY_RPI_GARAGE_SSH_PORT", "2222")

    parsed = config_module._deploy_targets()

    assert [t.name for t in parsed] == ["rpi-kitchen", "rpi-garage"]
    assert parsed[0].host == "192.168.1.50"
    assert parsed[0].key_path == "/keys/kitchen"
    assert parsed[0].broker == "192.168.1.10"
    assert parsed[0].ssh_port == 22
    assert parsed[1].ssh_port == 2222
    # user defaults to pi, the account every Pi image ships with
    assert parsed[1].user == "pi"


def test_unset_targets_parse_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No configuration means no targets — not one target named ""."""
    monkeypatch.delenv("DEPLOY_TARGETS", raising=False)
    assert config_module._deploy_targets() == ()
    monkeypatch.setenv("DEPLOY_TARGETS", " , ,")
    assert config_module._deploy_targets() == ()


def test_target_repr_hides_the_password() -> None:
    """A target that lands in a log line or traceback must not render its secret."""
    target = DeployTarget(name="rpi", host="10.0.0.5", password=SECRET)
    assert SECRET not in repr(target)
    assert SECRET not in str(target)


def test_lookup_is_slug_insensitive(targets) -> None:
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5"))
    assert deploy_target("rpi-kitchen") is not None
    assert deploy_target("RPi Kitchen") is not None
    assert deploy_target("rpi-garage") is None
    assert deploy_target_for_host("10.0.0.5") is not None
    assert deploy_target_for_host("10.0.0.6") is None


# ── The chat surface ───────────────────────────────────────────────────────


class _Replies:
    """Collects everything a slash command sends back."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class _FakeMain:
    """A main actor that records the installer payloads it is handed."""

    def __init__(self, result: dict | None = None) -> None:
        self.payloads: list[dict] = []
        self._result = result or {"success": True}

    async def delegate_to_installer(self, payload: dict, timeout: float = 0.0) -> dict:
        self.payloads.append(payload)
        return self._result


@pytest.fixture
def fake_main(monkeypatch: pytest.MonkeyPatch) -> _FakeMain:
    main = _FakeMain()
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
    return main


async def test_credential_arguments_are_refused_and_never_echoed(
    targets, fake_main: _FakeMain
) -> None:
    """The old form must not deploy — and must not repeat the password back.

    The reply is written into the persisted conversation history, so echoing the
    arguments would leave the credential at rest even though the command failed.
    """
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5", password="configured"))
    replies = _Replies()

    handled = await chat.handle_slash(f"/deploy rpi-kitchen 10.0.0.5 pi {SECRET}", replies)

    assert handled is True
    assert fake_main.payloads == []
    assert SECRET not in replies.text
    assert "node name only" in replies.text


async def test_unknown_target_is_refused_with_the_variables_to_set(
    targets, fake_main: _FakeMain
) -> None:
    targets()
    replies = _Replies()

    await chat.slash_deploy("rpi-kitchen", replies)

    assert fake_main.payloads == []
    assert "not a configured deploy target" in replies.text
    assert "DEPLOY_RPI_KITCHEN_HOST" in replies.text


async def test_configured_target_deploys_without_credentials_in_the_payload(
    targets, fake_main: _FakeMain
) -> None:
    targets(
        DeployTarget(
            name="rpi-kitchen",
            host="10.0.0.5",
            user="ubuntu",
            password=SECRET,
            broker="10.0.0.1",
            broker_port=1884,
        )
    )
    replies = _Replies()

    await chat.slash_deploy("rpi-kitchen", replies)

    assert len(fake_main.payloads) == 1
    payload = fake_main.payloads[0]
    assert payload["host"] == "10.0.0.5"
    assert payload["node_name"] == "rpi-kitchen"
    assert payload["broker"] == "10.0.0.1"
    assert payload["port"] == 1884
    # The installer resolves auth itself; a payload is message data and may be
    # logged, persisted or forwarded over MQTT.
    assert "password" not in payload
    assert "key_path" not in payload
    assert SECRET not in replies.text


async def test_usage_lists_configured_targets(targets, fake_main: _FakeMain) -> None:
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5"))
    replies = _Replies()

    await chat.handle_slash("/deploy", replies)

    assert "rpi-kitchen" in replies.text
    assert fake_main.payloads == []


# ── The streaming copy on the main actor ───────────────────────────────────
# /deploy exists twice: the request/response handler above and this generator,
# which is what the dashboard's WebSocket actually drives. Fixing one and
# leaving the other is the failure this section exists to catch.


async def _collect(agent, command: str) -> str:
    return "\n".join([chunk async for chunk in agent._slash_deploy_stream(command)])


@pytest.fixture
def main_actor(tmp_path: Path):
    from wactorz.agents.main.actor import MainActor

    return MainActor(llm_provider=None, persistence_dir=str(tmp_path))


async def test_stream_refuses_credential_arguments_without_echoing_them(
    main_actor, targets
) -> None:
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5", password="configured"))
    calls: list[dict] = []

    async def _delegate(payload, timeout=0.0):  # pragma: no cover - must never run
        calls.append(payload)
        return {"success": True}

    main_actor.delegate_to_installer = _delegate

    out = await _collect(main_actor, f"/deploy rpi-kitchen 10.0.0.5 pi {SECRET}")

    assert calls == []
    assert SECRET not in out
    assert "node name only" in out


async def test_stream_deploys_a_configured_target_without_credentials(main_actor, targets) -> None:
    targets(
        DeployTarget(name="rpi-kitchen", host="10.0.0.5", user="pi", password=SECRET, broker="b")
    )
    calls: list[dict] = []

    async def _delegate(payload, timeout=0.0):
        calls.append(payload)
        return {"success": True}

    main_actor.delegate_to_installer = _delegate

    out = await _collect(main_actor, "/deploy rpi-kitchen")

    assert len(calls) == 1
    assert "password" not in calls[0] and "key_path" not in calls[0]
    assert calls[0]["host"] == "10.0.0.5"
    assert SECRET not in out


async def test_stream_refuses_an_unconfigured_target(main_actor, targets) -> None:
    targets()
    out = await _collect(main_actor, "/deploy rpi-kitchen")
    assert "not a configured deploy target" in out
    assert "DEPLOY_RPI_KITCHEN_HOST" in out


# ── The subnet scan is gone ────────────────────────────────────────────────


def test_no_subnet_scanner_remains() -> None:
    """No code path may sweep the LAN for SSH again.

    A scan is a network-visible side effect of a chat message: it tells the
    caller where every SSH server on the operator's network is, and trips
    intrusion detection on any managed network. mDNS resolution covers the
    discovery case by asking about exactly one host.
    """
    package = Path(installer_module.__file__).parent.parent
    offenders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "_scan_subnet_ssh" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_deploy_never_opens_a_connection_to_a_host_it_was_not_configured_for(
    targets, fake_main: _FakeMain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mDNS is a name lookup for one host — nothing probes a range of addresses."""
    targets(DeployTarget(name="rpi-kitchen"))
    looked_up: list[str] = []

    def _resolve(name: str) -> str:
        looked_up.append(name)
        return "10.0.0.5"

    monkeypatch.setattr(chat.socket, "gethostbyname", _resolve)

    async def _opened(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"deploy opened a raw connection: {args} {kwargs}")

    monkeypatch.setattr(asyncio, "open_connection", _opened)

    asyncio.run(chat.slash_deploy("rpi-kitchen", _Replies()))

    assert looked_up == ["rpi-kitchen.local"]


# ── Installer: credentials, and host keys ──────────────────────────────────


@pytest.fixture
def installer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> InstallerAgent:
    agent = InstallerAgent()
    store: dict = {}
    monkeypatch.setattr(agent, "recall", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(agent, "persist", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(agent, "_log_remote", lambda _message: None)
    agent._test_store = store  # type: ignore[attr-defined]
    return agent


async def test_ssh_refuses_a_host_with_no_configured_target(installer, targets) -> None:
    targets()
    with pytest.raises(PermissionError, match="No deploy target configured"):
        await installer._ssh_kwargs({"host": "10.0.0.5", "node_name": "rpi-kitchen"})


async def test_ssh_ignores_credentials_supplied_in_the_payload(
    installer, targets, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task payload is message data — an LLM or a chat line can author one.

    The configured target's credential is the only one that may be used, so a
    payload naming a *different* password must not change what is sent.
    """
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5", user="pi", password="configured"))
    monkeypatch.setattr(installer, "_known_hosts", _fake_known_hosts(tmp_path))

    kwargs = await installer._ssh_kwargs(
        {"host": "10.0.0.5", "node_name": "rpi-kitchen", "user": "root", "password": SECRET}
    )

    assert kwargs["password"] == "configured"
    assert kwargs["username"] == "pi"


async def test_the_payload_cannot_redirect_a_target_to_another_host(
    installer, targets, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Credentials are bound to the configured host, not just to the node name.

    Resolving the password from configuration is only half of it. This used to
    take the *host* from the payload, so a task naming a configured node and a
    host of its own choosing had that node's password sent straight to it —
    the credential relay this whole change exists to close, through a door left
    open beside it.
    """
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5", user="pi", password=SECRET))
    monkeypatch.setattr(installer, "_known_hosts", _fake_known_hosts(tmp_path))

    with pytest.raises(PermissionError, match="Credentials are bound"):
        await installer._ssh_kwargs({"host": "attacker.example.com", "node_name": "rpi-kitchen"})

    # The configured host is still reachable, and case/whitespace do not matter.
    kwargs = await installer._ssh_kwargs({"host": " 10.0.0.5 ", "node_name": "rpi-kitchen"})
    assert kwargs["host"] == "10.0.0.5"


async def test_a_hostless_target_is_resolved_here_not_taken_from_the_payload(
    installer, targets, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The "find it by name" case must not become a way to supply a host."""
    targets(DeployTarget(name="rpi-kitchen", password=SECRET))
    monkeypatch.setattr(installer, "_known_hosts", _fake_known_hosts(tmp_path))
    looked_up: list[str] = []

    def _resolve(name: str) -> str:
        looked_up.append(name)
        return "10.0.0.9"

    monkeypatch.setattr(installer_module.socket, "gethostbyname", _resolve)

    kwargs = await installer._ssh_kwargs(
        {"host": "attacker.example.com", "node_name": "rpi-kitchen"}
    )

    assert looked_up == ["rpi-kitchen.local"]
    assert kwargs["host"] == "10.0.0.9"


async def test_ssh_refuses_a_target_with_no_credentials(installer, targets) -> None:
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5"))
    with pytest.raises(PermissionError, match="DEPLOY_RPI_KITCHEN_KEY"):
        await installer._ssh_kwargs({"host": "10.0.0.5", "node_name": "rpi-kitchen"})


async def test_host_keys_are_always_verified(
    installer, targets, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``known_hosts`` must never be None — that accepts whatever key answers."""
    targets(DeployTarget(name="rpi-kitchen", host="10.0.0.5", key_path="/keys/kitchen"))
    monkeypatch.setattr(installer, "_known_hosts", _fake_known_hosts(tmp_path))

    kwargs = await installer._ssh_kwargs({"host": "10.0.0.5", "node_name": "rpi-kitchen"})

    assert kwargs["known_hosts"] is not None
    assert kwargs["client_keys"] == ["/keys/kitchen"]


def _fake_known_hosts(tmp_path: Path):
    async def _known_hosts(_host: str, _port: int) -> str:
        return str(tmp_path / "known_hosts")

    return _known_hosts


async def test_first_contact_learns_the_key_then_verifies_against_it(
    installer, targets, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trust on first use, as ssh(1) does — and only on the *first* use.

    Fetching the key does not authenticate, so nothing is at risk when it is
    learned. What matters is that the second connection verifies against the
    recorded entry instead of fetching again, which is what makes a changed key
    fail rather than be silently accepted.
    """
    known_hosts = tmp_path / "known_hosts"
    targets(deploy_known_hosts=str(known_hosts), deploy_strict_host_keys=False)

    key = asyncssh.generate_private_key("ssh-ed25519")
    fetches: list[str] = []

    async def _fetch(host: str, port: int):
        fetches.append(host)
        return key.convert_to_public()

    monkeypatch.setattr(asyncssh, "get_server_host_key", _fetch)

    first = await installer._known_hosts("10.0.0.5", 22)
    assert first == str(known_hosts)
    assert fetches == ["10.0.0.5"]
    assert "10.0.0.5" in known_hosts.read_text()
    # 0600 — the file is the only record of which key we trust.
    assert known_hosts.stat().st_mode & 0o077 == 0

    await installer._known_hosts("10.0.0.5", 22)
    assert fetches == ["10.0.0.5"], "a known host was re-learned instead of verified"


async def test_strict_mode_refuses_an_unknown_host(installer, targets, tmp_path: Path) -> None:
    targets(deploy_known_hosts=str(tmp_path / "known_hosts"), deploy_strict_host_keys=True)

    with pytest.raises(PermissionError, match="DEPLOY_STRICT_HOST_KEYS"):
        await installer._known_hosts("10.0.0.5", 22)


# ── Node names that cannot be MQTT topics ──────────────────────────────────
# The name is interpolated into nodes/<name>/heartbeat and nodes/<name>/spawn.
# A wildcard in it is not a parse error anywhere — the deploy succeeds, the
# runner starts, the broker then refuses every publish and every subscribe, and
# it reconnects every 3s forever while /nodes stays empty. Caught by hand once;
# pinned here so it stays caught.


@pytest.mark.parametrize("bad", ["rpi-kitchen#", "rpi+kitchen", "rpi/kitchen", "", "  "])
def test_unusable_node_names_are_rejected(bad: str) -> None:
    assert deploy_name_error(bad) is not None


@pytest.mark.parametrize("ok", ["rpi-kitchen", "rpi_kitchen", "node1", "RPi.Kitchen"])
def test_usable_node_names_are_accepted(ok: str) -> None:
    assert deploy_name_error(ok) is None


def test_the_rule_matches_what_mqtt_actually_does() -> None:
    """The rule must match the broker's behaviour, not a guess at it.

    paho is the client the runner uses, so ask it directly. The two forbidden
    characters fail in different ways, which is why both are banned:

    * ``#`` and ``+`` are the wildcards — paho raises, so the runner is refused
      on every publish and every subscribe and reconnects forever.
    * ``/`` raises nothing at all. It is the level separator, so the runner
      quietly publishes to ``nodes/rpi/kitchen/heartbeat`` while the dashboard
      watches ``nodes/rpi/kitchen`` — a node that runs, works, and is invisible.
      The silent one is the more dangerous of the two.
    """
    paho = pytest.importorskip("paho.mqtt.client")
    client = paho.Client(paho.CallbackAPIVersion.VERSION2)

    for char in "#+":
        name = f"rpi{char}kitchen"
        with pytest.raises(ValueError):
            client.publish(f"nodes/{name}/heartbeat", b"x")
        with pytest.raises(ValueError):
            client.subscribe(f"nodes/{name}/spawn")
        assert deploy_name_error(name) is not None, f"{char!r} accepted but MQTT refuses it"

    # '/' is accepted by MQTT and still wrong — it adds a topic level.
    client.publish("nodes/rpi/kitchen/heartbeat", b"x")
    assert deploy_name_error("rpi/kitchen") is not None

    # Control case: a name the rule accepts has to actually work.
    client.publish("nodes/rpi-kitchen/heartbeat", b"x")
    client.subscribe("nodes/rpi-kitchen/spawn")


async def test_deploy_refuses_an_unusable_name_before_touching_the_network(
    installer, targets, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SSH, no upload, no runner started — and no success reported."""
    targets(DeployTarget(name="rpi-kitchen#", host="10.0.0.5", password="pw"))

    async def _no_ssh(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("deploy connected despite an unusable node name")

    monkeypatch.setattr(installer_module.asyncssh, "connect", _no_ssh)

    result = await installer._node_deploy({"host": "10.0.0.5", "node_name": "rpi-kitchen#"})

    assert result["success"] is False
    assert "MQTT topic" in result["error"]


# ── Installer: host keys, against a real SSH server ────────────────────────


class _PasswordServer(asyncssh.SSHServer):
    """Accepts one password, so a connection attempt proves auth was reached."""

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == "s3cret"


async def test_a_changed_host_key_is_rejected(
    installer, targets, tmp_path: Path, unused_tcp_port: int
) -> None:
    """The MITM case, end to end against a real server.

    Learning a key on first contact is only worth anything if the *second*
    connection is checked against it. Mocks cannot show that — asyncssh decides
    during the handshake — so this drives a real server, restarts it under a
    different host key, and requires the connection to fail. With the old
    ``known_hosts=None`` it would succeed and hand over the password.
    """
    known_hosts = tmp_path / "known_hosts"
    targets(
        DeployTarget(
            name="testnode",
            host="127.0.0.1",
            user="tester",
            password="s3cret",
            ssh_port=unused_tcp_port,
        ),
        deploy_known_hosts=str(known_hosts),
        deploy_strict_host_keys=False,
    )
    payload = {"host": "127.0.0.1", "node_name": "testnode"}
    original_key = asyncssh.generate_private_key("ssh-ed25519")
    other_key = asyncssh.generate_private_key("ssh-ed25519")

    async def _serve(host_key):
        return await asyncssh.create_server(
            _PasswordServer, "127.0.0.1", unused_tcp_port, server_host_keys=[host_key]
        )

    server = await _serve(original_key)
    try:
        # First contact: the key is learned and the connection is authenticated.
        async with asyncssh.connect(**(await installer._ssh_kwargs(payload))):
            pass
        assert known_hosts.read_text().strip() != ""

        # Second contact against the same key: verified, not re-learned.
        async with asyncssh.connect(**(await installer._ssh_kwargs(payload))):
            pass
    finally:
        server.close()
        await server.wait_closed()

    server = await _serve(other_key)
    try:
        with pytest.raises(asyncssh.HostKeyNotVerifiable):
            async with asyncssh.connect(**(await installer._ssh_kwargs(payload))):
                pass  # pragma: no cover - reaching here is the failure
    finally:
        server.close()
        await server.wait_closed()


# ── Installer: nothing secret at rest ──────────────────────────────────────


def test_node_info_records_no_credentials(installer) -> None:
    installer._persist_node_info(node_name="rpi-kitchen", host="10.0.0.5", user="pi")

    stored = installer._test_store["_node_credentials"]
    assert stored == {"rpi-kitchen": {"host": "10.0.0.5", "user": "pi"}}
    assert SECRET not in repr(installer._test_store)


def test_upgrading_removes_credentials_written_by_earlier_versions(installer) -> None:
    """Nothing reads these fields now, so leaving them would strand the secret.

    A password in the kv store outlives the deploy that created it — it is in
    every backup and every state dump until something deletes it.
    """
    installer._test_store["_node_credentials"] = {
        "rpi-kitchen": {
            "host": "10.0.0.5",
            "user": "pi",
            "password": SECRET,
            "key_path": "/keys/kitchen",
        }
    }

    installer._scrub_persisted_credentials()

    stored = installer._test_store["_node_credentials"]
    assert stored == {"rpi-kitchen": {"host": "10.0.0.5", "user": "pi"}}
    assert SECRET not in repr(stored)

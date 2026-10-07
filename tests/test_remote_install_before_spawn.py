"""Installing an agent's packages on its node before the agent is sent there.

Best-effort by design: the agent goes to the node whatever the installer said,
and one that starts without its packages fails on the import, which names the
problem. What the installer answered, or why it could not be asked, is logged
beside the spawn so that failure has its cause next to it.
"""

import types
from typing import Any

import pytest

from wactorz.agents.main import spawns as spawns_module
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService

_LOGGER = "wactorz.agents.main.spawns"


class _Installer:
    name = "installer"
    actor_id = "installer-id"

    def recall(self, _key: str) -> Any:
        return None


def _main(
    *, reply: dict[str, Any] | None = None, accepts: bool = True, answers: bool = True
) -> tuple[MainActor, list[dict[str, Any]]]:
    """A MainActor with just the surface the remote install touches, and what it sent."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.actor_id = "main-id"
    main.nodes = NodeManager()
    main._known_nodes = {"rpi": {"host": "10.0.0.5"}}
    main._result_futures = {}
    sent: list[dict[str, Any]] = []

    async def _send(_actor_id: str, _kind: Any, payload: dict[str, Any]) -> bool:
        sent.append(payload)
        if not accepts:
            return False  # a full mailbox: the request was never taken
        if answers:
            future = main._result_futures[payload["_task_id"]]
            future.set_result(reply if reply is not None else {"success": True})
        return True

    setattr(main, "send", _send)
    setattr(main, "recall", lambda *_a, **_k: {})
    setattr(
        main,
        "_registry",
        types.SimpleNamespace(find_by_name=lambda n: _Installer() if n == "installer" else None),
    )
    main.spawns = SpawnService(main)
    return main, sent


async def _install(main: MainActor, packages: list[str] | None = None) -> None:
    await main.spawns._install_packages("rpi", "scraper", packages or ["requests"])


class TestWhatIsAsked:
    async def test_the_installer_gets_the_node_and_the_packages(self) -> None:
        main, sent = _main()

        await _install(main, ["requests", "numpy"])

        request = sent[0]
        assert request["action"] == "node_install"
        assert request["host"] == "10.0.0.5"
        assert request["node_name"] == "rpi"
        assert request["packages"] == ["requests", "numpy"]
        assert request["_reply_to"] == "main-id"

    async def test_no_credentials_travel_in_the_request(self) -> None:
        # The installer holds the node's SSH target; a message would spread it.
        main, sent = _main()
        main._known_nodes["rpi"].update({"user": "pi", "password": "hunter2"})

        await _install(main)

        assert not {"user", "password", "ssh"} & set(sent[0])


class TestWhatTheInstallerSaid:
    async def test_a_success_is_logged_as_such(self, caplog: pytest.LogCaptureFixture) -> None:
        main, _ = _main(reply={"success": True})

        with caplog.at_level("INFO", logger=_LOGGER):
            await _install(main)

        assert "Remote install OK" in caplog.text

    async def test_an_answer_without_success_is_shown_whole(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        main, _ = _main(reply={"success": False, "output": "no matching distribution"})

        with caplog.at_level("WARNING", logger=_LOGGER):
            await _install(main)

        assert "Remote install issue" in caplog.text
        assert "no matching distribution" in caplog.text

    async def test_the_installers_own_error_is_the_reason_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        main, _ = _main(reply={"error": "ssh: connection refused"})

        with caplog.at_level("WARNING", logger=_LOGGER):
            await _install(main)

        assert "Remote install issue: ssh: connection refused" in caplog.text


class TestWhenTheInstallerCannotBeAsked:
    async def test_an_installer_with_no_room_is_logged_and_the_spawn_goes_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        main, _ = _main(accepts=False)

        with caplog.at_level("WARNING", logger=_LOGGER):
            await _install(main)

        assert "Remote install not sent" in caplog.text
        assert "not taking messages" in caplog.text
        assert main._result_futures == {}

    async def test_a_slow_install_is_not_waited_for_past_its_bound(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(spawns_module, "INSTALL_TIMEOUT_S", 0.01)
        main, _ = _main(answers=False)

        with caplog.at_level("WARNING", logger=_LOGGER):
            await _install(main)

        assert "Remote install timed out" in caplog.text
        assert main._result_futures == {}

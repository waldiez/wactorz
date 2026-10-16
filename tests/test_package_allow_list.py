"""Which packages are installed without asking, and how the operator allows the rest.

A package an agent asks for is named by a model, and a plausible guess at a name
is what a typosquat is published to catch. So the installer installs without
asking only what is on the list -- what the catalogue and examples use, what it
knows by import name, ``WACTORZ_PIP_ALLOW``, and what the operator has allowed
since -- and for anything else asks the operator on the dashboard and waits.

The yes is the whole control, so it must come from the operator. A press on the
dashboard reaches the installer from the web server in the same process; an
answer on the broker topic does not count, because any agent on main can
publish there and any node can write ``agents/#``. ``/packages allow``, typed on
a channel only the operator uses, is the other way to say yes. A question
nobody answers in time is closed and the install refused. An agent being
restored is not asked about: its packages were cleared when it was placed, and
nobody may be there to answer.
"""

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz.agents import installer_agent
from wactorz.agents.catalog_agent import _build_catalog
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.delegation import MODEL_INSTALLER_ACTIONS
from wactorz.core.actor import Message, MessageType
from wactorz.core.outbound import Answer, answer_topic
from wactorz.core.packages import (
    ALLOW_ENV,
    DEFAULT_ALLOWED,
    IMPORT_TO_PACKAGE,
    normalized,
    unlisted,
)
from wactorz.web import answers, runtime

MAIN_ID = "main-id"

#: How long a test waits for the installer to ask. Generous: it only bounds a failure.
ASKED_WITHIN_S = 10

#: Not on any list. No test runs pip, so whether it exists does not matter;
#: it is named for this project so that it reads as made up.
UNLISTED = "wactorz-unlisted-probe"


class _Registry:
    """Finds main by name, and any registered actor by id, as the real one does."""

    def __init__(self) -> None:
        self.main = MainActor.__new__(MainActor)
        self.main.name = "main"
        self.main.actor_id = MAIN_ID
        self.by_id: dict[str, Any] = {}

    def find_by_name(self, name: str) -> Any:
        return self.main if name == "main" else None

    def get(self, actor_id: str) -> Any:
        return self.by_id.get(actor_id)


class _Installer:
    """An installer whose pip and broker are recorded rather than run."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agent = InstallerAgent(persistence_dir=str(tmp_path))
        self.registry = _Registry()
        self.registry.by_id[self.agent.actor_id] = self.agent
        self.agent._registry = cast("Any", self.registry)
        self.installed: list[str] = []
        self.frames: list[tuple[str, Any]] = []
        self.asked = asyncio.Event()

        async def _publish(topic: str, payload: Any, **_: Any) -> None:
            self.frames.append((topic, payload))
            if topic.endswith("/chat") and isinstance(payload, dict) and payload.get("options"):
                self.asked.set()

        async def _pip(package: str) -> tuple[bool, str]:
            self.installed.append(package)
            return True, ""

        monkeypatch.setattr(self.agent, "_mqtt_publish", _publish)
        monkeypatch.setattr(self.agent, "_pip_install", _pip)
        monkeypatch.setattr(self.agent, "_is_installed", lambda _name: False)

    async def ask(self, action: str, sender: str = MAIN_ID, **payload: Any) -> dict[str, Any]:
        msg = Message(
            type=MessageType.TASK, sender_id=sender, payload={"action": action, **payload}
        )
        return await self.agent._handle_install(msg)

    async def until_asked(self) -> None:
        # Bounded so a missing question fails the test instead of hanging it.
        await asyncio.wait_for(self.asked.wait(), timeout=ASKED_WITHIN_S)

    def install(self, *packages: str) -> "asyncio.Task[dict[str, Any]]":
        return asyncio.ensure_future(self.ask("install", packages=list(packages), agent="scraper"))

    @property
    def question(self) -> dict[str, Any]:
        return next(p for t, p in self.frames if t.endswith("/chat") and p.get("options"))

    def press(self, option: str, question_id: str | None = None) -> Answer:
        return Answer(
            id=question_id or self.question["id"],
            agent_id=self.agent.actor_id,
            option=option,
            index=0,
            channel="dashboard",
        )


@pytest.fixture(name="installer")
def installer_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Installer:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    return _Installer(tmp_path, monkeypatch)


class TestTheList:
    def test_names_compare_as_pep_503_has_them(self) -> None:
        assert normalized("PyMuPDF") == "pymupdf"
        assert (
            normalized("Duckduckgo_Search")
            == normalized("duckduckgo.search")
            == "duckduckgo-search"
        )
        assert normalized("numpy>=2,<3") == "numpy"
        assert normalized("uvicorn[standard]") == "uvicorn"

    def test_every_package_the_catalogue_installs_is_on_it(self) -> None:
        installs = {p for recipe in _build_catalog().values() for p in recipe.get("install", [])}

        assert unlisted(sorted(installs), DEFAULT_ALLOWED) == []

    def test_every_package_known_by_its_import_name_is_on_it(self) -> None:
        assert unlisted(IMPORT_TO_PACKAGE.values(), DEFAULT_ALLOWED) == []

    def test_a_guess_at_a_name_is_not(self) -> None:
        assert unlisted([UNLISTED, "numpy"], DEFAULT_ALLOWED) == [UNLISTED]


class TestInstallingWithoutAsking:
    async def test_a_listed_package_is_installed_at_once(self, installer: _Installer) -> None:
        result = await installer.ask("install", packages=["numpy"])

        assert result["success"]
        assert installer.installed == ["numpy"]
        assert not installer.asked.is_set()

    async def test_one_named_by_its_import_is_listed_by_its_package(
        self, installer: _Installer
    ) -> None:
        await installer.ask("install", packages=["cv2"])

        assert installer.installed == ["opencv-python"]

    async def test_the_operators_variable_adds_to_the_list(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ALLOW_ENV, f"other, {UNLISTED.upper()}")

        await installer.ask("install", packages=[UNLISTED])

        assert installer.installed == [UNLISTED]


class TestAskingTheOperator:
    async def test_an_unlisted_package_waits_for_a_yes(self, installer: _Installer) -> None:
        pending = installer.install(UNLISTED)
        await installer.until_asked()

        assert not pending.done()
        assert installer.installed == []
        assert UNLISTED in installer.question["content"]
        assert "scraper" in installer.question["content"]

        await installer.agent.dashboard_answer(installer.press(installer_agent.INSTALL))
        result = await pending

        assert result["success"]
        assert installer.installed == [UNLISTED]

    async def test_a_yes_is_remembered(self, installer: _Installer) -> None:
        pending = installer.install(UNLISTED)
        await installer.until_asked()
        await installer.agent.dashboard_answer(installer.press(installer_agent.INSTALL))
        await pending
        installer.asked.clear()

        await installer.ask("install", packages=[UNLISTED])

        assert installer.installed == [UNLISTED, UNLISTED]
        assert not installer.asked.is_set()

    async def test_a_no_installs_nothing_and_says_how_to_allow_it(
        self, installer: _Installer
    ) -> None:
        pending = installer.install(UNLISTED, "numpy")
        await installer.until_asked()
        await installer.agent.dashboard_answer(installer.press(installer_agent.REFUSE))
        result = await pending

        assert installer.installed == []
        assert result["refused"] == [UNLISTED]
        assert f"/packages allow {UNLISTED}" in result["error"]

    async def test_no_answer_in_time_is_a_no(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer_agent, "APPROVAL_TIMEOUT_S", 0.01)

        result = await installer.ask("install", packages=[UNLISTED])

        assert installer.installed == []
        assert result["refused"] == [UNLISTED]

    async def test_a_question_not_answered_in_time_is_closed(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Left open, a later Install would allow the package for a spawn that
        # was already refused, and start nothing.
        monkeypatch.setattr(installer_agent, "APPROVAL_TIMEOUT_S", 0.01)
        await installer.ask("install", packages=[UNLISTED])
        question = installer.question

        closing = [p for t, p in installer.frames if t == answer_topic(installer.agent.actor_id)]
        assert [(p["id"], p["option"]) for p in closing] == [
            (question["id"], installer_agent.NO_ANSWER)
        ]

        await installer.agent.dashboard_answer(installer.press(installer_agent.INSTALL))
        assert installer.agent.recall(installer_agent.ALLOWED_KEY, []) == []

    async def test_a_yes_settles_every_question_it_answers(self, installer: _Installer) -> None:
        # Two spawns waiting for the same package: one Install lets both through.
        first = installer.install(UNLISTED)
        await installer.until_asked()
        first_question = installer.question["id"]
        installer.asked.clear()
        second = installer.install(UNLISTED)
        await installer.until_asked()

        await installer.agent.dashboard_answer(
            installer.press(installer_agent.INSTALL, question_id=first_question)
        )

        assert (await first)["success"]
        assert (await asyncio.wait_for(second, timeout=ASKED_WITHIN_S))["success"]

    async def test_a_restore_installs_without_asking(self, installer: _Installer) -> None:
        # Its packages were cleared when the agent was first placed.
        result = await installer.ask("install_restored", packages=[UNLISTED], agent="scraper")

        assert result["success"]
        assert installer.installed == [UNLISTED]
        assert not installer.asked.is_set()

    async def test_allowing_it_by_command_meanwhile_lets_it_through(
        self, installer: _Installer
    ) -> None:
        pending = installer.install(UNLISTED)
        await installer.until_asked()

        await installer.ask("packages_allow", packages=[UNLISTED])
        result = await pending

        assert result["success"]
        assert installer.installed == [UNLISTED]


class TestWhoMaySayYes:
    async def test_an_answer_on_the_broker_topic_is_not_listened_to(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribed: list[str] = []
        monkeypatch.setattr(
            installer.agent, "subscribe", lambda topic, *_a, **_k: subscribed.append(topic)
        )
        pending = installer.install(UNLISTED)
        await installer.until_asked()

        assert answer_topic(installer.agent.actor_id) not in subscribed
        pending.cancel()

    async def test_a_press_on_the_dashboard_reaches_it_in_process(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published: list[str] = []

        class _Broker:
            async def publish(self, topic: str, _payload: str, qos: int = 0) -> None:
                published.append(topic)

        monkeypatch.setattr(runtime, "registry", cast("Any", installer.registry))
        monkeypatch.setattr(runtime, "mqtt_client_ref", _Broker())
        pending = installer.install(UNLISTED)
        await installer.until_asked()

        problem = await answers.answer_from_dashboard(
            {
                "agent_id": installer.agent.actor_id,
                "id": installer.question["id"],
                "option": installer_agent.INSTALL,
            }
        )
        result = await pending

        assert problem is None
        assert result["success"]
        # Still published, so every open dashboard marks the question answered.
        assert published == [answer_topic(installer.agent.actor_id)]

    async def test_a_press_for_a_question_it_did_not_ask_changes_nothing(
        self, installer: _Installer
    ) -> None:
        await installer.agent.dashboard_answer(
            installer.press(installer_agent.INSTALL, question_id="not-asked")
        )

        assert installer.agent.recall(installer_agent.ALLOWED_KEY, []) == []

    @pytest.mark.parametrize(
        "action",
        [
            "install_restored",
            "packages_allow",
            "packages_clear",
            "packages_forget",
            "packages_list",
        ],
    )
    async def test_only_main_may_change_the_list(self, installer: _Installer, action: str) -> None:
        result = await installer.ask(action, sender="some-agent-id", packages=[UNLISTED])

        assert "only main" in result["error"]
        assert UNLISTED not in installer.agent.recall(installer_agent.ALLOWED_KEY, [])

    @pytest.mark.parametrize(
        "action",
        [
            "install_restored",
            "packages_allow",
            "packages_clear",
            "packages_forget",
            "packages_list",
        ],
    )
    def test_the_model_may_not_ask_to_change_it(self, action: str) -> None:
        assert action not in MODEL_INSTALLER_ACTIONS


class TestTheOperatorsList:
    async def test_allowed_packages_are_listed_and_can_be_forgotten(
        self, installer: _Installer
    ) -> None:
        allowing = await installer.ask("packages_allow", packages=[UNLISTED, "--index-url=x"])
        listed = await installer.ask("packages_list")

        assert allowing["refused"] == ["--index-url=x"]
        assert listed["allowed"] == [UNLISTED]
        assert "numpy" in listed["default"]

        forgetting = await installer.ask("packages_forget", packages=[UNLISTED, "never-allowed"])

        assert forgetting["forgotten"] == [UNLISTED]
        assert (await installer.ask("packages_list"))["allowed"] == []


class TestClearingASpawnForANode:
    async def test_listed_packages_are_cleared_without_asking(self, installer: _Installer) -> None:
        result = await installer.ask("packages_clear", packages=["numpy"], agent="scraper")

        assert result == {"success": True}
        assert installer.installed == []

    async def test_an_unlisted_one_is_asked_about_and_nothing_installed(
        self, installer: _Installer
    ) -> None:
        pending = asyncio.ensure_future(
            installer.ask("packages_clear", packages=[UNLISTED], agent="scraper")
        )
        await installer.until_asked()
        await installer.agent.dashboard_answer(installer.press(installer_agent.REFUSE))

        assert (await pending)["refused"] == [UNLISTED]
        assert installer.installed == []

    async def test_a_name_that_is_not_one_is_refused_without_asking(
        self, installer: _Installer
    ) -> None:
        result = await installer.ask(
            "packages_clear", packages=["--index-url=http://evil", "numpy"], agent="scraper"
        )

        assert "--index-url=http://evil" in result["error"]
        assert not installer.asked.is_set()


class TestOnANode:
    async def test_a_restore_installs_without_asking(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # As on main: cleared when it was placed, and nobody may be there to answer.
        reached: list[str] = []

        async def _ssh(_payload: dict[str, Any]) -> dict[str, Any]:
            reached.append("ssh")
            raise PermissionError("stop here")

        monkeypatch.setattr(installer.agent, "_ssh_kwargs", _ssh)
        monkeypatch.setattr(installer.agent, "_log_remote", lambda _m: None)

        await installer.ask(
            "node_install_restored", host="10.0.0.5", node_name="rpi", packages=[UNLISTED]
        )

        assert reached == ["ssh"]
        assert not installer.asked.is_set()

    def test_the_model_may_not_send_that(self) -> None:
        assert "node_install_restored" not in MODEL_INSTALLER_ACTIONS

    async def test_nor_may_an_agent_beside_main(self, installer: _Installer) -> None:
        result = await installer.ask(
            "node_install_restored", sender="some-agent-id", host="h", packages=[UNLISTED]
        )

        assert "only main" in result["error"]

    async def test_an_unlisted_package_is_refused_before_ssh(
        self, installer: _Installer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer_agent, "APPROVAL_TIMEOUT_S", 0.01)

        async def _no_ssh(_payload: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("connected to the node for a refused package")

        monkeypatch.setattr(installer.agent, "_ssh_kwargs", _no_ssh)

        result = await installer.ask(
            "node_install", host="10.0.0.5", node_name="rpi", packages=[UNLISTED]
        )

        assert result["refused"] == [UNLISTED]

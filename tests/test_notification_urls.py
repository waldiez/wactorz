"""Where a notification may be sent, and what the model is told about it.

Three sources: an address stored by `/webhook`, one written into the task, and
one set in the environment. The first two reach the model as they are, because
generated code has to contain them. The third does not: the model is given the
variable's name, and the code it writes reads the value at run time -- so the
address stays out of the prompt, out of the generated code, and out of anything
that keeps a copy of either.
"""

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.main.commands.state import manage_webhooks
from wactorz.agents.planner.pipeline import PipelineMixin
from wactorz.config import CONFIG

SECRET = "https://discord.com/api/webhooks/111/aaa"
STORED = "https://discord.com/api/webhooks/222/bbb"
IN_TASK = "https://discord.com/api/webhooks/333/ccc"


class FakeMain:
    """Stands in for the main actor, which is where stored addresses live."""

    def __init__(self, urls: dict[str, str] | None = None) -> None:
        self._urls = urls or {}

    def get_notification_urls(self) -> dict[str, Any]:
        return dict(self._urls)


def configure(monkeypatch: pytest.MonkeyPatch, webhook: str) -> None:
    """Rebind the pipeline's CONFIG to a copy carrying this address.

    Replaced rather than mutated: CONFIG is frozen so a caller cannot be handed
    something other than what the process started with, and a test is no
    exception to that.
    """
    monkeypatch.setattr(
        "wactorz.agents.planner.pipeline.CONFIG",
        dataclasses.replace(CONFIG, discord_webhook_url=webhook),
    )


@pytest.fixture(name="pipeline")
def pipeline_fixture() -> PipelineMixin:
    """A pipeline with no registry, so only the environment and task are read."""
    pipeline = PipelineMixin.__new__(PipelineMixin)
    pipeline._registry = None
    return pipeline


def section(pipeline: PipelineMixin, task: str = "notify me on discord") -> str:
    return asyncio.run(pipeline._gather_notification_urls(task))


class TestAnAddressInTheEnvironment:
    def test_the_model_is_given_the_name_and_not_the_value(
        self, pipeline: PipelineMixin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, SECRET)

        text = section(pipeline)

        assert "DISCORD_WEBHOOK_URL" in text
        assert SECRET not in text

    def test_nothing_is_claimed_when_it_is_unset(
        self, pipeline: PipelineMixin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, "")

        assert "DISCORD_WEBHOOK_URL" not in section(pipeline)


class TestWhichAddressWins:
    def test_an_address_in_the_task_is_used_instead(
        self, pipeline: PipelineMixin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, SECRET)

        text = section(pipeline, f"post this to {IN_TASK} please")

        # A one-off address written into the task is the one that turn means; a
        # deployment-wide default replacing it would send the message elsewhere.
        assert IN_TASK in text
        assert "DISCORD_WEBHOOK_URL" not in text
        assert SECRET not in text

    def test_a_stored_address_is_used_instead(
        self, pipeline: PipelineMixin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, SECRET)
        monkeypatch.setattr(
            "wactorz.agents.planner.pipeline.find_main_actor",
            lambda _registry: FakeMain({"discord": STORED}),
        )
        pipeline._registry = object()

        text = section(pipeline)

        assert STORED in text
        assert "DISCORD_WEBHOOK_URL" not in text

    def test_another_service_is_unaffected(
        self, pipeline: PipelineMixin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(monkeypatch, SECRET)
        monkeypatch.setattr(
            "wactorz.agents.planner.pipeline.find_main_actor",
            lambda _registry: FakeMain({"telegram": "https://api.telegram.org/bot9/x"}),
        )
        pipeline._registry = object()

        text = section(pipeline)

        # A stored telegram address says nothing about where discord goes.
        assert "DISCORD_WEBHOOK_URL" in text
        assert "api.telegram.org" in text


class FakeActor:
    """Only the two calls the command makes of it."""

    def __init__(self, urls: dict[str, str] | None = None) -> None:
        self.stored = dict(urls or {})

    def recall(self, key: str) -> Any:
        return self.stored.get(key)

    def persist(self, key: str, value: Any) -> None:
        self.stored[key] = value


def listing(monkeypatch: pytest.MonkeyPatch, webhook: str, stored: dict[str, str]) -> str:
    monkeypatch.setattr(
        "wactorz.agents.main.commands.state.CONFIG",
        dataclasses.replace(CONFIG, discord_webhook_url=webhook),
    )
    ctx = SimpleNamespace(actor=FakeActor({"_notification_urls": stored} if stored else {}))
    return asyncio.run(manage_webhooks(ctx, ""))  # pyright: ignore[reportArgumentType]


class TestWhatWebhookReports:
    def test_an_environment_address_is_named_not_printed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = listing(monkeypatch, SECRET, {})

        assert "environment" in text
        assert SECRET not in text

    def test_it_says_which_one_is_in_use_when_both_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = listing(monkeypatch, SECRET, {"discord": STORED})

        # The planner prefers the stored address, so reporting only the
        # environment one would describe a state that is not in effect.
        assert STORED in text
        assert "environment" in text
        assert "wins" in text
        assert SECRET not in text

    def test_nothing_configured_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "No notification URLs stored" in listing(monkeypatch, "", {})

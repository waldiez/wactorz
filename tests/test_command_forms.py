"""The ways to type each command: one record for `/help`, the API and the chat box.

A form that the registry does not route to its own command would be offered and
then answered by something else, so each one is typed here, with its arguments
filled in, and must reach the command it belongs to.
"""

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.agents.main.commands import groups, registry
from wactorz.agents.main.commands.dispatch import Command, Form, command
from wactorz.agents.main.commands.info import HELP_NOTES, help_lines
from wactorz.orchestration import (
    DIRECT_COMMANDS,
    DIRECT_FORMS,
    DIRECT_HELP,
    DirectOrchestrator,
    MainOrchestrator,
    command_forms,
)
from wactorz.web import api_commands, chat, runtime
from wactorz.web.app import build_app

#: What each placeholder is typed as when a form is tried.
_SAMPLES = {"active|shadow": "active", "--force": "--force"}


def _typed_out(usage: str) -> str:
    """``usage`` as a person would type it: optional parts dropped, arguments filled in."""
    text = re.sub(r" \[[^\]]*\]", "", usage)
    for placeholder, sample in _SAMPLES.items():
        text = text.replace(placeholder, sample)
    text = re.sub(r"<[^>]+>", "zeta", text)
    return text.replace(" …", "")


def _every_form() -> list[tuple[Command, Form]]:
    return [(described, form) for described in registry for form in described.forms]


@pytest.mark.parametrize(
    ("described", "form"), _every_form(), ids=lambda v: getattr(v, "usage", "")
)
def test_every_form_reaches_its_own_command(described: Command, form: Form) -> None:
    found = registry.find(_typed_out(form.usage))

    assert found is not None, form.usage
    assert found[0] is described, f"{form.usage} reaches {found[0].name}"


def test_every_command_is_under_a_heading_help_shows() -> None:
    for described in registry:
        assert described.group in groups.ORDER, described.name
        assert described.forms, described.name


def test_help_lists_every_form_and_the_notes() -> None:
    text = "\n".join(help_lines(registry))

    for _described, form in _every_form():
        assert form.usage in text
    for notes in HELP_NOTES.values():
        for note in notes:
            assert note.usage in text


def test_a_command_described_without_forms_has_its_name_as_one() -> None:
    @command("/thing", summary="do the thing")
    async def handler(_ctx: Any, _argument: str) -> str:
        return ""

    assert handler.command.forms == (Form("/thing", "do the thing"),)  # pyright: ignore[reportFunctionMemberAccess]


@pytest.mark.parametrize(
    ("usage", "typed"),
    [
        ("/help", "/help"),
        ("/plans approve <id>", "/plans approve"),
        ("/artifacts activate <name:version> [reason]", "/artifacts activate"),
        ("/migrate <agent> <node> [--force]", "/migrate"),
        ("/topics [keyword]", "/topics"),
    ],
)
def test_what_is_typed_before_the_first_argument(usage: str, typed: str) -> None:
    assert Form(usage, "").typed == typed


class TestTheSeam:
    def test_main_offers_its_slash_forms(self) -> None:
        forms = command_forms(MainOrchestrator(registry=None))  # pyright: ignore[reportArgumentType]

        usages = {form.usage for form in forms}
        assert "/plans approve <id>" in usages
        assert all(usage.startswith("/") for usage in usages)

    def test_the_model_free_profile_offers_its_four(self) -> None:
        forms = command_forms(DirectOrchestrator(registry=None))  # pyright: ignore[reportArgumentType]

        assert forms == list(DIRECT_FORMS)
        assert {form.usage for form in forms} == DIRECT_COMMANDS
        for form in forms:
            assert form.summary in DIRECT_HELP

    def test_an_orchestrator_without_forms_is_offered_by_name(self) -> None:
        class _Own:
            def commands(self) -> frozenset[str]:
                return frozenset({"/b", "/a"})

        assert command_forms(_Own()) == [Form("/a", ""), Form("/b", "")]  # pyright: ignore[reportArgumentType]

    def test_what_an_orchestrator_gives_that_is_not_a_form_is_left_out(self) -> None:
        class _Own:
            def commands(self) -> frozenset[str]:
                return frozenset({"/a"})

            def command_forms(self) -> list[Any]:
                return [Form("/a", "the a"), ("/b", "a tuple")]

        assert command_forms(_Own()) == [Form("/a", "the a")]  # pyright: ignore[reportArgumentType]


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


async def test_the_route_lists_what_the_orchestrator_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "orchestrator", DirectOrchestrator(registry=None))  # pyright: ignore[reportArgumentType]

    response = await client.get("/api/commands")

    assert response.status == 200
    listed = (await response.json())["commands"]
    assert listed[0] == {"usage": "/agents", "typed": "/agents", "summary": DIRECT_FORMS[0].summary}
    assert len(listed) == len(DIRECT_FORMS)


def test_without_an_orchestrator_main_answers_if_it_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "orchestrator", None)
    monkeypatch.setattr(runtime, "registry", object())
    monkeypatch.setattr(api_commands, "find_main_actor", lambda _registry: object())
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: object())

    listed = api_commands.listing()

    assert {"usage": "/plans approve <id>", "typed": "/plans approve"} in [
        {k: v for k, v in entry.items() if k != "summary"} for entry in listed
    ]


def test_with_nothing_installed_the_servers_own_listings_are_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "orchestrator", None)
    monkeypatch.setattr(runtime, "registry", None)

    assert [entry["usage"] for entry in api_commands.listing()] == ["/agents", "/nodes"]


class _Own:
    """An orchestrator of someone's that answers one command, and describes none."""

    def commands(self) -> frozenset[str]:
        return frozenset({"/report"})


def test_beside_an_orchestrator_of_your_own_what_the_server_answers_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "orchestrator", _Own())
    monkeypatch.setattr(runtime, "registry", object())
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: object())

    usages = [entry["usage"] for entry in api_commands.listing()]

    assert usages[0] == "/report"
    assert "/agents" in usages, "the server lists the agents: the orchestrator does not claim it"
    assert {"/deploy <node>", "/clear-plans"} <= set(usages), "handed to main by the server"
    assert any(usage.startswith("/migrate") for usage in usages)
    assert "/nodes" not in usages, "main is there, and the server's one-node listing would be wrong"

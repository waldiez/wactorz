"""Tests for the social-channel custom-spawn gate.

Discord/Telegram call ``MainActor.process_user_input(..., allow_spawn=False)``
because those channels have no monitor dashboard to stop or delete a misbehaving
agent. The enforcement point is ``MainActor._process_spawn_commands``: it must
refuse genuinely custom (LLM-authored) ``<spawn>`` blocks on such channels while
still letting curated catalog recipes through the catalog-dedup path.

These drive the real ``_process_spawn_commands`` against a lightweight fake host
so no LLM, broker, or registry backend is needed. Async methods run via
``asyncio.run`` — no pytest-asyncio required.
"""

import asyncio

from wactorz.agents.main_actor import SOCIAL_SPAWN_REFUSAL, MainActor


def run(coro):
    return asyncio.run(coro)


class FakeActor:
    def __init__(self, name):
        self.name = name
        self.actor_id = f"id-{name}"


class FakeRegistry:
    def __init__(self, agents=None):
        self._by_name = dict(agents or {})

    def find_by_name(self, name):
        return self._by_name.get(name)


class FakeMain:
    """Just enough of MainActor's surface for ``_process_spawn_commands``."""

    def __init__(self, *, registry=None, catalog_match=None):
        self.name = "main"
        self._registry = registry or FakeRegistry()
        self._catalog_match = catalog_match
        self.spawn_calls = []

    def _match_catalog_recipe(self, name):
        return self._catalog_match

    async def _spawn_from_config(self, config, save=True):
        self.spawn_calls.append(config)
        actor = FakeActor(config.get("name", "anon"))
        self._registry._by_name[actor.name] = actor
        return actor


CUSTOM_BLOCK = (
    'Sure, here you go!\n<spawn>{"name": "pinger", "type": "llm", '
    '"system_prompt": "ping google every minute"}</spawn>'
)


def test_custom_spawn_blocked_on_social_channel():
    main = FakeMain()  # no catalog match → genuinely custom
    clean, spawned = run(
        MainActor._process_spawn_commands(main, CUSTOM_BLOCK, allow_spawn=False)
    )
    assert spawned == []
    assert main.spawn_calls == []  # nothing was actually created
    assert clean == SOCIAL_SPAWN_REFUSAL


def test_custom_spawn_allowed_by_default():
    main = FakeMain()
    clean, spawned = run(MainActor._process_spawn_commands(main, CUSTOM_BLOCK))
    assert len(spawned) == 1
    assert len(main.spawn_calls) == 1
    assert clean != SOCIAL_SPAWN_REFUSAL


def test_catalog_recipe_still_allowed_on_social_channel():
    # A running catalog agent covers the requested name → dedup path returns it,
    # never reaching the custom-spawn gate. Must stay allowed even with no-spawn.
    running = FakeActor("smart-energy")
    main = FakeMain(
        registry=FakeRegistry({"smart-energy": running}),
        catalog_match="smart-energy",
    )
    block = '<spawn>{"name": "smart energy agent", "type": "llm", "system_prompt": "x"}</spawn>'
    clean, spawned = run(
        MainActor._process_spawn_commands(main, block, allow_spawn=False)
    )
    assert spawned == [running]
    assert main.spawn_calls == []  # served by catalog, not a custom spawn
    assert clean != SOCIAL_SPAWN_REFUSAL

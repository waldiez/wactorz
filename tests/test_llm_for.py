# SPDX-License-Identifier: Apache-2.0
"""An agent's own choice of model: `me.llm_for(provider, model)`.

It reads like `me.llm` and is metered the same way, and it is built from the
configuration the system already reads, never from the call: a spawn config can
be written by a model, so the system's key must not reach another provider.
"""

import importlib.util
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_node_runner import RecordingRunner
from wactorz import llm_factory, plugins
from wactorz.agents import function_agent as function_agent_module
from wactorz.agents.function_agent import FunctionAgent, MeteredLLM, agent, spec_of
from wactorz.agents.llm import cost as cost_module
from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.llm_agent import LLMProvider
from wactorz.llm_factory import ProviderUnavailableError, agent_provider, create_provider
from wactorz.node.hosted import NodeHosted

_KEY_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own keys would make a refusal pass or fail by machine."""
    for variable in _KEY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def _system(monkeypatch: pytest.MonkeyPatch, provider: str, key: str) -> None:
    """Run the test on a system whose provider is ``provider``, keyed by ``LLM_API_KEY``."""
    monkeypatch.setattr(
        llm_factory,
        "CONFIG",
        replace(llm_factory.CONFIG, llm_provider=provider, llm_api_key=key),
    )


def _recording(calls: list[tuple[str | None, str]]) -> Callable[[str | None, str], LLMProvider]:
    """A builder that records what it was given, and builds the fake."""

    def build(model: str | None, shared_key: str) -> LLMProvider:
        calls.append((model, shared_key))
        return FakeProvider(model=model or "")

    return build


@agent
async def ask_small(payload: dict[str, Any], me: FunctionAgent) -> str:
    """Asks a model of its own choosing rather than the system's."""
    text, _usage = await me.llm_for("fake", "small").complete(
        [{"role": "user", "content": payload["q"]}]
    )
    return text


@agent
def refused(payload: dict[str, Any], me: FunctionAgent) -> str:
    """Asks for a provider and answers with the reason it was refused, if it was."""
    try:
        me.llm_for(payload["provider"], payload["model"])
    except ProviderUnavailableError as exc:
        return str(exc)
    return "built"


class TestItIsMetered:
    @pytest.fixture
    def ledger(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr(cost_module, "accumulate_global_cost", recorded.append)
        return recorded

    async def test_its_calls_are_charged_to_the_agent(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        actor = spec_of(ask_small).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]

        assert isinstance(await actor.call({"q": "How is the pump?"}), str)

        assert actor.metrics.llm_calls == 1
        assert actor.metrics.llm_input_tokens > 0 and actor.metrics.llm_output_tokens > 0
        assert ledger == [pytest.approx(actor.metrics.llm_cost_usd)]

    async def test_it_needs_no_system_model(self, tmp_path: Path) -> None:
        actor = spec_of(ask_small).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]

        assert actor.llm is None
        metered = actor.llm_for("fake", "small")
        assert isinstance(metered, MeteredLLM)
        assert metered.model == "small", "the model asked for, not the system's"

    def test_it_is_built_once_per_provider_and_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[tuple[str, str]] = []

        def counting(provider: str, model: str) -> LLMProvider:
            built.append((provider, model))
            return FakeProvider(model=model)

        monkeypatch.setattr(function_agent_module, "agent_provider", counting)
        actor = spec_of(ask_small).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]

        first = actor.llm_for("fake", "small")
        assert actor.llm_for(" Fake ", "small ") is first
        assert actor.llm_for("fake", "large") is not first
        assert built == [("fake", "small"), ("fake", "large")]


class TestTheKeyComesFromTheConfiguration:
    def test_the_systems_own_provider_takes_llm_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str | None, str]] = []
        monkeypatch.setitem(llm_factory._PROVIDERS, "openai", _recording(calls))
        _system(monkeypatch, "openai", "sk-system")

        agent_provider("openai", "gpt-5.4-nano")

        assert calls == [("gpt-5.4-nano", "sk-system")]

    def test_another_provider_is_never_given_the_systems_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str | None, str]] = []
        monkeypatch.setitem(llm_factory._PROVIDERS, "openai", _recording(calls))
        _system(monkeypatch, "anthropic", "sk-ant-system")

        agent_provider("openai", "gpt-5.4-nano")

        assert calls == [("gpt-5.4-nano", "")]

    def test_the_systems_call_sites_keep_llm_api_key_for_every_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `LLM_OVERRIDES` is the operator's own configuration: unchanged.
        calls: list[tuple[str | None, str]] = []
        monkeypatch.setitem(llm_factory._PROVIDERS, "openai", _recording(calls))
        _system(monkeypatch, "anthropic", "sk-ant-system")

        create_provider("openai", "gpt-5.4-nano")

        assert calls == [("gpt-5.4-nano", "sk-ant-system")]

    def test_anthropic_without_a_key_is_refused_naming_the_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Its client builds without a key and fails only on the first request.
        _system(monkeypatch, "openai", "sk-system")

        with pytest.raises(
            ProviderUnavailableError, match="ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
        ):
            agent_provider("anthropic", "claude-haiku-5-5")

    @pytest.mark.skipif(
        importlib.util.find_spec("anthropic") is None, reason="the anthropic extra is not installed"
    )
    def test_anthropic_with_its_own_key_is_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _system(monkeypatch, "openai", "sk-system")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-used")

        provider = agent_provider("anthropic", "claude-haiku-5-5")

        assert provider.model == "claude-haiku-5-5"  # pyright: ignore[reportAttributeAccessIssue]


class TestARefusalSaysWhy:
    @pytest.mark.parametrize(
        ("provider", "model", "reason"),
        [
            ("cohere", "command", "Unknown LLM provider: 'cohere'"),
            ("none", "x", "Unknown LLM provider: 'none'"),
            ("", "x", "Unknown LLM provider: ''"),
            ("fake", "  ", "A model is needed with provider 'fake'"),
        ],
    )
    def test_a_request_that_names_nothing_buildable(
        self, provider: str, model: str, reason: str
    ) -> None:
        with pytest.raises(ProviderUnavailableError, match=reason):
            agent_provider(provider, model)

    def test_a_missing_sdk_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(model: str | None, shared_key: str) -> LLMProvider:
            raise ModuleNotFoundError("No module named 'google'")

        monkeypatch.setitem(llm_factory._PROVIDERS, "gemini", missing)

        with pytest.raises(ProviderUnavailableError, match=r"pip install 'wactorz\[google\]'"):
            agent_provider("gemini", "gemini-3.5-flash-lite")

    def test_a_client_that_refuses_gives_its_own_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unkeyed(model: str | None, shared_key: str) -> LLMProvider:
            raise RuntimeError("Missing credentials")

        monkeypatch.setitem(llm_factory._PROVIDERS, "openai", unkeyed)

        with pytest.raises(
            ProviderUnavailableError, match="'openai' could not be built: Missing credentials"
        ):
            agent_provider("openai", "gpt-5.4-nano")

    def test_a_refusal_is_a_value_error(self) -> None:
        assert issubclass(ProviderUnavailableError, ValueError)

    def test_the_agent_sees_the_refusal_and_nothing_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _system(monkeypatch, "openai", "sk-system")
        actor = spec_of(refused).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]

        reason = refused({"provider": "anthropic", "model": "claude-haiku-5-5"}, actor)

        assert "ANTHROPIC_API_KEY" in reason
        assert actor._llms == {}, "a refusal is not cached: the next call asks again"


class TestOnANode:
    @pytest.fixture(name="runner")
    async def runner_fixture(self, tmp_path: Path) -> AsyncIterator[RecordingRunner]:
        runner = RecordingRunner(tmp_path / "node-state")
        await runner.supervisor.start()
        try:
            yield runner
        finally:
            await runner.stop_all()
            if runner.supervisor._watch_task:
                runner.supervisor._watch_task.cancel()

    async def test_it_is_refused_as_me_llm_is_none(self, runner: RecordingRunner) -> None:
        with plugins.registered([refused]) as (plugin,):
            await runner.spawn_agent({"name": "refused", "type": "module", "target": plugin.target})

            hosted = runner.hosted("refused")
            assert isinstance(hosted, NodeHosted) and isinstance(hosted, FunctionAgent)
            # The fake builds anywhere, so only the node can be what refuses it.
            await runner._run_task(hosted, {"provider": "fake", "model": "small"}, "reply/here")

        (answer,) = [p for t, p, _r in runner.published if t == "reply/here"]
        assert answer == {"result": str(ProviderUnavailableError.on_node())}

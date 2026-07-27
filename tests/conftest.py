"""Suite-wide fixtures.

The point of the fixture below is that a test run must not depend on the machine
it runs on. `wactorz.config` reads a developer's `.env`, so ambient settings leak
into tests that believe they are fully injected — and the failure mode is
confusing rather than loud: the test looks wrong, not the environment.
"""

import pytest

from wactorz import llm_factory


@pytest.fixture(autouse=True)
def _no_ambient_llm_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore any `LLM_OVERRIDES` from the developer's environment or `.env`.

    Call sites resolve their provider through `provider_for(site, default)`,
    which returns a *real* provider built from the override table when the site
    has an entry — silently discarding the fake a test injected. A developer
    with `LLM_OVERRIDES="intent=ollama:llama3"` set therefore saw the routing
    and actuator tests fail against a live Ollama, while CI (which has no
    `.env`) stayed green.

    The override table is neutralised at its source rather than on `CONFIG`,
    which is a frozen dataclass. Tests that *want* overrides still pass them
    explicitly via `provider_for(..., overrides=...)`, which takes precedence.
    """
    monkeypatch.setattr(llm_factory, "parse_overrides", lambda _spec: {})

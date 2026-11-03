"""Our Anthropic provider's sampling rule, held against the SDK that is installed.

The provider's other tests use fake clients, so they say what it does with a
signature, not what signature the installed SDK has. This one asks the real
client. From API version 2026-10-21 the Messages API refuses `temperature`,
`top_p` and `top_k` on every model; the provider sends `temperature` only when
the client's `messages.create` / `messages.stream` take it, so on an SDK sending
that version or later neither may. A future SDK that took the parameters back
while sending such a version fails here, in CI, rather than with a 400 at run
time.
"""

import pytest

from wactorz.agents.llm.providers.anthropic import _carries_temperature

anthropic = pytest.importorskip("anthropic")

#: The first API version that refuses the sampling parameters.
SAMPLING_REFUSED_FROM = "2026-10-21"


def test_on_an_api_version_that_refuses_sampling_the_provider_sends_none() -> None:
    client = anthropic.AsyncAnthropic(api_key="sk-test")
    version = client.default_headers.get("anthropic-version", "")
    if version < SAMPLING_REFUSED_FROM:
        pytest.skip(f"anthropic {anthropic.__version__} sends {version}, which still takes them")

    carried = {
        name: _carries_temperature(getattr(client.messages, name)) for name in ("create", "stream")
    }

    assert carried == {"create": False, "stream": False}, (
        f"anthropic {anthropic.__version__} sends API version {version} but its messages "
        "methods take `temperature`: the provider would send it and get a 400"
    )

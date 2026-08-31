"""Suite-wide fixtures.

The point of the fixture below is that a test run must not depend on the machine
it runs on. `wactorz.config` reads a developer's `.env`, so ambient settings leak
into tests that believe they are fully injected — and the failure mode is
confusing rather than loud: the test looks wrong, not the environment.
"""

import pathlib
import sys
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from wactorz import config, llm_factory
from wactorz.core import mqtt
from wactorz.core.persistence.stores import Stores


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


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_state_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every store at a temp directory for the duration of a test.

    `resolve_state_dir()` falls back to `./state` — the directory a developer's
    own install writes to. An `Actor` built without an explicit
    `persistence_dir` therefore persists into it, and the damage is not
    theoretical or subtle: a `MainActor` constructed in a test wrote the test's
    own scripted conversation into `state/main/state.pkl`, where it then showed
    up in the running dashboard as a chat nobody had. It survived a chat reset,
    because the reset clears the database rows and the pickle keeps its own
    copy.

    Isolating the directory rather than fixing the call sites that forget: a
    forgotten `persistence_dir` is invisible in review — the test passes either
    way — and one is added every time someone constructs an actor.
    """
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore an `API_KEY` from the developer's environment or `.env`.

    `wactorz.config` reads the repo's `.env`, so a developer who sets a key —
    to try the sign-in page, say — turns every test that expects an open server
    into a 401. Sixteen of them, in files that have nothing to do with auth, all
    failing for a reason that is nowhere in the diff.

    CI never sees it, which is the worst shape for this kind of thing: green
    there, broken only on the machine of whoever is working on the feature.

    Tests that want a key set one explicitly and win, because this only replaces
    the default.
    """
    monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, api_key=""))


@pytest.fixture(autouse=True)
def _no_ambient_voice_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore `WACTORZ_STT_URI` / `WACTORZ_TTS_URI` from the environment or `.env`.

    Both are read at call time rather than through `CONFIG`, so a developer with
    a recogniser or a synthesiser configured — to try the feature, which is the
    likeliest reason anyone sets them — changes what unrelated tests see. A named
    service reports itself available and brings its own voices, which turns tests
    about the in-process default into failures nowhere near the diff.

    CI has no `.env` and so never sees it: green there, broken only for whoever
    is working on the feature. Tests that want a service set one explicitly and
    win, because this only clears the ambient value.
    """
    monkeypatch.delenv("WACTORZ_STT_URI", raising=False)
    monkeypatch.delenv("WACTORZ_TTS_URI", raising=False)


#: The real factory, for the tests that exist to exercise it.
real_mqtt_client = mqtt.mqtt_client


@pytest.fixture(autouse=True)
def _no_ambient_broker(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never open a real MQTT connection from a test.

    Two problems, one cause. A broker running on the default port
    results in tests quietly connecting to it, so what is exercised depends on
    whether mosquitto happens to be up. And where no broker is listening,
    an actor's command listener still builds an ``aiomqtt.Client`` before the
    connection failed; cancelling the listener mid-attempt leaves that client to
    be finalised after the loop ia closed, which paho reports at the end of a
    run as a wall of "Event loop is closed".

    Refusing at the factory means no client object is ever constructed. Callers
    treat it as a broker that is down, which they already handle. A test that
    wants to drive the connection substitutes its own factory, and wins — this
    fixture only sets the default.

    ⚠ **Every module that imported the name needs patching, not just the one
    that defines it.** A dozen modules do ``from ..core.mqtt import
    mqtt_client``, which binds the function into their own namespace; replacing
    it on `wactorz.core.mqtt` leaves all of those pointing at the real one. That
    gap is invisible on a machine with a broker running, where the test connects
    and passes, and shows up only where nothing is listening.
    """

    if request.node.get_closest_marker("real_mqtt_client"):
        return

    def _refuse(hostname: str, port: int, **_kwargs: Any) -> None:
        raise ConnectionRefusedError(f"no broker in tests ({hostname}:{port})")

    monkeypatch.setattr(mqtt, "mqtt_client", _refuse)
    for module in list(sys.modules.values()):
        # Only bindings still pointing at the real factory: a module that has
        # already substituted its own is a test driving the connection.
        if getattr(module, "mqtt_client", None) is real_mqtt_client:
            monkeypatch.setattr(module, "mqtt_client", _refuse)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_mqtt_client: leave wactorz.core.mqtt.mqtt_client alone — for tests of the factory",
    )


@pytest.fixture(autouse=True)
def _no_leaked_stores() -> Iterator[None]:
    """Put the process-wide stores back the way the test found them.

    `Stores` is module state: `install_stores` sets it and only `close_stores`
    unsets it, so a test that installs a database and does not close it leaves
    that database installed for everything after it.

    That is not theoretical. `events.snapshot()` reads **two** different database
    globals — `runtime.db` for most of its figures, and `Stores.db` through
    `get_global_alltime_cost()` for the all-time spend. A test that resets
    `runtime.db` looks isolated and is not: a leaked `Stores.db` still carries
    the previous test's spend into the headline total, so the snapshot reports a
    number nothing in that test produced.

    Restoring rather than clearing, so a test that installs stores on purpose
    still sees them for its own duration.
    """
    saved = (Stores.db, Stores.pickle)
    yield
    if Stores.db is not None and Stores.db is not saved[0]:
        Stores.db.close()  # don't leak the handle a test left installed
    Stores.db, Stores.pickle = saved
    Stores.memory.clear()

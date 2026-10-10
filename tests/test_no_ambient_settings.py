"""The fixture that keeps the developer's own settings out of the tests.

Its sibling `test_no_ambient_broker.py` keeps tests off a real broker; this one
keeps them off a real `.env`. `MQTT_TLS=1` or `WACTORZ_NODE_ACCOUNTS=1` there is a
developer trying the feature out, and the suite has to read the same on their
machine as in CI.

The subtle half is the same as there -- a dozen modules do `from ..config import
CONFIG`, and those bindings survive any replacement on `config` alone -- with one
more turn. `tests/test_dev_mode_defaults.py` reloads `wactorz.config`, which builds
a second `AppConfig` class, so finding those modules by `isinstance` against the
current class matched nothing once that file had run. Under a random order, that
put the developer's settings into whichever files happened to run after it.
"""

import importlib
from dataclasses import replace

import pytest

from tests import conftest
from wactorz import config
from wactorz.agents import installer_agent

#: The fixture itself, to be called outside the one pytest already applied.
GUARD = conftest._no_ambient_broker_tls.__wrapped__  # pyright: ignore[reportAttributeAccessIssue]  # the function pytest wrapped


def test_a_reload_leaves_the_importers_on_the_older_class() -> None:
    # The premise of the test below: after a reload those modules still hold the
    # object they imported, and it is no longer an instance of the module's class.
    importlib.reload(config)

    assert not isinstance(installer_agent.CONFIG, config.AppConfig)
    assert type(installer_agent.CONFIG).__name__ == "AppConfig"


def test_the_guard_still_reaches_a_module_after_a_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importlib.reload(config)
    monkeypatch.setattr(
        installer_agent,
        "CONFIG",
        replace(installer_agent.CONFIG, node_accounts=True, mqtt_tls="1"),
    )

    with pytest.MonkeyPatch.context() as guarded:
        GUARD(guarded)

        assert installer_agent.CONFIG.node_accounts is False
        assert installer_agent.CONFIG.mqtt_tls == ""

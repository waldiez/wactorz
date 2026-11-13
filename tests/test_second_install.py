# SPDX-License-Identifier: Apache-2.0
"""Another Wactorz install on this one's topics: told apart by the install its health names.

Two installs on one broker under the same namespace, or both under none, share
every control topic. Each main's monitor stamps its health with `install_id()`,
and main says once, per other install, that one is there and what to set.
"""

import json
from typing import Any, cast

import pytest

from wactorz.agents.main import installs
from wactorz.agents.main.hosts import NodeHost
from wactorz.agents.main.installs import OtherInstalls
from wactorz.agents.monitor_agent import HEALTH_INSTALL_FIELD, MonitorActor
from wactorz.core.mqtt import install_id


class _Host:
    """Stands in for main: records what it is asked to say."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notifications.append(notice)


@pytest.fixture(name="host")
def host_fixture() -> _Host:
    return _Host()


@pytest.fixture(name="other_installs")
def other_installs_fixture(host: _Host) -> OtherInstalls:
    return OtherInstalls(cast("NodeHost", host))


def _health(install: object) -> bytes:
    return json.dumps({HEALTH_INSTALL_FIELD: install, "timestamp": 0}).encode()


class TestHearingHealth:
    def test_this_installs_own_says_nothing(
        self, other_installs: OtherInstalls, host: _Host
    ) -> None:
        other_installs.receive(_health(install_id()))

        assert host.notifications == []

    def test_another_install_is_said_once_with_what_to_set(
        self, other_installs: OtherInstalls, host: _Host
    ) -> None:
        other_installs.receive(_health("f00dfeedcafe"))
        other_installs.receive(_health("f00dfeedcafe"))

        (notice,) = host.notifications
        assert "f00dfeed" in notice["message"] and "WACTORZ_NAMESPACE" in notice["message"]
        assert "no namespace" in notice["message"]

    def test_each_other_install_is_said(self, other_installs: OtherInstalls, host: _Host) -> None:
        other_installs.receive(_health("aaaa"))
        other_installs.receive(_health("bbbb"))

        assert len(host.notifications) == 2

    def test_under_a_namespace_it_is_named(
        self, other_installs: OtherInstalls, host: _Host, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installs, "NAMESPACE", "lab")

        other_installs.receive(_health("aaaa"))

        assert "namespace 'lab'" in host.notifications[0]["message"]

    @pytest.mark.parametrize(
        "payload",
        [b"", b"not json", b"[1]", json.dumps({"timestamp": 0}).encode(), _health(7), _health("")],
    )
    def test_a_health_that_names_no_install_is_let_be(
        self, other_installs: OtherInstalls, host: _Host, payload: bytes
    ) -> None:
        # From a release before the stamp, or not a health message at all.
        other_installs.receive(payload)

        assert host.notifications == []


class TestTheStamp:
    async def test_the_monitor_names_this_install_in_its_health(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor = MonitorActor()
        sent: list[tuple[str, Any]] = []

        async def _publish(topic: str, data: Any, **_kwargs: Any) -> None:
            sent.append((topic, data))

        monkeypatch.setattr(monitor, "_mqtt_publish", _publish)
        monitor._registry = cast("Any", type("_Registry", (), {"all_actors": lambda _self: []})())

        await monitor._publish_system_health()

        ((topic, health),) = sent
        assert topic == "system/health"
        assert health[HEALTH_INSTALL_FIELD] == install_id()

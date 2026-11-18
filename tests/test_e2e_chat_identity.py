# SPDX-License-Identifier: Apache-2.0
"""The browser guard follows the same agent when its display name changes."""

from types import SimpleNamespace

import pytest

browser = pytest.importorskip("e2e.harness.browser", exc_type=ModuleNotFoundError)


class _Targets:
    def __init__(self) -> None:
        self.names = {"local-id": "@helper@main"}
        self.selected = "local-id"

    def input_value(self) -> str:
        return self.selected

    def evaluate(self, _expression: str) -> str:
        return self.names[self.selected]

    def locator(self, _selector: str) -> "_Targets":
        return self

    def evaluate_all(self, _expression: str) -> list[str]:
        return list(self.names)

    def select_option(self, *, value: str, timeout: float) -> list[str]:
        assert value in self.names
        self.selected = value
        return [value]


def test_claims_follow_agent_ids_through_relabelling_and_shared_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = _Targets()
    dashboard = browser.Dashboard(page=SimpleNamespace(on=lambda *_args: None), base_url="")
    reply = browser.Said("helper", "Good morning to you too.")
    monkeypatch.setattr(dashboard, "_target_select", lambda: targets)
    monkeypatch.setattr(dashboard, "show", lambda _view: dashboard)
    monkeypatch.setattr(dashboard, "said", lambda: [reply])

    dashboard.expect("helper", reply.text, timeout=1)
    targets.names["local-id"] = "@helper"
    assert dashboard.unclaimed() == []
    assert dashboard.all_unclaimed() == {}

    targets.names.update({"local-id": "@helper@main", "remote-id": "@helper@edge"})
    assert dashboard.all_unclaimed() == {"helper@edge": [reply]}
    targets.selected = "remote-id"
    dashboard.expect("helper", reply.text, timeout=1)
    assert dashboard.all_unclaimed() == {}

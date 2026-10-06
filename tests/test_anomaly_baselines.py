"""The anomaly detector reads a baseline stored before its newer fields existed.

Fields were added to `EntityBaseline` over time (`is_binary`, `transition_freq`,
`ready`, the percentiles and rates). A baseline stored without them is read with
each one at the value a new baseline starts with, by the agent's own loader, so
no migration has to rewrite stored data for the agent to start on it.
"""

from typing import Any

import pytest

from wactorz.catalogue_agents.anomaly_detector_agent import AGENT_CODE


@pytest.fixture(name="baseline_class", scope="module")
def baseline_class_fixture() -> Any:
    """`EntityBaseline` as the agent defines it: its program run as a module."""
    namespace: dict[str, Any] = {}
    exec(compile(AGENT_CODE, "anomaly_detector_agent", "exec"), namespace)
    return namespace["EntityBaseline"]


def test_a_baseline_without_the_newer_fields_reads_them_as_a_new_one_has_them(
    baseline_class: Any,
) -> None:
    old = baseline_class.from_dict(
        {"entity_id": "sensor.kitchen", "field": "temperature", "global_mean": 21.5}
    )
    new = baseline_class("sensor.kitchen", "temperature")

    assert old.global_mean == 21.5
    for name in (
        "is_binary",
        "transition_freq",
        "ready",
        "hourly_count",
        "max_rate",
        "mean_interval",
        "p1",
        "p99",
    ):
        assert getattr(old, name) == getattr(new, name), name


def test_a_stored_field_is_kept(baseline_class: Any) -> None:
    stored = baseline_class.from_dict(
        {"entity_id": "binary_sensor.door", "field": "state", "is_binary": True, "ready": True}
    )

    assert stored.is_binary is True
    assert stored.ready is True

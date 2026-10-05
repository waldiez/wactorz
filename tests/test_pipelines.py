"""A pipeline declared in code: steps, a schedule and rules, wired and checked.

Each step stays an agent of its own; the pipeline is the record that groups
them, checked for wiring when it is declared rather than when a message goes
nowhere.
"""

from typing import Any, ClassVar

import pytest

from wactorz import pipelines, plugins
from wactorz.agents.function_agent import agent
from wactorz.agents.rule_agent import RuleConfig
from wactorz.core.actor import Actor, Message
from wactorz.pipelines import Pipeline, pipeline, wiring_errors


@agent(subscribes="sensors/imu/#", publishes="anomalies/imu")
def detect(reading: dict) -> dict:
    return reading


@agent(subscribes="anomalies/imu")
def notify(anomaly: dict) -> None:
    return None


@agent(subscribes="pipelines/imu-watch/tick", publishes="reports/imu")
def report(tick: dict) -> dict:
    return {"ok": True}


class Sink(Actor):
    SUBSCRIBES: ClassVar[list[str]] = ["reports/imu"]
    PUBLISHES: ClassVar[list[str]] = ["archive/imu"]

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    plugins.clear()
    pipelines.clear()
    yield
    plugins.clear()
    pipelines.clear()


class TestDeclaring:
    def test_steps_become_registered_plugins_that_the_pipeline_starts(self) -> None:
        pipe = pipeline("imu-watch", steps=[detect, notify])

        assert [s.name for s in pipe.steps] == ["detect", "notify"]
        # Registered, so a `type: "module"` spawn may name them, but not
        # started by the plugin loop as well as by the pipeline.
        assert plugins.for_name("detect") is not None
        assert plugins.for_name("detect").autostart is False  # type: ignore[union-attr]  # found above
        assert pipelines.discover()["imu-watch"] is pipe

    def test_a_schedule_and_rules_are_agents_too(self) -> None:
        pipe = pipeline(
            "imu-watch",
            steps=[detect, report, Sink],
            schedule={"type": "interval", "seconds": 300},
            rules=[
                {
                    "triggers": ["anomalies/imu"],
                    "conditions": [{"field": "score", "op": "gt", "value": 20}],
                    "actions": [
                        {"type": "task", "agent": "notify", "payload": {"text": "{score}"}}
                    ],
                }
            ],
        )

        assert pipe.agent_names == (
            "detect",
            "report",
            "sink",
            "imu-watch-schedule",
            "imu-watch-rule",
        )
        assert pipe.tick_topic == "pipelines/imu-watch/tick"
        assert set(pipe.producers()) == {
            "pipelines/imu-watch/tick",
            "anomalies/imu",
            "reports/imu",
            "archive/imu",
        }
        configs = pipe.spawn_configs()
        assert [c["type"] for c in configs] == ["module", "module", "module", "scheduled", "rule"]
        assert configs[3]["publish_topic"] == pipe.tick_topic
        assert configs[4]["triggers"] == ["anomalies/imu"]
        assert all(c["pipeline"] == "imu-watch" for c in configs)

    def test_the_record_is_what_main_keeps_for_a_planner_pipeline(self) -> None:
        pipe = pipeline("imu-watch", steps=[detect, notify], description="watch the IMU")

        record = pipe.record()

        assert record["rule_id"] == "imu-watch"
        assert record["task"] == "watch the IMU"
        assert record["agents"] == ["detect", "notify"]
        assert record["source"] == "pipeline"

    def test_a_target_string_is_a_step(self) -> None:
        pipe = pipeline("imu-watch", steps=["tests.test_pipelines:detect"])

        assert pipe.steps[0].target == "tests.test_pipelines:detect"

    def test_an_empty_pipeline_is_refused(self) -> None:
        with pytest.raises(ValueError, match="neither steps nor rules"):
            pipeline("empty")


class TestWiring:
    def test_a_later_step_listening_to_nothing_is_an_error(self) -> None:
        with pytest.raises(ValueError, match=r"notify.*anomalies/imu.*nothing"):
            pipeline("broken", steps=[Sink, notify])

    def test_the_first_step_may_listen_to_the_outside(self) -> None:
        assert wiring_errors(pipeline("ok", steps=[detect, notify])) == []

    def test_an_input_names_what_comes_from_outside(self) -> None:
        pipe = pipeline("ok", steps=[notify], inputs=["anomalies/imu"], rules=[])

        assert pipe.inputs == ("anomalies/imu",)

    def test_a_rule_triggering_on_nothing_is_an_error(self) -> None:
        with pytest.raises(ValueError, match=r"rule.*triggers on 'nowhere/x'"):
            pipeline(
                "broken",
                steps=[detect],
                rules=[{"triggers": ["nowhere/x"], "actions": [{"type": "publish", "topic": "t"}]}],
            )

    def test_a_wildcard_subscription_matches_a_published_topic(self) -> None:
        @agent(subscribes="anomalies/#")
        def wide(x: dict) -> None:
            return None

        assert wiring_errors(pipeline("ok", steps=[detect, wide])) == []

    def test_two_agents_may_not_share_a_name(self) -> None:
        pipe = Pipeline(
            name="dup",
            steps=(plugins.plugin_from(detect), plugins.plugin_from(detect)),
        )

        assert any("called 'detect'" in e for e in wiring_errors(pipe))


class TestDiscovery:
    def test_a_target_may_hold_one_pipeline_or_several(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one = pipeline("one", steps=[detect])
        two = pipeline("two", steps=[detect], rules=[])
        pipelines.clear()
        plugins.clear()
        monkeypatch.setattr(
            pipelines.plugins, "resolve_target", lambda t: [one, two] if t == "m:both" else one
        )

        found = pipelines.discover(env="m:both m:one")

        assert set(found) == {"one", "two"}
        assert found["one"].target == "m:one" or found["one"].target == "m:both"

    def test_a_bad_target_is_reported_not_fatal(self, caplog: pytest.LogCaptureFixture) -> None:
        found = pipelines.discover(env="no.such:thing json:dumps")

        assert found == {}
        assert "no.such:thing" in caplog.text
        assert "not a Pipeline" in caplog.text

    def test_a_declared_pipeline_survives_a_refresh(self) -> None:
        pipeline("kept", steps=[detect])

        assert "kept" in pipelines.discover(env="", refresh=True)


class TestRuleConfigsInPipelines:
    def test_a_rule_config_object_is_accepted_as_is(self) -> None:
        rule = RuleConfig.from_dict(
            {"triggers": ["anomalies/imu"], "actions": [{"type": "publish", "topic": "alerts/imu"}]}
        )

        pipe = pipeline("ok", steps=[detect], rules=[rule])

        assert pipe.rules == (rule,)
        assert "alerts/imu" in pipe.producers()

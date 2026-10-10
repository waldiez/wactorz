"""The smart-energy agent: the plug protection guard, energy accounting, and onboarding.

It switches real plugs through Home Assistant, and its central promise is that a
plug is powered down only when it is explicitly `auto_off_on_idle` and an idle
rule's condition holds: a `locked` plug, which is what every import creates,
never is. The program is exec'd as it is at spawn and driven through a stand-in
`agent`, with Home Assistant's state reads and turn-offs stubbed in its own
namespace and its clock set by the test.
"""

import time
from typing import Any, ClassVar

import pytest

from tests.programs import program_namespace

NS = program_namespace("smart_energy_agent.py")

LOCKED = NS["LOCKED"]
AUTO_OFF = NS["AUTO_OFF"]
MANUAL = NS["MANUAL"]
AUTO_OFF_TOPIC = NS["AUTO_OFF_TOPIC"]
SUMMARY_TOPIC = NS["SUMMARY_TOPIC"]


class _Agent:
    """What the program uses of `agent`, recording what it publishes, persists and logs."""

    def __init__(self, stored: dict[str, Any] | None = None, llm: Any = None) -> None:
        self.state: dict[str, Any] = {}
        self.stored: dict[str, Any] = dict(stored or {})
        self.published: list[tuple[str, Any]] = []
        self.logs: list[str] = []
        self.llm = llm
        self.contract: dict[str, Any] = {}

    def recall(self, key: str, default: Any = None) -> Any:
        return self.stored.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self.stored[key] = value

    async def log(self, text: str, level: str = "info") -> None:
        self.logs.append(f"{level}: {text}")

    async def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, payload))

    def declare_contract(self, **contract: Any) -> None:
        self.contract = contract

    def on(self, topic: str) -> list[Any]:
        return [payload for published, payload in self.published if published == topic]


class _HomeAssistant:
    """Home Assistant as the program reaches it: a state table, and turn-offs recorded."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.turned_off: list[str] = []
        self.fail_turn_off = False

    async def get_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.states)

    async def turn_off(self, entity_id: str) -> None:
        if self.fail_turn_off:
            raise RuntimeError("Home Assistant said no")
        self.turned_off.append(entity_id)

    def sensor(self, entity_id: str, value: Any, unit: str, friendly: str = "") -> None:
        device_class = {"w": "power", "kw": "power", "kwh": "energy", "wh": "energy"}[unit.lower()]
        self.states[entity_id] = {
            "entity_id": entity_id,
            "state": str(value),
            "attributes": {
                "unit_of_measurement": unit,
                "device_class": device_class,
                "friendly_name": friendly or entity_id,
            },
        }

    def switch(self, entity_id: str) -> None:
        self.states[entity_id] = {"entity_id": entity_id, "state": "on", "attributes": {}}


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


#: Noon on a weekday, local time: periods are local calendar days, as a bill counts them.
NOON = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))


@pytest.fixture(name="ha")
def ha_fixture(monkeypatch: pytest.MonkeyPatch) -> _HomeAssistant:
    ha = _HomeAssistant()
    monkeypatch.setitem(NS, "_ha_get_states", ha.get_states)
    monkeypatch.setitem(NS, "_ha_turn_off", ha.turn_off)
    return ha


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock(NOON)
    monkeypatch.setitem(NS, "time", clock)
    return clock


async def _ready(stored: dict[str, Any] | None = None, llm: Any = None) -> _Agent:
    agent = _Agent(stored, llm)
    await NS["setup"](agent)
    return agent


def _plug(name: str, protection: str = LOCKED, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "friendly": name.title(),
        "ha_entity_power": f"sensor.{name}_current_consumption",
        "ha_entity_switch": f"switch.{name}",
        "protection": protection,
        **fields,
    }


def _idle_rule(plug: str, threshold: float | None = 20.0, delay: float = 180.0) -> dict[str, Any]:
    rule: dict[str, Any] = {"type": "auto_off_on_idle", "plug": plug, "idle_delay_s": delay}
    if threshold is not None:
        rule["idle_threshold_watts"] = threshold
    return {"id": f"idle-{plug}", **rule}


async def _poll(
    agent: _Agent, ha: _HomeAssistant, clock: _Clock, at: float, **watts: float | str
) -> None:
    clock.now = at
    for name, value in watts.items():
        ha.sensor(f"sensor.{name}_current_consumption", value, "W")
    await NS["process"](agent)


# ── The guard ──────────────────────────────────────────────────────────────────


class TestOnlyAnAutoOffPlugIsEverTurnedOff:
    @pytest.mark.parametrize("protection", [LOCKED, MANUAL])
    async def test_a_protected_plug_is_refused_and_home_assistant_never_asked(
        self, protection: str, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()

        turned = await NS["_safe_turn_off"](agent, _plug("ac", protection), "test")

        assert turned is False
        assert ha.turned_off == []
        assert agent.on(AUTO_OFF_TOPIC) == []

    async def test_a_plug_that_names_no_protection_is_locked(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()
        plug = _plug("ac")
        del plug["protection"]

        assert await NS["_safe_turn_off"](agent, plug, "test") is False
        assert ha.turned_off == []

    async def test_an_auto_off_plug_is_turned_off_and_announced(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()

        turned = await NS["_safe_turn_off"](agent, _plug("printer", AUTO_OFF), "idle")

        assert turned is True
        assert ha.turned_off == ["switch.printer"]
        [event] = agent.on(AUTO_OFF_TOPIC)
        assert event["plug"] == "printer"
        assert event["reason"] == "idle"

    async def test_without_a_switch_nothing_is_sent(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()
        plug = _plug("printer", AUTO_OFF, ha_entity_switch=None)

        assert await NS["_safe_turn_off"](agent, plug, "idle") is False
        assert ha.turned_off == []

    async def test_a_failed_turn_off_is_not_announced(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()
        ha.fail_turn_off = True

        assert await NS["_safe_turn_off"](agent, _plug("printer", AUTO_OFF), "idle") is False
        assert agent.on(AUTO_OFF_TOPIC) == []


class TestTheIdleRule:
    async def _printer(self) -> _Agent:
        return await _ready(
            {
                "plugs": {"printer": _plug("printer", AUTO_OFF)},
                "rules": {"idle-printer": _idle_rule("printer")},
            }
        )

    async def test_it_turns_off_once_after_the_delay_and_not_before(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await self._printer()

        await _poll(agent, ha, clock, NOON, printer=150)  # printing
        await _poll(agent, ha, clock, NOON + 10, printer=5)  # idle from here
        await _poll(agent, ha, clock, NOON + 100, printer=5)
        assert ha.turned_off == []

        await _poll(agent, ha, clock, NOON + 200, printer=5)
        await _poll(agent, ha, clock, NOON + 400, printer=5)

        assert ha.turned_off == ["switch.printer"]

    async def test_drawing_power_again_restarts_the_wait_and_rearms_it(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await self._printer()
        await _poll(agent, ha, clock, NOON, printer=5)
        await _poll(agent, ha, clock, NOON + 100, printer=150)  # a new print
        await _poll(agent, ha, clock, NOON + 200, printer=5)
        await _poll(agent, ha, clock, NOON + 300, printer=5)
        assert ha.turned_off == []

        await _poll(agent, ha, clock, NOON + 400, printer=5)
        await _poll(agent, ha, clock, NOON + 500, printer=150)
        await _poll(agent, ha, clock, NOON + 600, printer=5)
        await _poll(agent, ha, clock, NOON + 800, printer=5)

        assert ha.turned_off == ["switch.printer", "switch.printer"]

    async def test_without_a_threshold_it_only_watches(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready(
            {
                "plugs": {"printer": _plug("printer", AUTO_OFF)},
                "rules": {"idle-printer": _idle_rule("printer", threshold=None, delay=0)},
            }
        )

        for at, watts in ((NOON, 180), (NOON + 60, 4), (NOON + 600, 3)):
            await _poll(agent, ha, clock, at, printer=watts)

        assert ha.turned_off == []
        assert agent.state["observed"]["printer"] == {"min": 3.0, "max": 180.0}

    async def test_a_rule_that_reached_a_locked_plug_still_cannot_turn_it_off(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        """`add_rule` refuses this, so here the rule is put in place behind its back."""
        agent = await _ready(
            {"plugs": {"ac": _plug("ac", LOCKED)}, "rules": {"idle-ac": _idle_rule("ac", delay=0)}}
        )

        for at in (NOON, NOON + 60, NOON + 600):
            await _poll(agent, ha, clock, at, ac=0)

        assert ha.turned_off == []
        assert any("Refusing to turn off" in line for line in agent.logs)


class TestAddingARule:
    async def test_an_idle_rule_on_a_locked_plug_is_refused(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac", LOCKED)}})

        result = NS["_add_rule"](agent, _idle_rule("ac"))

        assert result["result"] == "error"
        assert agent.state["rules"] == {}

    async def test_a_rule_names_its_plug_however_the_user_said_it(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"printer_3d": _plug("printer_3d", AUTO_OFF)}})
        agent.state["plugs"]["printer_3d"]["friendly"] = "Bambu Printer"

        result = NS["_add_rule"](agent, {"type": "auto_off_on_idle", "plug": "the bambu"})

        assert result["rule"]["plug"] == "printer_3d"

    async def test_an_unknown_plug_is_refused(self, clock: _Clock) -> None:
        agent = await _ready()

        assert NS["_add_rule"](agent, _idle_rule("nothing"))["result"] == "error"

    async def test_two_rules_added_in_the_same_second_are_both_kept(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"printer": _plug("printer", AUTO_OFF)}})

        first = NS["_add_rule"](agent, {"type": "auto_off_on_idle", "plug": "printer"})
        second = NS["_add_rule"](agent, {"type": "auto_off_on_idle", "plug": "printer"})

        assert first["rule"]["id"] != second["rule"]["id"]
        assert len(agent.state["rules"]) == 2


# ── Accounting ─────────────────────────────────────────────────────────────────


class TestEnergyAndCost:
    async def test_without_a_meter_watts_are_integrated_over_time(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"heater": _plug("heater")}, "rate": 0.2})

        await _poll(agent, ha, clock, NOON, heater=1000)
        await _poll(agent, ha, clock, NOON + 1800, heater=1000)

        record = agent.state["accum"]["heater"]
        assert record["day_kwh"] == pytest.approx(0.5)
        assert record["source"] == "estimated"
        [_, cost] = agent.on("custom/sensors/energy/heater/cost")
        assert cost["cost_today"] == pytest.approx(0.1)

    async def test_a_gap_of_an_hour_or_more_is_not_guessed_at(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"heater": _plug("heater")}})

        await _poll(agent, ha, clock, NOON, heater=1000)
        await _poll(agent, ha, clock, NOON + 7200, heater=1000)

        assert agent.state["accum"]["heater"]["day_kwh"] == 0.0

    async def test_a_sensor_that_stops_reporting_adds_no_energy(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"heater": _plug("heater")}})
        await _poll(agent, ha, clock, NOON, heater=1000)

        await _poll(agent, ha, clock, NOON + 1800, heater="unavailable")

        assert agent.state["accum"]["heater"]["day_kwh"] == 0.0

    async def test_a_kilowatt_sensor_is_read_in_watts(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"heater": _plug("heater", power_scale=1000.0)}})
        ha.sensor("sensor.heater_current_consumption", 1.5, "kW")
        clock.now = NOON

        await NS["process"](agent)

        assert agent.state["last_watts"]["heater"] == pytest.approx(1500.0)

    async def test_a_daily_meter_is_the_day_and_its_rises_feed_the_week(
        self, clock: _Clock
    ) -> None:
        accum: dict[str, Any] = {}
        account = NS["_account"]

        account(accum, "ac", NOON, 300, None, 2.0, "today")
        account(accum, "ac", NOON + 60, 300, 1 / 60, 2.5, "today")

        record = accum["ac"]
        assert record["day_kwh"] == 2.5
        assert record["week_kwh"] == pytest.approx(0.5)
        assert record["source"] == "meter"

    async def test_a_meter_that_resets_counts_nothing_negative(self, clock: _Clock) -> None:
        accum: dict[str, Any] = {}
        account = NS["_account"]

        account(accum, "ac", NOON, None, None, 5.0, "today")
        account(accum, "ac", NOON + 60, None, None, 0.1, "today")  # the device rebooted

        assert accum["ac"]["week_kwh"] == 0.0

    async def test_a_new_day_starts_the_day_from_nothing(self, clock: _Clock) -> None:
        accum: dict[str, Any] = {}
        account = NS["_account"]
        before_midnight = time.mktime((2026, 10, 7, 23, 50, 0, 0, 0, -1))

        account(accum, "heater", before_midnight, 1000, 0.1, None, None)
        assert accum["heater"]["day_kwh"] == pytest.approx(0.1)
        account(accum, "heater", before_midnight + 1200, 1000, 0.1, None, None)

        assert accum["heater"]["day_kwh"] == pytest.approx(0.1)
        assert accum["heater"]["week_kwh"] == pytest.approx(0.2)

    async def test_month_and_total_meters_are_found_and_mirrored(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}})
        ha.sensor("sensor.ac_current_consumption", 300, "W")
        ha.sensor("sensor.ac_this_month_s_consumption", 41.5, "kWh", "AC This month's consumption")
        ha.sensor("sensor.ac_total_energy", 900_000, "Wh", "AC Total energy")
        clock.now = NOON

        await NS["process"](agent)

        record = agent.state["accum"]["ac"]
        assert record["month_kwh"] == 41.5
        assert record["total_kwh"] == pytest.approx(900.0)
        assert agent.stored["plugs"]["ac"]["ha_entity_energy_month"] == (
            "sensor.ac_this_month_s_consumption"
        )

    async def test_the_summary_adds_up_every_plug(self, ha: _HomeAssistant, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac"), "fridge": _plug("fridge")}})

        await _poll(agent, ha, clock, NOON, ac=300, fridge=120)

        [summary] = agent.on(SUMMARY_TOPIC)
        assert summary["total_watts"] == 420.0
        assert {row["plug"] for row in summary["plugs"]} == {"ac", "fridge"}


# ── Finding plugs in Home Assistant ────────────────────────────────────────────


class TestDiscovery:
    def test_a_power_sensor_is_paired_with_its_switch_and_its_daily_meter(self) -> None:
        ha = _HomeAssistant()
        ha.sensor("sensor.ac_current_consumption", 362, "W", "AC Current consumption")
        ha.sensor("sensor.ac_today_s_consumption", 1.2, "kWh", "AC Today's consumption")
        ha.sensor("sensor.ac_total_energy", 5000, "Wh", "AC Total energy")
        ha.switch("switch.ac")

        [candidate] = NS["_discover_candidates"](ha.states)

        assert candidate["friendly"] == "AC"
        assert candidate["suggested_name"] == "ac"
        assert candidate["switch_entity"] == "switch.ac"
        assert candidate["energy_entity"] == "sensor.ac_today_s_consumption"
        assert candidate["energy_kind"] == "today"
        assert candidate["watts"] == 362.0

    def test_kilowatts_and_watt_hours_are_scaled(self) -> None:
        ha = _HomeAssistant()
        ha.sensor("sensor.oven_power", 2.0, "kW", "Oven Power")
        ha.sensor("sensor.oven_energy", 1500, "Wh", "Oven Energy")

        [candidate] = NS["_discover_candidates"](ha.states)

        assert candidate["watts"] == 2000.0
        assert candidate["power_scale"] == 1000.0
        assert candidate["energy_scale"] == 0.001

    def test_two_plugs_with_one_name_are_told_apart(self) -> None:
        ha = _HomeAssistant()
        ha.sensor("sensor.desk_a_power", 10, "W", "Desk Power")
        ha.sensor("sensor.desk_b_power", 20, "W", "Desk Power")

        names = sorted(c["suggested_name"] for c in NS["_discover_candidates"](ha.states))

        assert names == ["desk", "desk_2"]


# ── Talking to it ──────────────────────────────────────────────────────────────


def _two_plugs_in(ha: _HomeAssistant) -> None:
    ha.sensor("sensor.hall_lamp_power", 9, "W", "Hall lamp Power")
    ha.sensor("sensor.printer_power", 150, "W", "Printer Power")
    ha.switch("switch.hall_lamp")
    ha.switch("switch.printer")


class TestImporting:
    async def test_an_import_offers_what_it_found_and_adds_it_locked(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        agent = await _ready()

        offer = await NS["handle_task"](agent, {"text": "import my plugs"})
        done = await NS["handle_task"](agent, {"text": "all"})

        assert "Hall lamp" in offer["result"]
        assert "Printer" in offer["result"]
        assert "Done!" in done["result"]
        assert {p["protection"] for p in agent.state["plugs"].values()} == {LOCKED}
        assert agent.stored["plugs"] == agent.state["plugs"]

    async def test_an_imported_plug_is_never_turned_off(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        agent = await _ready()
        await NS["handle_task"](agent, {"text": "import my plugs"})
        await NS["handle_task"](agent, {"text": "all"})

        for plug in agent.state["plugs"].values():
            assert await NS["_safe_turn_off"](agent, plug, "test") is False
        assert ha.turned_off == []

    @pytest.mark.parametrize(
        ("reply", "chosen"),
        [
            ("2", {"printer"}),
            ("just the printer", {"printer"}),
            ("the hall lamp", {"hall_lamp"}),
            ("both", {"hall_lamp", "printer"}),
            ("all of them", {"hall_lamp", "printer"}),
            ("yes please", {"hall_lamp", "printer"}),
            ("yeah, all", {"hall_lamp", "printer"}),
        ],
    )
    async def test_a_reply_picks_what_it_names(
        self, reply: str, chosen: set[str], ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        agent = await _ready()
        await NS["handle_task"](agent, {"text": "import my plugs"})

        await NS["handle_task"](agent, {"text": reply})

        assert set(agent.state["plugs"]) == chosen

    async def test_cancel_adds_nothing(self, ha: _HomeAssistant, clock: _Clock) -> None:
        _two_plugs_in(ha)
        agent = await _ready()
        await NS["handle_task"](agent, {"text": "import my plugs"})

        await NS["handle_task"](agent, {"text": "cancel"})

        assert agent.state["plugs"] == {}
        assert agent.state["convo"] == {}

    async def test_without_home_assistant_it_says_so(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, {"text": "import my plugs"})

        assert "couldn't reach Home Assistant" in result["result"]


class TestConversation:
    @pytest.mark.parametrize(
        ("text", "rate"),
        [("set rate to 0.20", 0.2), ("my tariff is 0,31", 0.31), ("I used 5 kwh today", None)],
    )
    def test_a_rate_is_read_only_when_one_is_named(self, text: str, rate: float | None) -> None:
        assert NS["_parse_rate"](text.lower()) == rate

    async def test_stop_monitoring_removes_the_plug_and_its_figures(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}, "accum": {"ac": {"day_kwh": 1.0}}})

        result = await NS["handle_task"](agent, {"text": "stop monitoring the AC"})

        assert "Stopped monitoring" in result["result"]
        assert agent.state["plugs"] == {}
        assert agent.stored["accum"] == {}

    async def test_a_command_sent_as_json_text_is_obeyed(self, clock: _Clock) -> None:
        agent = await _ready()

        await NS["handle_task"](agent, {"text": '{"action": "set_rate", "rate": 0.25}'})

        assert agent.state["rate"] == 0.25
        assert agent.stored["rate"] == 0.25

    async def test_a_rate_that_is_not_a_number_is_refused(self, clock: _Clock) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, {"action": "set_rate", "rate": "cheap"})

        assert result["result"] == "error"

    async def test_status_and_report_read_from_the_same_figures(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}, "rate": 0.5})
        await _poll(agent, ha, clock, NOON, ac=300)
        # The figures the poll keeps for this period, set to round numbers.
        agent.state["accum"]["ac"].update(day_kwh=2.0, week_kwh=4.0, month_kwh=8.0)

        status = await NS["handle_task"](agent, {"action": "status"})
        report = await NS["handle_task"](agent, {"action": "report"})

        assert status["plugs_monitored"] == 1
        assert status["total_watts"] == 300.0
        assert report["cost_today"] == 1.0
        assert report["cost_week"] == 2.0
        assert report["cost_month"] == 4.0


# ── Reaching Home Assistant ────────────────────────────────────────────────────


class _Service:
    """Home Assistant's WebSocket client, recording the services it is asked to call."""

    calls: ClassVar[list[tuple[str, str, str]]] = []
    url: ClassVar[str] = ""

    def __init__(self, url: str, _token: str) -> None:
        _Service.url = url

    async def __aenter__(self) -> "_Service":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def call_service(self, domain: str, service: str, entity_id: str) -> None:
        _Service.calls.append((domain, service, entity_id))


class _Config:
    def __init__(self, url: str = "", token: str = "") -> None:
        self.ha_url = url
        self.ha_token = token


class TestHomeAssistantItself:
    @pytest.fixture(autouse=True)
    def _service(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _Service.calls = []
        monkeypatch.setitem(NS, "HAWebSocketClient", _Service)

    async def test_without_a_url_and_token_nothing_is_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "CONFIG", _Config())

        assert await NS["_ha_get_states"]() == {}
        with pytest.raises(RuntimeError, match="HA not configured"):
            await NS["_ha_turn_off"]("switch.printer")
        assert _Service.calls == []

    async def test_states_are_keyed_by_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def get_states(url: str, token: str) -> list[dict[str, Any]]:
            assert (url, token) == ("http://ha.local:8123", "t0ken")
            return [{"entity_id": "sensor.ac_power", "state": "300"}, {"state": "orphan"}]

        monkeypatch.setitem(NS, "CONFIG", _Config("http://ha.local:8123", "t0ken"))
        monkeypatch.setitem(NS, "get_states", get_states)

        assert await NS["_ha_get_states"]() == {
            "sensor.ac_power": {"entity_id": "sensor.ac_power", "state": "300"}
        }

    async def test_a_failed_read_is_no_states(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def get_states(_url: str, _token: str) -> list[dict[str, Any]]:
            raise OSError("unreachable")

        monkeypatch.setitem(NS, "CONFIG", _Config("http://ha.local:8123", "t0ken"))
        monkeypatch.setitem(NS, "get_states", get_states)

        assert await NS["_ha_get_states"]() == {}

    @pytest.mark.parametrize(
        ("entity", "domain"), [("switch.printer", "switch"), ("light.desk", "light")]
    )
    async def test_turning_off_calls_the_entitys_own_domain(
        self, entity: str, domain: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "CONFIG", _Config("http://ha.local:8123", "t0ken"))

        await NS["_ha_turn_off"](entity)

        assert _Service.calls == [(domain, "turn_off", entity)]
        assert _Service.url.startswith("ws://ha.local:8123")

    def test_a_state_that_is_not_a_number_is_no_reading(self) -> None:
        read = NS["_read_watts"]

        assert read(None) is None
        assert read({"state": "unavailable"}) is None
        assert read({"state": "12.5"}) == 12.5


# ── Polling, the less common shapes ────────────────────────────────────────────


class TestPollingEdges:
    async def test_with_no_plugs_nothing_is_read_or_sent(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()

        await NS["process"](agent)

        assert agent.published == []

    async def test_a_plug_with_nothing_to_read_is_left_out(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"ghost": _plug("ghost")}})

        await NS["process"](agent)

        [summary] = agent.on(SUMMARY_TOPIC)
        assert summary["plugs"] == []
        assert agent.on("custom/sensors/energy/ghost/power") == []

    async def test_a_lifetime_meter_feeds_the_day_from_its_rises(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        plug = _plug(
            "ac", ha_entity_energy="sensor.ac_energy", energy_scale=0.001, energy_kind="total"
        )
        agent = await _ready({"plugs": {"ac": plug}})
        ha.sensor("sensor.ac_energy", 10_000, "Wh")
        await _poll(agent, ha, clock, NOON, ac=300)
        ha.sensor("sensor.ac_energy", 10_500, "Wh")

        await _poll(agent, ha, clock, NOON + 60, ac=300)

        record = agent.state["accum"]["ac"]
        assert record["day_kwh"] == pytest.approx(0.5)
        assert record["source"] == "meter"


# ── Commands as main and power users send them ─────────────────────────────────


class TestCommands:
    async def test_a_plug_is_added_locked_unless_it_says_otherwise(self, clock: _Clock) -> None:
        agent = await _ready({"rate": 0.3})

        result = await NS["handle_task"](
            agent, {"action": "add_plug", "plug": {"name": "ac", "ha_entity_power": "sensor.ac"}}
        )
        listed = await NS["handle_task"](agent, {"action": "list_plugs"})

        assert result["plug"]["protection"] == LOCKED
        assert result["plug"]["cost_per_kwh"] == 0.3
        assert [p["name"] for p in listed["plugs"]] == ["ac"]
        assert agent.stored["plugs"]["ac"]["protection"] == LOCKED

    @pytest.mark.parametrize(
        "plug",
        ["ac", {"ha_entity_power": "sensor.ac"}, {"name": "ac", "protection": "sometimes"}],
    )
    async def test_a_plug_that_is_not_one_is_refused(self, plug: Any, clock: _Clock) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, {"action": "add_plug", "plug": plug})

        assert result["result"] == "error"
        assert agent.state["plugs"] == {}

    async def test_removing_by_name_and_by_nothing(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}})

        missing = await NS["handle_task"](agent, {"action": "remove_plug"})
        removed = await NS["handle_task"](agent, {"action": "remove_plug", "plug": "ac"})

        assert missing["result"] == "error"
        assert "Stopped monitoring" in removed["result"]

    async def test_rules_are_listed_and_removed_by_id(self, clock: _Clock) -> None:
        agent = await _ready(
            {
                "plugs": {"printer": _plug("printer", AUTO_OFF)},
                "rules": {"idle-printer": _idle_rule("printer")},
            }
        )

        listed = await NS["handle_task"](agent, {"action": "list_rules"})
        unknown = await NS["handle_task"](agent, {"action": "remove_rule", "id": ["idle-printer"]})
        removed = await NS["handle_task"](agent, {"action": "remove_rule", "rule": "idle-printer"})

        assert [r["id"] for r in listed["rules"]] == ["idle-printer"]
        assert unknown["result"] == "error"
        assert removed["result"] == "Removed rule 'idle-printer'"
        assert agent.stored["rules"] == {}

    async def test_a_rule_without_a_type_is_refused(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"printer": _plug("printer", AUTO_OFF)}})

        result = await NS["handle_task"](agent, {"action": "add_rule", "rule": {"plug": "printer"}})

        assert result["result"] == "error"

    async def test_a_rate_can_bring_its_currency(self, clock: _Clock) -> None:
        agent = await _ready()

        await NS["handle_task"](agent, {"action": "set_rate", "rate": "0.4", "currency": "GBP"})

        assert (agent.stored["rate"], agent.stored["currency"]) == (0.4, "GBP")

    async def test_an_empty_request_is_a_welcome(self, clock: _Clock) -> None:
        fresh = await _ready()
        with_plugs = await _ready({"plugs": {"ac": _plug("ac")}})

        first = await NS["handle_task"](fresh, {})
        again = await NS["handle_task"](with_plugs, {})

        assert "import my plugs" in first["result"]
        assert "1 plug(s)" in again["result"]

    async def test_a_bare_string_is_read_as_a_request(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, "help")

        assert "import my plugs" in result["result"]


# ── The rest of the conversation ───────────────────────────────────────────────


class _Llm:
    """A model that answers with what it is given, or fails."""

    def __init__(self, answer: str = "", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.prompts: list[str] = []

    async def chat(self, prompt: str, system: str = "") -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.answer


class TestTheRestOfTheConversation:
    async def _offered(self, ha: _HomeAssistant, llm: Any = None) -> _Agent:
        _two_plugs_in(ha)
        agent = await _ready(llm=llm)
        await NS["handle_task"](agent, {"text": "import my plugs"})
        return agent

    async def test_asking_what_mid_import_shows_the_menu_again(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await self._offered(ha)

        result = await NS["handle_task"](agent, {"text": "what plugs?"})

        assert "middle of importing" in result["result"]
        assert agent.state["convo"]["stage"] == "selecting"

    async def test_a_reply_that_names_nothing_asks_again(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await self._offered(ha)

        result = await NS["handle_task"](agent, {"text": "hmm, not sure"})

        assert "didn't catch" in result["result"]
        assert agent.state["plugs"] == {}

    async def test_none_means_none(self, ha: _HomeAssistant, clock: _Clock) -> None:
        agent = await self._offered(ha)

        await NS["handle_task"](agent, {"text": "none"})

        assert agent.state["plugs"] == {}

    async def test_the_model_settles_a_reply_the_words_do_not(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        llm = _Llm("You meant [2].")
        agent = await self._offered(ha, llm)

        await NS["handle_task"](agent, {"text": "the one that makes things"})

        assert set(agent.state["plugs"]) == {"printer"}
        assert "Printer" in llm.prompts[0]

    async def test_a_model_that_fails_chooses_nothing(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await self._offered(ha, _Llm(error=RuntimeError("offline")))

        result = await NS["handle_task"](agent, {"text": "the one that makes things"})

        assert "didn't catch" in result["result"]
        assert any("offline" in line for line in agent.logs)

    async def test_cancel_with_nothing_started(self, clock: _Clock) -> None:
        agent = await _ready()

        result = await NS["handle_task"](agent, {"text": "cancel"})

        assert "Nothing to cancel" in result["result"]

    async def test_a_rate_said_in_words_is_set(self, clock: _Clock) -> None:
        agent = await _ready()

        await NS["handle_task"](agent, {"text": "electricity costs 0.27 per kwh"})

        assert agent.state["rate"] == 0.27

    async def test_help_once_plugs_exist(self, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}})

        result = await NS["handle_task"](agent, {"text": "help"})

        assert "I **only monitor**" in result["result"]

    async def test_status_in_words(self, ha: _HomeAssistant, clock: _Clock) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac")}})
        ha.sensor("sensor.ac_current_consumption", 300, "W")

        result = await NS["handle_task"](agent, {"text": "give me a summary"})

        assert result["total_watts"] == 300.0
        assert "estimated = no energy meter" in result["result"]

    async def test_with_no_plugs_anything_about_energy_starts_an_import(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        agent = await _ready()

        offer = await NS["handle_task"](agent, {"text": "how much power am I using?"})
        other = await NS["handle_task"](await _ready(), {"text": "tell me a joke"})

        assert "I found these plugs" in offer["result"]
        assert "import my plugs" in other["result"]

    async def test_an_import_with_nothing_new_says_so(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        agent = await _ready()
        await NS["handle_task"](agent, {"text": "import my plugs"})
        await NS["handle_task"](agent, {"text": "all"})

        result = await NS["handle_task"](agent, {"text": "scan again"})

        assert "didn't find any *new* plugs" in result["result"]

    async def test_an_import_that_finds_no_power_sensor_says_what_to_check(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        ha.switch("switch.plain_plug")
        agent = await _ready()

        result = await NS["handle_task"](agent, {"text": "import my plugs"})

        assert "didn't find any plugs that report power" in result["result"]

    @pytest.mark.parametrize(
        ("meters", "said"),
        [
            ({"hall_lamp", "printer"}, "own energy meter"),
            (set(), "estimate cost from live wattage"),
            ({"printer"}, "1 of 2 expose an energy meter"),
        ],
    )
    async def test_an_import_says_how_its_cost_is_known(
        self, meters: set[str], said: str, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        _two_plugs_in(ha)
        for name in meters:
            ha.sensor(f"sensor.{name}_today_energy", 0.4, "kWh", f"{name} today energy")
        agent = await _ready()
        await NS["handle_task"](agent, {"text": "import my plugs"})

        done = await NS["handle_task"](agent, {"text": "all"})

        assert said in done["result"]

    async def test_a_question_is_answered_from_the_live_figures(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        llm = _Llm("The AC cost 0.12 today.")
        agent = await _ready({"plugs": {"ac": _plug("ac")}}, llm=llm)
        ha.sensor("sensor.ac_current_consumption", 300, "W")

        result = await NS["handle_task"](agent, {"text": "what did the AC cost today?"})

        assert result["result"] == "The AC cost 0.12 today."
        assert result["snapshot"]["plugs"]["ac"]["watts_now"] == 300.0
        assert "what did the AC cost today?" in llm.prompts[0]

    async def test_a_question_without_a_model_or_with_a_failing_one(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        quiet = await _ready({"plugs": {"ac": _plug("ac")}})
        broken = await _ready({"plugs": {"ac": _plug("ac")}}, llm=_Llm(error=OSError("down")))

        no_model = await NS["handle_task"](quiet, {"text": "what did the AC cost?"})
        failed = await NS["handle_task"](broken, {"text": "what did the AC cost?"})

        assert "No LLM configured" in no_model["result"]
        assert failed["result"] == "LLM error: down"

    async def test_a_report_says_which_figures_are_estimated(
        self, ha: _HomeAssistant, clock: _Clock
    ) -> None:
        agent = await _ready({"plugs": {"ac": _plug("ac"), "fridge": _plug("fridge")}})
        await _poll(agent, ha, clock, NOON, ac=300, fridge=100)

        report = await NS["handle_task"](agent, {"action": "cost"})

        assert "Ac: today" in report["result"]
        assert "Fridge: today" in report["result"]
        assert "'estimated' plugs have no energy meter" in report["result"]


class TestNamesAndPairing:
    def test_a_plug_is_found_by_its_name_slug_or_words(self) -> None:
        agent = _Agent()
        agent.state = {"plugs": {"living_room_ac": {"friendly": "Living Room AC"}}}
        resolve = NS["_resolve_plug_name"]

        assert resolve(agent, "living_room_ac") == "living_room_ac"
        assert resolve(agent, "Living Room AC") == "living_room_ac"
        assert resolve(agent, "stop the living room one") == "living_room_ac"
        assert resolve(agent, "") is None
        assert resolve(agent, "remove the plug please") is None
        assert resolve(agent, "kitchen") is None

    def test_a_switch_and_a_meter_named_a_little_differently_still_pair(self) -> None:
        ha = _HomeAssistant()
        ha.sensor("sensor.desk_plug_power", 40, "W", "Desk plug Power")
        ha.switch("switch.desk")
        ha.sensor("sensor.desk_this_month_s_consumption", 3.0, "kWh", "Desk this month")
        ha.states["sensor.broken"] = "not a state"  # pyright: ignore[reportArgumentType]

        [candidate] = NS["_discover_candidates"](ha.states)

        assert candidate["switch_entity"] == "switch.desk"
        assert candidate["energy_entity"] == "sensor.desk_this_month_s_consumption"
        assert candidate["energy_kind"] == "month"

    def test_a_power_sensor_without_a_number_is_offered_without_one(self) -> None:
        ha = _HomeAssistant()
        ha.sensor("sensor.tv_power", "unavailable", "W", "TV Power")

        [candidate] = NS["_discover_candidates"](ha.states)

        assert candidate["watts"] is None

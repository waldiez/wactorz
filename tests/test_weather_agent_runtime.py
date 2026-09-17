"""The weather agent against a stand-in Open-Meteo: lookups, memory, and replies.

`tests/test_weather_agent.py` pins the natural-language parsing. This covers
what happens after it: a place is geocoded once and cached, the forecast and
archive endpoints are chosen by how far back or ahead the question is, and the
answer leads with a verdict when the question was yes-or-no ("will it rain?",
"do I need a jacket?") before giving the numbers.

The agent remembers where it was last asked about, so a follow-up without a
place is answered for that place, and says so in the reply; a question with no
place and no history uses the default location, and says that instead.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wactorz.catalogue_agents import weather_agent as weather_mod
from wactorz.catalogue_agents.weather_agent import (
    _ARCHIVE_URL,
    _FORECAST_URL,
    _GEOCODE_URL,
    WeatherAgent,
    _idx,
    _short,
)
from wactorz.core.actor import Message, MessageType

#: The local calendar date, which is what the agent labels days against.
TODAY = datetime.now().astimezone().date()


def _iso(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


class _OpenMeteo:
    """Answers the three Open-Meteo endpoints from canned data."""

    def __init__(self) -> None:
        self.places: dict[str, dict[str, Any]] = {
            "athens": {
                "name": "Athens",
                "admin1": "Attica",
                "country": "Greece",
                "latitude": 37.98,
                "longitude": 23.72,
            },
            "paris": {"name": "Paris", "country": "France", "latitude": 48.85, "longitude": 2.35},
        }
        self.current: dict[str, Any] = {
            "temperature_2m": 21.44,
            "apparent_temperature": 19.0,
            "relative_humidity_2m": 60,
            "weather_code": 0,
            "wind_speed_10m": 12.0,
            "precipitation": 0,
            "time": "now",
        }
        self.daily: dict[str, Any] = {
            "time": [_iso(i) for i in range(-6, 17)],
            "temperature_2m_max": [25.0] * 23,
            "temperature_2m_min": [15.0] * 23,
            "weather_code": [61] * 23,
            "precipitation_sum": [2.5] * 23,
            "precipitation_probability_max": [80] * 23,
        }
        self.down = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append((url, params))
        if self.down and url != _GEOCODE_URL:
            return None
        if url == _GEOCODE_URL:
            place = self.places.get(params["name"].lower())
            return {"results": [place]} if place else {}
        if "current" in params:
            return {"current": self.current}
        return {"daily": self.daily}

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


@pytest.fixture(name="meteo")
def meteo_fixture() -> _OpenMeteo:
    return _OpenMeteo()


@pytest.fixture(name="agent")
def agent_fixture(tmp_path: Path, meteo: _OpenMeteo) -> WeatherAgent:
    agent = WeatherAgent(persistence_dir=str(tmp_path))
    agent._default_location = "Athens"
    agent._get_json = meteo  # pyright: ignore[reportAttributeAccessIssue]
    return agent


class _Llm:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply

    async def complete(self, messages: list[dict[str, Any]], system: str = "") -> Any:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, {}


class TestGeocoding:
    async def test_a_place_is_looked_up_once_and_labelled(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        first = await agent._geocode("Athens")
        second = await agent._geocode("athens")

        assert first == second == (37.98, 23.72, "Athens, Attica, Greece")
        assert meteo.urls() == [_GEOCODE_URL]

    async def test_coordinates_need_no_lookup(self, agent: WeatherAgent, meteo: _OpenMeteo) -> None:
        assert await agent._geocode("37.9, 23.7") == (37.9, 23.7, "37.900,23.700")
        assert meteo.calls == []

    async def test_trailing_qualifiers_are_dropped_until_a_place_matches(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        resolved = await agent._geocode("Paris lovely city")

        assert resolved is not None and resolved[2] == "Paris, France"
        assert [p["name"] for _, p in meteo.calls] == ["Paris lovely city", "Paris lovely", "Paris"]

    async def test_nothing_found_is_none(self, agent: WeatherAgent) -> None:
        assert await agent._geocode("Atlantis, deep") is None
        assert await agent._geocode("") is None


class TestCurrent:
    async def test_the_default_location_is_used_and_said(self, agent: WeatherAgent) -> None:
        reply = agent._format(await agent._handle_cmd({"action": "current"}))

        assert reply.startswith("In Athens, Attica, Greece it's currently clear, 21.4°C")
        assert "(feels like 19.0°C)" in reply
        assert reply.endswith("(using default location — say 'weather in <city>' to specify one)")

    async def test_a_follow_up_uses_the_place_asked_about_before(self, agent: WeatherAgent) -> None:
        await agent._handle_cmd({"action": "current", "location": "Paris"})

        reply = agent._format(await agent._handle_cmd({"action": "current"}))

        assert reply.endswith("(using Paris, France from earlier)")
        assert agent.recall("last_location") == "Paris, France"

    async def test_my_location_uses_the_saved_home(self, agent: WeatherAgent) -> None:
        await agent._handle_cmd({"action": "current", "location": "Paris"})

        result = await agent._handle_cmd({"action": "current", "use_default_location": True})

        assert result["location"] == "Athens, Attica, Greece"
        assert agent._format(result).endswith("(using your saved location)")

    async def test_fahrenheit_asks_for_imperial_units(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        reply = agent._format(await agent._handle_cmd({"action": "current", "units": "fahrenheit"}))

        assert meteo.calls[-1][1]["temperature_unit"] == "fahrenheit"
        assert "°F" in reply and "mph" in reply

    async def test_i_live_in_updates_the_default(self, agent: WeatherAgent) -> None:
        await agent._handle_cmd(
            {"action": "current", "location": "Paris", "update_default_location": True}
        )

        assert agent._default_location == "Paris"
        assert agent.recall("default_location") == "Paris"

    async def test_an_unknown_place_and_a_dead_service_are_explained(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        missing = await agent._handle_cmd({"action": "current", "location": "Atlantis"})
        meteo.down = True
        down = await agent._handle_cmd({"action": "current"})

        assert "couldn't find a place called 'Atlantis'" in missing["error"]
        assert down["error"].startswith("Weather service is unreachable")

    @pytest.mark.parametrize(
        ("concern", "current", "verdict"),
        [
            ("rain", {"weather_code": 61}, "Yes — it's raining."),
            ("rain", {}, "No — it's dry right now."),
            ("snow", {"weather_code": 71}, "Yes — it's snowing."),
            ("snow", {}, "No — no snow right now."),
            ("clothing", {"apparent_temperature": 8.0}, "Yes — wear a jacket."),
            (
                "clothing",
                {"apparent_temperature": 15.0, "wind_speed_10m": 25.0},
                "Yes — wear a jacket.",
            ),
            ("clothing", {"apparent_temperature": 18.0}, "A light jacket is a good idea."),
            (
                "clothing",
                {"apparent_temperature": None, "temperature_2m": 28.0},
                "No jacket needed.",
            ),
        ],
    )
    async def test_a_yes_or_no_question_is_answered_first(
        self,
        agent: WeatherAgent,
        meteo: _OpenMeteo,
        concern: str,
        current: dict[str, Any],
        verdict: str,
    ) -> None:
        meteo.current.update(current)

        reply = agent._format(await agent._handle_cmd({"action": "current", "concern": concern}))

        assert reply.startswith(verdict)

    def test_clothing_with_no_temperature_gives_no_verdict(self) -> None:
        assert WeatherAgent._verdict_now({"temp": None}, "clothing") == ""


class TestForecast:
    async def test_a_multi_day_forecast_lists_each_day(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        meteo.daily["time"] = [_iso(0), _iso(1), _iso(2)]

        reply = agent._format(await agent._handle_cmd({"action": "forecast", "days": 99}))

        lines = reply.splitlines()
        assert lines[0] == "Forecast for Athens, Attica, Greece:"
        assert lines[1].lstrip().startswith("today")
        assert lines[2].lstrip().startswith("tomorrow")
        assert meteo.calls[-1][1]["forecast_days"] == 16

    async def test_a_single_day_leads_with_its_verdict(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        reply = agent._format(
            await agent._handle_cmd({"action": "forecast", "date_from": _iso(1), "concern": "rain"})
        )

        assert reply.startswith("Yes — pack an umbrella, rain is likely tomorrow.")
        assert f"({_iso(1)}): light rain, 15.0–25.0°C, precip 2.5mm / 80% chance." in reply

    async def test_a_range_beyond_the_horizon_is_refused(self, agent: WeatherAgent) -> None:
        result = await agent._handle_cmd({"action": "forecast", "date_from": _iso(40)})

        assert result["error"] == "That date is beyond the 16-day forecast range."

    async def test_a_rain_question_over_several_days_names_the_wet_ones(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        meteo.daily["time"] = [_iso(1), _iso(2)]
        meteo.daily["weather_code"] = [0, 61]
        meteo.daily["precipitation_sum"] = [0, 3]
        meteo.daily["precipitation_probability_max"] = [0, 90]

        reply = agent._format(
            await agent._handle_cmd({"action": "forecast", "days": 2, "concern": "rain"})
        )

        assert reply.splitlines()[1] == f"  Rain likely on: {_short(_iso(2))}."

    @pytest.mark.parametrize(
        ("row", "concern", "verdict"),
        [
            ({"code": 0, "precip_prob": 30}, "rain", "Maybe — there's a chance of rain today."),
            ({"code": 0, "precip_prob": 5}, "rain", "No — it should stay dry today."),
            ({"code": 73}, "snow", "Yes — snow is expected today."),
            ({"code": 0}, "snow", "No — no snow expected today."),
            ({"code": 0, "temp_min": 12}, "clothing", "Bring a light jacket today."),
            ({"code": 0, "temp_min": 18}, "clothing", "You probably only need light layers today."),
            ({"code": 0, "temp_min": 24}, "clothing", "No jacket needed today."),
            ({"code": 0}, "clothing", ""),
            ({"code": 0}, None, ""),
        ],
    )
    def test_day_verdicts(self, row: dict[str, Any], concern: str | None, verdict: str) -> None:
        assert WeatherAgent._verdict_day(row, concern, "Athens", "today") == verdict

    @pytest.mark.parametrize(
        ("rows", "concern", "verdict"),
        [
            ([{"date": "2026-06-06", "code": 71}], "snow", "Snow expected on: Sat."),
            ([{"date": "2026-06-06", "code": 0}], "snow", "No snow expected over this period."),
            (
                [{"date": "2026-06-06", "code": 0}],
                "rain",
                "Looks dry across this period — no umbrella needed.",
            ),
        ],
    )
    def test_range_verdicts(self, rows: list[dict[str, Any]], concern: str, verdict: str) -> None:
        assert WeatherAgent._verdict_range(rows, concern) == verdict


class TestHistory:
    async def test_a_recent_day_comes_from_the_forecast_endpoint(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        reply = agent._format(await agent._handle_cmd({"action": "history"}))

        assert meteo.urls()[-1] == _FORECAST_URL
        assert meteo.calls[-1][1]["past_days"] == 2
        assert reply == (
            f"On {_iso(-1)}, Athens, Attica, Greece saw light rain, 15.0–25.0°C, "
            "precip 2.5mm / 80% chance."
        )

    async def test_an_older_day_comes_from_the_archive(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        old = "2020-01-01"
        meteo.daily = {
            "time": [old],
            "temperature_2m_max": [5.0],
            "temperature_2m_min": [1.0],
            "weather_code": [3],
        }

        reply = agent._format(await agent._handle_cmd({"action": "history", "date": old}))

        assert meteo.urls()[-1] == _ARCHIVE_URL
        assert reply == f"On {old}, Athens, Attica, Greece saw overcast, 1.0–5.0°C."

    async def test_yesterday_by_name(self, agent: WeatherAgent, meteo: _OpenMeteo) -> None:
        result = await agent._history("Athens", "yesterday")

        assert result["date"] == _iso(-1)

    async def test_missing_records_and_a_dead_archive_are_explained(
        self, agent: WeatherAgent, meteo: _OpenMeteo
    ) -> None:
        missing = await agent._history("Athens", "2020-01-01")
        nowhere = await agent._history("Atlantis", "2020-01-01")
        meteo.down = True
        down = await agent._history("Athens", "2020-01-01")

        assert missing == {"error": "No weather record found for 2020-01-01."}
        assert "couldn't find a place" in nowhere["error"]
        assert down["error"].startswith("Weather history is unreachable")


class TestCommands:
    async def test_not_weather_is_turned_away_politely(self, agent: WeatherAgent) -> None:
        result = await agent._handle_cmd({"action": "not_weather"})

        assert result["error"].startswith("I'm a weather agent")

    async def test_set_default_needs_a_place_and_remembers_it(self, agent: WeatherAgent) -> None:
        refused = await agent._handle_cmd({"action": "set-default"})
        saved = await agent._handle_cmd({"action": "set-default", "location": "Paris"})

        assert "which location to remember" in refused["error"]
        assert agent._format(saved) == "Default location set to Paris."
        assert agent.recall("default_location") == "Paris"

    async def test_an_unknown_action_is_explained(self, agent: WeatherAgent) -> None:
        result = await agent._handle_cmd({"action": "radar"})

        assert result["error"].startswith("I didn't understand that.")

    async def test_failing_persistence_does_not_fail_the_answer(
        self, agent: WeatherAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(*_args: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(agent, "persist", _refuse)

        saved = await agent._handle_cmd({"action": "set-default", "location": "Paris"})
        current = await agent._handle_cmd(
            {"action": "current", "location": "Paris", "update_default_location": True}
        )

        assert saved["status"] == "ok"
        assert current["location"] == "Paris, France"

    def test_an_unknown_result_shape_is_printed_as_is(self, agent: WeatherAgent) -> None:
        assert agent._format({"kind": "radar"}) == "{'kind': 'radar'}"


class TestMessages:
    @staticmethod
    def _replies(agent: WeatherAgent) -> list[tuple[str, dict[str, Any]]]:
        sent: list[tuple[str, dict[str, Any]]] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, payload))
            return True

        agent.send = _send  # pyright: ignore[reportAttributeAccessIssue]
        return sent

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "current", "location": "Paris", "_task_id": "t1"},
            {"city": "Paris", "_task_id": "t1"},
            {"text": "weather in paris", "_task_id": "t1"},
        ],
    )
    async def test_every_request_shape_is_answered_with_its_task_id(
        self, agent: WeatherAgent, payload: dict[str, Any]
    ) -> None:
        sent = self._replies(agent)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload=payload)
        )

        ((target, reply),) = sent
        assert target == "main"
        assert reply["_task_id"] == "t1"
        assert reply["result"].startswith("In Paris, France")

    async def test_an_empty_dict_asks_for_the_current_weather(self, agent: WeatherAgent) -> None:
        sent = self._replies(agent)

        await agent.handle_message(Message(type=MessageType.TASK, sender_id="main", payload={}))

        assert sent[0][1]["kind"] == "current"

    async def test_a_plain_string_is_parsed(self, agent: WeatherAgent) -> None:
        sent = self._replies(agent)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload="weather in athens")
        )
        await agent.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="main"))

        assert len(sent) == 1

    async def test_chat_returns_the_formatted_answer(self, agent: WeatherAgent) -> None:
        assert (await agent.chat("weather in paris")).startswith("In Paris, France")


class TestLlmLocationRecovery:
    async def test_a_place_the_parser_missed_is_recovered_and_validated(
        self, agent: WeatherAgent
    ) -> None:
        agent._llm = _Llm('"Paris"')  # pyright: ignore[reportAttributeAccessIssue]

        assert await agent._llm_location("how is it over at the city of light") == "Paris"

    @pytest.mark.parametrize("reply", ["NONE", "", "x" * 90, "Atlantis", RuntimeError("down")])
    async def test_an_unusable_answer_recovers_nothing(
        self, agent: WeatherAgent, reply: str | Exception
    ) -> None:
        agent._llm = _Llm(reply)  # pyright: ignore[reportAttributeAccessIssue]

        assert await agent._llm_location("somewhere nice") is None

    async def test_without_an_llm_nothing_is_recovered(self, agent: WeatherAgent) -> None:
        assert await agent._llm_location("x") is None

    async def test_the_parser_asks_the_llm_only_when_a_place_is_missing(
        self, agent: WeatherAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[str] = []

        async def _recover(message: str) -> str:
            asked.append(message)
            return "Paris"

        agent._llm = _Llm("unused")  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(agent, "_llm_location", _recover)
        monkeypatch.setattr(weather_mod, "parse_query", lambda _m: {"action": "current"})

        payload = await agent._parse_smart("how is it in the city of light")

        assert payload["location"] == "Paris"
        assert asked == ["how is it in the city of light"]


class TestStartAndHttp:
    async def test_the_saved_locations_are_restored(self, agent: WeatherAgent) -> None:
        agent.persist("default_location", "Paris")
        agent.persist("last_location", "Athens")

        await agent.on_start()

        assert (agent._default_location, agent._last_location) == ("Paris", "Athens")

    async def test_a_failing_or_refused_request_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = WeatherAgent(persistence_dir=str(tmp_path))

        class _Resp:
            def __init__(self, status: int) -> None:
                self.status = status

            async def json(self) -> Any:
                return {"ok": True}

            async def __aenter__(self) -> "_Resp":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        statuses = [500, 200]

        class _Session:
            def __init__(self, timeout: Any = None) -> None:
                self.statuses = statuses

            async def __aenter__(self) -> "_Session":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            def get(self, url: str, params: Any = None) -> _Resp:
                if url == "boom":
                    raise OSError("dns")
                return _Resp(self.statuses.pop(0))

        monkeypatch.setattr(weather_mod.aiohttp, "ClientSession", _Session)

        assert await agent._get_json("u", {}) is None
        assert await agent._get_json("u", {}) == {"ok": True}
        assert await agent._get_json("boom", {}) is None

    def test_list_helpers_tolerate_missing_values(self) -> None:
        assert _idx([1, 2], 5) is None
        assert _idx(None, 0) is None  # pyright: ignore[reportArgumentType]
        assert _short("not a date") == "not a date"

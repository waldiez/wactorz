"""
Tests for WeatherAgent's natural-language parsing.

Parsing is pure and network-free (`parse_query`), so we pin a fixed "today"
(Wednesday 2026-06-03) and assert on the structured intent for the kinds of
phrases ordinary, non-technical users actually type. The Open-Meteo HTTP calls
are exercised separately/manually — these tests guard the parsing logic that
previously mangled queries like "weather tomorrow".
"""

import unittest
from datetime import date, timedelta

from wactorz.agents.weather_agent import (
    WeatherAgent,
    parse_query,
    _clean_location,
    _next_weekday,
    _label_for,
)

WED = date(2026, 6, 3)  # a Wednesday


class ParseIntentTest(unittest.TestCase):
    def _p(self, text):
        return parse_query(text, WED)

    def assertIntent(self, text, action, location=None):
        p = self._p(text)
        self.assertEqual(p.get("action"), action, f"{text!r} action")
        got = (p.get("location") or "").lower()
        self.assertEqual(got, (location or "").lower(), f"{text!r} location")
        return p

    def test_current_default_and_named(self):
        self.assertIntent("what's the weather", "current", None)
        self.assertIntent("whats the weather like rn", "current", None)
        self.assertIntent("whats the weather in athens gr", "current", "athens gr")
        self.assertIntent("temperature in paris", "current", "paris")
        self.assertIntent("athens", "current", "athens")
        self.assertIntent("whats it like outside", "current", None)

    def test_tomorrow_is_a_forecast_not_a_location(self):
        # regression: previously geocoded the whole sentence
        p = self.assertIntent("what's the weather tomorrow", "forecast", None)
        self.assertEqual(p["date_from"], "2026-06-04")
        p = self.assertIntent("whats the weather in athens gr tomorrow", "forecast", "athens gr")
        self.assertEqual(p["date_from"], "2026-06-04")

    def test_weekend_spans_saturday_sunday(self):
        p = self.assertIntent("how hot will it be in rome this weekend", "forecast", "rome")
        self.assertEqual((p["date_from"], p["date_to"]), ("2026-06-06", "2026-06-07"))

    def test_weekday_targets_next_occurrence(self):
        p = self.assertIntent("what's the forecast for friday in madrid", "forecast", "madrid")
        self.assertEqual(p["date_from"], "2026-06-05")  # Fri after Wed

    def test_next_week_is_following_monday_window(self):
        p = self.assertIntent("how's the weather in los angeles next week", "forecast", "los angeles")
        self.assertEqual((p["date_from"], p["date_to"]), ("2026-06-08", "2026-06-14"))

    def test_n_day_horizon(self):
        self.assertEqual(self._p("forecast for tokyo next 5 days")["days"], 5)
        self.assertEqual(self._p("weather in cape town in 10 days")["days"], 10)
        self.assertEqual(self._p("weather in new york this week")["days"], 7)

    def test_following_week_is_next_week(self):
        p = self.assertIntent("whats the weather in athens for the following week", "forecast", "athens")
        # "following week" = Mon–Sun of NEXT calendar week (same as "next week")
        self.assertEqual((p["date_from"], p["date_to"]), ("2026-06-08", "2026-06-14"))
        p2 = self._p("what will it be like the week after next in berlin")
        self.assertEqual(p2["action"], "forecast")

    def test_history(self):
        p = self.assertIntent("what was the weather yesterday", "history", None)
        self.assertEqual(p["date"], "2026-06-02")
        p = self.assertIntent("weather in berlin 3 days ago", "history", "berlin")
        self.assertEqual(p["date"], "2026-05-31")
        p = self.assertIntent("history berlin 2026-05-20", "history", "berlin")
        self.assertEqual(p["date"], "2026-05-20")

    def test_rain_and_snow_concerns(self):
        self.assertEqual(self._p("will it rain in london tomorrow?").get("concern"), "rain")
        self.assertEqual(self._p("is it raining?").get("concern"), "rain")
        self.assertEqual(self._p("is it raining?").get("action"), "current")
        self.assertEqual(self._p("do i need an umbrella tomorrow").get("concern"), "rain")
        for text in (
            "should i pack an umbrella tomorrow",
            "should i bring an umbrella tomorrow",
            "should i take an umbrella tomorrow",
            "should i carry an umbrella tomorrow",
        ):
            p = self._p(text)
            self.assertEqual(p.get("action"), "forecast", text)
            self.assertEqual(p.get("concern"), "rain", text)
            self.assertNotIn("location", p, text)
        self.assertEqual(self._p("is it going to snow in boston").get("concern"), "snow")
        self.assertIsNone(self._p("weather in paris").get("concern"))

    def test_clothing_questions_are_weather_queries(self):
        p = self._p("should i wear a jacket")
        self.assertEqual(p["action"], "current")
        self.assertEqual(p.get("concern"), "clothing")
        self.assertNotEqual(p["action"], "not_weather")

    def test_fahrenheit(self):
        self.assertEqual(self._p("temperature in cairo in fahrenheit").get("units"), "fahrenheit")
        self.assertNotIn("units", self._p("temperature in cairo"))  # celsius is implicit

    def test_multiword_city_names_survive(self):
        # "city"/"town" must not be stripped out of real place names
        self.assertIntent("weather in kansas city", "current", "kansas city")
        self.assertIntent("forecast for mexico city this week", "forecast", "mexico city")
        self.assertIntent("weather in cape town tomorrow", "forecast", "cape town")

    def test_set_default(self):
        self.assertIntent("set default to Athens", "set-default", "Athens")
        self.assertIntent("set-default Tokyo", "set-default", "Tokyo")
        self.assertIntent("change my default location to Paris", "set-default", "Paris")
        self.assertIntent("remember my location as Athens", "set-default", "Athens")
        self.assertIntent("set my home to Thessaloniki", "set-default", "Thessaloniki")

    def test_home_location_references_use_default_without_fake_location(self):
        p = self._p("weather where I am")
        self.assertEqual(p["action"], "current")
        self.assertTrue(p.get("use_default_location"))
        self.assertNotIn("location", p)
        p = self._p("is it raining near me?")
        self.assertEqual(p.get("concern"), "rain")
        self.assertTrue(p.get("use_default_location"))
        self.assertNotIn("location", p)

    def test_self_location_statement_updates_default_location(self):
        p = self.assertIntent("im in athens gr silly", "current", "athens gr")
        self.assertTrue(p.get("update_default_location"))
        p = self.assertIntent("I'm near Thessaloniki", "current", "Thessaloniki")
        self.assertTrue(p.get("update_default_location"))

    def test_json_passthrough(self):
        p = parse_query('{"action": "forecast", "location": "Rome", "days": 4}', WED)
        self.assertEqual(p, {"action": "forecast", "location": "Rome", "days": 4})

    def test_empty(self):
        self.assertEqual(parse_query("", WED), {"action": "current"})


class HelperTest(unittest.TestCase):
    def test_clean_location_strips_noise(self):
        self.assertIsNone(_clean_location("what is the weather tomorrow"))
        self.assertEqual(_clean_location("in london tomorrow"), "london")

    def test_next_weekday(self):
        # Wed 2026-06-03; Friday is +2
        self.assertEqual(_next_weekday(WED, 4), date(2026, 6, 5))
        # same weekday returns today unless force_next
        self.assertEqual(_next_weekday(WED, 2), WED)
        self.assertEqual(_next_weekday(WED, 0, force_next=True), date(2026, 6, 8))

    def test_label_for(self):
        self.assertEqual(_label_for(WED, WED), "today")
        self.assertEqual(_label_for(date(2026, 6, 4), WED), "tomorrow")
        self.assertEqual(_label_for(date(2026, 6, 2), WED), "yesterday")
        self.assertEqual(_label_for(date(2026, 6, 5), WED), "Friday")


class FormatTest(unittest.TestCase):
    def setUp(self):
        self.agent = WeatherAgent(llm_provider=None, name="weather-agent")

    def test_current_format(self):
        out = self.agent._format({
            "kind": "current", "location": "Paris", "temp": 20.0, "feels_like": 19.0,
            "humidity": 50, "wind": 7.0, "precip": 0, "code": 0, "condition": "clear",
            "units": "celsius", "concern": None,
        })
        self.assertIn("Paris", out)
        self.assertIn("20", out)
        self.assertIn("°C", out)

    def test_rain_verdict_yes(self):
        out = self.agent._format({
            "kind": "forecast", "location": "London", "units": "celsius", "concern": "rain",
            "forecast": [{"date": "2026-06-04", "temp_min": 13.0, "temp_max": 18.0,
                          "precip_mm": 5.0, "precip_prob": 100, "code": 61, "condition": "light rain"}],
        })
        self.assertIn("Yes", out)
        self.assertIn("umbrella", out.lower())

    def test_rain_verdict_no(self):
        out = self.agent._format({
            "kind": "forecast", "location": "Cairo", "units": "celsius", "concern": "rain",
            "forecast": [{"date": "2026-06-04", "temp_min": 22.0, "temp_max": 35.0,
                          "precip_mm": 0.0, "precip_prob": 0, "code": 0, "condition": "clear"}],
        })
        self.assertIn("No", out)

    def test_current_clothing_verdict(self):
        out = self.agent._format({
            "kind": "current", "location": "Athens", "temp": 29.0, "feels_like": 31.0,
            "humidity": 39, "wind": 12.0, "precip": 0, "code": 0, "condition": "clear",
            "units": "celsius", "concern": "clothing",
        })
        self.assertIn("No jacket needed", out)

    def test_error_passthrough(self):
        self.assertIn("couldn't find", self.agent._format({"error": "I couldn't find a place called 'x'."}))


class ConversationContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_followups_reuse_last_successful_location(self):
        class FakeWeatherAgent(WeatherAgent):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.memory = {}

            def persist(self, key, value):
                self.memory[key] = value

            def recall(self, key, default=None):
                return self.memory.get(key, default)

            async def _current(self, location: str, units: str = "celsius") -> dict:
                label = "Athens, Attica, Greece" if "athens" in location.lower() else location
                return {
                    "kind": "current", "location": label, "temp": 29.5, "feels_like": 31.4,
                    "humidity": 39, "wind": 12.2, "precip": 0, "code": 0, "condition": "clear",
                }

            async def _forecast(self, location: str, units: str = "celsius", **kwargs) -> dict:
                label = "Athens, Attica, Greece" if "athens" in location.lower() else location
                # Use tomorrow relative to today so the agent labels it "tomorrow"
                # (the agent anchors to the real current date; a fixed date rots).
                tomorrow = (date.today() + timedelta(days=1)).isoformat()
                return {
                    "kind": "forecast", "location": label,
                    "forecast": [{"date": tomorrow, "temp_min": 22.0, "temp_max": 31.0,
                                  "precip_mm": 0.0, "precip_prob": 5, "code": 0, "condition": "clear"}],
                }

        agent = FakeWeatherAgent(llm_provider=None, name="weather-agent")

        first = await agent.chat("hey bro whats the weather in athens gr?")
        self.assertIn("Athens", first)

        jacket = await agent.chat("should i wear a jacket")
        self.assertIn("Athens", jacket)
        self.assertIn("from earlier", jacket)
        self.assertIn("No jacket needed", jacket)
        self.assertNotIn("London", jacket)

        rain = await agent.chat("is it raining?")
        self.assertIn("No", rain)
        self.assertIn("dry right now", rain)
        self.assertIn("Athens", rain)

        tomorrow = await agent.chat("what about tomorrow?")
        self.assertIn("Athens", tomorrow)
        self.assertIn("clear", tomorrow)
        self.assertNotIn("London", tomorrow)

        umbrella = await agent.chat("should i pack an umbrella tomorrow")
        self.assertIn("Athens", umbrella)
        self.assertIn("dry tomorrow", umbrella)
        self.assertNotIn("Pack", umbrella)
        self.assertNotIn("Austria", umbrella)

    async def test_home_location_is_separate_from_last_discussed_city(self):
        class FakeWeatherAgent(WeatherAgent):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.memory = {}

            def persist(self, key, value):
                self.memory[key] = value

            def recall(self, key, default=None):
                return self.memory.get(key, default)

            async def _current(self, location: str, units: str = "celsius") -> dict:
                labels = {
                    "athens": "Athens, Attica, Greece",
                    "paris": "Paris, Ile-de-France, France",
                }
                key = location.split(",", 1)[0].lower()
                return {
                    "kind": "current", "location": labels.get(key, location),
                    "temp": 20.0, "feels_like": 20.0, "humidity": 50,
                    "wind": 7.0, "precip": 0, "code": 0, "condition": "clear",
                }

        agent = FakeWeatherAgent(llm_provider=None, name="weather-agent")

        remembered = await agent.chat("remember my location as Athens")
        self.assertIn("Athens", remembered)
        self.assertEqual(agent.memory["default_location"], "Athens")

        paris = await agent.chat("weather in Paris")
        self.assertIn("Paris", paris)

        followup = await agent.chat("is it raining?")
        self.assertIn("Paris", followup)
        self.assertIn("from earlier", followup)

        home = await agent.chat("is it raining where I am?")
        self.assertIn("Athens", home)
        self.assertNotIn("Paris", home)

    async def test_self_location_statement_changes_where_i_am(self):
        class FakeWeatherAgent(WeatherAgent):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.memory = {}

            def persist(self, key, value):
                self.memory[key] = value

            def recall(self, key, default=None):
                return self.memory.get(key, default)

            async def _current(self, location: str, units: str = "celsius") -> dict:
                label = "Athens, Attica, Greece" if "athens" in location.lower() else location
                return {
                    "kind": "current", "location": label, "temp": 29.5, "feels_like": 31.6,
                    "humidity": 40, "wind": 12.5, "precip": 0, "code": 0, "condition": "clear",
                }

        agent = FakeWeatherAgent(llm_provider=None, name="weather-agent")

        current = await agent.chat("whats the weather like rn")
        self.assertNotIn("rn", current)

        moved = await agent.chat("im in athens gr silly")
        self.assertIn("Athens", moved)
        self.assertEqual(agent.memory["default_location"], "athens gr")

        home = await agent.chat("weather where i am")
        self.assertIn("Athens", home)
        self.assertNotIn("London", home)

        near_me = await agent.chat("is it raining near me?")
        self.assertIn("Athens", near_me)
        self.assertIn("dry right now", near_me)

        at_home = await agent.chat("weather at home")
        self.assertIn("Athens", at_home)
        self.assertIn("saved location", at_home)
        self.assertNotIn("say 'weather in <city>'", at_home)


class CatalogNativeWeatherTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_recipe_info_hides_factory_and_native_restore_is_mutable(self):
        from wactorz.agents.catalog_agent import CatalogAgent

        class MemoryCatalog(CatalogAgent):
            def __init__(self):
                self.memory = {}
                super().__init__(name="catalog-test")

            def persist(self, key, value):
                self.memory[key] = value

            def recall(self, key, default=None):
                return self.memory.get(key, default)

        catalog = MemoryCatalog()

        info = catalog._action_info("weather-agent")
        self.assertTrue(info["ok"])
        self.assertIn("weather-agent", info["message"])
        self.assertNotIn("factory", info["recipe"])
        self.assertNotIn("code", info["recipe"])

        await catalog._remember_native("weather-agent")
        self.assertEqual(catalog.memory["_active_native"], ["weather-agent"])

        forgotten = await catalog.forget_native("weather-agent")
        self.assertTrue(forgotten)
        self.assertEqual(catalog.memory["_active_native"], [])

        forgotten_again = await catalog.forget_native("weather-agent")
        self.assertFalse(forgotten_again)


if __name__ == "__main__":
    unittest.main()

"""
Tests for WeatherAgent's natural-language parsing.

Parsing is pure and network-free (`parse_query`), so we pin a fixed "today"
(Wednesday 2026-06-03) and assert on the structured intent for the kinds of
phrases ordinary, non-technical users actually type. The Open-Meteo HTTP calls
are exercised separately/manually — these tests guard the parsing logic that
previously mangled queries like "weather tomorrow".
"""

import unittest
from datetime import date

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

    def test_history(self):
        p = self.assertIntent("what was the weather yesterday", "history", None)
        self.assertEqual(p["date"], "2026-06-02")
        p = self.assertIntent("weather in berlin 3 days ago", "history", "berlin")
        self.assertEqual(p["date"], "2026-05-31")
        p = self.assertIntent("history berlin 2026-05-20", "history", "berlin")
        self.assertEqual(p["date"], "2026-05-20")

    def test_rain_and_snow_concerns(self):
        self.assertEqual(self._p("will it rain in london tomorrow?").get("concern"), "rain")
        self.assertEqual(self._p("do i need an umbrella tomorrow").get("concern"), "rain")
        self.assertEqual(self._p("is it going to snow in boston").get("concern"), "snow")
        self.assertIsNone(self._p("weather in paris").get("concern"))

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

    def test_error_passthrough(self):
        self.assertIn("couldn't find", self.agent._format({"error": "I couldn't find a place called 'x'."}))


if __name__ == "__main__":
    unittest.main()

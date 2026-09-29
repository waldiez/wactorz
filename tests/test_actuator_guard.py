"""Tests for the actuator post-resolution guard (filter_unrequested_actions)
and the request-derived color/brightness detection."""

from wactorz.agents.home_assistant_actuator_agent import ActuatorAction
from wactorz.agents.one_off_actuator_agent import (
    OneOffActuatorAgent,
    _request_tokens,
    filter_unrequested_actions,
)

_DEVICES = [
    {"entity_id": "light.philips_hue_lct015", "friendly_name": "Hue Bookshelf"},
    {"entity_id": "light.wiz_rgbw_tunable_351b6e", "friendly_name": "Wiz Bulb"},
    {"entity_id": "media_player.tv_samsung_7_series_55", "friendly_name": "Samsung TV"},
    {"entity_id": "climate.living_room", "friendly_name": "Thermostat"},
    {"entity_id": "lock.front_door", "friendly_name": "Front Door"},
]


def _action(domain, service, entity_id):
    return ActuatorAction(domain=domain, service=service, entity_id=entity_id)


_WIZ_ON = ("light", "turn_on", "light.wiz_rgbw_tunable_351b6e")


# ── _request_tokens ──────────────────────────────────────────────────────────


def test_tokens_split_specific_and_hints():
    specific, hints = _request_tokens("turn off the hue lights")
    assert "hue" in specific
    assert hints == {"light"}


def test_tokens_generic_only_request():
    specific, hints = _request_tokens("turn off the tv")
    assert specific == set()
    assert hints == {"media_player"}


def test_tokens_recognize_greek_light_request():
    specific, hints = _request_tokens("κλείσε μου τα φώτα")
    assert specific == set()
    assert hints == {"light"}


def test_tokens_ignore_colors_digits_stopwords():
    specific, hints = _request_tokens("set the light to pink at 50")
    assert specific == set()
    assert hints == {"light"}


# ── filter_unrequested_actions ───────────────────────────────────────────────


def test_drops_bonus_action_when_specific_match_exists():
    # "turn off the hue lights" -> [hue turn_off, wiz turn_on (bonus)]
    hue = _action("light", "turn_off", "light.philips_hue_lct015")
    wiz = _action(*_WIZ_ON)
    kept, dropped = filter_unrequested_actions("turn off the hue lights", [hue, wiz], _DEVICES)
    assert kept == [hue]
    assert dropped == [wiz]


def test_drops_bonus_action_on_generic_tv_request():
    # "turn off the tv" -> [tv turn_off, wiz turn_on (bonus)]
    tv = _action("media_player", "turn_off", "media_player.tv_samsung_7_series_55")
    wiz = _action(*_WIZ_ON)
    kept, dropped = filter_unrequested_actions("turn off the tv", [tv, wiz], _DEVICES)
    assert kept == [tv]
    assert dropped == [wiz]


def test_vetoes_substitution_when_device_kind_absent():
    # "turn off the tv" but the model returned only a light: original bug.
    wiz = _action(*_WIZ_ON)
    kept, dropped = filter_unrequested_actions("turn off the tv", [wiz], _DEVICES)
    assert kept == []
    assert dropped == [wiz]


def test_vetoes_substitution_for_unknown_specific_device():
    # "turn off the projector" (no device word in the map) with a light action.
    wiz = _action(*_WIZ_ON)
    kept, dropped = filter_unrequested_actions("turn off the projector", [wiz], _DEVICES)
    assert kept == []
    assert dropped == [wiz]


def test_keeps_generic_domain_action():
    # "make it warmer" -> climate action passes via the domain hint.
    clim = _action("climate", "set_temperature", "climate.living_room")
    kept, dropped = filter_unrequested_actions("make it warmer", [clim], _DEVICES)
    assert kept == [clim]
    assert dropped == []


def test_keeps_every_light_for_greek_plural_request():
    hue = _action("light", "turn_off", "light.philips_hue_lct015")
    wiz = _action("light", "turn_off", "light.wiz_rgbw_tunable_351b6e")
    kept, dropped = filter_unrequested_actions("Κλείσα μου τα φώτα", [hue, wiz], _DEVICES)
    assert kept == [hue, wiz]
    assert dropped == []


def test_keeps_multi_device_request():
    lock = _action("lock", "lock", "lock.front_door")
    hue = _action("light", "turn_off", "light.philips_hue_lct015")
    kept, dropped = filter_unrequested_actions(
        "lock the front door and turn off the hue light", [lock, hue], _DEVICES
    )
    assert kept == [lock, hue]
    assert dropped == []


def test_passes_through_unadjudicable_request():
    # Only stopwords ("turn it on"): no device information at all — the guard
    # cannot judge, so it keeps whatever the resolver chose.
    clim = _action("climate", "set_temperature", "climate.living_room")
    kept, dropped = filter_unrequested_actions("turn it on", [clim], _DEVICES)
    assert kept == [clim]
    assert dropped == []


def test_vetoes_when_specific_words_match_nothing():
    # Words that name nothing we can find ("do the usual"): safer to refuse.
    clim = _action("climate", "set_temperature", "climate.living_room")
    kept, dropped = filter_unrequested_actions("do the usual", [clim], _DEVICES)
    assert kept == []
    assert dropped == [clim]


def test_empty_actions_noop():
    assert filter_unrequested_actions("turn off the tv", [], _DEVICES) == ([], [])


# ── resolver output parsing tolerates prose wrapping ─────────────────────────


def test_parse_actions_prose_wrapped():
    raw = 'Here are the actions:\n[{"domain": "light", "service": "turn_off", "entity_id": "light.x"}]\nDone.'
    parsed = _bare_agent("x")._parse_actions_json(raw)
    assert parsed == [{"domain": "light", "service": "turn_off", "entity_id": "light.x"}]


def test_parse_actions_fenced_still_works():
    raw = '```json\n[{"domain": "light", "service": "turn_off", "entity_id": "light.x"}]\n```'
    parsed = _bare_agent("x")._parse_actions_json(raw)
    assert parsed[0]["entity_id"] == "light.x"


def test_parse_actions_garbage_still_raises():
    import json as _json

    import pytest

    with pytest.raises(_json.JSONDecodeError):
        _bare_agent("x")._parse_actions_json("no json at all")


# ── color detection reads only the user's words ──────────────────────────────


def _bare_agent(request):
    # __new__ skips Actor init — these helpers only read self.request.
    agent = OneOffActuatorAgent.__new__(OneOffActuatorAgent)
    agent.request = request
    return agent


def test_color_detection_ignores_entity_list():
    enriched = (
        "turn on the tv\n\n"
        "[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
        "  remote.infrared_hub (Infrared Hub)\n"
        "  light.wiz_rgbw_tunable_351b6e (Wiz Bulb)\n"
        "]"
    )
    assert _bare_agent(enriched)._requested_rgb() is None


def test_color_detection_requires_whole_word():
    assert _bare_agent("turn on the infrared heater")._requested_rgb() is None
    assert _bare_agent("pair my bluetooth speaker")._requested_rgb() is None
    assert _bare_agent("make the lamp red")._requested_rgb() == [255, 0, 0]


def test_max_brightness_ignores_entity_list():
    enriched = (
        "turn on the lamp\n\n"
        "[AVAILABLE HA ENTITIES:\n  sensor.full_brightness_meter (Full Brightness Meter)\n]"
    )
    assert not _bare_agent(enriched)._requests_max_brightness()
    assert _bare_agent("lamp to full brightness please")._requests_max_brightness()


def test_ignores_injected_entity_list():
    # Routing enriches the request with every discovered entity; the guard
    # must judge only the user's words, or every action self-matches.
    enriched = (
        "turn off the tv\n\n"
        "[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
        "  media_player.tv_samsung_7_series_55 (Samsung TV)\n"
        "  light.wiz_rgbw_tunable_351b6e (Wiz Bulb)\n"
        "]"
    )
    tv = _action("media_player", "turn_off", "media_player.tv_samsung_7_series_55")
    wiz = _action(*_WIZ_ON)
    kept, dropped = filter_unrequested_actions(enriched, [tv, wiz], _DEVICES)
    assert kept == [tv]
    assert dropped == [wiz]

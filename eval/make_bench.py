"""Generate a starter bench.jsonl for wactorz.evalharness."""

import json
from pathlib import Path

# Entity list injected by routing before the actuator sees a request.
# Mirrors the real setup (hue in the office, wiz in the living room, Samsung TV)
# plus a few common devices. Deliberately does NOT contain: bedroom light,
# back door, sprinklers, garage — those drive the refusal cases.
ENTITIES = """

[AVAILABLE HA ENTITIES — match the user's device to one of these:
  light.philips_hue_lct015 (Hue Office Lamp)
  light.wiz_rgbw_tunable_351b6e (Wiz Living Room Bulb)
  media_player.samsung_7_series_55_ue55nu7023 (Samsung 7 Series 55)
  switch.coffee_machine (Coffee Machine)
  climate.living_room (Living Room Thermostat)
  lock.front_door (Front Door)
  cover.bedroom_blinds (Bedroom Blinds)
  binary_sensor.front_door_contact (Front Door Contact)
  sensor.living_room_temperature (Living Room Temperature)
]"""

HUE = "light.philips_hue_lct015"
WIZ = "light.wiz_rgbw_tunable_351b6e"
TV = "media_player.samsung_7_series_55_ue55nu7023"
COFFEE = "switch.coffee_machine"
CLIMATE = "climate.living_room"
LOCK = "lock.front_door"
BLINDS = "cover.bedroom_blinds"

cases = []


def add(cid, category, prompt, expected):
    cases.append({"id": cid, "category": category, "prompt": prompt, "expected": expected})


# ── intent: ACTUATE | HA | PIPELINE | OTHER ──────────────────────────────────
intent = [
    ("turn on the office light", "ACTUATE"),
    ("turn off the tv", "ACTUATE"),
    ("set the thermostat to 22 degrees", "ACTUATE"),
    ("lock the front door", "ACTUATE"),
    ("dim the living room bulb to 30%", "ACTUATE"),
    ("when a person is detected in my pc camera, open the office light", "PIPELINE"),
    ("if the front door opens after midnight send me a discord message", "PIPELINE"),
    ("whenever the living room temperature goes above 28 turn on the fan", "PIPELINE"),
    ("every weekday at 7:30 start the coffee machine", "PIPELINE"),
    ("notify me on telegram when the washing machine finishes", "PIPELINE"),
    ("list my home assistant automations", "HA"),
    ("what devices do I have?", "HA"),
    ("delete the automation that turns off the lights at midnight", "HA"),
    ("what's the capital of Greece?", "OTHER"),
    ("write me a python function that reverses a string", "OTHER"),
]
for i, (p, e) in enumerate(intent, 1):
    add(f"intent-{i:03d}", "intent", p, e)

# ── ha: Home Assistant action classification ─────────────────────────────────
ha = [
    ("create an automation that turns off all lights at midnight", "create_automation"),
    ("add an automation to close the blinds at sunset", "create_automation"),
    ("set up an automation for the coffee machine on weekday mornings", "create_automation"),
    ("delete the sunset blinds automation", "delete_automation"),
    ("remove the automation called night mode", "delete_automation"),
    ("rename my midnight lights automation to bedtime", "edit_automation"),
    ("change the morning coffee automation to fire at 8am", "edit_automation"),
    ("list all my automations", "list_automations"),
    ("show me every automation I have", "list_automations"),
    ("what areas are configured?", "list_areas"),
    ("show me all my devices", "list_devices"),
    ("list all entity ids", "list_entities"),
    ("what motion sensor should I buy for the hallway?", "recommend_hardware"),
    ("do I have any thermometers?", "other"),
    ("what is the state of my living room thermostat?", "other"),
]
for i, (p, e) in enumerate(ha, 1):
    add(f"ha-{i:03d}", "ha", p, e)

# ── actuator: request → HA service calls (entity list appended) ──────────────
actuator = [
    ("turn on the office light", [{"domain": "light", "service": "turn_on", "entity_id": HUE}]),
    ("turn off the hue lamp", [{"domain": "light", "service": "turn_off", "entity_id": HUE}]),
    (
        "turn on the living room bulb",
        [{"domain": "light", "service": "turn_on", "entity_id": WIZ}],
    ),
    ("turn off the tv", [{"domain": "media_player", "service": "turn_off", "entity_id": TV}]),
    ("turn on the tv", [{"domain": "media_player", "service": "turn_on", "entity_id": TV}]),
    (
        "start the coffee machine",
        [{"domain": "switch", "service": "turn_on", "entity_id": COFFEE}],
    ),
    (
        "switch off the coffee machine",
        [{"domain": "switch", "service": "turn_off", "entity_id": COFFEE}],
    ),
    (
        "set the thermostat to 22 degrees",
        [{"domain": "climate", "service": "set_temperature", "entity_id": CLIMATE}],
    ),
    ("lock the front door", [{"domain": "lock", "service": "lock", "entity_id": LOCK}]),
    ("unlock the front door", [{"domain": "lock", "service": "unlock", "entity_id": LOCK}]),
    (
        "open the bedroom blinds",
        [{"domain": "cover", "service": "open_cover", "entity_id": BLINDS}],
    ),
    (
        "make the living room bulb blue",
        [{"domain": "light", "service": "turn_on", "entity_id": WIZ}],
    ),
    (
        "lock the front door and turn off the office light",
        [
            {"domain": "lock", "service": "lock", "entity_id": LOCK},
            {"domain": "light", "service": "turn_off", "entity_id": HUE},
        ],
    ),
    # ── refusal cases: the named device does not exist in the payload ────────
    ("turn on the bedroom light", []),
    ("lock the back door", []),
    ("start the garden sprinklers", []),
    ("open the garage door", []),
]
for i, (p, e) in enumerate(actuator, 1):
    add(f"actuator-{i:03d}", "actuator", p + ENTITIES, e)

# ── planner: pipeline requests → allowed agent types ─────────────────────────
planner = [
    (
        "when the front door opens, turn on the office light (light.philips_hue_lct015). "
        "Door state arrives on homeassistant/state_changes/# with entity_id "
        "binary_sensor.front_door_contact.",
        ["ha_actuator", "dynamic"],
    ),
    (
        "every weekday at 7:30 turn on the coffee machine (switch.coffee_machine).",
        ["scheduled", "ha_actuator"],
    ),
    (
        "at 23:00 every day close the bedroom blinds (cover.bedroom_blinds).",
        ["scheduled", "ha_actuator"],
    ),
    (
        "when sensor.living_room_temperature goes above 28, send a Discord notification "
        "via webhook https://discord.example/hook.",
        ["dynamic", "ha_actuator", "scheduled"],
    ),
    (
        "if the living room bulb (light.wiz_rgbw_tunable_351b6e) stays on for more than "
        "2 hours, turn it off.",
        ["dynamic", "ha_actuator", "scheduled"],
    ),
    (
        "when a person is detected on the pc camera, turn on the office light "
        "(light.philips_hue_lct015).",
        ["dynamic", "ha_actuator"],
    ),
    (
        "every 30 minutes publish the living room temperature to custom/home/temp.",
        ["scheduled", "dynamic"],
    ),
    (
        "when the front door unlocks, send me a Telegram message.",
        ["dynamic", "ha_actuator"],
    ),
    (
        "turn off the tv (media_player.samsung_7_series_55_ue55nu7023) at midnight every night.",
        ["scheduled", "ha_actuator"],
    ),
    (
        "when the coffee machine turns on, log a message.",
        ["dynamic", "ha_actuator"],
    ),
    (
        "if nobody is home and the office light is on, turn it off.",
        ["dynamic", "ha_actuator"],
    ),
    (
        "every morning at 8 read the weather and post it to custom/home/weather.",
        ["scheduled", "dynamic"],
    ),
    (
        "when the bedroom blinds open, set the thermostat to 21 degrees.",
        ["dynamic", "ha_actuator"],
    ),
    (
        "alert me when the living room temperature drops below 16 degrees.",
        ["dynamic", "ha_actuator", "scheduled"],
    ),
    (
        "at sunset turn on the living room bulb (light.wiz_rgbw_tunable_351b6e).",
        ["scheduled", "ha_actuator"],
    ),
]
for i, (p, e) in enumerate(planner, 1):
    add(f"planner-{i:03d}", "planner", p, e)

# ── dynamic: agent code generation → required async functions ────────────────
dynamic = [
    (
        "Write an agent that subscribes to sensors/temperature and stores the latest value "
        "in state; process() should log an alert when the value exceeds 30.",
        ["setup", "process"],
    ),
    (
        'Write an agent whose handle_task(agent, payload) receives {"city": str} and returns '
        '{"result": str} with the city name upper-cased.',
        ["handle_task"],
    ),
    (
        'Write an agent that every poll publishes {"value": <random 0-100>} to '
        "custom/demo/random and logs the value.",
        ["process"],
    ),
    (
        "Write an agent that subscribes to homeassistant/state_changes/# and logs every "
        "change whose entity_id starts with light.",
        ["setup"],
    ),
    (
        "Write an agent that counts messages on custom/demo/events, persists the counter "
        "so it survives a restart, and logs the total each poll.",
        ["setup", "process"],
    ),
    (
        "Write an agent that on each poll reads sensor.living_room_temperature via "
        "agent.mqtt_get and publishes a rolling average to custom/home/temp_avg.",
        ["setup", "process"],
    ),
    (
        "Write an agent that posts a message to a Discord webhook when it receives a task. "
        "The webhook URL is in payload['url'] and the text in payload['text'].",
        ["handle_task"],
    ),
    (
        "Write an agent that keeps a 5-minute sliding window over custom/home/temp and "
        "alerts when the temperature is rising fast.",
        ["setup", "process"],
    ),
    (
        "Write an agent that answers questions using agent.llm.chat and returns the reply "
        "in {'result': ...}.",
        ["handle_task"],
    ),
    (
        "Write an agent that publishes the machine's CPU percentage to custom/host/cpu "
        "every poll using psutil.",
        ["process"],
    ),
    (
        "Write an agent that subscribes to custom/demo/commands and calls agent.log for "
        "each command received, cleaning up its state on stop.",
        ["setup", "cleanup"],
    ),
    (
        "Write an agent that on start declares a topic contract publishing "
        "custom/home/presence, then publishes presence updates every poll.",
        ["setup", "process"],
    ),
    (
        "Write an agent that reads a JSON file path from payload['path'] and returns the "
        "number of keys in it.",
        ["handle_task"],
    ),
    (
        "Write an agent that watches custom/home/energy and persists the highest value "
        "seen so far.",
        ["setup"],
    ),
    (
        "Write an agent that每 poll checks whether custom/home/heartbeat has been silent "
        "for 60 seconds and alerts if so.".replace("每", "every "),
        ["setup", "process"],
    ),
]
for i, (p, e) in enumerate(dynamic, 1):
    add(f"dynamic-{i:03d}", "dynamic", p, e)

out = Path("bench.jsonl")
with out.open("w", encoding="utf-8") as fh:
    for case in cases:
        fh.write(json.dumps(case, ensure_ascii=False) + "\n")

from collections import Counter

print(f"{len(cases)} cases -> {out}")
print(Counter(c["category"] for c in cases))

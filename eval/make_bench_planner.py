"""Grounded `planner` cases — entity, topic AND agent-reuse resolution.

The real PlannerAgent prompt always carries three context blocks the model must
use: AVAILABLE HA ENTITIES, AVAILABLE AGENTS (with input/output contracts,
published topics and observed payload samples), and LIVE TOPIC SCHEMAS. It has
to decide for itself which entity a phrase refers to, which topic carries a
trigger, and — critically — whether an agent that can already do the job exists
or a new one must be spawned.

These cases reproduce all three blocks and score with the grounded form

    {"types": [...], "must_contain": [...], "must_not_contain": [...]}

so a plan passes only if it has the right shape *and* names the right entity,
the right topic, or the right existing agent.

    python make_bench_planner.py --source actuator.jsonl --out planner_grounded.jsonl

The agent roster below is synthetic but formatted exactly like the framework's
`_fmt_worker()` output. Edit ROSTER to match the agents you actually run, then
regenerate — the closer it is to your live system, the more the numbers mean.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# ── This home ────────────────────────────────────────────────────────────────
HUE = "light.philips_hue_lct015"  # Office
WIZ = "light.wiz_rgbw_tunable_351b6e"  # Living Room
TV = "media_player.samsung_7_series_55_ue55nu7023"  # Living Room
REC = "switch.living_room_record"  # Camera Record
STATE_TOPIC = "homeassistant/state_changes"

# ── Existing agents the planner can reuse ────────────────────────────────────
# Deliberately includes quirky field names ("temp", "pct") — reusing a topic
# means using ITS field names, which is the auto-wiring failure this framework
# exists to prevent.
ROSTER = [
    {
        "name": "office-climate-sensor",
        "type": "dynamic",
        "description": "Publishes office temperature and humidity every 60s",
        "capabilities": ["temperature", "humidity", "office", "climate"],
        "publishes": ["custom/office/climate"],
        "samples": {"custom/office/climate": ({"temp": "float", "hum": "float"}, {"temp": 24.6, "hum": 41})},
    },
    {
        "name": "discord-notifier",
        "type": "dynamic",
        "description": "Sends a message to a Discord webhook",
        "capabilities": ["discord", "notify", "message", "alert"],
        "input_schema": {"text": "str — message body"},
        "output_schema": {"ok": "bool"},
    },
    {
        "name": "telegram-notifier",
        "type": "dynamic",
        "description": "Sends a Telegram message to the configured chat",
        "capabilities": ["telegram", "notify", "message"],
        "input_schema": {"text": "str — message body"},
        "output_schema": {"ok": "bool"},
    },
    {
        "name": "camera-person-detector",
        "type": "dynamic",
        "description": "Runs person detection on the pc camera and publishes detections",
        "capabilities": ["camera", "vision", "person", "detection"],
        "publishes": ["custom/camera/detections"],
        "samples": {
            "custom/camera/detections": (
                {"person": "bool", "conf": "float"},
                {"person": True, "conf": 0.91},
            )
        },
    },
    {
        "name": "energy-monitor",
        "type": "dynamic",
        "description": "Publishes current household power draw",
        "capabilities": ["energy", "power", "consumption"],
        "publishes": ["custom/home/energy"],
        "samples": {"custom/home/energy": ({"watts": "int"}, {"watts": 812})},
    },
    {
        "name": "weather-agent",
        "type": "dynamic",
        "description": "Fetches current weather for a city from wttr.in",
        "capabilities": ["weather", "forecast", "temperature", "outdoor"],
        "input_schema": {"city": "str"},
        "output_schema": {"temp_c": "float", "condition": "str"},
    },
    {
        "name": "presence-tracker",
        "type": "dynamic",
        "description": "Tracks whether anyone is home from phone device trackers",
        "capabilities": ["presence", "occupancy", "home", "away"],
        "publishes": ["custom/home/presence"],
        "samples": {"custom/home/presence": ({"home": "bool", "who": "str"}, {"home": False, "who": ""})},
    },
    {
        "name": "battery-watch",
        "type": "dynamic",
        "description": "Publishes phone battery level and charging state",
        "capabilities": ["battery", "phone", "charge"],
        "publishes": ["custom/phone/battery"],
        "samples": {"custom/phone/battery": ({"pct": "int", "charging": "bool"}, {"pct": 63, "charging": False})},
    },
    {
        "name": "anomaly-detector",
        "type": "dynamic",
        "description": "Flags outliers in any numeric MQTT stream",
        "capabilities": ["anomaly", "outlier", "statistics"],
        "input_schema": {"topic": "str", "field": "str"},
        "output_schema": {"anomaly": "bool", "score": "float"},
    },
    {
        "name": "manual-agent",
        "type": "dynamic",
        "description": "Finds device manuals online and answers questions from them",
        "capabilities": ["manuals", "pdf", "device_docs"],
        "input_schema": {"text": "str"},
        "output_schema": {"result": "str"},
    },
]


def roster_block() -> str:
    """Formatted exactly like PlannerAgent._fmt_worker() + LIVE TOPIC SCHEMAS."""
    lines = ["\n\nAVAILABLE AGENTS (with input/output contracts):"]
    schema_lines = []
    for w in ROSTER:
        lines.append(f"  - {w['name']} ({w['type']}): {w['description']}")
        if w.get("capabilities"):
            lines.append(f"    capabilities: {', '.join(w['capabilities'])}")
        if w.get("input_schema"):
            lines.append(f"    input_schema : {w['input_schema']}")
        if w.get("output_schema"):
            lines.append(f"    output_schema: {w['output_schema']}")
        if w.get("publishes"):
            lines.append(f"    publishes: {w['publishes']}")
        for topic, (fields, example) in (w.get("samples") or {}).items():
            lines.append(f"    topic '{topic}' payload fields: {fields}  example: {example}")
            schema_lines.append(f"  {topic} (by {w['name']}): fields={fields}  example={example}")
    if schema_lines:
        lines.append("\nLIVE TOPIC SCHEMAS (use EXACTLY these field names in generated code):")
        lines.extend(schema_lines)
        lines.append(
            "CRITICAL: Use the exact field names from the samples above. "
            "If a sample shows 'temp', use payload['temp'] — NOT payload['temperature']."
        )
    return "\n".join(lines)


ACTUATOR = ["ha_actuator", "dynamic"]
SCHEDULED = ["scheduled", "ha_actuator", "dynamic"]
REACTIVE = ["dynamic", "ha_actuator", "scheduled"]

# (request, expected, group)
CASES = [
    # ── entity resolution ───────────────────────────────────────────────────
    (
        "when the front door opens, turn on the office light",
        {"types": ACTUATOR, "must_contain": [HUE], "must_not_contain": [WIZ]},
        "entity",
    ),
    (
        "when motion is detected, switch on the living room lamp",
        {"types": ACTUATOR, "must_contain": [WIZ], "must_not_contain": [HUE]},
        "entity",
    ),
    (
        "turn off the hue lamp every night at midnight",
        {"types": SCHEDULED, "must_contain": [HUE], "must_not_contain": [WIZ]},
        "entity",
    ),
    ("at 23:00 turn off the samsung tv", {"types": SCHEDULED, "must_contain": [TV]}, "entity"),
    (
        "when someone comes home, switch on the lamp in the living room",
        {"types": REACTIVE, "must_contain": [WIZ], "must_not_contain": [HUE]},
        "entity",
    ),
    (
        "every morning at 7 turn on the philips hue in the office",
        {"types": SCHEDULED, "must_contain": [HUE], "must_not_contain": [WIZ]},
        "entity",
    ),
    (
        "when the camera detects motion at night, start recording",
        {"types": REACTIVE, "must_contain": [REC]},
        "entity",
    ),
    (
        "when the tv is switched off, turn the office light back on",
        {"types": REACTIVE, "must_contain": [TV, HUE]},
        "entity",
    ),
    # ── topic resolution against HA state bridge ────────────────────────────
    (
        "when the tv turns on, dim the living room lamp",
        {"types": ACTUATOR, "must_contain": [STATE_TOPIC, WIZ]},
        "topic",
    ),
    (
        "if the living room lamp stays on for more than two hours, turn it off",
        {"types": REACTIVE, "must_contain": [STATE_TOPIC, WIZ]},
        "topic",
    ),
    (
        "log every time the office light changes state",
        {"types": REACTIVE, "must_contain": [STATE_TOPIC, HUE]},
        "topic",
    ),
    (
        "when the front door contact opens, turn on the camera recording",
        {"types": REACTIVE, "must_contain": [STATE_TOPIC, REC]},
        "topic",
    ),
    (
        "tell me when the tv has been on for more than three hours",
        {"types": REACTIVE, "must_contain": [STATE_TOPIC, TV]},
        "topic",
    ),
    # ── REUSE: an existing agent already publishes what is needed ───────────
    (
        "when the office gets warmer than 25 degrees, turn on the living room lamp",
        {"types": REACTIVE, "must_contain": ["custom/office/climate", WIZ]},
        "reusetopic",
    ),
    (
        "when a person is detected on the camera, turn on the office light",
        {"types": REACTIVE, "must_contain": ["custom/camera/detections", HUE]},
        "reusetopic",
    ),
    (
        "alert me when power consumption goes above 3000 watts",
        {"types": REACTIVE, "must_contain": ["custom/home/energy"]},
        "reusetopic",
    ),
    (
        "when nobody is home, turn off the living room lamp",
        {"types": REACTIVE, "must_contain": ["custom/home/presence", WIZ]},
        "reusetopic",
    ),
    (
        "warn me when the phone battery drops below 15%",
        {"types": REACTIVE, "must_contain": ["custom/phone/battery"]},
        "reusetopic",
    ),
    (
        "if the office humidity goes above 60, turn off the tv",
        {"types": REACTIVE, "must_contain": ["custom/office/climate", TV]},
        "reusetopic",
    ),
    (
        "when someone is at the camera and nobody is home, start recording",
        {"types": REACTIVE, "must_contain": ["custom/camera/detections", "custom/home/presence", REC]},
        "reusetopic",
    ),
    (
        "turn off the living room lamp when the phone starts charging",
        {"types": REACTIVE, "must_contain": ["custom/phone/battery", WIZ]},
        "reusetopic",
    ),
    (
        "publish a daily summary of household power use",
        {"types": SCHEDULED, "must_contain": ["custom/home/energy"]},
        "reusetopic",
    ),
    (
        "when the office temperature drops below 18, turn on the hue lamp as a reminder",
        {"types": REACTIVE, "must_contain": ["custom/office/climate", HUE]},
        "reusetopic",
    ),
    # ── REUSE: an existing agent should be delegated to, not recreated ──────
    (
        "send me a discord message when the front door opens",
        {"types": REACTIVE, "must_contain": ["discord-notifier", STATE_TOPIC]},
        "reuseagent",
    ),
    (
        "notify me on telegram every morning at 8 with the weather",
        {"types": SCHEDULED, "must_contain": ["telegram-notifier", "weather-agent"]},
        "reuseagent",
    ),
    (
        "flag anomalies in the office temperature stream",
        {"types": REACTIVE, "must_contain": ["anomaly-detector", "custom/office/climate"]},
        "reuseagent",
    ),
    (
        "discord me when a person is detected on the camera",
        {"types": REACTIVE, "must_contain": ["discord-notifier", "custom/camera/detections"]},
        "reuseagent",
    ),
    (
        "look up the manual for my hue bulb and tell me how to reset it",
        {"types": REACTIVE, "must_contain": ["manual-agent"]},
        "reuseagent",
    ),
    (
        "telegram me if the power draw looks anomalous",
        {"types": REACTIVE, "must_contain": ["anomaly-detector", "custom/home/energy"]},
        "reuseagent",
    ),
    (
        "send a discord message when nobody has been home for 8 hours",
        {"types": REACTIVE, "must_contain": ["discord-notifier", "custom/home/presence"]},
        "reuseagent",
    ),
    # ── clock triggers must become scheduled agents ─────────────────────────
    (
        "every day at 3pm turn on the samsung tv",
        {"types": SCHEDULED, "must_contain": [TV]},
        "schedule",
    ),
    (
        "turn off all the lights every weekday at 18:30",
        {"types": SCHEDULED, "must_contain": [HUE]},
        "schedule",
    ),
    (
        "every 30 minutes publish the current time to custom/home/clock",
        {"types": SCHEDULED, "must_contain": ["custom/home/clock"]},
        "schedule",
    ),
    (
        "every hour check the weather and log it",
        {"types": SCHEDULED, "must_contain": ["weather-agent"]},
        "schedule",
    ),
    (
        "on saturday mornings at 9 turn on the living room lamp",
        {"types": SCHEDULED, "must_contain": [WIZ]},
        "schedule",
    ),
    # ── multi-agent chains ──────────────────────────────────────────────────
    (
        "when a person is detected, turn on the office light AND send me a discord message",
        {"types": REACTIVE, "must_contain": ["custom/camera/detections", HUE, "discord-notifier"]},
        "chain",
    ),
    (
        "if the office gets too warm, turn off the tv and telegram me about it",
        {"types": REACTIVE, "must_contain": ["custom/office/climate", TV, "telegram-notifier"]},
        "chain",
    ),
    (
        "when nobody is home, turn off the lamp and the tv and start the camera recording",
        {"types": REACTIVE, "must_contain": ["custom/home/presence", WIZ, TV, REC]},
        "chain",
    ),
    (
        "every evening at 22:00 turn off the tv, then discord me a confirmation",
        {"types": SCHEDULED, "must_contain": [TV, "discord-notifier"]},
        "chain",
    ),
    # ── CREATE: nothing in the roster covers this ───────────────────────────
    ("log a heartbeat message every hour", SCHEDULED, "create"),
    (
        "publish the wifi signal strength to custom/host/wifi every minute",
        SCHEDULED,
        "create",
    ),
    (
        "watch my downloads folder and publish new filenames to custom/host/downloads",
        REACTIVE,
        "create",
    ),
    (
        "publish the number of unread emails to custom/mail/unread every 10 minutes",
        SCHEDULED,
        "create",
    ),
    (
        "monitor the CPU temperature and alert me if it exceeds 80 degrees",
        REACTIVE,
        "create",
    ),
    (
        "scrape the price of a product page daily and publish it to custom/shop/price",
        SCHEDULED,
        "create",
    ),
    # ── infeasible: the device does not exist in this home ──────────────────
    (
        "when the front door opens, turn on the kitchen light",
        {"types": ACTUATOR, "must_not_contain": [HUE, WIZ]},
        "infeasible",
    ),
    (
        "every evening at 6 set the thermostat to 21 degrees",
        {"types": SCHEDULED, "must_not_contain": [HUE, WIZ, TV]},
        "infeasible",
    ),
    (
        "lock the front door every night at midnight",
        {"types": SCHEDULED, "must_not_contain": [HUE, WIZ, TV, REC]},
        "infeasible",
    ),
    (
        "close the bedroom blinds at sunset",
        {"types": SCHEDULED, "must_not_contain": [HUE, WIZ, TV, REC]},
        "infeasible",
    ),
    (
        "start the dishwasher when electricity is cheapest",
        {"types": REACTIVE, "must_not_contain": [HUE, WIZ, TV, REC]},
        "infeasible",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="actuator.jsonl", help="file from make_bench_actuator.py")
    ap.add_argument("--out", default="planner_grounded.jsonl")
    ap.add_argument(
        "--with-devices",
        action="store_true",
        help="also attach the full devices JSON (the planner's own prompt is "
        "entity-list based, so this is off by default)",
    )
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"{src} not found — run make_bench_actuator.py first.")
        return 1

    sample = json.loads(src.read_text(encoding="utf-8").splitlines()[0])["prompt"]
    devices = None
    if sample.lstrip().startswith("{"):
        parsed = json.loads(sample)
        devices = parsed["devices"]
        entities = "\n\n[AVAILABLE HA ENTITIES" + parsed["user_request"].split(
            "[AVAILABLE HA ENTITIES", 1
        )[1]
    else:
        entities = "\n\n[AVAILABLE HA ENTITIES" + sample.split("[AVAILABLE HA ENTITIES", 1)[1]

    agents = roster_block()

    def build_prompt(request: str) -> str:
        enriched = request + entities + agents
        if devices is not None and args.with_devices:
            return json.dumps({"user_request": enriched, "devices": devices})
        return enriched

    cases = [
        {
            "id": f"planner-{group}-{i:03d}",
            "category": "planner",
            "prompt": build_prompt(request),
            "expected": expected,
        }
        for i, (request, expected, group) in enumerate(CASES, 1)
    ]

    out = Path(args.out)
    out.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )

    from collections import Counter

    print(f"{len(cases)} cases -> {out} [{out.stat().st_size / 1024:.0f} KB]")
    print("groups:", dict(Counter(c["id"].split("-")[1] for c in cases)))
    print(f"roster: {len(ROSTER)} agents — edit ROSTER to match the agents you actually run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

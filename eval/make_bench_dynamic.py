"""Strict `dynamic` (code-generation) cases — 50 of them.

Checking only "does it compile and define setup/process" scores this as a pass::

    async def setup(agent): pass
    async def process(agent): pass

so every case here uses the strict form: required entry points, required API
calls and topics the task actually needs, plus structural checks for the
mistakes the framework's own prompt warns about (module-level imports,
``await agent.subscribe(...)``, subscribing without a callback, importing an
LLM client instead of using ``agent.llm``, empty function bodies).

    python make_bench_dynamic.py --out dynamic_strict.jsonl

Sized at 50 deliberately. At n=15 the 95% CI on 80% accuracy runs [55%, 93%] —
wide enough to swallow the entire gap between a 4B model and a frontier one, so
no ranking claim survives review. 50 roughly halves that interval; report paired
tests (McNemar) over the shared cases rather than comparing intervals.

The agent API below is the one the framework actually exposes (see
``_CODEGEN_SYSTEM`` in wactorz/evalharness.py, which mirrors the production
orchestrator prompt)::

    agent.state                      dict, persists across process() calls
    await agent.publish(topic, data)
    await agent.mqtt_get(topic)      one-shot read
    await agent.log(msg) / await agent.alert(msg, severity)
    agent.persist(key, value)        NOT awaitable
    agent.recall(key)                NOT awaitable
    agent.subscribe(topic, cb)       NOT awaitable, callback REQUIRED
    agent.window(topic, seconds=N)   NOT awaitable
    agent.declare_contract(publishes=[...], subscribes=[...])
    await agent.llm.chat(prompt)     never import your own client

What is still NOT checked: whether the agent actually *works*. That needs the
code spawned in a live DynamicAgent with synthetic MQTT traffic — the
"contract-pass rate" experiment. These checks are a strong static proxy, and the
paper must say "satisfies structural contracts", never "is correct".
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

# (request, expected, group)
CASES = [
    # ── subscribe / react ───────────────────────────────────────────────────
    (
        "Write an agent that subscribes to custom/office/climate and stores the latest "
        "reading in agent.state; process() should alert when the temperature exceeds 30. "
        "The payload fields are {'temp': float, 'hum': float}.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/office/climate", "agent.subscribe", "temp"],
            "must_not_contain": ["'temperature'", '"temperature"'],
        },
        "subscribe",
    ),
    (
        "Write an agent that watches custom/camera/detections and logs a message whenever "
        "a person is detected. Payload fields: {'person': bool, 'conf': float}.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/camera/detections", "agent.subscribe", "person"],
        },
        "subscribe",
    ),
    (
        "Write an agent that every poll checks whether custom/home/heartbeat has been "
        "silent for 60 seconds and alerts if so.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/home/heartbeat", "agent.subscribe"],
        },
        "subscribe",
    ),
    (
        "Write an agent that subscribes to custom/home/doorbell and sends an alert with "
        "severity 'warning' every time the bell rings.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/home/doorbell", "agent.subscribe", "agent.alert"],
        },
        "subscribe",
    ),
    (
        "Write an agent that subscribes to BOTH custom/office/climate and "
        "custom/home/energy and keeps the newest payload from each in agent.state.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/office/climate", "custom/home/energy", "agent.subscribe"],
        },
        "subscribe",
    ),
    (
        "Write an agent that watches homeassistant/state_changes and logs whenever "
        "binary_sensor.front_door_contact changes to 'on'. The state is nested: "
        "payload['new_state']['state'].",
        {
            "functions": ["setup"],
            "must_contain": [
                "homeassistant/state_changes",
                "agent.subscribe",
                "binary_sensor.front_door_contact",
                "new_state",
            ],
        },
        "subscribe",
    ),
    (
        "Write an agent that counts how many messages arrive on custom/demo/events and "
        "logs a running total every poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/demo/events", "agent.subscribe", "agent.log"],
        },
        "subscribe",
    ),
    # ── publish ─────────────────────────────────────────────────────────────
    (
        "Write an agent that every poll publishes {'value': <random 0-100>} to "
        "custom/demo/random and logs the value.",
        {
            "functions": ["process"],
            "must_contain": ["custom/demo/random", "agent.publish", "random"],
        },
        "publish",
    ),
    (
        "Write an agent that publishes the machine's CPU percentage to custom/host/cpu "
        "every poll using psutil.",
        {
            "functions": ["process"],
            "must_contain": ["custom/host/cpu", "agent.publish", "psutil"],
        },
        "publish",
    ),
    (
        "Write an agent that publishes free disk space in gigabytes to custom/host/disk "
        "on every poll.",
        {
            "functions": ["process"],
            "must_contain": ["custom/host/disk", "agent.publish"],
        },
        "publish",
    ),
    (
        "Write an agent that reads custom/office/climate (fields {'temp': float, "
        "'hum': float}) and republishes the temperature in Fahrenheit to "
        "custom/office/climate_f.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/office/climate", "custom/office/climate_f", "agent.publish", "temp"],
            "must_not_contain": ["'temperature'", '"temperature"'],
        },
        "publish",
    ),
    (
        "Write an agent that publishes the current UTC timestamp to custom/home/clock "
        "on every poll.",
        {
            "functions": ["process"],
            "must_contain": ["custom/home/clock", "agent.publish"],
        },
        "publish",
    ),
    # ── handle_task ─────────────────────────────────────────────────────────
    (
        "Write an agent whose handle_task(agent, payload) receives {'city': str} and "
        "returns {'result': str} with the city name upper-cased.",
        {"functions": ["handle_task"], "must_contain": ["city", "result", "upper"]},
        "task",
    ),
    (
        "Write an agent that answers questions with the framework's LLM: handle_task "
        "receives {'text': str} and returns {'result': <the reply>}.",
        {
            "functions": ["handle_task"],
            "must_contain": ["agent.llm", "result"],
            "must_not_contain": ["import openai", "import anthropic", "api_key"],
        },
        "task",
    ),
    (
        "Write an agent that posts to a Discord webhook when it receives a task. The URL "
        "is payload['url'] and the message payload['text'].",
        {"functions": ["handle_task"], "must_contain": ["url", "text"]},
        "task",
    ),
    (
        "Write an agent whose handle_task receives {'a': float, 'b': float} and returns "
        "{'result': <a divided by b>}, returning an error string instead of crashing "
        "when b is zero.",
        {"functions": ["handle_task"], "must_contain": ["result"]},
        "task",
    ),
    (
        "Write an agent whose handle_task receives {'topic': str} and returns "
        "{'result': <the latest payload on that topic>} using the one-shot read API.",
        {"functions": ["handle_task"], "must_contain": ["agent.mqtt_get", "result", "topic"]},
        "task",
    ),
    (
        "Write an agent whose handle_task receives {'text': str}, publishes it to "
        "custom/notify/out, and returns {'ok': True}.",
        {
            "functions": ["handle_task"],
            "must_contain": ["custom/notify/out", "agent.publish", "text"],
        },
        "task",
    ),
    # ── state / persistence ─────────────────────────────────────────────────
    (
        "Write an agent that counts messages on custom/demo/events, persists the counter "
        "so it survives a restart, and logs the total each poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/demo/events", "agent.persist", "agent.recall"],
        },
        "state",
    ),
    (
        "Write an agent that watches custom/home/energy (fields {'watts': int}) and "
        "persists the highest value seen so far.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/home/energy", "agent.persist", "watts"],
        },
        "state",
    ),
    (
        "Write an agent that restores its counter from disk in setup() and increments it "
        "on every poll, saving it back each time.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.recall", "agent.persist"],
            "must_not_contain": ["await agent.persist", "await agent.recall"],
        },
        "state",
    ),
    (
        "Write an agent that keeps the last 10 readings from custom/office/climate in "
        "agent.state and logs their average every poll. Payload fields: {'temp': float}.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/office/climate", "agent.state", "temp"],
        },
        "state",
    ),
    (
        "Write an agent that persists the timestamp of the last message seen on "
        "custom/home/presence so it knows how long the house has been empty.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/home/presence", "agent.persist"],
        },
        "state",
    ),
    # ── sliding window / temporal ───────────────────────────────────────────
    (
        "Write an agent that keeps a 5-minute sliding window over custom/office/climate "
        "and alerts when the temperature is rising fast. Use the framework's window API.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.window", "custom/office/climate"],
            "must_not_contain": ["await agent.window"],
        },
        "window",
    ),
    (
        "Write an agent that uses a 60-second window over custom/home/energy to publish a "
        "rolling mean to custom/home/energy_avg every poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.window", "custom/home/energy_avg", "agent.publish"],
            "must_not_contain": ["await agent.window"],
        },
        "window",
    ),
    (
        "Write an agent that keeps a 10-minute window over custom/camera/detections and "
        "alerts if more than 5 people were detected in that time.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.window", "custom/camera/detections", "agent.alert"],
            "must_not_contain": ["await agent.window"],
        },
        "window",
    ),
    (
        "Write an agent that windows custom/phone/battery over 30 minutes and warns when "
        "the level is falling while not charging. Fields: {'pct': int, 'charging': bool}.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.window", "custom/phone/battery", "pct"],
            "must_not_contain": ["await agent.window", "'level'", '"level"'],
        },
        "window",
    ),
    # ── one-shot read ───────────────────────────────────────────────────────
    (
        "Write an agent that on each poll reads one message from custom/home/energy with "
        "the one-shot read API and republishes a rolling average to custom/home/energy_avg.",
        {
            "functions": ["process"],
            "must_contain": ["agent.mqtt_get", "custom/home/energy_avg", "agent.publish"],
        },
        "oneshot",
    ),
    (
        "Write an agent that reads the newest value from custom/office/climate once per "
        "poll and logs it. Do not hold a continuous subscription.",
        {
            "functions": ["process"],
            "must_contain": ["agent.mqtt_get", "custom/office/climate"],
            "must_not_contain": ["agent.subscribe"],
        },
        "oneshot",
    ),
    (
        "Write an agent that on each poll one-shot reads custom/home/presence and "
        "custom/home/energy and publishes a combined summary to custom/home/status.",
        {
            "functions": ["process"],
            "must_contain": [
                "agent.mqtt_get",
                "custom/home/presence",
                "custom/home/status",
                "agent.publish",
            ],
        },
        "oneshot",
    ),
    # ── cleanup ─────────────────────────────────────────────────────────────
    (
        "Write an agent that subscribes to custom/demo/commands, logs each command, and "
        "releases its resources when the agent stops.",
        {
            "functions": ["setup", "cleanup"],
            "must_contain": ["custom/demo/commands", "agent.subscribe"],
        },
        "cleanup",
    ),
    (
        "Write an agent that opens a file handle in setup(), appends every message from "
        "custom/demo/events to it, and closes it when the agent stops.",
        {
            "functions": ["setup", "cleanup"],
            "must_contain": ["custom/demo/events", "agent.subscribe", "close"],
        },
        "cleanup",
    ),
    (
        "Write an agent that grabs the webcam with cv2 in setup() and releases it in "
        "cleanup(), publishing a frame count to custom/camera/frames each poll.",
        {
            "functions": ["setup", "process", "cleanup"],
            "must_contain": ["cv2", "custom/camera/frames", "release"],
        },
        "cleanup",
    ),
    # ── contract declaration ────────────────────────────────────────────────
    (
        "Write an agent that declares a topic contract publishing custom/home/presence, "
        "then publishes presence updates every poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["declare_contract", "custom/home/presence", "agent.publish"],
        },
        "contract",
    ),
    (
        "Write an agent that declares it subscribes to custom/office/climate and "
        "publishes custom/office/comfort, then computes a comfort score each poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": [
                "declare_contract",
                "custom/office/climate",
                "custom/office/comfort",
            ],
        },
        "contract",
    ),
    (
        "Write an agent that declares a contract publishing custom/host/wifi and "
        "publishes the wifi signal strength every poll.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["declare_contract", "custom/host/wifi", "agent.publish"],
        },
        "contract",
    ),
    # ── blocking work done correctly ────────────────────────────────────────
    (
        "Write an agent that grabs a frame from the webcam with cv2 every poll and "
        "publishes the frame size to custom/camera/frames. Keep the event loop free.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["cv2", "custom/camera/frames", "run_in_executor"],
        },
        "blocking",
    ),
    (
        "Write an agent that runs a CPU-heavy checksum over a large file each poll and "
        "publishes the digest to custom/host/checksum without stalling the event loop.",
        {
            "functions": ["process"],
            "must_contain": ["custom/host/checksum", "run_in_executor", "agent.publish"],
        },
        "blocking",
    ),
    (
        "Write an agent that runs a YOLO model over a captured image every poll and "
        "publishes detections to custom/camera/detections. The model call is blocking.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["custom/camera/detections", "run_in_executor"],
        },
        "blocking",
    ),
    (
        "Write an agent that resolves a DNS name with a blocking library each poll and "
        "publishes the address to custom/host/dns. Do not block the loop.",
        {
            "functions": ["process"],
            "must_contain": ["custom/host/dns", "run_in_executor"],
        },
        "blocking",
    ),
    # ── framework LLM, never a private client ───────────────────────────────
    (
        "Write an agent that summarises the last 10 messages from custom/demo/events "
        "with the framework's LLM and publishes the summary to custom/demo/summary.",
        {
            "functions": ["setup", "process"],
            "must_contain": ["agent.llm", "custom/demo/summary", "agent.publish"],
            "must_not_contain": ["import openai", "import anthropic", "api_key"],
        },
        "llm",
    ),
    (
        "Write an agent that classifies each message on custom/mail/inbox as spam or not "
        "using the framework's LLM, and logs the verdict.",
        {
            "functions": ["setup"],
            "must_contain": ["agent.llm", "custom/mail/inbox", "agent.subscribe"],
            "must_not_contain": ["import openai", "import anthropic", "openai.chat"],
        },
        "llm",
    ),
    (
        "Write an agent that turns a temperature reading into a one-sentence comment "
        "using the framework's LLM and publishes it to custom/office/comment.",
        {
            "functions": ["process"],
            "must_contain": ["agent.llm", "custom/office/comment"],
            "must_not_contain": ["import openai", "import anthropic"],
        },
        "llm",
    ),
    # ── schema fidelity: use the field names you were given ─────────────────
    # The auto-wiring failure this framework exists to prevent — a generated
    # agent reading payload['temperature'] from a topic that publishes 'temp'
    # compiles, runs, and silently produces nothing.
    (
        "Write an agent that subscribes to custom/office/climate and alerts when it is "
        "too humid. A sample payload is {'temp': 24.6, 'hum': 41}.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/office/climate", "hum"],
            "must_not_contain": ["'humidity'", '"humidity"'],
        },
        "schema",
    ),
    (
        "Write an agent that subscribes to custom/phone/battery and warns below 15%. "
        "A sample payload is {'pct': 63, 'charging': False}.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/phone/battery", "pct"],
            "must_not_contain": ["'battery'", '"battery"', "'level'", '"level"', "'percent'"],
        },
        "schema",
    ),
    (
        "Write an agent that subscribes to custom/home/energy and publishes an alert "
        "above 3000. A sample payload is {'watts': 812}.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/home/energy", "watts"],
            "must_not_contain": ["'power'", '"power"', "'energy'", '"energy"'],
        },
        "schema",
    ),
    (
        "Write an agent that subscribes to custom/camera/detections and counts people. "
        "A sample payload is {'person': True, 'conf': 0.91}.",
        {
            "functions": ["setup"],
            "must_contain": ["custom/camera/detections", "person"],
            "must_not_contain": ["'people'", '"people"', "'detected'", '"detected"'],
        },
        "schema",
    ),
    # ── process() is polled, not a loop ─────────────────────────────────────
    (
        "Write an agent that publishes the current memory usage to custom/host/memory "
        "every poll. process() is called repeatedly on a timer.",
        {
            "functions": ["process"],
            "must_contain": ["custom/host/memory", "agent.publish"],
            "must_not_contain": ["while true"],
        },
        "poll",
    ),
    (
        "Write an agent that checks an HTTP endpoint each poll and alerts when it is "
        "unreachable. Do one check per call.",
        {
            "functions": ["process"],
            "must_contain": ["agent.alert"],
            "must_not_contain": ["while true"],
        },
        "poll",
    ),
    (
        "Write an agent that every poll publishes an incrementing sequence number to "
        "custom/demo/seq.",
        {
            "functions": ["process"],
            "must_contain": ["custom/demo/seq", "agent.publish"],
            "must_not_contain": ["while true", "time.sleep"],
        },
        "poll",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="dynamic_strict.jsonl")
    args = ap.parse_args()

    cases = [
        {
            "id": f"dynamic-{group}-{i:03d}",
            "category": "dynamic",
            "prompt": request,
            "expected": expected,
        }
        for i, (request, expected, group) in enumerate(CASES, 1)
    ]

    out = Path(args.out)
    out.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )

    print(f"{len(cases)} cases -> {out}")
    print("groups:", dict(Counter(c["id"].split("-")[1] for c in cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

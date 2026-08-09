"""Harder `intent` and `ha` cases — the documented decision boundaries.

The starter cases in make_bench.py are unambiguous, so every model gets them
right and the call site can't discriminate. These sit on the boundaries the
production prompts define *for themselves*, which is what makes them fair: a
wrong answer here is a real routing failure, not a gotcha.

    python make_bench_hard.py --out intent_ha_hard.jsonl
    python -m wactorz.evalharness --cases intent_ha_hard.jsonl \\
        --categories intent ha --models ... --temperature 0

Every expected label below is derivable from the shipped prompt text
(INTENT_CLASSIFIER_PROMPT in wactorz/agents/prompts/main_actor_prompts.py,
HA_ACTION_CLASSIFICATION_PROMPT in .../home_assistant_prompts.py) — the rule
each group tests is quoted in the comment above it. If you change either
prompt, re-read these labels before trusting the numbers.

Ids follow `<category>-<group>-<n>` so analyze_results.py breaks accuracy down
by *which* ambiguity a model fails on. That per-boundary table is the
interesting result: models rarely fail routing uniformly, they fail one
distinction.

DELIBERATELY EXCLUDED — genuinely ambiguous under the current prompts, so a
label would be arbitrary. Worth mentioning as a limitation rather than scoring:
  - "turn on the lights in 10 minutes"  (one-shot delayed: ACTUATE or PIPELINE?)
  - "turn on the office light and list my devices"  (ACTUATE + HA: the mixed
    rule says OTHER, the CRUD rule says HA — the prompt contradicts itself)
  - "it's dark in here"  (implicit actuation; no rule covers implicature)
  - "how much power am I using?"  (the energy-monitor agent publishes it, but a
    HA sensor also holds it — OTHER vs HA is a real coin-flip)
  - "spawn an agent that watches my cpu"  (asks to create a continuous agent:
    PIPELINE by outcome, OTHER by phrasing)
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

# ── intent: ACTUATE | HA | PIPELINE | OTHER ──────────────────────────────────
# (prompt, expected, group)
INTENT = [
    # "If the request mixes device control with non-HA tasks, return OTHER."
    # The failure mode: models latch onto the imperative verb and say ACTUATE.
    ("turn off the tv and write me a python function that reverses a string", "OTHER", "mixed"),
    ("lock the front door and then summarise this article for me", "OTHER", "mixed"),
    ("dim the office light and remind me what the capital of Greece is", "OTHER", "mixed"),
    ("set the thermostat to 21 and help me debug this stack trace", "OTHER", "mixed"),
    # PIPELINE = "a reactive rule that should run continuously". A recurring
    # time trigger is a standing rule, not an immediate command — but it is
    # phrased exactly like one.
    ("turn off all the lights at midnight", "PIPELINE", "sched"),
    ("start the coffee machine every morning at seven", "PIPELINE", "sched"),
    ("turn on the porch light at sunset every day", "PIPELINE", "sched"),
    ("close the blinds every evening when the sun goes down", "PIPELINE", "sched"),
    # Contrast pairs for the above — same verbs, no recurrence.
    ("turn off all the lights", "ACTUATE", "sched"),
    ("close the blinds", "ACTUATE", "sched"),
    # "If the request is about automations, listing, discovery, or CRUD,
    # return HA." An automation CRUD request reads exactly like a pipeline
    # rule; the word "automation" is the whole signal.
    (
        "create an automation that turns on the office light when the front door opens",
        "HA",
        "crud",
    ),
    (
        "add an automation to send me a notification when the temperature goes above 28",
        "HA",
        "crud",
    ),
    ("set up a home assistant automation for the blinds at sunset", "HA", "crud"),
    ("delete the automation that turns the lights off at midnight", "HA", "crud"),
    # Contrast pairs — the same rules, without "automation".
    ("when the front door opens turn on the office light", "PIPELINE", "crud"),
    ("if the temperature goes above 28 send me a notification", "PIPELINE", "crud"),
    # HA also covers querying "what devices or automations exist" and, via
    # recommend_hardware, hardware feasibility.
    ("what's the state of the living room thermostat?", "HA", "query"),
    ("is the office light on right now?", "HA", "query"),
    ("how many lights do I have?", "HA", "query"),
    ("what motion sensor should I buy for the hallway?", "HA", "query"),
    # Smart-home vocabulary, but general conversation/coding — OTHER.
    ("write me a python function that parses MQTT messages", "OTHER", "lexical"),
    ("explain how home assistant automations work", "OTHER", "lexical"),
    ("what's the difference between zigbee and z-wave?", "OTHER", "lexical"),
    ("review this yaml snippet and tell me why it fails to parse", "OTHER", "lexical"),
    # Real users don't speak in imperatives. Same intents, conversational.
    ("hey so I'm heading to bed, can you kill the tv please", "ACTUATE", "verbose"),
    ("it's freezing in here, bump the thermostat up to 23 would you", "ACTUATE", "verbose"),
    (
        "I keep forgetting to lock up — if the front door is still unlocked at 11pm "
        "just lock it and ping me on telegram",
        "PIPELINE",
        "verbose",
    ),
    (
        "could you please, whenever my phone battery drops under 20 percent, "
        "send me a discord message about it",
        "PIPELINE",
        "verbose",
    ),
    # Several devices, one immediate command — still ACTUATE, not a pipeline.
    ("turn off the tv, lock the front door and dim the office light", "ACTUATE", "multi"),
    ("close the blinds and switch on the living room lamp", "ACTUATE", "multi"),
    # Delegation to another wactorz agent. There is no DELEGATE label, so these
    # must land in OTHER (the main orchestrator path, which emits <delegate>).
    # The HA rule ends "...listing, discovery, or CRUD, return HA", and
    # "discovery" swallows anything with a device-shaped noun in it — so this is
    # where HA over-triggers. Boundary: does Home Assistant hold the answer, or
    # does a wactorz agent?
    ("look up the manual for my Philips 2200 and tell me how to descale it", "OTHER", "delegate"),
    ("what's the weather in Athens right now?", "OTHER", "delegate"),
    ("send a discord message saying I'm home", "OTHER", "delegate"),
    ("what agents are running?", "OTHER", "delegate"),
    ("install pdfplumber on the raspberry pi node", "OTHER", "delegate"),
    ("convert C:/docs/report.pdf to a presentation", "OTHER", "delegate"),
]

# ── ha: Home Assistant action classification ─────────────────────────────────
HA = [
    # "create_automation → ... even if they also mention choosing between
    # existing sensors, lights, or devices". The hardware talk is a decoy.
    (
        "which of my sensors should I use to build an automation that turns on "
        "the light when someone walks in?",
        "create_automation",
        "create",
    ),
    (
        "I want an automation for the blinds — is sunset or a light sensor the better trigger?",
        "create_automation",
        "create",
    ),
    (
        "build me something that starts the coffee machine when my alarm goes off",
        "create_automation",
        "create",
    ),
    # "recommend_hardware → user ONLY wants hardware suggestions, compatibility
    # info, or to know what the existing hardware can do" — no automation asked for.
    ("what motion sensor works best with home assistant?", "recommend_hardware", "hardware"),
    ("can my hue bulbs do colour temperature or just rgb?", "recommend_hardware", "hardware"),
    ("would a zigbee door sensor work with my current setup?", "recommend_hardware", "hardware"),
    # "Use other for existence, count, lookup, or state questions."
    # "'Do I have any thermometers?' is other, not list_devices."
    ("do I have any thermometers?", "other", "existence"),
    ("how many lights are in the office?", "other", "existence"),
    ("is there a camera in the living room?", "other", "existence"),
    ("do I have anything that measures humidity?", "other", "existence"),
    # "'What is the state of my thermometer?' is other, not list_entities."
    ("what is the state of my living room thermostat?", "other", "state"),
    ("what's the current temperature in the office?", "other", "state"),
    ("was the front door open at 5pm yesterday?", "other", "state"),
    # "Use other for camera/snapshot/stream questions" — explicitly enumerated.
    ("show me the kitchen camera", "other", "camera"),
    ("take a snapshot of the front door", "other", "camera"),
    ("give me the stream url for camera.backyard", "other", "camera"),
    # "delete_automation → user wants to delete/remove/DISABLE an existing
    # automation." Sounds like device actuation, or like an edit.
    ("turn off the automation called night mode", "delete_automation", "disable"),
    ("disable my sunset blinds automation", "delete_automation", "disable"),
    ("stop the midnight lights automation from running", "delete_automation", "disable"),
    # "edit_automation → update/change/rename/modify an existing automation."
    # The last one is phrased as an addition but targets an existing automation.
    ("change the morning coffee automation to fire at 8am instead", "edit_automation", "edit"),
    ("rename my bedtime automation to night mode", "edit_automation", "edit"),
    (
        "make the sunset blinds automation close the office blinds as well",
        "edit_automation",
        "edit",
    ),
    # Genuine inventory listing — phrased without the trigger words
    # "list"/"show" that make the starter cases trivial.
    ("give me a rundown of every automation currently configured", "list_automations", "list"),
    ("dump every entity id you know about", "list_entities", "list"),
    ("what areas are set up in home assistant?", "list_areas", "list"),
    ("I'd like an inventory of all my devices", "list_devices", "list"),
    # "Use unknown for non-Home-Assistant requests."
    ("what's the capital of Greece?", "unknown", "unknown"),
    ("write me a bash script that tails a log file", "unknown", "unknown"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="intent_ha_hard.jsonl")
    ap.add_argument(
        "--append-to",
        help="also concatenate an existing bench file (e.g. bench.jsonl) into --out",
    )
    args = ap.parse_args()

    cases = []
    for category, rows in (("intent", INTENT), ("ha", HA)):
        for i, (prompt, expected, group) in enumerate(rows, 1):
            cases.append(
                {
                    "id": f"{category}-{group}-{i:03d}",
                    "category": category,
                    "prompt": prompt,
                    "expected": expected,
                }
            )

    lines = [json.dumps(c, ensure_ascii=False) + "\n" for c in cases]
    if args.append_to:
        src = Path(args.append_to)
        if not src.exists():
            print(f"{src} not found")
            return 1
        lines = [ln + "\n" for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()] + lines

    Path(args.out).write_text("".join(lines), encoding="utf-8")

    print(f"{len(cases)} hard cases -> {args.out}")
    for category, rows in (("intent", INTENT), ("ha", HA)):
        groups = Counter(g for _, _, g in rows)
        labels = Counter(e for _, e, _ in rows)
        print(f"  {category:<7} {len(rows):>3} cases  groups={dict(groups)}")
        print(f"  {'':<7}     labels={dict(labels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Curated `actuator` cases for THIS home, reusing the real HA payload.

Reads the payload out of a file produced by make_bench_actuator.py (so no second
Home Assistant round-trip, and prompts stay byte-identical to production), then
writes hand-written requests that probe how a device is referred to:

  A. by name          "turn on the wiz lamp"
  B. by location      "turn on the office light"
  C. name + location  "turn off the hue lamp in the office"
  D. by attribute     dim / colour / volume / mute / pause
  E. camera switches  the switch.* entities this home actually has
  F. multi-action     two devices in one request
  G. refusals         devices this home does not own  -> expected []

    python make_bench_actuator_curated.py --source actuator.jsonl --out actuator_curated.jsonl

Only domain/service/entity_id are scored — never brightness/colour values, which
are legitimately model-dependent ("dim" could be 30% or 50%). What is being
measured is whether the right device gets the right kind of command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from make_bench_actuator import compact_devices, scrub_states

# ── This home's controllable entities ───────────────────────────────────────
WIZ = "light.wiz_rgbw_tunable_351b6e"  # "WiZ RGBW Tunable 351B6E"  — Living Room
HUE = "light.philips_hue_lct015"  # "Philips Hue LCT015"        — Office
TV = "media_player.samsung_7_series_55_ue55nu7023"  # Samsung 7 Series (55)      — Living Room
REC = "switch.living_room_record"  # "Camera Record"            — Living Room
PRIV = "switch.living_room_privacy_mode"  # "Camera Privacy mode"      — Living Room


def light(service, entity):
    return {"domain": "light", "service": service, "entity_id": entity}


def player(service, entity=TV):
    return {"domain": "media_player", "service": service, "entity_id": entity}


def switch(service, entity):
    return {"domain": "switch", "service": service, "entity_id": entity}


# (request, expected actions, group)
CASES = [
    # ── A. referred to by name ──────────────────────────────────────────────
    ("turn on the wiz lamp", [light("turn_on", WIZ)], "name"),
    ("turn off the wiz lamp", [light("turn_off", WIZ)], "name"),
    ("turn on the philips hue", [light("turn_on", HUE)], "name"),
    ("turn off the philips hue lamp", [light("turn_off", HUE)], "name"),
    ("turn off the samsung tv", [player("turn_off")], "name"),
    ("turn on the samsung tv", [player("turn_on")], "name"),
    # ── B. referred to by location ──────────────────────────────────────────
    ("turn on the office light", [light("turn_on", HUE)], "location"),
    ("turn off the office light", [light("turn_off", HUE)], "location"),
    ("turn off the lights in the office", [light("turn_off", HUE)], "location"),
    # NOTE: the living room also holds light.living_room_status_led (a camera
    # status LED). "lamp" should still resolve to the WiZ bulb — this case
    # deliberately tests that a non-lamp distractor is not picked.
    ("turn on the living room lamp", [light("turn_on", WIZ)], "location"),
    ("turn off the living room tv", [player("turn_off")], "location"),
    # ── C. name + location ──────────────────────────────────────────────────
    ("turn on the wiz lamp in the living room", [light("turn_on", WIZ)], "name+location"),
    ("turn off the hue lamp in the office", [light("turn_off", HUE)], "name+location"),
    ("switch off the wiz bulb in the living room", [light("turn_off", WIZ)], "name+location"),
    ("turn on the samsung tv in the living room", [player("turn_on")], "name+location"),
    # ── D. attribute-level actions ──────────────────────────────────────────
    ("dim the wiz lamp to 30%", [light("turn_on", WIZ)], "attribute"),
    ("set the office light to 80% brightness", [light("turn_on", HUE)], "attribute"),
    ("make the wiz lamp blue", [light("turn_on", WIZ)], "attribute"),
    ("set the wiz lamp to red", [light("turn_on", WIZ)], "attribute"),
    ("set the office lamp to warm white", [light("turn_on", HUE)], "attribute"),
    ("set the tv volume to 20", [player("volume_set")], "attribute"),
    ("mute the tv", [player("volume_mute")], "attribute"),
    ("pause the tv", [player("media_pause")], "attribute"),
    # ── E. the switches this home really has ────────────────────────────────
    ("turn on camera recording", [switch("turn_on", REC)], "switch"),
    ("stop recording on the camera", [switch("turn_off", REC)], "switch"),
    ("enable camera privacy mode", [switch("turn_on", PRIV)], "switch"),
    # ── F. two devices in one request ───────────────────────────────────────
    (
        "turn off the wiz lamp and the samsung tv",
        [light("turn_off", WIZ), player("turn_off")],
        "multi",
    ),
    (
        "turn on the office light and turn off the living room lamp",
        [light("turn_on", HUE), light("turn_off", WIZ)],
        "multi",
    ),
    # ── G. devices this home does not own -> must refuse ────────────────────
    ("set the thermostat to 22 degrees", [], "refusal"),
    ("lock the front door", [], "refusal"),
    ("open the blinds", [], "refusal"),
    ("turn on the kitchen light", [], "refusal"),
    ("turn on the bedroom lamp", [], "refusal"),
    ("start the coffee machine", [], "refusal"),
    ("turn on the fan", [], "refusal"),
    ("start the vacuum cleaner", [], "refusal"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="actuator.jsonl", help="file from make_bench_actuator.py")
    ap.add_argument("--out", default="actuator_curated.jsonl")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"{src} not found — run make_bench_actuator.py first.")
        return 1

    with src.open(encoding="utf-8") as fh:
        sample = json.loads(fh.readline())["prompt"]

    # Recover the payload shape (full = JSON with devices, enriched = plain text).
    if sample.lstrip().startswith("{"):
        parsed = json.loads(sample)
        devices = parsed["devices"]
        block = "\n\n[AVAILABLE HA ENTITIES" + parsed["user_request"].split(
            "[AVAILABLE HA ENTITIES", 1
        )[1]
        mode = "full"
    else:
        devices = None
        block = "\n\n[AVAILABLE HA ENTITIES" + sample.split("[AVAILABLE HA ENTITIES", 1)[1]
        mode = "enriched"

    def build_prompt(request: str) -> str:
        enriched = request + block
        if devices is None:
            return enriched
        return json.dumps({"user_request": enriched, "devices": scrub_states(compact_devices(devices))})

    cases = []
    for i, (request, expected, group) in enumerate(CASES, 1):
        cases.append(
            {
                "id": f"actuator-{group}-{i:03d}",
                "category": "actuator",
                "prompt": build_prompt(request),
                "expected": expected,
            }
        )

    out = Path(args.out)
    out.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )

    from collections import Counter

    groups = Counter(c["id"].split("-")[1] for c in cases)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"{len(cases)} cases -> {out} [{size_mb:.1f} MB, mode={mode}]")
    print("groups:", dict(groups))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate grounded `actuator` benchmark cases from a live Home Assistant.

Run this on the machine that talks to your HA (it reads HA_URL / HA_TOKEN from
the environment / .env, exactly like the actuator does). It fetches the real
device payload and writes benchmark cases whose prompt is byte-identical to what
``OneOffActuatorAgent`` sends in production, so the numbers describe the real
system rather than a toy entity list.

    python make_bench_actuator.py                      # full-fidelity payload
    python make_bench_actuator.py --mode enriched      # entity list only (smaller)
    python make_bench_actuator.py --limit 40 --out actuator.jsonl

Then merge with the rest:

    Get-Content bench.jsonl, actuator.jsonl | Set-Content bench_full.jsonl   # PowerShell
    cat bench.jsonl actuator.jsonl > bench_full.jsonl                        # bash

Every generated case is labelled mechanically from the entity's domain, so
REVIEW THE FILE before using it: friendly names can be ambiguous ("Lamp" when
you own three), and a request that matches several entities should either be
made specific or dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

# Requests generated per domain: (phrasing, service, extra service_data hint)
# Only domains that are unambiguous to actuate are included.
DOMAIN_REQUESTS = {
    "light": [("turn on {name}", "turn_on"), ("turn off {name}", "turn_off")],
    "switch": [("turn on {name}", "turn_on"), ("switch off {name}", "turn_off")],
    "media_player": [("turn off {name}", "turn_off"), ("turn on {name}", "turn_on")],
    "lock": [("lock {name}", "lock"), ("unlock {name}", "unlock")],
    "cover": [("open {name}", "open_cover"), ("close {name}", "close_cover")],
    "fan": [("turn on {name}", "turn_on"), ("turn off {name}", "turn_off")],
    "climate": [("set {name} to 22 degrees", "set_temperature")],
    "vacuum": [("start {name}", "start")],
}

# Device kinds that must NOT exist in your setup for the refusal case to be
# valid. The script keeps only those whose keyword appears in no entity.
REFUSAL_CANDIDATES = [
    ("turn on the garden sprinklers", "sprinkler"),
    ("open the garage door", "garage"),
    ("turn on the pool pump", "pool"),
    ("start the dishwasher", "dishwasher"),
    ("turn on the attic light", "attic"),
    ("lock the back door", "back door"),
]


def iter_entities(node, seen=None):
    """Yield (entity_id, friendly_name) from the nested HA payload."""
    if seen is None:
        seen = set()
    if isinstance(node, dict):
        eid = str(node.get("entity_id") or "")
        if eid and eid not in seen:
            seen.add(eid)
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
            state = node.get("state")
            state_attrs = (
                state.get("attributes")
                if isinstance(state, dict) and isinstance(state.get("attributes"), dict)
                else {}
            )
            name = (
                node.get("name")
                or node.get("friendly_name")
                or attrs.get("friendly_name")
                or state_attrs.get("friendly_name")
                or ""
            )
            yield eid, str(name)
        for value in node.values():
            yield from iter_entities(value, seen)
    elif isinstance(node, list):
        for item in node:
            yield from iter_entities(item, seen)


def entity_block(pairs, limit=300):
    """The exact '[AVAILABLE HA ENTITIES …]' block routing appends."""
    lines = []
    for eid, name in pairs[:limit]:
        lines.append(f"  {eid} ({name})" if name and name != eid else f"  {eid}")
    return (
        "\n\n[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
        + "\n".join(lines)
        + "\n]"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mode",
        choices=("full", "enriched"),
        default="full",
        help="full: exact production payload (request + devices JSON). "
        "enriched: request + entity list only (much smaller files).",
    )
    ap.add_argument("--limit", type=int, default=24, help="max generated control cases")
    ap.add_argument("--out", default="actuator.jsonl")
    args = ap.parse_args()

    from wactorz.config import CONFIG
    from wactorz.core.integrations.home_assistant.ha_helper import (
        fetch_devices_entities_with_location,
    )

    if not CONFIG.ha_url or not CONFIG.ha_token:
        print("HA_URL / HA_TOKEN not set — run this where wactorz talks to Home Assistant.")
        return 1

    devices = await fetch_devices_entities_with_location(
        CONFIG.ha_url, CONFIG.ha_token, include_states=True
    )
    pairs = list(iter_entities(devices))
    print(f"fetched {len(pairs)} entities from {CONFIG.ha_url}")

    block = entity_block(pairs)
    all_ids = " ".join(f"{e} {n}" for e, n in pairs).lower()

    def build_prompt(request: str) -> str:
        enriched = request + block
        if args.mode == "enriched":
            return enriched
        # Exactly what OneOffActuatorAgent._resolve_actions sends as user content.
        return json.dumps({"user_request": enriched, "devices": devices})

    cases = []
    n = 0
    for eid, name in pairs:
        domain = eid.split(".", 1)[0]
        if domain not in DOMAIN_REQUESTS or n >= args.limit:
            continue
        label = name or eid.split(".", 1)[1].replace("_", " ")
        # Skip entities whose name is too generic to identify unambiguously.
        if len(re.sub(r"[^a-z ]", "", label.lower()).strip()) < 3:
            continue
        for phrasing, service in DOMAIN_REQUESTS[domain]:
            if n >= args.limit:
                break
            n += 1
            cases.append(
                {
                    "id": f"actuator-live-{n:03d}",
                    "category": "actuator",
                    "prompt": build_prompt(phrasing.format(name=f"the {label}")),
                    "expected": [
                        {"domain": domain, "service": service, "entity_id": eid}
                    ],
                }
            )

    # Refusal cases — only for device kinds absent from your setup.
    for request, keyword in REFUSAL_CANDIDATES:
        if keyword.replace(" ", "") in all_ids.replace(" ", ""):
            print(f"skipping refusal case (you appear to own one): {request!r}")
            continue
        n += 1
        cases.append(
            {
                "id": f"actuator-live-{n:03d}",
                "category": "actuator",
                "prompt": build_prompt(request),
                "expected": [],
            }
        )

    out = Path(args.out)
    # noqa: ASYNC230 — one-shot script, blocking write at the end is fine
    out.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )

    refusals = sum(1 for c in cases if c["expected"] == [])
    size_kb = out.stat().st_size / 1024
    print(f"{len(cases)} cases ({refusals} refusal) -> {out} [{size_kb:.0f} KB, mode={args.mode}]")
    print("REVIEW the file: labels are derived mechanically from entity domains.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Generate grounded `actuator` benchmark cases from a live Home Assistant.

Run this on the machine that talks to your HA (it reads HA_URL / HA_TOKEN from
the environment / .env, exactly like the actuator does). It fetches the real
device payload and writes benchmark cases whose prompt is byte-identical to what
``OneOffActuatorAgent`` sends in production, so the numbers describe the real
system rather than a toy entity list.

    python make_bench_actuator.py                      # full-fidelity payload
    python make_bench_actuator.py --mode enriched      # entity list only (smaller)
    python make_bench_actuator.py --limit 40 --out actuator.jsonl

A SECOND SITE (held-out set). Point it at another Home Assistant without
touching .env, and cache the payload so you only need access once::

    python make_bench_actuator.py --ha-url http://other:8123 --ha-token <tok> \
        --site b --save-devices office_b_devices.json --out actuator_b.jsonl

    # later, offline — regenerate as often as you like
    python make_bench_actuator.py --devices-file office_b_devices.json \
        --site b --out actuator_b.jsonl

If you cannot run this at the other site, one curl there is enough::

    curl -H "Authorization: Bearer <TOKEN>" http://<host>:8123/api/states > b.json

then use --devices-file b.json. That dump has entity ids and friendly names but
no area registry, so location-based phrasings ("the meeting room light") cannot
be grounded from it — name-based and refusal cases are unaffected.

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
    ap.add_argument(
        "--site",
        default="a",
        help="site tag baked into case ids (actuator-<site>_control-001), so results "
        "from two homes stay distinguishable in one results.jsonl",
    )
    ap.add_argument("--ha-url", help="override HA_URL (target another Home Assistant)")
    ap.add_argument("--ha-token", help="override HA_TOKEN")
    ap.add_argument(
        "--devices-file",
        help="skip the live fetch and read the payload from JSON — either a cached "
        "--save-devices file or a raw /api/states dump",
    )
    ap.add_argument(
        "--save-devices",
        help="write the fetched payload here so cases can be regenerated offline",
    )
    args = ap.parse_args()

    if args.devices_file:
        devices = json.loads(Path(args.devices_file).read_text(encoding="utf-8"))
        source = args.devices_file
    else:
        from wactorz.config import CONFIG
        from wactorz.core.integrations.home_assistant.ha_helper import (
            fetch_devices_entities_with_location,
        )

        url = args.ha_url or CONFIG.ha_url
        token = args.ha_token or CONFIG.ha_token
        if not url or not token:
            print(
                "No Home Assistant to read. Set HA_URL / HA_TOKEN, or pass "
                "--ha-url/--ha-token, or supply --devices-file."
            )
            return 1
        devices = await fetch_devices_entities_with_location(url, token, include_states=True)
        source = url
        if args.save_devices:
            Path(args.save_devices).write_text(
                json.dumps(devices, ensure_ascii=False), encoding="utf-8"
            )
            print(f"cached payload -> {args.save_devices} (regenerate offline with --devices-file)")

    pairs = list(iter_entities(devices))
    print(f"{len(pairs)} entities from {source}")
    if not pairs:
        print("No entity_id found in that payload — wrong file?")
        return 1

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
                    "id": f"actuator-{args.site}_control-{n:03d}",
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
                "id": f"actuator-{args.site}_refusal-{n:03d}",
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

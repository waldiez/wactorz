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


# Integration "platforms" that expose infrastructure rather than devices.
# Home Assistant models each add-on as a device carrying a start/stop `switch`,
# indistinguishable by domain from a smart plug and nonsense in an actuation
# benchmark ("turn on the Cloudflared").
_SOFTWARE_PLATFORMS = {"hassio", "hacs"}
_SOFTWARE_PLATFORM_PREFIXES = ("homeassistant_connect",)  # dongle firmware toggles


def software_entities(payload) -> set[str]:
    """Entity ids belonging to add-ons/integrations rather than real devices.

    Keyed on the entity registry's ``platform`` field, which names the
    integration that created it: `hassio` and `hacs` are software, while `zha`,
    `wiz`, `reolink`, `samsungtv`, `nuki`, `zwave_js` and friends are hardware.

    An earlier version of this keyed on "the device also exposes an update.*
    entity", which looked right on one site and quietly excluded a Philips Hue
    bulb and a whole camera on another — real devices with Zigbee OTA updates.
    Every exclusion is printed for review, and --keep-addons turns it off.
    """
    excluded: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        eid = str(node.get("entity_id") or "")
        platform = str(node.get("platform") or "").lower()
        if eid and (
            platform in _SOFTWARE_PLATFORMS
            or platform.startswith(_SOFTWARE_PLATFORM_PREFIXES)
        ):
            excluded.add(eid)
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return excluded


# Values that identify a household rather than describe a device. They live in
# entity *states* (sensor.<x>_mac_address, _ssid, _ip_address, latitude/longitude)
# and survive compaction, because compaction keeps state. A model resolving
# "turn off the tv" can never use them, so redacting costs no fidelity — and the
# generated files routinely end up in a repo or a paper artifact.
_REDACT_PATTERNS = [
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), "00:00:00:00:00:00"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "0.0.0.0"),
]
_REDACT_ENTITY_HINTS = ("ssid", "bssid", "mac", "ip_address", "serial", "latitude", "longitude")


def scrub_states(devices):
    """Redact identifying values from entity states, in place-safe fashion."""
    for device in devices:
        for entity in device.get("entities") or []:
            eid = str(entity.get("entity_id") or "").lower()
            state = entity.get("state")
            if not isinstance(state, str):
                continue
            if any(h in eid for h in _REDACT_ENTITY_HINTS):
                entity["state"] = "redacted"
                continue
            for pattern, replacement in _REDACT_PATTERNS:
                state = pattern.sub(replacement, state)
            entity["state"] = state
    return devices


def compact_devices(devices):
    """Trim the payload exactly as production does before the LLM sees it.

    ``OneOffActuatorAgent._resolve_actions`` calls
    ``ha_helper.compact_devices_for_prompt`` — the raw dashboard payload carries
    unique_id, platform, swid, icons and feature bitmasks that no prompt in the
    codebase mentions (~24k tokens on a real home, versus ~10k trimmed). A
    benchmark that embeds the raw payload measures a prompt shape production no
    longer sends.

    Uses the production function when importable so the two cannot drift; the
    fallback below is a mirror for running this script outside the package.
    """
    try:
        from wactorz.core.integrations.home_assistant.ha_helper import (
            compact_devices_for_prompt,
        )
        return compact_devices_for_prompt(devices)
    except ImportError:
        pass

    useful = {"friendly_name", "device_class", "unit_of_measurement",
              "supported_color_modes", "color_mode"}
    out = []
    for device in devices:
        entities = []
        for entity in device.get("entities") or []:
            attrs = entity.get("attributes")
            kept = ({k: v for k, v in attrs.items() if k in useful}
                    if isinstance(attrs, dict) else {})
            trimmed = {"entity_id": entity.get("entity_id")}
            for key in ("name", "original_name", "area", "state"):
                if entity.get(key) is not None:
                    trimmed[key] = entity[key]
            if kept:
                trimmed["attributes"] = kept
            entities.append(trimmed)
        entry = {"name": device.get("name"), "entities": entities}
        for key in ("area", "model"):
            if device.get(key) is not None:
                entry[key] = device[key]
        out.append(entry)
    return out


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
        "--raw-payload",
        action="store_true",
        help="embed the untrimmed dashboard payload (~2.4x larger). Production "
        "compacts before prompting, so this no longer mirrors it — use only to "
        "reproduce the pre-compaction baseline.",
    )
    ap.add_argument(
        "--keep-identifiers",
        action="store_true",
        help="do NOT redact MAC/IP/SSID/coordinate values from entity states. "
        "They are irrelevant to device resolution and identify your household, "
        "so they are removed by default.",
    )
    ap.add_argument(
        "--keep-addons",
        action="store_true",
        help="do not filter out Home Assistant add-on / integration switches",
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

    if not args.keep_addons:
        software = software_entities(devices)
        dropped = [(e, n) for e, n in pairs if e in software and e.split(".", 1)[0] in DOMAIN_REQUESTS]
        if dropped:
            print(f"excluding {len(dropped)} add-on/integration entities from control cases:")
            for eid, name in dropped:
                print(f"  {eid} ({name})" if name else f"  {eid}")
            print("  (--keep-addons to disable; they stay in the entity list the model sees)")
        actuatable = [(e, n) for e, n in pairs if e not in software]
    else:
        actuatable = pairs

    block = entity_block(pairs)
    all_ids = " ".join(f"{e} {n}" for e, n in pairs).lower()

    def build_prompt(request: str) -> str:
        enriched = request + block
        if args.mode == "enriched":
            return enriched
        # Exactly what OneOffActuatorAgent._resolve_actions sends as user content.
        payload = devices if args.raw_payload else compact_devices(devices)
        if not args.keep_identifiers:
            payload = scrub_states(payload)
        return json.dumps({"user_request": enriched, "devices": payload})

    cases = []
    n = 0
    for eid, name in actuatable:
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

"""Paired cross-site `actuator` cases — the same request against two homes.

The strongest evidence that a model *resolves* devices rather than pattern-matching
its way to a plausible answer is a request whose correct response FLIPS when only
the injected entity list changes. Site A owns a TV, a Hue lamp and a camera; site B
owns neither, but owns a door lock site A lacks. Both own a WiZ bulb — with
different entity ids.

So the same sentence is:
  - a valid action at one site and a mandatory refusal at the other, or
  - a valid action at both, resolving to DIFFERENT entity ids.

A model that answers from priors ("a home has a TV") fails the refusal half. A model
that memorised site A during development fails the differing-id half. Neither failure
is visible from a single site, which is the whole point of collecting the second one.

    python make_bench_crosssite.py --a office_a_devices.json --b office_b_devices.json \\
        --out crosssite.jsonl

Ids are actuator-{a,b}_cross-NNN, and paired cases share the trailing number, so
`crosssite-<n>` at site A and at site B are the same sentence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from make_bench_actuator import compact_devices, scrub_states, entity_block, iter_entities

# Live entity ids, verified against both payloads.
A_HUE = "light.philips_hue_lct015"  # Office
A_WIZ = "light.wiz_rgbw_tunable_351b6e"  # Living Room
# Site A registers the same TV twice (integration + cast bridge). Either id is
# a correct answer, so the pair is scored with entity_id_any.
A_TV_ANY = [
    "media_player.samsung_7_series_55_ue55nu7023",
    "media_player.tv_samsung_7_series_55",
]
A_REC = "switch.living_room_record"  # Living Room camera
B_WIZ = "light.wiz_rgbw_tunable_2f982a"  # small table
B_LOCK = "lock.office_door"  # Office room

L, LOCK, MP, SW = "light", "lock", "media_player", "switch"


def act(domain, service, entity):
    return [{"domain": domain, "service": service, "entity_id": entity}]


def act_any(domain, service, entities):
    """One action whose entity may be any of several equivalent ids."""
    return [{"domain": domain, "service": service, "entity_id_any": list(entities)}]


# (request, expected at site A, expected at site B, group)
#
# [] means "refuse": the device genuinely does not exist at that site, and the
# correct behaviour is to return no actions rather than substitute a neighbour.
PAIRS = [
    # ── present at A, absent at B ────────────────────────────────────────────
    ("turn off the tv", act_any(MP, "turn_off", A_TV_ANY), [], "flip"),
    ("turn on the tv", act_any(MP, "turn_on", A_TV_ANY), [], "flip"),
    ("turn on the hue lamp", act(L, "turn_on", A_HUE), [], "flip"),
    ("turn off the philips hue", act(L, "turn_off", A_HUE), [], "flip"),
    ("start recording on the camera", act(SW, "turn_on", A_REC), [], "flip"),
    # ── absent at A, present at B ────────────────────────────────────────────
    ("lock the office door", [], act(LOCK, "lock", B_LOCK), "flip"),
    ("unlock the office door", [], act(LOCK, "unlock", B_LOCK), "flip"),
    # ── same device class at both sites, DIFFERENT entity id ────────────────
    ("turn on the wiz light", act(L, "turn_on", A_WIZ), act(L, "turn_on", B_WIZ), "same"),
    ("turn off the wiz light", act(L, "turn_off", A_WIZ), act(L, "turn_off", B_WIZ), "same"),
    (
        "turn on the wiz rgbw tunable bulb",
        act(L, "turn_on", A_WIZ),
        act(L, "turn_on", B_WIZ),
        "same",
    ),
    # ── near-miss substitution: a similar device exists, but not THIS one ────
    # Site B owns lock.office_door; "front door" is not it. Site A owns no lock
    # at all. Substituting either way is the exact failure that made a TV command
    # actuate a lamp.
    ("lock the front door", [], [], "nearmiss"),
    ("unlock the back door", [], [], "nearmiss"),
    # Site A's Hue is in the Office; site B's only bulb sits in "small table".
    # Neither site has a *kitchen* light, so neither may borrow one.
    ("turn on the kitchen light", [], [], "nearmiss"),
    ("turn off the bedroom lamp", [], [], "nearmiss"),
    # Neither site owns a climate entity — a very high-prior device.
    ("set the thermostat to 22 degrees", [], [], "nearmiss"),
    ("turn on the fan", [], [], "nearmiss"),
]


def load_site(path: str):
    devices = json.loads(Path(path).read_text(encoding="utf-8"))
    pairs = list(iter_entities(devices))
    return devices, entity_block(pairs), len(pairs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--a", default="office_a_devices.json")
    ap.add_argument("--b", default="office_b_devices.json")
    ap.add_argument("--out", default="crosssite.jsonl")
    ap.add_argument(
        "--mode",
        choices=("full", "enriched"),
        default="full",
        help="full: exact production payload. enriched: entity list only (smaller).",
    )
    args = ap.parse_args()

    sites = {}
    for tag, path in (("a", args.a), ("b", args.b)):
        if not Path(path).exists():
            print(f"{path} not found")
            return 1
        devices, block, n = load_site(path)
        sites[tag] = (devices, block)
        print(f"site {tag}: {n} entities from {path}")

    cases = []
    for i, (request, exp_a, exp_b, group) in enumerate(PAIRS, 1):
        for tag, expected in (("a", exp_a), ("b", exp_b)):
            devices, block = sites[tag]
            enriched = request + block
            prompt = (
                enriched
                if args.mode == "enriched"
                else json.dumps({"user_request": enriched, "devices": scrub_states(compact_devices(devices))})
            )
            cases.append(
                {
                    "id": f"actuator-{tag}_{group}-{i:03d}",
                    "category": "actuator",
                    "prompt": prompt,
                    "expected": expected,
                }
            )

    out = Path(args.out)
    out.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )

    refusals = sum(1 for c in cases if c["expected"] == [])
    print(
        f"{len(cases)} cases ({len(PAIRS)} sentences x 2 sites, {refusals} refusals) "
        f"-> {out} [{out.stat().st_size / 1024:.0f} KB]"
    )
    print("groups: flip (answer changes by site), same (same class, different id), "
          "nearmiss (must refuse at both)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

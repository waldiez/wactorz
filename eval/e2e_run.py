"""End-to-end run: drive the whole stack and check what actually happened.

The per-call-site harness scores one LLM call in isolation. This drives a real
running wactorz through its REST chat endpoint and then verifies the *outcome* —
did the bulb change state, did the AC reach the requested setpoint, did an agent
get spawned, did the system correctly do nothing — under a given LLM_OVERRIDES
configuration.

That is the experiment the hybrid claim actually rests on: per-site accuracy says
which calls a small model can serve, but only this says whether a system wired
that way still works.

    # 1. set LLM_OVERRIDES in .env, restart wactorz
    # 2. run:
    python e2e_run.py --config all-e2b --out e2e/

Config names are labels only — they record which LLM_OVERRIDES was live, since
the overrides are read at startup and cannot be changed from here. Restart
between configs and pass a different --config.

Entities are DISCOVERED from Home Assistant at startup rather than hardcoded, so
this works on any site and reports plainly what it found. Cases whose device is
absent are skipped with a note instead of counting as failures.

Verification is by side effect, never by reading the assistant's prose:

    actuate  poll Home Assistant until the entity reaches the expected state
             (or the expected attribute value, for a thermostat setpoint)
    refuse   snapshot every actuatable entity and require that NONE of them moved
    spawn    poll /api/actors for a new agent

Every actuated entity is restored to its pre-case state afterwards, including
the door lock and the air conditioner, so a run leaves the site as it found it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp


# ── Home Assistant ───────────────────────────────────────────────────────────
async def ha_get(session, args, path):
    url = f"{args.ha_url.rstrip('/')}/api/{path.lstrip('/')}"
    async with session.get(url, headers={"Authorization": f"Bearer {args.ha_token}"}) as r:
        return await r.json() if r.status == 200 else None


async def ha_call(session, args, domain, service, data):
    url = f"{args.ha_url.rstrip('/')}/api/services/{domain}/{service}"
    async with session.post(
        url, headers={"Authorization": f"Bearer {args.ha_token}"}, json=data
    ) as r:
        return r.status


async def ha_entity(session, args, entity):
    return await ha_get(session, args, f"states/{entity}")


async def ha_snapshot(session, args):
    """Current state of every entity a request could plausibly actuate."""
    states = await ha_get(session, args, "states") or []
    domains = ("light", "switch", "lock", "media_player", "climate", "cover", "fan", "vacuum")
    return {
        s["entity_id"]: s.get("state")
        for s in states
        if s.get("entity_id", "").split(".")[0] in domains
    }


async def discover(session, args):
    """Find the devices this site actually owns, by domain and name."""
    states = await ha_get(session, args, "states") or []
    found = {"light": None, "lock": None, "climate": None}
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".")[0]
        name = str((s.get("attributes") or {}).get("friendly_name") or "")
        if domain == "light" and found["light"] is None and "wiz" in (eid + name).lower():
            found["light"] = (eid, name)
        elif domain == "lock" and found["lock"] is None:
            found["lock"] = (eid, name)
        elif domain == "climate" and found["climate"] is None:
            found["climate"] = (eid, name)
    return found


# ── wactorz ──────────────────────────────────────────────────────────────────
# `--interface rest` serves /actors and /chat; the web dashboard serves the same
# under /api/. Probe once rather than make the caller know which is running.
async def detect_prefix(session, base, headers):
    for prefix in ("", "/api"):
        try:
            async with session.get(f"{base}{prefix}/actors", headers=headers) as r:
                if r.status == 200:
                    return prefix
                if r.status in (401, 403):
                    raise SystemExit(
                        f"{base}{prefix}/actors returned {r.status} — set --api-key "
                        "(the REST interface is guarded when API_KEY is configured)."
                    )
        except aiohttp.ClientError:
            return None
    return None


async def agents(session, args):
    async with session.get(f"{args.base}{args.prefix}/actors", headers=args.headers) as r:
        if r.status != 200:
            return []
        data = await r.json()
    items = data if isinstance(data, list) else data.get("actors", data.get("agents", []))
    return [a for a in items if isinstance(a, dict)]


async def cost(session, args):
    try:
        async with session.get(f"{args.base}{args.prefix}/cost", headers=args.headers) as r:
            return await r.json() if r.status == 200 else {}
    except Exception:
        return {}


async def send(session, args, message):
    async with session.post(
        f"{args.base}{args.prefix}/chat", json={"message": message}, headers=args.headers
    ) as r:
        return r.status, await r.text()


# ── cases ────────────────────────────────────────────────────────────────────
def build_cases(found):
    """(id, message, kind, check) — check describes the expected side effect."""
    cases = []
    light = found["light"]
    lock = found["lock"]
    ac = found["climate"]

    if light:
        eid, name = light
        cases += [
            ("e2e-act-light-001", "turn on the wiz light", "actuate", ("state", eid, "on")),
            ("e2e-act-light-002", "turn off the wiz light", "actuate", ("state", eid, "off")),
            ("e2e-act-light-003", "switch on the light on the small table", "actuate", ("state", eid, "on")),
            ("e2e-act-light-004", "turn the wiz light off please", "actuate", ("state", eid, "off")),
        ]
    if lock:
        eid, name = lock
        cases += [
            ("e2e-act-lock-001", "lock the office door", "actuate", ("state", eid, "locked")),
            ("e2e-act-lock-002", "unlock the office door", "actuate", ("state", eid, "unlocked")),
        ]
    if ac:
        eid, name = ac
        # set_temperature moves an attribute, not the state; on/off moves state.
        cases += [
            ("e2e-act-ac-001", "set the air conditioner to 22 degrees", "actuate", ("attr", eid, "temperature", 22)),
            ("e2e-act-ac-002", "turn on the air conditioning", "actuate", ("state_not", eid, "off")),
            ("e2e-act-ac-003", "turn off the air conditioner", "actuate", ("state", eid, "off")),
            ("e2e-act-ac-004", "set the office temperature to 24", "actuate", ("attr", eid, "temperature", 24)),
        ]

    # Devices this site does not own. Same phrasings that made site B substitute
    # in the offline benchmark — here a wrong answer physically actuates the
    # wrong device, so the check watches every entity, not just the target.
    cases += [
        ("e2e-ref-001", "turn on the kitchen light", "refuse", None),
        ("e2e-ref-002", "turn off the bedroom lamp", "refuse", None),
        ("e2e-ref-003", "turn off the tv", "refuse", None),
        ("e2e-ref-004", "start the dishwasher", "refuse", None),
        ("e2e-ref-005", "open the garage door", "refuse", None),
    ]
    # Pipelines. These are the only cases that EXECUTE generated code: the planner
    # picks a shape, codegen writes Python, and DynamicAgent runs the module body
    # through exec(compile(...)). A spawn whose code raises — a module-level
    # import of a package the installer has not fetched, a syntax error the LLM
    # repair pass could not fix — never registers, so an agent appearing in
    # /actors is evidence the code ran, not merely that it parsed.
    #
    # Task shapes mirror the groups in dynamic_strict.jsonl, so a static score of
    # 80% on `dynamic` can be compared against how many of those shapes survive
    # contact with the real runtime.
    cases += [
        # scheduled / publish
        ("e2e-spawn-001", "every 5 minutes publish the current time to custom/e2e/clock", "spawn", "clock"),
        ("e2e-spawn-002", "publish the cpu percentage to custom/e2e/cpu every minute", "spawn", "cpu"),
        ("e2e-spawn-003", "log a heartbeat message every 10 minutes", "spawn", "heartbeat"),
        ("e2e-spawn-004", "publish free disk space to custom/e2e/disk every 2 minutes", "spawn", "disk"),
        # subscribe / react — needs agent.subscribe with a callback, the rule
        # that sank 26B before the codegen prompt stated it
        ("e2e-spawn-005", "watch custom/e2e/clock and log every message it receives", "spawn", "clock"),
        (
            "e2e-spawn-006",
            "when something is published on custom/e2e/cpu above 90, send me an alert",
            "spawn",
            "cpu",
        ),
        # state / persistence — agent.persist and agent.recall, not awaited
        (
            "e2e-spawn-007",
            "count the messages on custom/e2e/clock and remember the total across restarts",
            "spawn",
            "count",
        ),
        # temporal window — agent.window is synchronous
        (
            "e2e-spawn-008",
            "keep a 5 minute window over custom/e2e/cpu and publish the average to custom/e2e/cpu_avg",
            "spawn",
            "cpu",
        ),
        # blocking work off the event loop — run_in_executor
        (
            "e2e-spawn-009",
            "every 3 minutes read the machine memory usage and publish it to custom/e2e/memory",
            "spawn",
            "memory",
        ),
        # grounded in this site: a real entity, via the HA state bridge
        (
            "e2e-spawn-010",
            "log a message whenever the wiz light changes state",
            "spawn",
            "light",
        ),
    ]
    return cases


async def check_passes(session, args, check):
    kind = check[0]
    data = await ha_entity(session, args, check[1])
    if data is None:
        return False
    if kind == "state":
        return data.get("state") == check[2]
    if kind == "state_not":
        return data.get("state") not in (check[2], "unavailable", "unknown")
    if kind == "attr":
        got = (data.get("attributes") or {}).get(check[2])
        try:
            return abs(float(got) - float(check[3])) < 0.51
        except (TypeError, ValueError):
            return False
    return False


async def arm(session, args, check):
    """Force the target into a state that does NOT satisfy the check.

    Without this the harness scores free passes: "turn off the light" against a
    light that is already off is satisfied the instant it is polled, whether or
    not the system did anything at all. Arming makes a pass require a real,
    observed transition caused by the request.

    Returns True if the target is now in a non-satisfying state.
    """
    kind, entity = check[0], check[1]
    domain = entity.split(".")[0]

    if kind == "attr":  # thermostat setpoint — arm to a clearly different value
        want = float(check[3])
        await ha_call(session, args, domain, "set_temperature",
                      {"entity_id": entity, "temperature": want + 3})
    elif domain == "lock":
        want_locked = (kind == "state" and check[2] == "locked")
        await ha_call(session, args, "lock", "unlock" if want_locked else "lock",
                      {"entity_id": entity})
    elif domain == "climate":
        # "turn on" -> arm off; "turn off" -> arm to a running mode
        if kind == "state_not" or (kind == "state" and check[2] == "off"):
            if kind == "state_not":
                await ha_call(session, args, "climate", "turn_off", {"entity_id": entity})
            else:
                await ha_call(session, args, "climate", "turn_on", {"entity_id": entity})
    else:
        want_on = (kind == "state" and check[2] == "on") or kind == "state_not"
        await ha_call(session, args, domain, "turn_off" if want_on else "turn_on",
                      {"entity_id": entity})

    # A Nuki lock takes 10-30s to complete a physical turn and report back, and
    # a thermostat setpoint can lag too. Six seconds silently skipped exactly the
    # cases that are hardest and most interesting.
    deadline = time.perf_counter() + args.arm_timeout
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.5)
        if not await check_passes(session, args, check):
            return True
    return False


async def restore(session, args, entity, snapshot):
    """Put one entity back exactly where it was before the case ran."""
    domain = entity.split(".")[0]
    before_state, before_attrs = snapshot
    now = await ha_entity(session, args, entity)
    if now is None:
        return
    if domain == "climate":
        want = before_attrs.get("temperature")
        if want is not None:
            await ha_call(session, args, "climate", "set_temperature",
                          {"entity_id": entity, "temperature": want})
        if before_state == "off":
            await ha_call(session, args, "climate", "turn_off", {"entity_id": entity})
        elif now.get("state") != before_state:
            await ha_call(session, args, "climate", "set_hvac_mode",
                          {"entity_id": entity, "hvac_mode": before_state})
        return
    if now.get("state") == before_state:
        return
    if domain == "lock":
        await ha_call(session, args, "lock",
                      "lock" if before_state == "locked" else "unlock", {"entity_id": entity})
    elif domain in ("light", "switch", "fan", "media_player"):
        await ha_call(session, args, domain,
                      "turn_on" if before_state == "on" else "turn_off", {"entity_id": entity})


async def run_case(session, args, case, sink):
    cid, message, kind, check = case
    rec = {
        "config": args.config, "case_id": cid, "message": message, "kind": kind,
        "passed": False, "seconds": None, "detail": "", "ts": time.time(),
    }

    before_all = await ha_snapshot(session, args)
    before_agents = {a.get("name") for a in await agents(session, args)}
    before_cost = await cost(session, args)

    print(f"  {cid:<22} ...", end="", flush=True)
    target = check[1] if kind == "actuate" else None
    target_snapshot = None
    if target:
        data = await ha_entity(session, args, target)
        target_snapshot = (data.get("state"), data.get("attributes") or {}) if data else None
        # A case only means something if the target starts in a state the request
        # must actually change.
        if not await arm(session, args, check):
            rec["detail"] = f"could not arm {target} into a non-satisfying state — case skipped"
            rec["armed"] = False
            sink.write(json.dumps(rec) + "\n"); sink.flush()
            print(f"\r  {cid:<22} SKIP  could not arm {target}" + " " * 20)
            return rec
        rec["armed"] = True
        before_all = await ha_snapshot(session, args)

    print(" armed, sending", end="", flush=True)
    post_start = time.perf_counter()
    try:
        status, body = await send(session, args, message)
    except asyncio.TimeoutError:
        rec["detail"] = f"no reply within --turn-timeout ({args.turn_timeout}s)"
        rec["turn_seconds"] = args.turn_timeout
        print(f"\r  {cid:<22} TIMEOUT after {args.turn_timeout}s" + " " * 20)
        sink.write(json.dumps(rec) + "\n"); sink.flush()
        return rec
    rec["turn_seconds"] = round(time.perf_counter() - post_start, 2)
    if status != 200:
        rec["detail"] = f"chat POST failed: {status} {body[:120]}"
        sink.write(json.dumps(rec) + "\n"); sink.flush()
        return rec

    start = time.perf_counter()
    if kind == "actuate":
        while time.perf_counter() - start < args.timeout:
            if await check_passes(session, args, check):
                rec["passed"] = True
                break
            await asyncio.sleep(1.0)
        rec["verify_seconds"] = round(time.perf_counter() - start, 2)
        rec["seconds"] = round(rec["turn_seconds"] + rec["verify_seconds"], 2)
        if not rec["passed"]:
            data = await ha_entity(session, args, target)
            rec["detail"] = f"{target} state={data and data.get('state')!r} attrs-temp=" \
                            f"{data and (data.get('attributes') or {}).get('temperature')!r}; wanted {check[2:]}"
        if target_snapshot and not args.no_restore:
            await restore(session, args, target, target_snapshot)

    elif kind == "refuse":
        await asyncio.sleep(args.refuse_settle)
        after = await ha_snapshot(session, args)
        moved = {k: (before_all.get(k), v) for k, v in after.items() if before_all.get(k) != v}
        moved = {
            k: v for k, v in moved.items()
            if not {"unavailable", "unknown"} & {str(v[0]), str(v[1])}
        }
        rec["passed"] = not moved
        rec["seconds"] = round(rec.get("turn_seconds", 0.0) + args.refuse_settle, 2)
        if moved:
            rec["detail"] = "actuated anyway: " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in moved.items())
            if not args.no_restore:
                for eid, (was, _) in moved.items():
                    await restore(session, args, eid, (was, {}))

    elif kind == "spawn":
        # Pass = an agent registered. Registration means DynamicAgent got through
        # exec(compile(...)) without raising, which is the execution evidence this
        # case exists to collect. The planner picks its own names, so requiring a
        # particular one would score naming, not runnability — the name hint is
        # recorded as a softer signal instead.
        while time.perf_counter() - start < args.timeout:
            new = [a for a in await agents(session, args) if a.get("name") not in before_agents]
            if new:
                rec["passed"] = True
                rec["name_hint_matched"] = any(
                    check in str(a.get("name", "")).lower() for a in new
                )
                break
            await asyncio.sleep(1.0)
        rec["verify_seconds"] = round(time.perf_counter() - start, 2)
        rec["seconds"] = round(rec["turn_seconds"] + rec["verify_seconds"], 2)
        new = [a.get("name") for a in await agents(session, args) if a.get("name") not in before_agents]
        rec["spawned"] = new
        rec["detail"] = f"new agents: {new}" if new else "no new agent appeared"

    after_cost = await cost(session, args)
    for key in ("total_cost_usd", "total_tokens", "input_tokens", "output_tokens"):
        if key in before_cost and key in after_cost:
            try:
                rec[f"delta_{key}"] = round(after_cost[key] - before_cost[key], 6)
            except Exception:
                pass

    sink.write(json.dumps(rec) + "\n"); sink.flush()
    print(
        f"\r  {cid:<22} {'PASS' if rec['passed'] else 'FAIL'}  "
        f"turn={rec.get('turn_seconds')}s total={rec['seconds']}s  {rec['detail'][:60]}" + " " * 10
    )
    return rec


async def cleanup_agents(session, args, names):
    for a in await agents(session, args):
        if a.get("name") in names:
            aid = a.get("actor_id") or a.get("id")
            if aid:
                async with session.delete(
                    f"{args.base}{args.prefix}/actors/{aid}", headers=args.headers
                ) as r:
                    print(f"  removed {a.get('name')} ({r.status})")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="label for the live LLM_OVERRIDES, e.g. all-e2b")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--ha-url")
    ap.add_argument("--ha-token")
    ap.add_argument("--out", default="e2e")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--refuse-settle", type=float, default=25.0)
    ap.add_argument(
        "--arm-timeout", type=float, default=45.0,
        help="seconds to wait for a device to reach its armed state before skipping "
        "the case. Physical locks are slow.",
    )
    ap.add_argument("--repeat", type=int, default=1, help="passes over the case list")
    ap.add_argument("--no-restore", action="store_true", help="leave devices as the run left them")
    ap.add_argument("--only", help="substring filter on case id")
    ap.add_argument("--api-key", help="X-API-Key for a guarded REST interface (API_KEY in .env)")
    ap.add_argument(
        "--turn-timeout", type=float, default=180.0,
        help="seconds to wait for /chat to reply. The endpoint is synchronous, so a "
        "full orchestrator turn (main + intent + actuator) sits inside this.",
    )
    args = ap.parse_args()
    args.headers = {"X-API-Key": args.api_key} if args.api_key else {}
    args.prefix = ""

    if not args.ha_url or not args.ha_token:
        from wactorz.config import CONFIG
        args.ha_url = args.ha_url or CONFIG.ha_url
        args.ha_token = args.ha_token or CONFIG.ha_token
    if not args.ha_url or not args.ha_token:
        print("Home Assistant URL/token missing — pass --ha-url/--ha-token or set HA_URL/HA_TOKEN.")
        return 1

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_read=args.turn_timeout)
    ) as session:
        prefix = await detect_prefix(session, args.base, args.headers)
        if prefix is None:
            print(
                f"No wactorz REST API at {args.base}. Tried {args.base}/actors and "
                f"{args.base}/api/actors.\n"
                "  --interface rest  serves /actors and /chat\n"
                "  the web dashboard serves /api/actors and /api/chat\n"
                "Check the port in the startup log and pass --base accordingly."
            )
            return 1
        args.prefix = prefix
        print(f"REST API at {args.base}{prefix or '/'} (prefix={prefix or 'none'})")

        found = await discover(session, args)
        print("discovered devices:")
        for domain, hit in found.items():
            print(f"  {domain:<8} {hit[0] + '  ' + hit[1] if hit else '(none found — cases skipped)'}")
        if found["climate"] is None:
            print("  NOTE: no climate entity visible. If the AC exists in your dashboard,")
            print("        the device-less-entity fix is not live in this process.")

        cases = build_cases(found)
        if args.only:
            cases = [c for c in cases if args.only in c[0]]
        if not cases:
            print("no cases matched")
            return 1

        out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "e2e_results.jsonl"
        print(f"\nconfig={args.config}  cases={len(cases)}")

        records, spawned = [], []
        with path.open("a", encoding="utf-8") as sink:
            for rep in range(args.repeat):
                if args.repeat > 1:
                    print(f"-- pass {rep + 1}/{args.repeat}")
                for case in cases:
                    rec = await run_case(session, args, case, sink)
                    rec["rep"] = rep
                    records.append(rec)
                    spawned += rec.get("spawned") or []
        if spawned and not args.no_restore:
            await cleanup_agents(session, args, set(spawned))

    by_kind: dict[str, list] = {}
    for r in records:
        by_kind.setdefault(r["kind"], []).append(r)
    print(f"\nconfig={args.config}")
    for kind, rs in sorted(by_kind.items()):
        p = sum(bool(r["passed"]) for r in rs)
        turns = [r["turn_seconds"] for r in rs if r.get("turn_seconds") is not None]
        tail = f"   mean turn {sum(turns)/len(turns):.1f}s" if turns else ""
        skipped = sum(1 for r in rs if r.get("armed") is False)
        tail += f"   ({skipped} skipped, could not arm)" if skipped else ""
        if kind == "spawn":
            named = sum(1 for r in rs if r.get("name_hint_matched"))
            tail += f"   [{named}/{len(rs)} named as expected]"
        print(f"  {kind:<8} {p}/{len(rs)}{tail}")
    print(f"  OVERALL  {sum(bool(r['passed']) for r in records)}/{len(records)}")
    print(f"\nrecords -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

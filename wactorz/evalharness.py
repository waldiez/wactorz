"""Call-site evaluation harness — compare SLMs/LLMs per framework call site.

Runs a benchmark of prompts for each LLM call site against one or more model
specs (same ``provider[:model]`` format as ``LLM_OVERRIDES``), scores every
output automatically, and writes per-call records (JSONL) plus an
accuracy/latency/cost summary (CSV + console table).

Usage::

    python -m wactorz.evalharness --models "ollama:qwen3:4b,anthropic:claude-sonnet-4-6"
    python -m wactorz.evalharness --models ollama:llama3 --categories intent,ha
    python -m wactorz.evalharness --prompts bench.jsonl --repeat 3 --out results/
    python -m wactorz.evalharness --models ollama:llama3 --temperature 0

Benchmark file format (JSONL, one case per line)::

    {"id": "intent-001", "category": "intent", "prompt": "turn on the lamp",
     "expected": "ACTUATE"}

``expected`` semantics per category:

    intent    exact routing label: ACTUATE | HA | PIPELINE | OTHER
    ha        exact action label (create_automation, list_devices, ...)
    actuator  list of {domain, service, entity_id} dicts; the returned JSON
              action array must contain exactly these actions (extra keys
              inside an action are fine, extra ACTIONS are a failure).
              An empty list means the model must refuse with []
    planner   list of allowed agent "type" values (shape only), or a dict
              {"types": [...], "must_contain": [...], "must_not_contain": [...]}
              to also require that the plan references specific entity ids or
              topics — i.e. that the planner resolved them itself
    dynamic   list of function names the generated Python must define
              (checked by parsing the code — it must also compile)

The intent / ha / actuator categories use the framework's unmodified
production system prompts. The planner / dynamic system prompts here are
condensed variants of the production ones (which are built dynamically inside
the agents and run to hundreds of lines); they preserve the required output
format so scoring stays faithful.

Without ``--prompts``, a small built-in seed set (three cases per category)
runs so the pipeline can be smoke-tested end to end; real experiments should
supply the full benchmark file.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATEGORIES = ("intent", "ha", "actuator", "planner", "dynamic")

# Output budget per category — mirrors what the framework passes in production
# (e.g. the intent classifier runs with max_tokens=10).
_MAX_TOKENS = {"intent": 10, "ha": 10, "actuator": 500, "planner": 800, "dynamic": 1200}

_PLANNER_SYSTEM = (
    "You are designing reactive automation pipelines for a multi-agent IoT system.\n"
    "Output ONLY a valid JSON array - no explanation, no markdown, no code fences.\n"
    "Each array item is a COMPLETE spawn config: it must contain the concrete\n"
    "entity ids, MQTT topics and schedules needed to run, not just a description.\n"
    "\n"
    'TYPE 1 - "ha_actuator": call a Home Assistant service when an MQTT message arrives\n'
    '  {"name": "<kebab-case>", "type": "ha_actuator", "description": "<what it does>",\n'
    '   "mqtt_topics": ["<trigger-topic>"],\n'
    '   "actions": [{"domain": "<ha-domain>", "service": "<ha-service>",\n'
    '                "entity_id": "<entity_id from the entity list>", "service_data": {}}],\n'
    '   "detection_filter": {"<payload-key>": <value>} or null,\n'
    '   "cooldown_seconds": <number>}\n'
    "\n"
    'TYPE 2 - "scheduled": fire at a time or interval\n'
    '  {"name": "<kebab-case>", "type": "scheduled", "description": "<what it fires>",\n'
    '   "schedule": {"type": "daily", "at": "17:00"} | {"type": "weekly", "at": "07:30",\n'
    '                "days": ["mon"]} | {"type": "interval", "seconds": 1800},\n'
    '   "publish_topic": "schedule/<name>/fired"}\n'
    "\n"
    'TYPE 3 - "dynamic": custom logic in Python\n'
    '  {"name": "<kebab-case>", "type": "dynamic", "description": "<what it does>",\n'
    '   "poll_interval": <seconds>, "code": "<async setup/process/handle_task>"}\n'
    "\n"
    "Rules:\n"
    "- Use entity ids EXACTLY as they appear in the AVAILABLE HA ENTITIES list.\n"
    "- Reuse a topic already published by an agent in AVAILABLE AGENTS rather than\n"
    "  inventing a new one, and use that topic's exact payload field names.\n"
    "- Delegate to an existing agent by name when one already does the job.\n"
    "- HA state changes arrive on homeassistant/state_changes/#; filter by entity_id\n"
    "  inside the payload (state is nested: payload['new_state']['state']).\n"
)

_CODEGEN_SYSTEM = (
    "You write Python code for a dynamic agent in a multi-agent IoT framework.\n"
    "Output ONLY Python code - no explanation, no markdown fences.\n"
    "Define async functions among: setup(agent), process(agent),\n"
    "handle_task(agent, payload), cleanup(agent).\n"
    "\n"
    "AGENT API\n"
    "  agent.state                        dict, persists across process() calls\n"
    "  await agent.publish(topic, data)   publish to MQTT\n"
    "  await agent.mqtt_get(topic)        ONE-SHOT read, returns one payload\n"
    "  await agent.log(msg)               dashboard log\n"
    "  await agent.alert(msg, severity)   dashboard alert\n"
    "  agent.persist(key, value)          save to disk (NOT awaitable)\n"
    "  agent.recall(key)                  load from disk (NOT awaitable)\n"
    "  agent.subscribe(topic, callback)   continuous subscription (NOT awaitable)\n"
    "  agent.window(topic, seconds=N)     sliding window object (NOT awaitable)\n"
    "  agent.declare_contract(publishes=[...], subscribes=[...])\n"
    "  await agent.llm.chat(prompt)       the framework's LLM - never import your own\n"
    "\n"
    "CRITICAL RULES\n"
    "- agent.subscribe() is NOT awaitable and REQUIRES a callback. It runs as a\n"
    "  background task; the callback must be an async function taking exactly one\n"
    "  argument (the payload).\n"
    "    CORRECT:  async def on_msg(payload): ...\n"
    "              agent.subscribe('some/topic', on_msg)\n"
    "    WRONG:    await agent.subscribe('some/topic', on_msg)   # not awaitable\n"
    "    WRONG:    agent.subscribe('some/topic')                 # no callback\n"
    "    WRONG:    async def on_msg(topic, payload): ...         # too many args\n"
    "- agent.window() is synchronous: `w = agent.window(t, seconds=300)`, never await.\n"
    "- agent.persist() / agent.recall() are synchronous - do not await them.\n"
    "- process() is called repeatedly on a poll interval. Do NOT write `while True`\n"
    "  loops inside it; just do one iteration of work and return.\n"
    "- Import libraries INSIDE functions, never at module level.\n"
    "- Wrap blocking calls (cv2, torch, psutil-heavy work) in\n"
    "  `await asyncio.get_event_loop().run_in_executor(None, fn)`.\n"
)

# Three seed cases per category so the harness runs out of the box.
_SEED_CASES: list[dict[str, Any]] = [
    {
        "id": "intent-001",
        "category": "intent",
        "prompt": "turn on the office light",
        "expected": "ACTUATE",
    },
    {
        "id": "intent-002",
        "category": "intent",
        "prompt": "when a person is detected in my pc camera, open the office light",
        "expected": "PIPELINE",
    },
    {
        "id": "intent-003",
        "category": "intent",
        "prompt": "list my home assistant automations",
        "expected": "HA",
    },
    {
        "id": "ha-001",
        "category": "ha",
        "prompt": "create an automation that turns off all lights at midnight",
        "expected": "create_automation",
    },
    {
        "id": "ha-002",
        "category": "ha",
        "prompt": "show me all my devices",
        "expected": "list_devices",
    },
    {
        "id": "ha-003",
        "category": "ha",
        "prompt": "what motion sensor should I buy for the hallway?",
        "expected": "recommend_hardware",
    },
    {
        "id": "actuator-001",
        "category": "actuator",
        "prompt": "turn on the living room lamp\n\n[AVAILABLE HA ENTITIES:\n  light.wiz_rgbw_02cba0 (Living Room Lamp)\n  switch.kettle (Kettle)\n]",
        "expected": [
            {"domain": "light", "service": "turn_on", "entity_id": "light.wiz_rgbw_02cba0"}
        ],
    },
    {
        "id": "actuator-002",
        "category": "actuator",
        "prompt": "set the thermostat to 22 degrees\n\n[AVAILABLE HA ENTITIES:\n  climate.living_room (Thermostat)\n  light.hall (Hall Light)\n]",
        "expected": [
            {"domain": "climate", "service": "set_temperature", "entity_id": "climate.living_room"}
        ],
    },
    {
        "id": "actuator-003",
        "category": "actuator",
        "prompt": "lock the front door and turn off the hall light\n\n[AVAILABLE HA ENTITIES:\n  lock.front_door (Front Door)\n  light.hall (Hall Light)\n]",
        "expected": [
            {"domain": "lock", "service": "lock", "entity_id": "lock.front_door"},
            {"domain": "light", "service": "turn_off", "entity_id": "light.hall"},
        ],
    },
    {
        "id": "actuator-004",
        "category": "actuator",
        "prompt": "turn off the TV\n\n[AVAILABLE HA ENTITIES:\n  light.wiz_rgbw_02cba0 (Living Room Lamp)\n  switch.kettle (Kettle)\n]",
        "expected": [],
    },
    {
        "id": "planner-001",
        "category": "planner",
        "prompt": "when the front door opens, turn on the hallway light (entity light.hall). Door state arrives on homeassistant/state_changes/# with entity_id binary_sensor.front_door.",
        "expected": ["ha_actuator", "dynamic"],
    },
    {
        "id": "planner-002",
        "category": "planner",
        "prompt": "every weekday at 7:30 turn on the coffee machine (switch.coffee).",
        "expected": ["scheduled", "ha_actuator"],
    },
    {
        "id": "planner-003",
        "category": "planner",
        "prompt": "when temperature on sensors/livingroom/temp goes above 28, send a Discord notification via webhook https://discord.example/hook.",
        "expected": ["dynamic", "ha_actuator", "scheduled"],
    },
    {
        "id": "dynamic-001",
        "category": "dynamic",
        "prompt": "Write an agent that subscribes to sensors/temperature and stores the latest value in state; process() should log an alert when the value exceeds 30.",
        "expected": ["setup", "process"],
    },
    {
        "id": "dynamic-002",
        "category": "dynamic",
        "prompt": 'Write an agent whose handle_task(agent, payload) receives {"city": str} and returns {"result": str} echoing the city name upper-cased.',
        "expected": ["handle_task"],
    },
    {
        "id": "dynamic-003",
        "category": "dynamic",
        "prompt": 'Write an agent that every poll publishes {"value": <random 0-100>} to custom/demo/random and logs the value.',
        "expected": ["process"],
    },
]


# ── Output extraction ────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Drop markdown code fences (``` / ```json / ```python) if present."""
    lines = (text or "").strip().splitlines()
    body = [ln for ln in lines if not ln.strip().startswith("```")]
    return "\n".join(body).strip()


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction: try verbatim, then fenced-stripped, then the
    first [...] or {...} span. Returns None when nothing parses.
    """
    for candidate in (text, _strip_fences(text)):
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            pass
    stripped = _strip_fences(text)
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except Exception:
                continue
    return None


# ── Scorers (pure functions — unit-tested) ───────────────────────────────────


def score_intent(output: str, expected: str) -> bool:
    """First token, upper-cased — the same parse _classify_intent applies."""
    token = (output or "").strip().upper().split()[0] if (output or "").strip() else ""
    token = token.strip(".,:;!\"'")
    return token == str(expected).upper()


def score_ha(output: str, expected: str) -> bool:
    token = (output or "").strip().lower().split()[0] if (output or "").strip() else ""
    token = token.strip(".,:;!\"'")
    return token == str(expected).lower()


def score_actuator(output: str, expected: list[dict]) -> bool:
    """Exact action-set match. Every expected action must appear in the
    returned array (matched on domain/service/entity_id; extra keys inside an
    action, like service_data, are fine) AND the array must contain no extra
    actions — an unrequested actuation is a failure, not a bonus. An empty
    ``expected`` means the model must refuse: only ``[]`` passes.

    An expected action may carry ``entity_id_any`` instead of ``entity_id`` when
    a device is exposed under several entity ids and any of them is correct::

        {"domain": "media_player", "service": "turn_off",
         "entity_id_any": ["media_player.samsung_7_series_55_ue55nu7023",
                           "media_player.tv_samsung_7_series_55"]}

    Home Assistant really does register one TV twice (integration + cast, say),
    and marking the model wrong for picking the other valid id measures our
    ground truth, not the model.
    """
    actions = extract_json(output)
    if not isinstance(actions, list):
        return False
    if not expected:
        return actions == []
    if len(actions) != len(expected):
        return False

    def matches(exp: dict, got: Any) -> bool:
        if not isinstance(got, dict):
            return False
        alternatives = exp.get("entity_id_any")
        if alternatives is not None and got.get("entity_id") not in alternatives:
            return False
        return all(got.get(k) == v for k, v in exp.items() if k != "entity_id_any")

    return all(any(matches(exp, got) for got in actions) for exp in expected)


def score_planner(output: str, expected: list[str] | dict) -> bool:
    """Plan must be a non-empty JSON array of dicts whose "type" values are all
    allowed and which all carry a name/description.

    ``expected`` is either a list of allowed agent types (shape only), or a dict
    for grounded cases::

        {"types": ["ha_actuator", "dynamic"],
         "must_contain": ["light.philips_hue_lct015"],
         "must_not_contain": ["light.wiz_rgbw_tunable_351b6e"]}

    ``must_contain`` / ``must_not_contain`` are matched against the serialized
    plan, so they check that the planner resolved references to the right entity
    ids and topics — not just that the plan has the right shape.
    """
    if isinstance(expected, dict):
        allowed_types = expected.get("types") or []
        must_contain = expected.get("must_contain") or []
        must_not_contain = expected.get("must_not_contain") or []
    else:
        allowed_types, must_contain, must_not_contain = expected, [], []

    plan = extract_json(output)
    if not isinstance(plan, list) or not plan:
        return False
    allowed = {str(t) for t in allowed_types}
    for item in plan:
        if not isinstance(item, dict):
            return False
        if str(item.get("type")) not in allowed:
            return False
        if not item.get("name") or not item.get("description"):
            return False

    serialized = json.dumps(plan).lower()
    if any(str(token).lower() not in serialized for token in must_contain):
        return False
    return all(str(token).lower() not in serialized for token in must_not_contain)


def _structural_findings(tree: ast.AST, code: str) -> set[str]:
    """Framework rules the generated code must not break.

    These are the mistakes the orchestrator prompt explicitly warns about, and
    each one breaks a real DynamicAgent at runtime — so a benchmark that ignores
    them scores broken agents as successes.
    """
    findings: set[str] = set()

    # Imports must live inside functions: DynamicAgent runs the module body via
    # exec(compile(...)), so a module-level import of a package the installer has
    # not fetched yet raises ImportError and the spawn fails outright.
    #
    # Only third-party imports can do that. `import asyncio` at module level is
    # against the framework's house style but cannot fail, and flagging it scored
    # a working agent as broken — one 4B model put `import asyncio` at the top of
    # 42 of 50 answers and scored 12% instead of 80%. Style adherence and "would
    # this actually run" are different questions; this measures the second.
    # Pass "strict_imports": true in a case to fail any module-level import.
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        roots = [m.split(".")[0] for m in modules if m]
        findings.add("module_level_import")
        if any(r not in sys.stdlib_module_names for r in roots):
            findings.add("module_level_thirdparty_import")

    for node in ast.walk(tree):
        # agent.subscribe() is not awaitable and needs a callback.
        if isinstance(node, ast.Await):
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "subscribe"
            ):
                findings.add("awaited_subscribe")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "subscribe" and len(node.args) < 2:
                findings.add("subscribe_without_callback")
            # agent.window() is synchronous; awaiting it raises TypeError.
            if node.func.attr == "window" and isinstance(getattr(node, "parent", None), ast.Await):
                findings.add("awaited_window")
        # Generated agents must use agent.llm, never their own client.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])] + [
                getattr(node, "module", "") or ""
            ]
            if any(n.split(".")[0] in {"openai", "anthropic", "ollama"} for n in names if n):
                findings.add("own_llm_client")

    # A body that is nothing but `pass` / docstring is a stub, not an agent.
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            meaningful = [
                s
                for s in node.body
                if not isinstance(s, ast.Pass)
                and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            ]
            if not meaningful:
                findings.add(f"empty_body:{node.name}")
    return findings


def score_dynamic(output: str, expected: list[str] | dict) -> bool:
    """Generated agent code must parse, define the required async entry points,
    reference what the task requires, and not break the framework's rules.

    ``expected`` is either a list of required function names (compile + entry
    points only), or a dict::

        {"functions": ["setup", "process"],
         "must_contain": ["custom/home/heartbeat", "agent.subscribe"],
         "must_not_contain": ["requests.get"],
         "allow_empty_bodies": false}

    Structural checks always run for the dict form: module-level imports,
    ``await agent.subscribe(...)``, ``subscribe`` without a callback, importing
    an LLM client instead of using ``agent.llm``, and stub function bodies.
    """
    if isinstance(expected, dict):
        functions = expected.get("functions") or []
        must_contain = expected.get("must_contain") or []
        must_not_contain = expected.get("must_not_contain") or []
        structural = True
        allow_empty = bool(expected.get("allow_empty_bodies"))
    else:
        functions, must_contain, must_not_contain = expected, [], []
        structural = False
        allow_empty = True

    code = _strip_fences(output)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    async_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    if not all(name in async_defs for name in functions):
        return False

    lowered = code.lower()
    if any(str(token).lower() not in lowered for token in must_contain):
        return False
    if any(str(token).lower() in lowered for token in must_not_contain):
        return False

    if structural:
        findings = _structural_findings(tree, code)
        # Only the entry points the task actually requires must have a body.
        # Models routinely emit the full lifecycle with `pass` in the slots they
        # do not need (setup/cleanup stubs alongside a real handle_task); that is
        # idiomatic, not a failure.
        required = set(functions)
        findings = {
            f
            for f in findings
            if not f.startswith("empty_body:")
            or (not allow_empty and f.split(":", 1)[1] in required)
        }
        if not expected.get("strict_imports"):
            findings.discard("module_level_import")
        if findings:
            return False
    return True


_SCORERS = {
    "intent": score_intent,
    "ha": score_ha,
    "actuator": score_actuator,
    "planner": score_planner,
    "dynamic": score_dynamic,
}


def _system_prompts() -> dict[str, str]:
    """Production prompts where importable; condensed variants otherwise.
    Imported lazily so scorer unit tests never pull agent modules.
    """
    from .agents.one_off_actuator_agent import _RESOLVER_PROMPT
    from .agents.prompts.home_assistant_prompts import HA_ACTION_CLASSIFICATION_PROMPT
    from .agents.prompts.main_actor_prompts import INTENT_CLASSIFIER_PROMPT

    return {
        "intent": INTENT_CLASSIFIER_PROMPT,
        "ha": HA_ACTION_CLASSIFICATION_PROMPT,
        "actuator": _RESOLVER_PROMPT,
        "planner": _PLANNER_SYSTEM,
        "dynamic": _CODEGEN_SYSTEM,
    }


# ── Case loading ─────────────────────────────────────────────────────────────


def load_cases(path: str | None, categories: list[str]) -> list[dict[str, Any]]:
    """Load benchmark cases from a JSONL file (or the built-in seed set) and
    filter to the requested categories. Malformed lines are skipped loudly.
    """
    if path:
        cases = []
        for i, line in enumerate(Path(path).read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except Exception as exc:
                logger.warning("[eval] %s:%d skipped (bad JSON: %s)", path, i, exc)
                continue
            if not all(k in case for k in ("id", "category", "prompt", "expected")):
                logger.warning("[eval] %s:%d skipped (missing keys)", path, i)
                continue
            if case["category"] not in CATEGORIES:
                logger.warning(
                    "[eval] %s:%d skipped (unknown category %r)", path, i, case["category"]
                )
                continue
            cases.append(case)
    else:
        cases = list(_SEED_CASES)
    return [c for c in cases if c["category"] in categories]


# ── Runner ───────────────────────────────────────────────────────────────────


async def run_case(
    provider,
    system: str,
    case: dict,
    rep: int,
    model_spec: str,
    temperature: float | None = None,
) -> dict:
    """One (model, case, repetition) call → a flat result record."""
    record = {
        "model": model_spec,
        "category": case["category"],
        "case_id": case["id"],
        "rep": rep,
        "temperature": temperature,
        "ok": False,
        "passed": False,
        "latency_s": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "output": "",
        "error": None,
        "ts": time.time(),
    }
    start = time.perf_counter()
    try:
        text, usage = await provider.complete(
            messages=[{"role": "user", "content": case["prompt"]}],
            system=system,
            max_tokens=_MAX_TOKENS[case["category"]],
            reasoning_effort="none",
            **({} if temperature is None else {"temperature": temperature}),
        )
        record["latency_s"] = round(time.perf_counter() - start, 3)
        record["ok"] = True
        record["output"] = text
        record["input_tokens"] = usage.get("input_tokens", 0)
        record["output_tokens"] = usage.get("output_tokens", 0)
        record["cost_usd"] = usage.get("cost_usd", 0.0)
        record["passed"] = bool(_SCORERS[case["category"]](text, case["expected"]))
    except Exception as exc:
        record["latency_s"] = round(time.perf_counter() - start, 3)
        record["error"] = str(exc)
    return record


def summarize(records: list[dict]) -> list[dict]:
    """Per (model, category): n, error count, accuracy, mean latency, cost."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        buckets.setdefault((r["model"], r["category"]), []).append(r)
    rows = []
    for (model, category), rs in sorted(buckets.items()):
        ok = [r for r in rs if r["ok"]]
        latencies = [r["latency_s"] for r in ok if r["latency_s"] is not None]
        rows.append(
            {
                "model": model,
                "category": category,
                "n": len(rs),
                "errors": len(rs) - len(ok),
                "accuracy": round(sum(r["passed"] for r in rs) / len(rs), 4) if rs else 0.0,
                "mean_latency_s": round(sum(latencies) / len(latencies), 3) if latencies else None,
                "total_cost_usd": round(sum(r["cost_usd"] for r in rs), 6),
            }
        )
    return rows


async def run_eval(
    model_specs: list[str],
    cases: list[dict],
    repeat: int,
    out_dir: Path,
    temperature: float | None = None,
) -> list[dict]:
    from .llm_factory import create_provider

    systems = _system_prompts()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    records: list[dict] = []

    with results_path.open("a") as sink:
        for spec in model_specs:
            provider_name, _, model = spec.partition(":")
            provider = create_provider(provider_name, model or None)
            if provider is None:
                logger.error("[eval] Skipping %r — no provider", spec)
                continue
            logger.info("[eval] Model %s: %d cases x %d reps", spec, len(cases), repeat)
            for case in cases:
                for rep in range(repeat):
                    record = await run_case(
                        provider,
                        systems[case["category"]],
                        case,
                        rep,
                        spec,
                        temperature=temperature,
                    )
                    records.append(record)
                    sink.write(json.dumps(record) + "\n")
                    sink.flush()
                    status = "PASS" if record["passed"] else ("ERR " if record["error"] else "fail")
                    logger.info(
                        "[eval]   %s %s rep%d %s (%.1fs)",
                        spec,
                        case["id"],
                        rep,
                        status,
                        record["latency_s"] or 0.0,
                    )

    rows = summarize(records)
    with (out_dir / "summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "model",
                "category",
                "n",
                "errors",
                "accuracy",
                "mean_latency_s",
                "total_cost_usd",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _print_summary(rows: list[dict]) -> None:
    header = f"{'model':<38} {'category':<10} {'n':>3} {'err':>3} {'acc':>6} {'lat(s)':>7} {'cost($)':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        lat = f"{r['mean_latency_s']:.2f}" if r["mean_latency_s"] is not None else "-"
        print(
            f"{r['model']:<38} {r['category']:<10} {r['n']:>3} {r['errors']:>3} "
            f"{r['accuracy']:>6.2%} {lat:>7} {r['total_cost_usd']:>8.4f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wactorz.evalharness",
        description="Per-call-site SLM/LLM evaluation harness.",
    )
    parser.add_argument(
        "--models",
        required=True,
        help='Comma-separated provider[:model] specs, e.g. "ollama:qwen3:4b,anthropic:claude-sonnet-4-6"',
    )
    parser.add_argument(
        "--prompts", default=None, help="Benchmark JSONL file (default: built-in seed set)"
    )
    parser.add_argument(
        "--categories",
        default=",".join(CATEGORIES),
        help=f"Comma-separated subset of: {', '.join(CATEGORIES)}",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Repetitions per case (default 1)")
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature for every model (e.g. 0 for deterministic runs). "
        "Omit to use LLM_TEMPERATURE, or each provider's default when that is unset.",
    )
    parser.add_argument(
        "--out", default="eval_results", help="Output directory (default ./eval_results)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        parser.error(f"unknown categories: {unknown}")
    model_specs = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = load_cases(args.prompts, categories)
    if not cases:
        parser.error("no benchmark cases matched")

    rows = asyncio.run(
        run_eval(model_specs, cases, args.repeat, Path(args.out), temperature=args.temperature)
    )
    _print_summary(rows)
    print(
        f"\nRecords: {Path(args.out) / 'results.jsonl'}\nSummary: {Path(args.out) / 'summary.csv'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

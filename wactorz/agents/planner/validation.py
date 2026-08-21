"""Repair of the agent code an LLM generates for a pipeline step.

The model reliably makes the same few mistakes -- awaiting synchronous agent
methods, reaching for raw aiomqtt instead of agent.subscribe, calling the Home
Assistant REST API directly -- so they are corrected here rather than left to
fail at spawn time. Anything that cannot be corrected is logged and left alone.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def rewrite_aiomqtt_to_subscribe(code: str, topic: str) -> str:
    """Best-effort rewrite of raw aiomqtt MQTT subscription code to use agent.subscribe().
    Extracts the message handling callback and rewires it.
    Returns empty string if rewrite fails (original code kept).
    """
    # Try to extract the callback body — look for the inner async for loop body
    # Pattern: async for msg/message in client.messages: ... payload handling ...
    match = re.search(
        r"async\s+for\s+\w+\s+in\s+client\.messages:\s*\n(.*?)(?=\n\s*except|\n\s*$)",
        code,
        re.DOTALL,
    )
    if not match:
        return ""

    callback_body = match.group(1)

    # Detect how payload is parsed — json.loads(msg.payload) or similar
    payload_parse = ""
    if "json.loads" in callback_body:
        payload_parse = "    # payload is already a dict (parsed by agent.subscribe)\n"

    # Strip leading indentation from callback body
    lines = callback_body.splitlines()
    min_indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=4)
    dedented = "\n".join("    " + ln[min_indent:] for ln in lines if ln.strip())

    # Extract any setup code before the aiomqtt block
    pre_match = re.split(r"async\s+with\s+aiomqtt\.Client", code)[0]
    pre_lines = [
        ln
        for ln in pre_match.splitlines()
        if ln.strip()
        and not ln.strip().startswith("import aiomqtt")
        and not ln.strip().startswith("async def setup")
    ]
    pre_code = (
        "\n".join("    " + ln.strip() for ln in pre_lines if ln.strip()) + "\n" if pre_lines else ""
    )

    rewritten = (
        f"async def setup(agent):\n"
        f"{pre_code}"
        f"    async def _on_message(payload):\n"
        f"{payload_parse}"
        f"{dedented}\n"
        f"    agent.subscribe('{topic}', _on_message)\n"
        f"    await agent.log('Subscribed to {topic}')\n"
    )

    # Preserve any process() or handle_task() that existed

    for fn in ("process", "handle_task"):
        fn_match = re.search(rf"async\s+def\s+{fn}\s*\(", code)
        if fn_match:
            rewritten += "\n" + code[fn_match.start() :]
            break

    return rewritten


#: Agent API methods that are synchronous, so awaiting one is a runtime error.
SYNC_METHODS = (
    "subscribe",
    "window",
    "persist",
    "recall",
    "declare_contract",
    "agents",
    "nodes",
    "topics",
    "capabilities",
    "increment_processed",
    "increment_errors",
)
_SYNC_AWAIT_RE = re.compile(r"\bawait\s+(agent\.(?:" + "|".join(SYNC_METHODS) + r")\s*\()")

#: Ways generated code reaches for the Home Assistant REST API directly.
HA_API_PATTERNS = (
    r"/api/services/",
    r"/api/states/",
    r"httpx.*api/services",
    r"requests\.(post|put|get).*api/services",
    r"aiohttp.*api/services",
)


def _strip_spurious_awaits(spawn_config: dict[str, Any]) -> str | None:
    """Drop `await` from synchronous agent calls. Returns a note if it changed."""
    fixed, count = _SYNC_AWAIT_RE.subn(r"\1", spawn_config["code"])
    if not count:
        return None
    spawn_config["code"] = fixed
    return f"removed {count} spurious await(s) on sync agent methods"


def _replace_raw_aiomqtt(spawn_config: dict[str, Any], log_name: str, step_name: str) -> str | None:
    """Rewire raw aiomqtt to agent.subscribe. Returns a note if the code used it."""
    code = spawn_config["code"]
    if "aiomqtt.Client(" not in code and "aiomqtt.connect(" not in code:
        return None

    topics = re.findall(r'await\s+client\.subscribe\(["\']([^"\']+)["\']', code)
    if topics:
        rewritten = rewrite_aiomqtt_to_subscribe(code, topics[0])
        if rewritten:
            spawn_config["code"] = rewritten
            logger.info(
                "[%s] Auto-fixed raw aiomqtt in '%s' -> agent.subscribe('%s')",
                log_name,
                step_name,
                topics[0],
            )
    return "raw aiomqtt.Client() - should use agent.subscribe()"


def _flag_direct_ha_api(code: str, log_name: str, step_name: str) -> str | None:
    """Report generated code that calls the Home Assistant REST API itself."""
    for pattern in HA_API_PATTERNS:
        if re.search(pattern, code):
            logger.warning(
                "[%s] '%s' calls the HA API directly and will likely fail. Should use "
                "the ha_actuator pattern: the dynamic agent publishes a trigger and "
                "ha_actuator executes the HA service call.",
                log_name,
                step_name,
            )
            return f"direct HA API call ('{pattern[:30]}...') - should use the ha_actuator agent"
    return None


def validate_pipeline_code(plan: list[dict[str, Any]], log_name: str) -> list[dict[str, Any]]:
    """Repair the mistakes an LLM commonly makes in generated agent code.

    Edits each dynamic step's code in place and returns the same plan. What
    cannot be repaired is reported and left as written, so a step never silently
    becomes something the model did not generate.
    """
    for step in plan:
        spawn_config = step.get("spawn_config", {})
        if spawn_config.get("type") != "dynamic" or not spawn_config.get("code"):
            continue

        step_name = step.get("name", "")
        issues = [
            note
            for note in (
                _strip_spurious_awaits(spawn_config),
                _replace_raw_aiomqtt(spawn_config, log_name, step_name),
                _flag_direct_ha_api(spawn_config["code"], log_name, step_name),
            )
            if note
        ]
        if issues:
            logger.warning("[%s] Code issues in '%s': %s", log_name, step_name, "; ".join(issues))

    return plan

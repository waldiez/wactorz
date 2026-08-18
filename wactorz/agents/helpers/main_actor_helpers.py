"""Pure, stateless helpers and registry-key constants for MainActor.

Extracted verbatim from main_actor.py — no behaviour change. These have no
dependency on `self`, so they live as module-level functions/constants.
"""

import json
import re

SPAWN_REGISTRY_KEY = "_spawned_agents"
PIPELINE_RULES_KEY = "_pipeline_rules"
PENDING_PLANS_KEY = "_pending_plans"  # dry-run proposals awaiting user approval
NODE_REGISTRY_KEY = "_known_nodes"  # tracks online remote nodes


def _normalize_agent_name(name: str) -> str:
    """Canonicalise an agent name for fuzzy matching.

    Lowercases, turns spaces/underscores into dashes, and strips a redundant
    trailing '-agent' suffix so 'Smart Energy Agent', 'smart_energy_agent',
    and 'smart-energy' all collapse to 'smart-energy'.
    """
    norm = (name or "").lower().strip().replace("_", "-").replace(" ", "-")
    while "--" in norm:
        norm = norm.replace("--", "-")
    norm = norm.strip("-")
    if norm.endswith("-agent") and norm != "-agent":
        norm = norm[: -len("-agent")]
    return norm


def _parse_plan_envelope(planner_result: str) -> dict | None:
    """Try to parse a planner result string as a plan envelope (the JSON dict
    returned by plan_only mode). Returns the envelope dict if it's a valid
    proposal, or None if the result is a regular answer (e.g. error message,
    feasibility failure, or fallback prose).
    """
    if not planner_result or not planner_result.strip().startswith("{"):
        return None
    try:
        envelope = json.loads(planner_result)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(envelope, dict) and envelope.get("_plan_proposal") is True:
        return envelope
    return None


def _parse_spawn_config(raw: str) -> dict:
    """Robustly parse a spawn config that may contain raw multiline code strings.
    Uses character scanning to correctly handle } and " inside the code value.
    """
    raw = raw.strip()

    # Strategy 1: standard JSON (works when LLM properly escapes newlines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: backtick-delimited code (rare but some LLMs use it)
    bt_match = re.search(r'"code"\s*:\s*`(.*?)`', raw, re.DOTALL)
    if bt_match:
        code_raw = bt_match.group(1)
        placeholder = re.sub(r'"code"\s*:\s*`.*?`', '"code": "__CODE__"', raw, flags=re.DOTALL)
        config = json.loads(placeholder)
        config["code"] = code_raw
        return config

    # Strategy 3: locate the code value's bounds, then swap it out for a
    # placeholder so the surrounding object parses as normal JSON.
    #
    # We can't find the value's end by scanning forward for the first unescaped
    # '"': the code is raw (that's why strategies 1/2 failed) and legitimately
    # contains its own double quotes — e.g. a Python string like
    # 'phrases like "Vamos!"'. A forward scan stops at that first inner quote,
    # truncates the code, and corrupts the placeholder ("Expecting ',' delimiter").
    #
    # Instead, treat every '"' after the opening one as a candidate closing
    # quote, right-most first, and keep the first that makes the REST of the
    # object valid JSON. The genuine closing quote is the right-most one (code
    # is the last field by convention), so it wins on the first try; inner
    # quotes are only tried if a trailing field moves the true end leftward.
    key_match = re.search(r'"code"\s*:\s*"', raw)
    if not key_match:
        raise ValueError(f"No 'code' key found in spawn config:\n{raw[:200]}")

    code_start = key_match.end()  # index right after the opening "
    prefix = raw[: key_match.start()]
    quote_positions = [code_start + m.start() for m in re.finditer(r'"', raw[code_start:])]

    config = None
    code_raw = ""
    for close in reversed(quote_positions):
        placeholder = prefix + '"code": "__CODE__"' + raw[close + 1 :]
        try:
            config = json.loads(placeholder)
        except json.JSONDecodeError:
            continue
        code_raw = raw[code_start:close]
        break

    if config is None:
        raise ValueError(
            "Spawn config JSON invalid after code extraction: no closing quote for "
            f"the code value produced parseable JSON.\nRaw:\n{raw[:300]}"
        )

    # Unescape sequences the LLM may have added. Decode as a real JSON string in
    # one correct pass (json.loads handles \\, \n, \t, \", \uXXXX, … together and,
    # crucially, collapses an escaped backslash instead of leaving it doubled).
    # strict=False tolerates the raw newlines/tabs that made the top-level
    # json.loads fail in the first place. Falling back to sequential .replace()
    # would re-introduce the original bug: a Python escape like \' arrives here as
    # \\' (JSON-escaped), and without collapsing \\ the string literal terminates
    # early — stranding any following non-ASCII char (e.g. an em-dash) outside the
    # string and raising SyntaxError only once the runner compiles it.
    try:
        config["code"] = json.loads('"' + code_raw + '"', strict=False)
    except json.JSONDecodeError:
        # Best-effort fallback for genuinely invalid escapes (e.g. a bare \');
        # collapse the common sequences with the backslash LAST so it doesn't
        # corrupt the ones decoded before it.
        config["code"] = (
            code_raw.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return config


# ── Text/intent helpers (pure string heuristics, no state) ──────────────


def _looks_like_home_automation_request(text: str) -> bool:
    lowered = (text or "").lower()
    if "home assistant" in lowered:
        return True
    if lowered.startswith(("spawn ", "/")):
        return False

    # Wactorz pipeline requests — these involve external sensors/agents, not HA natively
    # Route to planner instead of HA agent
    _pipeline_keywords = [
        "camera",
        "webcam",
        "yolo",
        "detect",
        "detection",
        "person detect",
        "object detect",
        "laptop camera",
        "cv2",
        "opencv",
        "when detected",
        "if detected",
        "whenever detected",
        "notify me",
        "send me a message",
        "send me a discord",
        "discord",
        "telegram",
        "whatsapp",
    ]
    if any(kw in lowered for kw in _pipeline_keywords):
        return False

    has_trigger = any(
        token in lowered
        for token in [
            "when ",
            "if ",
            "on ",
            "whenever ",
            "after ",
            "before ",
            "as soon as ",
            "at ",
        ]
    )
    has_action = any(
        token in lowered
        for token in [
            "turn on",
            "turn off",
            "open",
            "close",
            "lock",
            "unlock",
            "dim",
            "set",
        ]
    )
    has_automation_intent = any(
        token in lowered
        for token in [
            "automate",
            "automation",
            "routine",
            "scene",
            "trigger",
            "schedule",
            "presence",
            "motion",
            "door",
            "window",
            "sensor",
            "alarm",
            "romantic",
            "cozy",
            "ambience",
            "ambiance",
        ]
    )
    has_home_context = any(
        token in lowered
        for token in [
            "home",
            "house",
            "apartment",
            "room",
            "living room",
            "bedroom",
            "kitchen",
            "hallway",
            "garage",
            "porch",
        ]
    )

    return (
        (has_trigger and has_action)
        or (has_trigger and has_automation_intent)
        or (has_automation_intent and has_home_context)
    )


def _strip_live_context(message: str) -> str:
    """Remove the [CURRENT SYSTEM STATE...][END SYSTEM STATE] prefix if present.
    Used before fact extraction so the auto-injected agent list doesn't get
    treated as user-stated facts.
    """
    if not isinstance(message, str) or "[CURRENT SYSTEM STATE" not in message:
        return message
    end_marker = "[END SYSTEM STATE]"
    idx = message.find(end_marker)
    if idx == -1:
        return message
    # Skip past the marker and any whitespace following it
    return message[idx + len(end_marker) :].lstrip("\n").lstrip()


#: Prefixes that skip dry-run and approval for PIPELINE intent.
#:
#: One tuple because three places act on them — the planner deciding whether to
#: gate a request, the helper stripping the marker off the task text, and the
#: restricted channel refusing the admin surface — and a marker added to one of
#: those and missed by another is a guard that catches some spellings of the
#: same thing. Add a marker here, not at a call site.
BYPASS_MARKERS = ("pipeline!", "coordinate!", "@planner!")


def starts_with_bypass(text: str) -> bool:
    """Whether `text` opens with a bypass marker, whatever its case or spacing."""
    return text.lower().lstrip().startswith(BYPASS_MARKERS)


def _strip_dryrun_bypass(text: str) -> str:
    """Strip the bypass marker from the user's text so the planner does not see
    it as part of the task.
    """
    if not text:
        return text
    lowered = text.lower().lstrip()
    for bypass in BYPASS_MARKERS:
        if lowered.startswith(bypass):
            # Find the bypass in the original (case-insensitive) and skip it
            idx = text.lower().find(bypass)
            if idx != -1:
                return text[idx + len(bypass) :].lstrip(" :,-")
    return text

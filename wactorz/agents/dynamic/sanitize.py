"""Removing the LLM self-setup a model writes out of habit.

Generated code often imports `openai` and reaches for an API key of its own.
The agent already has a provider, so that setup is replaced with an inert
line rather than deleted -- keeping the line count means a traceback still
points at the right place in the code the model wrote.
"""

import re


def sanitize_code(code: str) -> str:
    """Block-aware sanitizer. Removes LLM self-setup patterns entirely:
    - try/except blocks containing LLM imports
    - if/else blocks checking api_key or llm_backend
    - orphan else:/elif: that follow sanitized blocks
    - call_llm/call_openai/call_ollama functions -> agent.llm shim
    - standalone bad lines
    """
    LLM_PATTERNS = [
        r"\bimport\s+(openai|anthropic|ollama|langchain)\b",
        r"\bfrom\s+(openai|anthropic|ollama|langchain)\b",
        r"\b(OPENAI_API_KEY|ANTHROPIC_API_KEY)\b",
        r"os\.environ.*API_KEY",
        r"\b(openai|anthropic|ollama)\.(OpenAI|Anthropic|Client|AsyncOpenAI|AsyncAnthropic)\b",
        # api_key as a variable assignment (not as a dict key like 'api_key': ...)
        r"^\s*api_key\s*=",
        # llm_backend as a variable assignment only
        r"^\s*agent\.state\[.llm_backend.\]\s*=",
    ]

    def line_is_bad(line):
        return any(re.search(p, line) for p in LLM_PATTERNS)

    def collect_block(lines, start, base_indent, conts=("except", "else", "finally", "elif")):
        j, block = start, []
        pat = r"\s*(" + "|".join(conts) + r")\b" if conts else r"(?!x)x"
        while j < len(lines):
            bl = lines[j]
            bl_ind = len(bl) - len(bl.lstrip()) if bl.strip() else base_indent + 4
            if bl.strip() and bl_ind <= base_indent and not re.match(pat, bl):
                break
            block.append(bl)
            j += 1
        return block, j

    lines = code.split("\n")
    result = []
    i = 0
    last_sanitized = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0
        prefix = " " * indent

        if not stripped:
            result.append(line)
            last_sanitized = False
            i += 1
            continue

        # try: blocks — nuke entirely if they touch LLM
        if stripped == "try:":
            block, j = collect_block(lines, i + 1, indent)
            full = [line, *block]
            if any(line_is_bad(ln) for ln in full):
                result.append(prefix + "pass  # sanitized: LLM setup block")
                last_sanitized = True
            else:
                result.extend(full)
                last_sanitized = False
            i = j
            continue

        # if/elif whose condition references LLM vars — nuke whole branch
        if re.match(r"\s*(if|elif)\b", line) and line_is_bad(line):
            _, j = collect_block(lines, i + 1, indent, ("elif", "else"))
            result.append(prefix + "pass  # sanitized: LLM conditional")
            last_sanitized = True
            i = j
            continue

        # orphan else:/elif: after a sanitized block — drop silently
        if re.match(r"\s*(else\s*:|elif\b)", line) and last_sanitized:
            _, j = collect_block(lines, i + 1, indent, ())
            i = j
            continue

        # LLM wrapper functions — replace with agent.llm shim
        fn_m = re.match(
            r"(\s*)(async\s+)?def\s+"
            r"(call_llm|call_openai|call_ollama|call_anthropic|call_gpt|"
            r"get_llm|setup_llm|create_llm|query_llm|ask_llm|llm_call)\s*\(",
            line,
        )
        if fn_m:
            _, j = collect_block(lines, i + 1, len(fn_m.group(1)), ())
            p, fname = fn_m.group(1), fn_m.group(3)
            result += [
                p + "async def " + fname + "(agent, messages, system='', **kw):",
                p + "    # sanitized: rewired to agent.llm",
                p
                + "    sys_p = system or next((m.get('content','') for m in messages if m.get('role')=='system'), '')",
                p + "    msgs  = [m for m in messages if m.get('role') != 'system']",
                p + "    return await agent.llm.complete(messages=msgs, system=sys_p)",
            ]
            last_sanitized = False
            i = j
            continue

        # standalone bad lines
        if line_is_bad(line):
            result.append(prefix + "pass  # sanitized: " + stripped[:60])
            last_sanitized = True
            i += 1
            continue

        last_sanitized = False
        result.append(line)
        i += 1

    sanitized = "\n".join(result)

    # ── Strip spurious `await` on known synchronous agent API methods ──
    # LLMs write `await agent.subscribe(...)` because setup() is async.
    # These methods already return _AwaitableNone so the code won't crash,
    # but stripping `await` keeps the code clean and avoids confusion.
    _SYNC_METHODS = (
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
    _sync_pat = r"\bawait\s+(agent\.(?:" + "|".join(_SYNC_METHODS) + r")\s*\()"
    return re.sub(_sync_pat, r"\1", sanitized)

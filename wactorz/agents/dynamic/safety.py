"""What generated code is refused for, and what is only warned about.

A pattern scan over source text in front of an exec that happens by design.
It catches the mistakes a model makes, not an adversary: anything spelling a
call differently -- through getattr, or with whitespace inside the dotted
name -- passes. Treat a clean result as 'nothing obviously wrong', never as
containment.
"""

import logging
import re

logger = logging.getLogger(__name__)

BLOCKED_PATTERNS: list[tuple[str, str]] = [
    # System-level access
    (r"\bos\.system\s*\(", "os.system() — use subprocess instead or avoid shell commands"),
    (r"\bos\.popen\s*\(", "os.popen() — use subprocess instead"),
    (r"\bos\.exec[a-z]*\s*\(", "os.exec*() — direct process replacement not allowed"),
    (r"\bos\.remove\s*\(", "os.remove() — file deletion not allowed in agent code"),
    (r"\bos\.rmdir\s*\(", "os.rmdir() — directory deletion not allowed"),
    (r"\bshutil\.rmtree\s*\(", "shutil.rmtree() — recursive deletion not allowed"),
    (
        r"\bsubprocess\.(?:call|run|Popen)\s*\(.{0,20}rm\s",
        "subprocess with rm — destructive shell command",
    ),
    # Network abuse
    (r"\bsocket\.socket\s*\(", "raw socket creation — use httpx or agent.publish instead"),
    # Code execution / eval
    (r"\beval\s*\(", "eval() — arbitrary code execution not allowed"),
    (r"\b__import__\s*\(", "__import__() — use regular import statements"),
    # File system writes outside agent scope
    (r'\bopen\s*\([^)]*["\'][wab]["\']', "open() in write mode — use agent.persist() instead"),
]

WARN_PATTERNS: list[tuple[str, str]] = [
    (r"\bsubprocess\b", "subprocess usage — ensure this is necessary"),
    (r"\bctypes\b", "ctypes — low-level C interface, use with caution"),
    (r"\bpickle\.loads?\b", "pickle — deserialization risk if data is untrusted"),
    (
        r"\bwhile\s+True\s*:(?!.*await)",
        "tight while-True loop without await — may block event loop",
    ),
]

PROCESS_ANTIPATTERNS: list[tuple[str, str]] = [
    (
        r"asyncio\.sleep\s*\(",
        "asyncio.sleep() inside process() — NEVER sleep in process(). "
        "The framework loops process() every poll_interval seconds. "
        "Move MQTT-reactive logic to setup() + agent.subscribe() instead.",
    ),
    (
        r"await\s+agent\.mqtt_get\s*\(",
        "await agent.mqtt_get() inside process() — this blocks until a message arrives. "
        "Use agent.subscribe() in setup() for reactive MQTT logic instead.",
    ),
    (
        r"while\s+True\s*:",
        "while True loop inside process() — process() must return after each iteration. "
        "The framework already loops it. Remove the while loop.",
    ),
    (
        r"\.release\s*\(\s*\)[\s\S]{0,200}?cv2\.VideoCapture\s*\(",
        "cap.release() followed by cv2.VideoCapture() inside process() — "
        "do NOT reopen the camera on a single failed read. The framework's "
        "cv2 shim already retries opens with backoff and a settle delay. "
        "On a failed cap.read(), just `return` from process() — the next "
        "poll_interval tick will retry. Releasing+reopening from process() "
        "produces a flap loop on Windows MSMF/DSHOW.",
    ),
]


def extract_function_body(code: str, fn_name: str) -> str | None:
    """Extract the body of a top-level function by name from source code.
    Simple indentation-based parser — good enough for LLM-generated code.
    """
    lines = code.splitlines()
    in_fn = False
    body_lines = []
    fn_indent = 0

    for line in lines:
        stripped = line.strip()
        if not in_fn:
            if re.match(rf"^(async\s+)?def\s+{fn_name}\s*\(", stripped):
                in_fn = True
                fn_indent = len(line) - len(line.lstrip())
            continue
        if not stripped:
            body_lines.append(line)
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= fn_indent and stripped:
            break
        body_lines.append(line)

    return "\n".join(body_lines) if body_lines else None


def validate_code_safety(code: str, log_name: str) -> str | None:
    """Scan sanitized code for dangerous patterns before exec().

    Returns an error message string if blocked, None if OK.
    Warnings are logged but don't block execution.
    """
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, code):
            logger.warning("[%s] BLOCKED dangerous code pattern: %s", log_name, reason)
            return f"Code blocked for safety: {reason}"

    for pattern, reason in WARN_PATTERNS:
        if re.search(pattern, code):
            logger.warning("[%s] Safety warning: %s", log_name, reason)

    # ── Detect anti-patterns specifically inside process() ─────────────
    process_body = extract_function_body(code, "process")
    if process_body:
        for pattern, reason in PROCESS_ANTIPATTERNS:
            if re.search(pattern, process_body):
                logger.warning(
                    "[%s] process() anti-pattern detected - this will cause "
                    "120s timeout crashes: %s",
                    log_name,
                    reason,
                )

    return None  # OK — warnings never block execution

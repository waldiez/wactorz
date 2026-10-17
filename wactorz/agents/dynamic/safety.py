"""What generated code is refused for, and what is only warned about.

A check in front of an exec that happens by design. The refusals come from
parsing the code and reading each name through the code's own imports, so
``os . system``, ``import os as o; o.system`` and ``getattr(os, "system")`` are
all read as ``os.system``, whether called or only referenced. That makes walking
past the check take code that looks deliberately evasive; it does not make the
check containment. A name looked up through a value the parser cannot follow --
a string held in a variable, ``sys.modules``, a subscript of ``globals()`` --
passes, and so does anything a permitted module can do (``pathlib`` deletes,
for one). Writing a file is not refused in any form: from the text alone a data
export and an overwritten state file look the same, so where an agent may write
is for the process running it to decide. Treat a clean result as 'nothing
obviously wrong', never as a sandbox: isolating untrusted code means running it
in another process.

The warnings and the ``process()`` anti-patterns stay a pattern scan over the
text. They are advice about code that hangs or deserves a second look, and
never refuse anything.

**The parse itself is guarded.** CPython's parser and AST builder recurse once
per level of nesting and stop themselves with a `RecursionError` when the code
is too deep, but only if the C stack lasts that long. A thread on Windows gets
a megabyte, which deeply nested code uses up first, and a C stack overflow
takes the whole process down, every actor in it. So the code is parsed on a
thread given room enough to reach the guard, and whatever parses is measured:
deeper than `MAX_DEPTH`, it is refused here, so the compile that would follow
on the ordinary stack never sees it.
"""

import ast
import logging
import re
import threading
from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Names that refuse the code wherever they appear, keyed by what they resolve
#: to through the code's imports. Referencing one is refused as well as calling
#: it, so binding it to another name first does not help.
BLOCKED_NAMES: dict[str, str] = {
    "os.system": "os.system() — use subprocess instead or avoid shell commands",
    "os.popen": "os.popen() — use subprocess instead",
    "os.remove": "os.remove() — file deletion not allowed in agent code",
    "os.unlink": "os.unlink() — file deletion not allowed in agent code",
    "os.rmdir": "os.rmdir() — directory deletion not allowed",
    "shutil.rmtree": "shutil.rmtree() — recursive deletion not allowed",
    "socket.socket": "raw socket creation — use httpx or agent.publish instead",
    "eval": "eval() — arbitrary code execution not allowed",
    "exec": "exec() — arbitrary code execution not allowed",
    "compile": "compile() — builds code to run; write the code itself instead",
    "__import__": "__import__() — use regular import statements",
    "importlib.__import__": "__import__() — use regular import statements",
    "importlib.import_module": "importlib.import_module() — use regular import statements",
    # Named one by one rather than by prefix, so os.executable stays readable.
    **dict.fromkeys(
        (
            f"os.{name}"
            for name in (
                "execl",
                "execle",
                "execlp",
                "execlpe",
                "execv",
                "execve",
                "execvp",
                "execvpe",
            )
        ),
        "os.exec*() — direct process replacement not allowed",
    ),
}

#: The modules the blocked names live in, and ``subprocess``, whose ``rm`` check
#: needs to see which of its functions is called. Reaching into one by a name the
#: parser cannot read -- a star import, ``getattr`` with a computed name,
#: ``vars()`` or ``__dict__`` -- hides which function is used, so it is refused too.
GUARDED_MODULES = frozenset({"builtins", "importlib", "os", "shutil", "socket", "subprocess"})

#: Attributes that reach the interpreter's internals: a function's globals, a
#: class's subclasses, the builtins. Ordinary agent code has no use for them,
#: and they are the usual first step around a check like this one.
INTERNAL_ATTRIBUTES = frozenset(
    {
        "__base__",
        "__bases__",
        "__builtins__",
        "__closure__",
        "__code__",
        "__getattribute__",
        "__globals__",
        "__loader__",
        "__mro__",
        "__subclasses__",
    }
)

#: What a module is under another name: ``os`` is ``posix`` or ``nt`` underneath.
_MODULE_ALIASES = {"nt": "os", "posix": "os"}


_SUBPROCESS_CALLS = frozenset(
    {
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.run",
    }
)
_SUBPROCESS_REASON = "subprocess with rm — destructive shell command"

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


#: Modules a repair may import though the program it repairs did not: standard
#: modules that only compute. A missing ``import json`` is among the commonest
#: crashes a repair fixes, and none of these reaches a process, a socket or a
#: file the program could not reach already.
REPAIR_MAY_IMPORT = frozenset(
    {
        "abc",
        "asyncio",
        "bisect",
        "collections",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "heapq",
        "itertools",
        "json",
        "math",
        "numbers",
        "operator",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "typing",
        "uuid",
    }
)

#: An import line, for code that does not parse: a syntax repair starts there.
_IMPORT_LINE = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)|import[ \t]+([A-Za-z_][\w \t,.]*))", re.MULTILINE
)


def imports_a_repair_adds(original: str, repaired: str) -> list[str]:
    """The modules ``repaired`` imports that ``original`` did not, beyond `REPAIR_MAY_IMPORT`.

    Top-level names, sorted: ``import os.path`` is ``os``, and a relative import
    is the program's own code. What a repair is asked to fix comes from the
    failing run, whose error text can carry what the agent was processing, so
    a repair keeps to the capabilities the program it repairs already had.
    ``original`` is the program the agent was first given, never an earlier
    repair, so successive repairs cannot add a module each. A ``repaired`` that
    does not parse adds nothing: compiling it reports that.
    """
    added = _imported(repaired)
    if added is None:
        return []
    had = _imported(original)
    if had is None:
        had = _imported_by_line(original)
    return sorted(added - had - REPAIR_MAY_IMPORT)


def _imported(code: str) -> set[str] | None:
    try:
        tree = _parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _imported_by_line(code: str) -> set[str]:
    names: set[str] = set()
    for found in _IMPORT_LINE.finditer(code):
        if found[1]:
            names.add(found[1])
        else:
            for part in found[2].split(","):
                words = part.split()
                if words:
                    names.add(words[0].split(".")[0])
    return names


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


def _canonical(name: str) -> str:
    """`name` with a module's other spelling replaced and ``builtins.`` dropped."""
    head, _, rest = name.partition(".")
    head = _MODULE_ALIASES.get(head, head)
    if head == "builtins" and rest:
        return rest
    return f"{head}.{rest}" if rest else head


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Each name the code's imports bind, mapped to the dotted name it stands for.

    Collected from every import in the code, at any depth: a name imported
    inside a function still says what the code means by it.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.partition(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _literal_attribute(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The attribute a ``getattr(obj, "name", ...)`` call names, if it is a literal."""
    if not (isinstance(node, ast.Call) and len(node.args) >= 2):
        return None
    if _resolve(node.func, aliases) != "getattr":
        return None
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return name.value
    return None


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """The dotted name `node` refers to, read through the imports; None if unknown.

    A name nothing imports stands for itself, so ``os.system`` without an
    ``import os`` still reads as ``os.system``.
    """
    if isinstance(node, ast.Name):
        return _canonical(aliases.get(node.id, node.id))
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, aliases)
        return None if base is None else _canonical(f"{base}.{node.attr}")
    attribute = _literal_attribute(node, aliases)
    if attribute is not None and isinstance(node, ast.Call):
        base = _resolve(node.args[0], aliases)
        return None if base is None else _canonical(f"{base}.{attribute}")
    return None


def _blocked_reason(name: str | None) -> str | None:
    return None if name is None else BLOCKED_NAMES.get(name)


def _runs_rm(node: ast.Call) -> bool:
    """Whether a subprocess call's command, written out literally, is ``rm``."""
    if not node.args:
        return False
    command = node.args[0]
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        first = command.value.split(maxsplit=1)[:1]
    elif isinstance(command, (ast.List, ast.Tuple)) and command.elts:
        head = command.elts[0]
        first = [head.value] if isinstance(head, ast.Constant) else []
    else:
        return False
    return first == ["rm"]


def _call_refusal(node: ast.Call, aliases: dict[str, str]) -> str | None:
    """What a call is refused for beyond the names it uses."""
    func = _resolve(node.func, aliases)
    if func in ("getattr", "vars") and node.args:
        target = _resolve(node.args[0], aliases)
        attribute = _literal_attribute(node, aliases)
        if attribute in INTERNAL_ATTRIBUTES:
            return f"{attribute} — reaches the interpreter's internals"
        if target in GUARDED_MODULES and attribute is None:
            shown = f"vars({target})" if func == "vars" else f"getattr({target}, …)"
            return f"{shown} — call the function by its name"
    if func in _SUBPROCESS_CALLS and _runs_rm(node):
        return _SUBPROCESS_REASON
    return None


def _reaching_in(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """What `node` is refused for as a way into a guarded module or the interpreter."""
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        module = _canonical(node.module)
        if module in GUARDED_MODULES and any(alias.name == "*" for alias in node.names):
            return f"from {node.module} import * — import the names you use"
    elif isinstance(node, ast.Name) and node.id in INTERNAL_ATTRIBUTES:
        return f"{node.id} — reaches the interpreter's internals"
    elif isinstance(node, ast.Attribute):
        if node.attr in INTERNAL_ATTRIBUTES:
            return f"{node.attr} — reaches the interpreter's internals"
        module = _resolve(node.value, aliases)
        if node.attr == "__dict__" and module in GUARDED_MODULES:
            return f"{module}.__dict__ — call the function by its name"
    return None


def _refusals(tree: ast.AST, aliases: dict[str, str]) -> Iterator[str]:
    """Every reason `tree` is refused for, in the order the walk meets them."""
    for node in ast.walk(tree):
        reasons = (
            _reaching_in(node, aliases),
            _blocked_reason(_resolve(node, aliases)),
            _call_refusal(node, aliases) if isinstance(node, ast.Call) else None,
        )
        yield from (reason for reason in reasons if reason is not None)


def _refusal(code: str) -> str | None:
    """The first reason `code` is refused for, or None."""
    try:
        tree = _parse(code)
    except (SyntaxError, ValueError):
        # compile() is handed this same text next and fails on it the same
        # way, so nothing runs; its error is the one worth sending back.
        return None
    except (RecursionError, MemoryError):
        return TOO_DEEP
    if _depth(tree) > MAX_DEPTH:
        return TOO_DEEP
    try:
        return next(_refusals(tree, _import_aliases(tree)), None)
    except RecursionError:
        return TOO_DEEP


#: How deep a program's syntax tree may nest before it is refused unread. Real
#: code, generated or not, nests a few dozen levels at most; this is the depth
#: the tokenizer itself allows for brackets, and well within what the compiler
#: handles on the smallest stack a thread is given.
MAX_DEPTH = 200

TOO_DEEP = "code nested too deeply to check"

#: The stack the parse runs on: enough for the parser to reach its own
#: recursion guard rather than the end of the stack, on every platform.
_PARSE_STACK_BYTES = 64 * 1024 * 1024


def _parse(code: str) -> ast.AST:
    """``ast.parse(code)``, run where a too-deep program is an error and not a crash.

    Raises whatever the parse raised. Where a thread cannot be given the
    stack, the parse runs in place, as it always did.
    """
    outcome: list[ast.AST | BaseException] = []

    def run() -> None:
        try:
            outcome.append(ast.parse(code))
        except BaseException as exc:  # handed back to the caller to raise
            outcome.append(exc)

    try:
        previous = threading.stack_size(_PARSE_STACK_BYTES)
    except (ValueError, RuntimeError):
        return ast.parse(code)
    try:
        worker = threading.Thread(target=run, name="parse-generated-code")
        worker.start()
    finally:
        threading.stack_size(previous)
    worker.join()
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _depth(tree: ast.AST) -> int:
    """How deeply ``tree`` nests, counted without recursing."""
    deepest = 0
    pending: list[tuple[ast.AST, int]] = [(tree, 1)]
    while pending:
        node, depth = pending.pop()
        deepest = max(deepest, depth)
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return deepest


def validate_code_safety(code: str, log_name: str) -> str | None:
    """Check sanitized code before exec().

    Returns an error message string if blocked, None if OK.
    Warnings are logged but don't block execution.
    """
    reason = _refusal(code)
    if reason is not None:
        logger.warning("[%s] BLOCKED dangerous code: %s", log_name, reason)
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

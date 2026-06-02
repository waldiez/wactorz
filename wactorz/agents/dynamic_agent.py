"""
DynamicAgent - A generic actor shell whose behavior is defined by LLM-generated code.

The LLM writes three async functions:
  async def setup(agent):        # called once on start — load models, open connections
  async def process(agent):      # called in a loop — core logic, publish results
  async def handle_task(agent, payload): # called when another agent sends a TASK

The `agent` parameter gives access to:
  agent.publish(topic, data)     # publish to MQTT
  agent.log(message)             # add to event log
  agent.alert(message, severity) # trigger an alert
  agent.name                     # agent name
  agent.actor_id                 # unique ID
  agent.state                    # current state
  agent.persist(key, val)        # save to disk
  agent.recall(key)              # load from disk
  agent.send_to(name, payload)   # send task to another agent
"""

import asyncio
import logging
import time
import traceback
import types
from typing import Any, Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)


class _AwaitableNone:
    """
    Sentinel that can be safely awaited (returns None) or used in bool context (False).

    LLMs writing async code inside DynamicAgent frequently add `await` to sync API
    calls like agent.subscribe(), agent.window(), agent.persist(), etc.  Returning
    this instead of bare None prevents 'TypeError: object NoneType can't be used
    in await expression' — the #1 runtime failure in LLM-generated agent code.
    """

    def __await__(self):
        return iter([])        # completes immediately, yields None

    def __bool__(self):
        return False

    def __repr__(self):
        return "None"


_AWAITABLE_NONE = _AwaitableNone()


class DynamicAgent(Actor):
    """
    Generic actor shell. Core behavior is provided as Python source code strings.
    The LLM writes setup/process/handle_task functions; this class runs them.
    """

    def __init__(
        self,
        code: str,                          # LLM-generated Python source
        poll_interval: float = 1.0,         # seconds between process() calls
        description: str = "",              # what this agent does
        input_schema: dict = None,          # expected task payload fields
        output_schema: dict = None,         # returned result fields
        llm_provider=None,                  # optional LLM for agent.llm.chat()
        trusted: bool = False,              # True = catalog agent, skip safety validator
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._code           = code
        self.poll_interval   = poll_interval
        self.description     = description
        self.input_schema    = input_schema  or {}
        self.output_schema   = output_schema or {}
        self._llm_provider   = llm_provider
        self._trusted        = trusted       # catalog agents bypass safety checks

        # Compiled functions — populated in on_start
        self._fn_setup       = None
        self._fn_process     = None
        self._fn_handle_task = None

        # Namespace shared across all calls (agent can store state here)
        self._ns: dict       = {}

        # Cost tracking (populated by _LLMInterface if LLM is used)
        self.total_input_tokens  = 0
        self.total_output_tokens = 0
        self.total_cost_usd      = 0.0

        # Error tracking for health classification
        self._consecutive_errors: int   = 0
        self._error_threshold:    int   = 3      # DEGRADED after this many
        self._last_error_time:    float = 0.0
        self._error_phase:        str   = ""     # compile|setup|process|handle_task

        # Public API exposed to generated code via `agent` parameter
        self._api            = _AgentAPI(self)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        # ── Compile with LLM self-correction on syntax errors ─────────────
        current_code = self._code
        error_msg    = self._compile_code(current_code)

        if error_msg:
            for attempt in range(1, self._MAX_COMPILE_RETRIES + 1):
                logger.warning(
                    f"[{self.name}] Compile error (attempt {attempt}): {error_msg}"
                )
                fixed = await self._fix_syntax_with_llm(current_code, error_msg)
                if fixed is None:
                    # LLM unavailable — no point retrying
                    break
                self._ns = {}                      # fresh namespace for retry
                new_err = self._compile_code(fixed)
                if new_err is None:
                    # Fix worked — update stored code so restarts use the good version
                    self._code = fixed
                    error_msg  = None
                    logger.info(f"[{self.name}] Code fixed by LLM after {attempt} attempt(s).")
                    # ── Write fixed code back to spawn registry so restart uses it ──
                    self._persist_fixed_code(fixed)
                    await self._mqtt_publish(
                        f"agents/{self.actor_id}/logs",
                        {"type": "log",
                         "message": f"Syntax error fixed by LLM after {attempt} attempt(s).",
                         "timestamp": time.time()},
                    )
                    break
                # Fix compiled but still broken — feed it back for the next attempt
                current_code = fixed
                error_msg    = new_err

        if error_msg:
            # All attempts exhausted — publish fatal and stop
            err_exc = SyntaxError(error_msg)
            logger.error(f"[{self.name}] Code compilation failed permanently: {error_msg}")
            # ── Erlang/OTP: mark FAILED so Supervisor's watch_loop detects us ──
            self.state = ActorState.FAILED
            await self._publish_error(phase="compile", error=err_exc,
                                      traceback_str=error_msg, fatal=True)
            return

        # ── setup() ───────────────────────────────────────────────────────
        if self._fn_setup:
            # Run setup as a background task so long-running loops (e.g. aiomqtt
            # subscriptions) don't block on_start() and prevent heartbeats from firing.
            self._tasks.append(asyncio.create_task(self._run_setup()))
        else:
            if self._fn_process:
                self._tasks.append(asyncio.create_task(self._process_loop()))

        # Publish manifest immediately so main's registry knows this agent exists
        # even if it never calls publish() (pure handle_task agents, etc.)
        await self._api._publish_manifest()

    async def on_stop(self):
        # ── Persist final cost metrics so they survive agent deletion ──────
        # Without this, cost data dies with the agent object and the UI
        # can't show lifetime costs for deleted agents.
        if hasattr(self, "total_cost_usd") and self.total_cost_usd > 0:
            self.persist("_final_cost", {
                "input_tokens":  self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "cost_usd":      round(self.total_cost_usd, 6),
                "name":          self.name,
                "stopped_at":    time.time(),
            })

        # ── Publish final metrics before heartbeat loop is cancelled ───────
        try:
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                self._build_metrics() if hasattr(self, '_build_metrics') else {
                    "actor_id":           self.actor_id,
                    "input_tokens":       getattr(self, "total_input_tokens", 0),
                    "output_tokens":      getattr(self, "total_output_tokens", 0),
                    "cost_usd":           round(getattr(self, "total_cost_usd", 0.0), 6),
                    "messages_processed": self.metrics.messages_processed,
                    "errors":             self.metrics.errors,
                    "uptime":             self.metrics.uptime,
                    "final":              True,   # signals UI this is the last metrics msg
                },
            )
        except Exception:
            pass

        # ── Unregister from TopicBus so stale contracts don't accumulate ───
        try:
            from ..core.topic_bus import get_topic_bus
            bus = get_topic_bus()
            if bus:
                bus.unregister(self.name)
                logger.debug(f"[{self.name}] Unregistered from TopicBus")
        except Exception:
            pass  # TopicBus unavailable — not fatal

        # ── Give generated code a chance to clean up ───────────────────────
        cleanup = self._ns.get("cleanup")
        if cleanup:
            try:
                await asyncio.wait_for(cleanup(self._api), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.name}] cleanup() timed out after 10s")
            except Exception as e:
                logger.warning(f"[{self.name}] cleanup() error: {e}")

        # ── Force-release common resources that LLM code may have opened ───
        # Even if cleanup() didn't run or missed something, we try to release
        # known resource types stored in agent.state OR in module-level globals
        # inside the compiled namespace (_ns). LLM-generated code frequently uses
        # globals like `_cap = None` instead of agent.state, so we must check both.
        state = getattr(self._api, 'state', {}) if self._api else {}

        # Skip builtins/modules/functions — only look at plain objects
        _SKIP_TYPES = (type(None), bool, int, float, str, bytes, type, types.ModuleType,
                       types.FunctionType, types.CoroutineType)

        def _release_obj(key, obj):
            """Release a single resource object, logging the result."""
            if obj is None or isinstance(obj, _SKIP_TYPES):
                return
            # cv2.VideoCapture (and anything with release/isOpened)
            if hasattr(obj, 'release') and hasattr(obj, 'isOpened'):
                try:
                    if obj.isOpened():
                        obj.release()
                        logger.info(f"[{self.name}] Released camera handle '{key}'")
                except Exception:
                    pass
            # Open file handles
            elif hasattr(obj, 'close') and hasattr(obj, 'closed'):
                try:
                    if not obj.closed:
                        obj.close()
                        logger.debug(f"[{self.name}] Closed file handle '{key}'")
                except Exception:
                    pass

        # Scan agent.state (preferred pattern)
        for key in list(state.keys()):
            _release_obj(key, state.get(key))

        # Scan module-level globals in the compiled namespace (common LLM pattern)
        # e.g. `_cap = None` / `_model = None` at module level
        for key, obj in list(self._ns.items()):
            if key.startswith('__') or key in ('setup', 'process', 'cleanup', 'handle_task'):
                continue
            _release_obj(key, obj)

        # ── Cancel any tasks spawned inside setup/process code ─────────────
        # Generated code may have called asyncio.create_task() directly without
        # adding to _tasks. We can't track those, but we can ensure all tasks
        # we DO track are properly cancelled and awaited.
        for task in self._tasks:
            if not task.done():
                task.cancel()
        # Give cancelled tasks a moment to actually stop
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── Code compilation ───────────────────────────────────────────────────

    @staticmethod
    def _sanitize_code(code: str) -> str:
        """
        Block-aware sanitizer. Removes LLM self-setup patterns entirely:
        - try/except blocks containing LLM imports
        - if/else blocks checking api_key or llm_backend
        - orphan else:/elif: that follow sanitized blocks
        - call_llm/call_openai/call_ollama functions -> agent.llm shim
        - standalone bad lines
        """
        import re

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

        def collect_block(lines, start, base_indent, conts=("except","else","finally","elif")):
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

        lines  = code.split("\n")
        result = []
        i      = 0
        last_sanitized = False

        while i < len(lines):
            line     = lines[i]
            stripped = line.strip()
            indent   = len(line) - len(line.lstrip()) if stripped else 0
            prefix   = " " * indent

            if not stripped:
                result.append(line)
                last_sanitized = False
                i += 1
                continue

            # try: blocks — nuke entirely if they touch LLM
            if stripped == "try:":
                block, j = collect_block(lines, i + 1, indent)
                full = [line] + block
                if any(line_is_bad(l) for l in full):
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
                    p + "    sys_p = system or next((m.get('content','') for m in messages if m.get('role')=='system'), '')",
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
            "subscribe", "window", "persist", "recall",
            "declare_contract", "agents", "nodes", "topics",
            "capabilities", "increment_processed", "increment_errors",
        )
        _sync_pat = r"\bawait\s+(agent\.(?:" + "|".join(_SYNC_METHODS) + r")\s*\()"
        sanitized = re.sub(_sync_pat, r"\1", sanitized)

        return sanitized




    # Max times on_start will ask the LLM to fix a syntax error before giving up
    _MAX_COMPILE_RETRIES = 2

    # ── Pre-exec safety validator ──────────────────────────────────────────
    # Scans sanitized code for dangerous patterns BEFORE exec().
    # This is NOT a sandbox — it's a best-effort blocklist.
    # For true isolation, run DynamicAgents in a subprocess or container.

    _BLOCKED_PATTERNS = [
        # System-level access
        (r'\bos\.system\s*\(',              "os.system() — use subprocess instead or avoid shell commands"),
        (r'\bos\.popen\s*\(',               "os.popen() — use subprocess instead"),
        (r'\bos\.exec[a-z]*\s*\(',          "os.exec*() — direct process replacement not allowed"),
        (r'\bos\.remove\s*\(',              "os.remove() — file deletion not allowed in agent code"),
        (r'\bos\.rmdir\s*\(',               "os.rmdir() — directory deletion not allowed"),
        (r'\bshutil\.rmtree\s*\(',          "shutil.rmtree() — recursive deletion not allowed"),
        (r'\bsubprocess\.(?:call|run|Popen)\s*\(.{0,20}rm\s',
                                            "subprocess with rm — destructive shell command"),
        # Network abuse
        (r'\bsocket\.socket\s*\(',          "raw socket creation — use httpx or agent.publish instead"),
        # Code execution / eval
        (r'\beval\s*\(',                    "eval() — arbitrary code execution not allowed"),
        (r'\b__import__\s*\(',              "__import__() — use regular import statements"),
        # File system writes outside agent scope
        (r'\bopen\s*\([^)]*["\'][wab]["\']', "open() in write mode — use agent.persist() instead"),
    ]

    # Patterns that are suspicious but allowed — just logged as warnings
    _WARN_PATTERNS = [
        (r'\bsubprocess\b',                 "subprocess usage — ensure this is necessary"),
        (r'\bctypes\b',                     "ctypes — low-level C interface, use with caution"),
        (r'\bpickle\.loads?\b',             "pickle — deserialization risk if data is untrusted"),
        (r'\bwhile\s+True\s*:(?!.*await)',  "tight while-True loop without await — may block event loop"),
    ]

    # Patterns checked specifically inside process() body — cause 120s timeout crashes
    _PROCESS_ANTIPATTERNS = [
        (r'asyncio\.sleep\s*\(',
         "asyncio.sleep() inside process() — NEVER sleep in process(). "
         "The framework loops process() every poll_interval seconds. "
         "Move MQTT-reactive logic to setup() + agent.subscribe() instead."),
        (r'await\s+agent\.mqtt_get\s*\(',
         "await agent.mqtt_get() inside process() — this blocks until a message arrives. "
         "Use agent.subscribe() in setup() for reactive MQTT logic instead."),
        (r'while\s+True\s*:',
         "while True loop inside process() — process() must return after each iteration. "
         "The framework already loops it. Remove the while loop."),
        (r'\.release\s*\(\s*\)[\s\S]{0,200}?cv2\.VideoCapture\s*\(',
         "cap.release() followed by cv2.VideoCapture() inside process() — "
         "do NOT reopen the camera on a single failed read. The framework's "
         "cv2 shim already retries opens with backoff and a settle delay. "
         "On a failed cap.read(), just `return` from process() — the next "
         "poll_interval tick will retry. Releasing+reopening from process() "
         "produces a flap loop on Windows MSMF/DSHOW."),
    ]

    def _validate_code_safety(self, code: str) -> Optional[str]:
        """
        Scan sanitized code for dangerous patterns before exec().

        Returns an error message string if blocked, None if OK.
        Warnings are logged but don't block execution.
        """
        import re

        for pattern, reason in self._BLOCKED_PATTERNS:
            if re.search(pattern, code):
                logger.warning(f"[{self.name}] BLOCKED dangerous code pattern: {reason}")
                return f"Code blocked for safety: {reason}"

        for pattern, reason in self._WARN_PATTERNS:
            if re.search(pattern, code):
                logger.warning(f"[{self.name}] Safety warning: {reason}")

        # ── Detect anti-patterns specifically inside process() ─────────────
        process_body = self._extract_function_body(code, "process")
        if process_body:
            for pattern, reason in self._PROCESS_ANTIPATTERNS:
                if re.search(pattern, process_body):
                    logger.warning(
                        f"[{self.name}] process() anti-pattern detected — "
                        f"this will cause 120s timeout crashes: {reason}"
                    )

        return None  # OK — warnings never block execution

    @staticmethod
    def _extract_function_body(code: str, fn_name: str) -> Optional[str]:
        """
        Extract the body of a top-level function by name from source code.
        Simple indentation-based parser — good enough for LLM-generated code.
        """
        import re
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

    def _compile_code(self, code: Optional[str] = None) -> Optional[str]:
        """
        Sanitize, validate safety, then compile LLM-generated code into self._ns.

        Returns the error message string if compilation fails, None on success.
        Callers use the error string to ask the LLM to fix the code and retry
        (see on_start / _fix_syntax_with_llm).

        Trusted agents (from the catalog) skip the safety validator — their code
        is pre-built and tested, and may legitimately use __import__, subprocess,
        etc. that the safety validator would block.
        """
        source = code if code is not None else self._code
        clean  = self._sanitize_code(source) if not self._trusted else source

        # ── Safety check before exec (skipped for trusted/catalog agents) ──
        if not self._trusted:
            safety_error = self._validate_code_safety(clean)
            if safety_error:
                return safety_error
        else:
            logger.info(f"[{self.name}] Trusted agent — skipping safety validator")

        # Pre-inject the LLM shim so generated code can call agent.llm directly
        def _get_llm_shim(*args, **kwargs):
            return self._api.llm
        self._ns["get_llm"]    = _get_llm_shim
        self._ns["setup_llm"]  = _get_llm_shim
        self._ns["create_llm"] = _get_llm_shim

        # ── cv2 shim: wrap VideoCapture with retry + release-before-reopen ──
        # Only injected when the agent code actually references cv2 — no-op for
        # chat agents, schedulers, or anything else that doesn't use the camera.
        # LLM code uses `_cap = cv2.VideoCapture(0)` as a global. On Windows
        # (MSMF backend) the previous session's handle may not be fully released
        # by the OS yet, so the first open succeeds but grabFrame() immediately
        # fails with -1072873821. The shim retries with increasing delays so the
        # agent recovers without manual intervention.
        import re as _re
        if _re.search(r'\bcv2\b', clean):
            try:
                import cv2 as _real_cv2
                import types as _types

                _agent_name_for_shim = self.name  # capture for closure

                class _ResilientVideoCapture(_real_cv2.VideoCapture):
                    """
                    Drop-in replacement for cv2.VideoCapture that retries the open
                    with backoff when the MSMF backend grabs the device index but
                    then immediately fails to deliver frames.

                    Transparent to LLM code — same API, same isinstance() checks.
                    """
                    _RETRY_DELAYS = [1.0, 2.0, 4.0, 8.0]   # seconds between retries
                    # Time to wait after a successful open() before probing read().
                    # MSMF/DSHOW source readers need ~200-300ms to start streaming
                    # even after isOpened() returns True. Probing too soon yields
                    # the cyclic "opened but read failed" log we used to see.
                    _POST_OPEN_SETTLE = 0.3                 # seconds

                    def __init__(self, index_or_path, *args, **kwargs):
                        import sys as _sys
                        super().__init__()
                        # ── Windows: force DSHOW for integer indices ──────────
                        # MSMF (the OpenCV default on Windows) is flaky on
                        # consumer laptop / cheap USB cameras and produces
                        # error -1072873821 (MF_E_HW_MFT_FAILED_START_STREAMING)
                        # in a flap loop. DSHOW (DirectShow) is older but far
                        # more reliable for this hardware class. Only override
                        # when the LLM didn't pass an explicit backend.
                        if (_sys.platform == "win32"
                                and isinstance(index_or_path, int)
                                and not args
                                and "apiPreference" not in kwargs):
                            try:
                                args = (_real_cv2.CAP_DSHOW,)
                                logger.info(
                                    f"[{_agent_name_for_shim}] Windows detected — "
                                    f"forcing CAP_DSHOW backend for camera index "
                                    f"{index_or_path} (more reliable than MSMF)"
                                )
                            except Exception:
                                pass
                        self._index  = index_or_path
                        self._args   = args
                        self._kwargs = kwargs
                        self._do_open()

                    def read(self):
                        # Return the probe frame captured during open verification
                        # so the first cap.read() in process() is not lost.
                        if hasattr(self, '_probe_frame') and self._probe_frame is not None:
                            frame, self._probe_frame = self._probe_frame, None
                            return True, frame
                        return super().read()

                    def _do_open(self):
                        for attempt, delay in enumerate(
                            [0.0] + self._RETRY_DELAYS, start=1
                        ):
                            if delay:
                                import time as _t
                                # Release before retrying so MSMF frees the device
                                try:
                                    super().release()
                                except Exception:
                                    pass
                                logger.info(
                                    f"[{_agent_name_for_shim}] Camera open retry "
                                    f"{attempt}/{len(self._RETRY_DELAYS)+1} "
                                    f"— waiting {delay:.0f}s for OS to release device"
                                )
                                _t.sleep(delay)

                            super().open(self._index, *self._args, **self._kwargs)
                            if not super().isOpened():
                                continue

                            # Give the source reader time to start streaming
                            # before the probe. MSMF/DSHOW both need a beat
                            # after isOpened() returns True; probing immediately
                            # produces -1072873821 even when the device is fine.
                            import time as _t
                            _t.sleep(self._POST_OPEN_SETTLE)

                            # Verify we can actually grab a frame — MSMF sometimes
                            # reports isOpened()=True but then immediately errors.
                            # Use read() and stash the probe frame on the instance so
                            # the first cap.read() in process() doesn't get an empty
                            # result (grab() is destructive and has no unread()).
                            ok, probe = super().read()
                            if ok and probe is not None:
                                self._probe_frame = probe
                                logger.info(
                                    f"[{_agent_name_for_shim}] Camera opened successfully "
                                    f"on attempt {attempt}"
                                )
                                return   # success

                            logger.warning(
                                f"[{_agent_name_for_shim}] Camera opened but read() failed "
                                f"on attempt {attempt} — device may not be fully released yet"
                            )

                        logger.error(
                            f"[{_agent_name_for_shim}] Camera could not be opened after "
                            f"{len(self._RETRY_DELAYS)+1} attempts"
                        )

                # Wrap in a module proxy so `import cv2` inside agent code still works,
                # and `cv2.VideoCapture` transparently becomes the resilient version.
                _cv2_shim = _types.ModuleType("cv2")
                _cv2_shim.__dict__.update(_real_cv2.__dict__)
                _cv2_shim.VideoCapture = _ResilientVideoCapture
                self._ns["cv2"] = _cv2_shim

            except ImportError:
                pass  # cv2 not installed — no shim needed

        try:
            exec(compile(clean, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup       = self._ns.get("setup")
            self._fn_process     = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")
            fns = [f for f in ["setup", "process", "handle_task", "cleanup"] if f in self._ns]
            logger.info(f"[{self.name}] Code compiled OK. Functions: {fns}")
            if not fns:
                logger.warning(f"[{self.name}] No functions found in compiled code.")
            return None   # success
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    async def _fix_syntax_with_llm(self, bad_code: str, error_msg: str) -> Optional[str]:
        """
        Ask the configured LLM to fix a syntax error in agent code.

        Returns the (possibly still-broken) code string from the LLM, or None
        only if the LLM is completely unavailable (no provider, API error).
        The caller is responsible for verifying the fix with _compile_code().
        """
        if self._llm_provider is None:
            return None

        prompt = (
            "The following Python code has a syntax error.\n"
            f"Error: {error_msg}\n\n"
            "Fix ONLY the syntax error. Do not change logic or add features.\n"
            "Return ONLY the corrected Python code — no explanations, "
            "no markdown fences, no commentary.\n\n"
            f"```python\n{bad_code}\n```"
        )
        logger.info(f"[{self.name}] Asking LLM to fix syntax error: {error_msg[:120]}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log",
             "message": f"Syntax error — asking LLM to fix: {error_msg[:120]}",
             "timestamp": time.time()},
        )
        try:
            response, usage = await self._llm_provider.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a Python syntax expert. Return only valid Python code.",
                max_tokens=4096,
            )
            # Track cost
            if hasattr(self, "total_input_tokens"):
                self.total_input_tokens  += usage.get("input_tokens", 0)
                self.total_output_tokens += usage.get("output_tokens", 0)
                self.total_cost_usd      += usage.get("cost_usd", 0.0)

            # Strip markdown fences the LLM may add despite instructions
            fixed = response.strip()
            if fixed.startswith("```"):
                fixed = "\n".join(
                    l for l in fixed.split("\n")
                    if not l.strip().startswith("```")
                ).strip()

            return fixed   # caller validates with _compile_code()

        except Exception as e:
            logger.warning(f"[{self.name}] LLM fix call failed: {e}")
            return None    # only None when LLM is truly unreachable

    # ── Setup wrapper ───────────────────────────────────────────────────────

    # Max times _run_setup will ask the LLM to fix a runtime error before giving up
    _MAX_SETUP_RETRIES = 2

    async def _run_setup(self):
        """
        Run setup() as a background task with LLM self-correction on failure.

        If setup() raises a runtime error (e.g. TypeError from await on sync call,
        NameError, AttributeError), the LLM is asked to fix the code and the whole
        compile-then-setup cycle is retried up to _MAX_SETUP_RETRIES times.

        - If process() is also defined, it is started AFTER setup() returns.
          For agents whose setup() never returns (e.g. aiomqtt subscription loops),
          process() is simply not started — the subscription loop IS the process.
        """
        current_code = self._code
        last_error   = None

        for attempt in range(1 + self._MAX_SETUP_RETRIES):
            try:
                await self._fn_setup(self._api)
                if attempt > 0:
                    logger.info(f"[{self.name}] setup() succeeded after {attempt} fix(es).")
                    # ── Write fixed code back to spawn registry so restart uses it ──
                    self._persist_fixed_code(self._code)
                    await self._mqtt_publish(
                        f"agents/{self.actor_id}/logs",
                        {"type": "log",
                         "message": f"setup() runtime error fixed by LLM after {attempt} attempt(s).",
                         "timestamp": time.time()},
                    )
                else:
                    logger.info(f"[{self.name}] setup() completed.")
                last_error = None
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                last_error = e
                err = traceback.format_exc()
                logger.error(f"[{self.name}] setup() failed (attempt {attempt + 1}): {e}")

                if attempt >= self._MAX_SETUP_RETRIES:
                    break  # exhausted retries

                # Ask LLM to fix the runtime error
                fixed = await self._fix_runtime_with_llm(current_code, str(e), err)
                if fixed is None:
                    logger.warning(f"[{self.name}] LLM unavailable — cannot fix setup() error")
                    break

                # Recompile the fixed code
                self._ns = {}
                compile_err = self._compile_code(fixed)
                if compile_err:
                    logger.warning(f"[{self.name}] LLM fix introduced compile error: {compile_err}")
                    # Try to fix the compile error too
                    fixed2 = await self._fix_syntax_with_llm(fixed, compile_err)
                    if fixed2:
                        self._ns = {}
                        compile_err2 = self._compile_code(fixed2)
                        if compile_err2:
                            break  # can't fix compile error either
                        fixed = fixed2
                    else:
                        break
                else:
                    # compile_err is None — code is good
                    pass

                self._code   = fixed
                current_code = fixed
                logger.info(f"[{self.name}] Retrying setup() with LLM-fixed code (attempt {attempt + 1})...")

        if last_error is not None:
            err = traceback.format_exc()
            logger.error(f"[{self.name}] setup() failed permanently: {last_error}")
            # ── Erlang/OTP: mark FAILED so Supervisor's watch_loop can see us ──
            self.state = ActorState.FAILED
            await self._publish_error(
                phase="setup", error=last_error, traceback_str=err, fatal=True
            )
            return

        # setup() returned cleanly — start process() loop if defined
        if self._fn_process and self.state not in (ActorState.STOPPED, ActorState.FAILED):
            self._tasks.append(asyncio.create_task(self._process_loop()))

    async def _fix_runtime_with_llm(
        self, code: str, error_msg: str, traceback_str: str
    ) -> Optional[str]:
        """
        Ask the LLM to fix a runtime error in agent code (setup/process).

        Similar to _fix_syntax_with_llm but provides the traceback and
        explicit guidance about the agent API (sync vs async methods).
        """
        if self._llm_provider is None:
            return None

        prompt = (
            "The following Python code raised a RUNTIME ERROR when executed.\n\n"
            f"Error: {error_msg}\n"
            f"Traceback (last 800 chars):\n{traceback_str[-800:]}\n\n"
            "IMPORTANT API RULES — these are the most common mistakes:\n"
            "  - agent.subscribe(topic, callback) is SYNCHRONOUS — do NOT use await\n"
            "  - agent.window(topic, seconds=N) is SYNCHRONOUS — do NOT use await\n"
            "  - agent.persist(key, val) is SYNCHRONOUS — do NOT use await\n"
            "  - agent.recall(key) is SYNCHRONOUS — do NOT use await\n"
            "  - agent.declare_contract(...) is SYNCHRONOUS — do NOT use await\n"
            "  - agent.agents() is SYNCHRONOUS — do NOT use await\n"
            "  - await agent.publish(topic, data) — this IS async, use await\n"
            "  - await agent.log(msg) — this IS async, use await\n"
            "  - await agent.alert(msg) — this IS async, use await\n"
            "  - await agent.send_to(name, payload) — this IS async, use await\n"
            "  - await agent.mqtt_get(topic) — this IS async, use await\n\n"
            "STREAMWINDOW API — w = agent.window('topic', seconds=N):\n"
            "  StreamWindow is NOT a dict. Use methods, not dict-style access.\n"
            "  Methods: count(), mean('field'), min('field'), max('field'),\n"
            "           values('field'), latest(), rising('field', threshold=X),\n"
            "           falling(), stable(), absent_for(seconds),\n"
            "           event_count(key='k', value=V, seconds=N)\n"
            "  WRONG: w.get('temp')        — StreamWindow is not a dict\n"
            "  WRONG: w['temp']            — no __getitem__ by key intended\n"
            "  RIGHT: w.latest()           — returns latest payload dict (or None)\n"
            "  RIGHT: w.values('temp')     — list of all 'temp' values in window\n"
            "  RIGHT: w.mean('temp')       — average of 'temp' over window\n\n"
            "Fix the error. Return ONLY the corrected Python code — no explanations, "
            "no markdown fences, no commentary.\n\n"
            f"```python\n{code}\n```"
        )
        logger.info(f"[{self.name}] Asking LLM to fix runtime error: {error_msg[:120]}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log",
             "message": f"Runtime error — asking LLM to fix: {error_msg[:120]}",
             "timestamp": time.time()},
        )
        try:
            response, usage = await self._llm_provider.complete(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You are a Python runtime-error expert for an async agent framework. "
                    "Return only valid Python code."
                ),
                max_tokens=4096,
            )
            if hasattr(self, "total_input_tokens"):
                self.total_input_tokens  += usage.get("input_tokens", 0)
                self.total_output_tokens += usage.get("output_tokens", 0)
                self.total_cost_usd      += usage.get("cost_usd", 0.0)

            fixed = response.strip()
            if fixed.startswith("```"):
                fixed = "\n".join(
                    l for l in fixed.split("\n")
                    if not l.strip().startswith("```")
                ).strip()
            return fixed

        except Exception as e:
            logger.warning(f"[{self.name}] LLM runtime-fix call failed: {e}")
            return None

    # ── Process loop ───────────────────────────────────────────────────────

    # Max time a single process() or handle_task() call can take before
    # we assume it's stuck in a blocking call and cancel it.
    _PROCESS_TIMEOUT = 120.0    # seconds
    _HANDLE_TASK_TIMEOUT = 60.0

    # ── How many consecutive process() errors before we attempt LLM self-fix ──
    _PROCESS_LLM_FIX_THRESHOLD = 3    # try to fix after this many errors in a row
    # How many consecutive process() errors trigger state=FAILED (Supervisor sees this)
    _PROCESS_FAIL_THRESHOLD    = 5

    async def _process_loop(self):
        """
        Continuously call the generated process() function.

        Erlang/OTP semantics:
        - Each error increments _consecutive_errors.
        - At _PROCESS_LLM_FIX_THRESHOLD consecutive errors, ask the LLM to fix the code
          and recompile in-place (self-healing).
        - At _PROCESS_FAIL_THRESHOLD consecutive errors (or after LLM fix fails),
          set state=FAILED — the Supervisor's _watch_loop will detect this and restart us.
          This is the "let it crash" principle: don't spin in degraded mode forever.
        """
        _llm_fix_attempted = False   # only try the LLM fix once per process_loop lifetime

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            if self.state == ActorState.PAUSED:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await asyncio.wait_for(
                    self._fn_process(self._api),
                    timeout=self._PROCESS_TIMEOUT,
                )
                self._reset_error_count()
                _llm_fix_attempted = False   # reset after a clean run
            except asyncio.TimeoutError:
                self.metrics.errors += 1
                logger.error(
                    f"[{self.name}] process() timed out after {self._PROCESS_TIMEOUT}s "
                    f"— likely a blocking call without run_in_executor"
                )
                await self._publish_error(
                    phase="process",
                    error=TimeoutError(f"process() exceeded {self._PROCESS_TIMEOUT}s"),
                    traceback_str=f"process() did not return within {self._PROCESS_TIMEOUT}s. "
                                  f"Wrap blocking calls (cv2, torch) in: "
                                  f"await asyncio.get_event_loop().run_in_executor(None, fn)",
                )
                # Erlang: escalate to FAILED after too many timeouts — Supervisor takes over
                if self._consecutive_errors >= self._PROCESS_FAIL_THRESHOLD:
                    logger.critical(
                        f"[{self.name}] process() timed out {self._consecutive_errors}x "
                        f"— setting FAILED so Supervisor can restart cleanly."
                    )
                    self.state = ActorState.FAILED
                    return
                backoff = min(2 ** self._consecutive_errors, 30)
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.errors += 1
                tb = traceback.format_exc()
                logger.error(f"[{self.name}] process() error: {e}\n{tb}")
                await self._publish_error(phase="process", error=e, traceback_str=tb)

                # ── LLM self-healing: try to fix the code in-place ────────────
                if (
                    not _llm_fix_attempted
                    and self._consecutive_errors >= self._PROCESS_LLM_FIX_THRESHOLD
                    and self._llm_provider is not None
                ):
                    _llm_fix_attempted = True
                    logger.warning(
                        f"[{self.name}] {self._consecutive_errors} consecutive process() "
                        f"errors — asking LLM to fix code in-place."
                    )
                    fixed = await self._fix_runtime_with_llm(self._code, str(e), tb)
                    if fixed is not None:
                        self._ns = {}
                        compile_err = self._compile_code(fixed)
                        if compile_err is None:
                            self._code = fixed
                            self._consecutive_errors = 0   # give the fixed code a clean slate
                            # ── Write fixed code back to spawn registry so restart uses it ──
                            self._persist_fixed_code(fixed)
                            logger.info(
                                f"[{self.name}] LLM fixed process() code — "
                                f"resuming with patched version."
                            )
                            await self._mqtt_publish(
                                f"agents/{self.actor_id}/logs",
                                {"type": "log",
                                 "message": "process() runtime error fixed by LLM in-place.",
                                 "timestamp": time.time()},
                            )
                            await asyncio.sleep(self.poll_interval)
                            continue
                        else:
                            logger.warning(
                                f"[{self.name}] LLM fix introduced compile error: {compile_err}"
                            )

                # ── Erlang: too many errors → FAILED → Supervisor restarts us ──
                if self._consecutive_errors >= self._PROCESS_FAIL_THRESHOLD:
                    logger.critical(
                        f"[{self.name}] {self._consecutive_errors} consecutive process() "
                        f"errors — setting FAILED so Supervisor can restart cleanly."
                    )
                    self.state = ActorState.FAILED
                    await self._publish_error(
                        phase="process", error=e, traceback_str=tb, fatal=True
                    )
                    return

                backoff = min(2 ** self._consecutive_errors, 30)
                await asyncio.sleep(backoff)
            await asyncio.sleep(self.poll_interval)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            self.metrics.messages_processed += 1
            if self._fn_handle_task:
                try:
                    result = await asyncio.wait_for(
                        self._fn_handle_task(self._api, msg.payload or {}),
                        timeout=self._HANDLE_TASK_TIMEOUT,
                    )
                    if msg.sender_id and result is not None:
                        await self.send(msg.sender_id, MessageType.RESULT, result)
                except asyncio.TimeoutError:
                    logger.error(
                        f"[{self.name}] handle_task() timed out after "
                        f"{self._HANDLE_TASK_TIMEOUT}s"
                    )
                    await self._publish_error(
                        phase="handle_task",
                        error=TimeoutError(f"handle_task() exceeded {self._HANDLE_TASK_TIMEOUT}s"),
                        traceback_str="",
                    )
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, {
                            "error": f"handle_task() timed out after {self._HANDLE_TASK_TIMEOUT}s",
                            "error_phase": "handle_task",
                            "agent": self.name,
                        })
                except Exception as e:
                    tb = traceback.format_exc()
                    logger.error(f"[{self.name}] handle_task() error: {e}\n{tb}")
                    await self._publish_error(phase="handle_task", error=e, traceback_str=tb)
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, {
                            "error":       str(e),
                            "error_phase": "handle_task",
                            "agent":       self.name,
                        })
            else:
                if msg.sender_id:
                    await self.send(msg.sender_id, MessageType.RESULT,
                                    {"info": f"{self.name} has no handle_task defined"})

    async def _publish_error(
        self,
        phase: str,
        error: Exception,
        traceback_str: str = "",
        fatal: bool = False,
    ):
        """
        Publish a structured error event to agents/{id}/errors AND send
        a direct actor message to MonitorAgent so it works without MQTT.
        """
        self._consecutive_errors += 1
        self._last_error_time     = time.time()
        self._error_phase         = phase
        severity = (
            "critical"
            if fatal or self._consecutive_errors >= self._error_threshold
            else "warning"
        )
        event = {
            "actor_id":    self.actor_id,
            "name":        self.name,
            "phase":       phase,
            "error":       str(error),
            "traceback":   traceback_str[-1200:] if traceback_str else "",
            "consecutive": self._consecutive_errors,
            "fatal":       fatal,
            "severity":    severity,
            "degraded":    self._consecutive_errors >= self._error_threshold,
            "timestamp":   time.time(),
        }
        await self._mqtt_publish(f"agents/{self.actor_id}/errors", event)
        # Direct actor message to monitor (works without MQTT broker)
        if self._registry:
            monitor = self._registry.find_by_name("monitor")
            if monitor and monitor.actor_id != self.actor_id:
                try:
                    await self.send(monitor.actor_id, MessageType.TASK, {
                        **event,
                        "_monitor_error_event": True,
                    })
                except Exception:
                    pass
        # Mirror to /alert so the dashboard picks it up immediately
        await self._mqtt_publish(f"agents/{self.actor_id}/alert", {
            "actor_id":  self.actor_id,
            "name":      self.name,
            "message":   f"[{phase}] {error}",
            "severity":  severity,
            "timestamp": time.time(),
        })

    def _reset_error_count(self):
        """
        Reset the process()/setup() error counter after a clean run.

        Deliberately does NOT touch _cb_error_count / _cb_error_last — those
        track subscribe callback errors which are independent of process().
        A successful process() call doesn't mean the callback is fixed.
        """
        if self._consecutive_errors > 0:
            logger.info(f"[{self.name}] Recovered — resetting error counter.")
            self._consecutive_errors = 0
            self._error_phase        = ""

    def _persist_fixed_code(self, fixed_code: str):
        """
        Write the LLM-fixed code back to:
          1. main's spawn registry  — so system restarts use the fixed code
          2. Supervisor's factory   — so Supervisor-driven restarts use the fixed code

        When the LLM fixes a runtime/syntax error it updates self._code in memory,
        but both the spawn registry (on disk) and the Supervisor factory closure
        (in memory) still reference the original broken code.  This method patches
        both atomically the moment a fix is confirmed working.
        """
        try:
            # ── 1. Persist to spawn registry (survives system restart) ─────
            if self._registry:
                main = self._registry.find_by_name("main")
                if main is not None and hasattr(main, "_get_spawn_registry"):
                    reg = main._get_spawn_registry()
                    if self.name in reg:
                        entry = dict(reg[self.name])
                        if entry.get("code") != fixed_code:
                            entry["code"] = fixed_code
                            entry["_code_fixed_at"] = time.time()
                            main._save_to_spawn_registry(entry)
                            logger.info(
                                f"[{self.name}] Fixed code written to spawn registry "
                                f"({len(fixed_code)} chars)."
                            )

            # ── 2. Update Supervisor factory (survives Supervisor-driven restart) ─
            # The factory closure captures the original kwargs including the old code.
            # Replace it with a new closure that uses fixed_code so the next
            # Supervisor restart spawns a working agent.
            if self._registry and hasattr(self._registry, "_supervisor_ref"):
                supervisor = self._registry._supervisor_ref
                if supervisor is not None and self.name in supervisor._specs:
                    spec = supervisor._specs[self.name]
                    # Build a new factory that injects the fixed code
                    _fixed = fixed_code
                    _old_factory = spec.factory
                    _name = self.name
                    _mqtt_client = self._mqtt_client
                    _mqtt_broker = self._mqtt_broker
                    _mqtt_port   = self._mqtt_port
                    _registry    = self._registry

                    async def _fixed_factory(
                        old_f=_old_factory, code=_fixed,
                        mc=_mqtt_client, mb=_mqtt_broker, mp=_mqtt_port,
                    ):
                        # Call the original factory to get a correctly configured instance
                        actor = await old_f() if asyncio.iscoroutinefunction(old_f) else old_f()
                        # Patch in the fixed code before the actor starts
                        actor._code = code
                        return actor

                    spec.factory = _fixed_factory
                    logger.info(
                        f"[{self.name}] Supervisor factory updated with fixed code."
                    )

        except Exception as exc:
            logger.warning(f"[{self.name}] Could not persist fixed code: {exc}")


    def get_status(self) -> dict:
        s = super().get_status()
        s["description"] = self.description
        s["code"]        = self._code
        s["agent_type"]  = "dynamic"
        return s

    def _build_heartbeat(self) -> dict:
        hb = super()._build_heartbeat()
        hb["code"]        = self._code      # include code in every heartbeat
        hb["description"] = self.description
        hb["agent_type"]  = "dynamic"
        return hb

    def _current_task_description(self) -> str:
        return self.description or "running dynamic code"


class _LLMInterface:
    """
    Thin LLM wrapper exposed to generated code via agent.llm
    Tracks token usage and cost just like LLMAgent does.
    """
    def __init__(self, actor: "DynamicAgent", agent_state: dict):
        self._actor = actor
        self._agent_state = agent_state  # reference to _AgentAPI.state

    async def chat(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the LLM and return the response text."""
        provider = self._actor._llm_provider
        if provider is None:
            return "[No LLM configured for this agent]"
        try:
            from .llm_agent import LLMAgent
            # Build a minimal single-turn message
            messages = [{"role": "user", "content": prompt}]
            response, usage = await provider.complete(messages=messages, system=system)
            # Track cost on the actor metrics if it has those fields
            if hasattr(self._actor, "total_input_tokens"):
                self._actor.total_input_tokens  += usage.get("input_tokens", 0)
                self._actor.total_output_tokens += usage.get("output_tokens", 0)
                self._actor.total_cost_usd      += usage.get("cost_usd", 0.0)
                await self._actor._mqtt_publish(
                    f"agents/{self._actor.actor_id}/metrics",
                    self._actor._build_metrics(),
                )
            return response
        except Exception as e:
            logger.error(f"[{self._actor.name}] agent.llm.chat() failed: {e}")
            return f"[LLM error: {e}]"

    async def complete(self, messages: list, system: str = "") -> str:
        """Multi-turn version — pass a full messages list."""
        provider = self._actor._llm_provider
        if provider is None:
            return "[No LLM configured]"
        response, usage = await provider.complete(messages=messages, system=system)
        if hasattr(self._actor, "total_input_tokens"):
            self._actor.total_input_tokens  += usage.get("input_tokens", 0)
            self._actor.total_output_tokens += usage.get("output_tokens", 0)
            self._actor.total_cost_usd      += usage.get("cost_usd", 0.0)
            await self._actor._mqtt_publish(
                f"agents/{self._actor.actor_id}/metrics",
                self._actor._build_metrics(),
            )
        return response

    async def converse(self, user_message: str, system: str = "") -> str:
        """
        Stateful multi-turn chat — automatically maintains conversation history
        in agent.state['_chat_history']. Simplest way to build a chat agent.

        async def handle_task(agent, payload):
            reply = await agent.llm.converse(payload['text'], system="You are helpful.")
            return {"reply": reply}
        """
        history = self._agent_state.setdefault("_chat_history", [])
        history.append({"role": "user", "content": user_message})
        reply = await self.complete(messages=history, system=system)
        history.append({"role": "assistant", "content": reply})
        return reply


def _ensure_result_handler(actor):
    """
    Patch handle_message once so that RESULT messages carrying _task_id
    resolve the corresponding future. Safe to call multiple times.
    """
    if getattr(actor, "_result_handler_patched", False):
        return
    actor._result_handler_patched = True
    if not hasattr(actor, "_result_futures"):
        actor._result_futures = {}
    original = actor.handle_message.__func__ if hasattr(actor.handle_message, "__func__") else None

    import types
    async def _patched_handle_message(self, msg: Message):
        if msg.type == MessageType.RESULT:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            task_id = payload.get("_task_id")
            if task_id and task_id in self._result_futures:
                if not self._result_futures[task_id].done():
                    self._result_futures[task_id].set_result(payload)
                return
        # Fall through to original handle_message
        if original:
            await original(self, msg)
        else:
            pass  # base class has no-op handle_message

    actor.handle_message = types.MethodType(_patched_handle_message, actor)



class _AgentAPI:
    """
    Clean API surface exposed to LLM-generated code via the `agent` parameter.
    Wraps the actual Actor internals so generated code can't break the framework.
    """

    def __init__(self, actor: DynamicAgent):
        self._actor = actor
        self.name     = actor.name
        self.actor_id = actor.actor_id
        # Shared mutable namespace — generated code can store anything here
        self.state: dict = {}
        # LLM interface — available if llm_provider was passed at spawn time
        self.llm = _LLMInterface(actor, self.state) if actor._llm_provider else None
        # Auto-discovered topics this agent publishes to
        self._published_topics: set = set()
        # MQTT broker info — exposed so generated code can create aiomqtt clients
        self._mqtt_broker = actor._mqtt_broker
        self._mqtt_port   = actor._mqtt_port

    # ── Identity properties (parity with _RemoteAgentAPI) ──────────────────
    # The remote API exposes `agent.node` as the node_name of the runner the
    # agent is running on. Generated code uses this for topic prefixing
    # patterns like f"{agent.node}/{agent.name}/detections" — common enough
    # that the LLM emits it routinely. Without the same property on local
    # _AgentAPI, agents that migrate from a remote node back to main crash
    # immediately with "'_AgentAPI' object has no attribute 'node'".
    #
    # The canonical "this agent is local" value across the rest of the
    # framework (spawn registry, desired_state, list_nodes filters) is the
    # empty string "" — see main_actor's `is_target_local` check which
    # treats ("", "local", "main") as equivalent. For *display* though, an
    # empty string concatenated into a topic produces a malformed leading
    # slash. We compromise by returning "local" so f-strings stay readable
    # and topics stay valid; user code that compares against "" should be
    # updated to also accept "local".
    @property
    def node(self) -> str:
        node = getattr(self._actor, "_node", None)
        if node:
            return str(node)
        return "local"

    # ── LLM convenience shims (parity with remote _RemoteAgentAPI) ─────────
    # The remote runner exposes agent.chat(messages, ...) directly on the
    # API object — generated code written on a remote node will use that
    # form. Without the same surface here, migrating an agent local→remote
    # and back (or copy-pasting code originally written for a remote node)
    # crashes with "'_AgentAPI' object has no attribute 'chat'".
    #
    # These delegate to agent.llm so generated code keeps working in both
    # environments. Both forms — agent.chat(...) and agent.llm.chat(...) —
    # are valid; pick whichever feels cleaner in your code.

    async def chat(self, messages, system: str = "", timeout: float = 60.0) -> str:
        """
        Multi-turn LLM call — mirrors _RemoteAgentAPI.chat() so the same
        generated code runs locally and remotely.

        ``messages`` is a list of {"role": "user"/"assistant", "content": "..."}.
        For a single-turn prompt, prefer ``agent.llm.chat("prompt")`` instead.
        """
        if self.llm is None:
            return "[No LLM configured for this agent]"
        # Allow callers passing a bare string by promoting it to a single
        # user-turn list — same forgiveness the remote side offers in practice.
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        return await self.llm.complete(messages, system=system)

    async def complete(self, messages, system: str = "", timeout: float = 60.0) -> str:
        """Alias for chat() — matches _LLMInterface.complete() naming."""
        return await self.chat(messages, system=system, timeout=timeout)

    # ── MQTT ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, data: Any):
        """Publish data to an MQTT topic. Auto-registers topic in capability manifest
        and TopicBus contract so the agent is discoverable without explicit declare_contract().
        On every publish, captures the actual payload schema (field names + types)
        so the planner and other agents know the real field names — not guesses."""
        await self._actor._mqtt_publish(topic, data)

        is_new_topic = topic not in self._published_topics

        # ── Auto-capture observed schema from real payloads ────────────────
        # This solves the "temp" vs "temperature" vocabulary mismatch:
        # the schema reflects what the code ACTUALLY publishes.
        # Uses TopicContract.update_observed() — a proper dataclass field,
        # not monkey-patched attributes.
        try:
            from ..core.topic_bus import TopicContract, get_topic_bus
            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(self.name)
                if existing:
                    if is_new_topic and topic not in existing.publishes:
                        existing.publishes.append(topic)
                    # Record actual field names on every publish (first call
                    # per topic populates; subsequent calls are no-ops if
                    # fields haven't changed, but cheap either way)
                    if isinstance(data, dict):
                        existing.update_observed(topic, data)
                        # Also keep produces_schema in sync
                        for k, v in existing.observed_samples.get(topic, {}).get("fields", {}).items():
                            existing.produces_schema[k] = v
                    bus.registry.register(existing)
                elif is_new_topic:
                    # Create minimal contract from published topics
                    contract = TopicContract(
                        name            = self.name,
                        publishes       = list(self._published_topics | {topic}),
                        actor_id        = self.actor_id,
                        node            = getattr(self._actor, "_node", None),
                    )
                    if isinstance(data, dict):
                        contract.update_observed(topic, data)
                        # Bootstrap produces_schema from observed
                        contract.produces_schema = dict(
                            contract.observed_samples.get(topic, {}).get("fields", {})
                        )
                    bus.register_contract(contract)
        except Exception:
            pass  # TopicBus unavailable — not fatal

        if is_new_topic:
            self._published_topics.add(topic)
            await self._publish_manifest()

    def subscribe(self, topic: str, callback):
        """
        Subscribe to an MQTT topic and call callback(payload_dict) for each message.
        Runs as a background task — setup() returns immediately.

        IMPORTANT: callback is REQUIRED and must be an async function.
        subscribe() is NOT awaitable and does NOT return data.
        For a one-shot read use: data = await agent.mqtt_get(topic)

        Correct usage in setup(agent):
            async def on_message(payload):
                agent.state['latest'] = payload.get('value')
            agent.subscribe('sensors/temperature', on_message)
        """
        if callback is None or not callable(callback):
            raise TypeError(
                f"agent.subscribe('{topic}', callback) requires a callable callback. "
                f"Got: {type(callback).__name__}. "
                f"Define: async def on_msg(payload): ... then call agent.subscribe('{topic}', on_msg). "
                f"For a one-shot read use: data = await agent.mqtt_get('{topic}')"
            )

        # Validate callback accepts exactly one argument (the payload)
        import inspect
        try:
            sig = inspect.signature(callback)
            params = [p for p in sig.parameters.values()
                      if p.default is inspect.Parameter.empty]
            if len(params) == 0:
                raise TypeError(
                    f"Subscribe callback must accept one argument (the payload dict). "
                    f"Got a function with no required parameters. "
                    f"Fix: async def {callback.__name__}(payload): ..."
                )
        except (TypeError, ValueError):
            pass  # Can't inspect — proceed and let runtime catch it
        import asyncio, json
        actor = self._actor

        # Wrap the callback so `await None` errors from LLM-generated code
        # (e.g. `await agent.persist(...)`) don't crash the listener.
        # We log the first occurrence, then silently suppress subsequent ones.
        _await_warned = False

        async def _safe_invoke(cb, payload):
            nonlocal _await_warned
            try:
                await cb(payload)
            except TypeError as e:
                if "NoneType" in str(e) and "await" in str(e):
                    if not _await_warned:
                        logger.warning(
                            f"[{actor.name}] subscribe callback has "
                            f"'await None' error (suppressed): {e}"
                        )
                        _await_warned = True
                    # Swallow: a sync API method was awaited, harmless
                else:
                    raise

        # ── Callback error tracking (actor-level, survives reconnects) ──────
        # Stored on the actor so:
        #   1. MQTT reconnects don't reset counts (closure vars would reset)
        #   2. process() success doesn't clear subscribe errors (_consecutive_errors
        #      is shared — a clean process() run was resetting callback error counts)
        #   3. Multiple subscriptions on the same actor share one error budget
        _cb_attr = f"_cb_err_{topic.replace('/','_').replace('#','x').replace('+','y')}"
        if not hasattr(actor, "_cb_error_last"):
            actor._cb_error_last:  dict[str, float] = {}
        if not hasattr(actor, "_cb_error_count"):
            actor._cb_error_count: dict[str, int]   = {}
        # After this many escalations without recovery, stop the listener entirely
        # and mark the actor FAILED so the Supervisor can restart with fresh code.
        _CB_MAX_ESCALATIONS      = 5
        _CB_ERROR_REPORT_INTERVAL = 30.0   # seconds between escalations per error key

        async def _listener():
            try:
                import aiomqtt
            except ImportError:
                logger.error(f"[{actor.name}] aiomqtt not installed")
                return
            while True:
                try:
                    async with aiomqtt.Client(actor._mqtt_broker, actor._mqtt_port) as client:
                        await client.subscribe(topic)
                        logger.info(f"[{actor.name}] Subscribed to {topic}")
                        async for msg in client.messages:
                            try:
                                payload = json.loads(msg.payload.decode())
                            except Exception:
                                payload = {"raw": msg.payload.decode()}
                            try:
                                await _safe_invoke(callback, payload)
                                # Successful invocation — reset this topic's error budget
                                actor._cb_error_count.pop(topic, None)
                                actor._cb_error_last.pop(topic, None)
                            except Exception as e:
                                import time as _t, traceback as _tb
                                now        = _t.time()
                                last       = actor._cb_error_last.get(topic, 0)
                                escalations = actor._cb_error_count.get(topic, 0)

                                logger.error(
                                    f"[{actor.name}] subscribe callback error "
                                    f"(escalation #{escalations + 1}/{_CB_MAX_ESCALATIONS},"
                                    f" topic={topic}): {e}"
                                )

                                # Rate-limit escalation to supervision
                                if (now - last) >= _CB_ERROR_REPORT_INTERVAL:
                                    escalations += 1
                                    actor._cb_error_count[topic]  = escalations
                                    actor._cb_error_last[topic]   = now

                                    fatal = escalations >= _CB_MAX_ESCALATIONS
                                    await actor._publish_error(
                                        phase="subscribe_callback",
                                        error=e,
                                        traceback_str=_tb.format_exc(),
                                        fatal=fatal,
                                    )

                                    if fatal:
                                        # Budget exhausted — stop looping, let Supervisor restart
                                        logger.critical(
                                            f"[{actor.name}] subscribe callback on '{topic}' "
                                            f"failed {escalations}x — marking FAILED for Supervisor."
                                        )
                                        from ..core.actor import ActorState
                                        actor.state = ActorState.FAILED
                                        return   # exits _listener task
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[{actor.name}] MQTT subscribe error: {e} — retrying in 5s")
                    await asyncio.sleep(5)

        # Deduplication guard — prevent double-subscription if setup() is called
        # more than once (e.g. on reconnect). Same topic+callback combo gets one listener.
        if not hasattr(actor, '_subscribed_topics'):
            actor._subscribed_topics: set = set()
        sub_key = (topic, id(callback))
        if sub_key in actor._subscribed_topics:
            logger.debug(f"[{actor.name}] Already subscribed to {topic} — skipping duplicate")
            return _AWAITABLE_NONE
        actor._subscribed_topics.add(sub_key)

        task = asyncio.create_task(_listener())
        actor._tasks.append(task)

        # Auto-register subscription in TopicBus
        try:
            from ..core.topic_bus import TopicContract, get_topic_bus
            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(self.name)
                if existing:
                    if topic not in existing.subscribes:
                        existing.subscribes.append(topic)
                        bus.registry.register(existing)
                else:
                    contract = TopicContract(
                        name       = self.name,
                        subscribes = [topic],
                        actor_id   = self.actor_id,
                        node       = getattr(actor, "_node", None),
                    )
                    bus.register_contract(contract)
        except Exception:
            pass  # TopicBus unavailable — not fatal

        # Return an awaitable no-op so `await agent.subscribe(...)` doesn't crash.
        # LLMs frequently add `await` because setup() is async — this makes it safe.
        return _AWAITABLE_NONE

    async def publish_detection(self, data: Any):
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any):
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

    async def _publish_manifest(self):
        """
        Publish retained capability manifest so main/planner can discover this agent.
        Now includes full TopicContract (publishes, subscribes, triggers_when, schemas)
        so the planner can wire agents by data compatibility, not just by name.
        """
        import time as _t
        actor = self._actor
        # Include TopicContract fields if declared
        contract = getattr(actor, "_topic_contract", None)
        manifest = {
            "name":            self.name,
            "actor_id":        self.actor_id,
            "node":            getattr(actor, "_node", None),
            "description":     getattr(actor, "description", ""),
            "capabilities":    [],
            "input_schema":    getattr(actor, "input_schema",  {}),
            "output_schema":   getattr(actor, "output_schema", {}),
            "publishes":       sorted(self._published_topics),
            # TopicContract fields — populated via declare_contract()
            "subscribes":      contract.subscribes      if contract else [],
            "triggers_when":   contract.triggers_when   if contract else {},
            "produces_schema": contract.produces_schema if contract else {},
            "consumes_schema": contract.consumes_schema if contract else {},
            # Observed payload schemas — auto-captured from real publishes
            "observed_samples": contract.observed_samples if contract else {},
            "timestamp":       _t.time(),
        }
        await actor._mqtt_publish(
            f"agents/{self.actor_id}/manifest", manifest, retain=True
        )

    # ── Logging / alerting ─────────────────────────────────────────────────

    async def log(self, message: str, level: str = "info"):
        """Add a message to the event log visible in the dashboard."""
        # Encode safely for Windows terminals that can't handle all unicode
        safe_msg = message.encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()}
        )

    @property
    def logger(self):
        """Compatibility shim — allows agent.logger.info/warning/error in generated code."""
        api = self
        class _LoggerShim:
            def info(self, msg):    asyncio.ensure_future(api.log(msg, "info"))
            def warning(self, msg): asyncio.ensure_future(api.log(msg, "warning"))
            def error(self, msg):   asyncio.ensure_future(api.log(msg, "error"))
            def debug(self, msg):   asyncio.ensure_future(api.log(msg, "debug"))
        return _LoggerShim()

    async def alert(self, message: str, severity: str = "warning"):
        """Trigger an alert visible in the dashboard."""
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/alert",
            {
                "actor_id":  self._actor.actor_id,
                "name":      self.name,
                "message":   message,
                "severity":  severity,
                "timestamp": time.time(),
            }
        )

    # ── Persistence ────────────────────────────────────────────────────────

    def persist(self, key: str, value: Any):
        self._actor.persist(key, value)
        return _AWAITABLE_NONE           # safe to await

    def recall(self, key: str, default: Any = None) -> Any:
        """
        Load a persisted value. Returns `default` (None by default) if the
        key doesn't exist — same shape as dict.get(), and identical to the
        remote runner's _RemoteAgentAPI.recall() so the same agent code
        works on local and remote without modification.

        Note: recall() is synchronous — do NOT use await.
        The sanitizer strips `await agent.recall(...)` at compile time.
        If an accidental `await` slips through, the _safe_invoke callback
        wrapper (layer 4) will catch the TypeError.

        The return value is always the real persisted value (or the default).
        We do NOT substitute _AWAITABLE_NONE here because that would break
        the `if agent.recall('key') is None:` idiom that existing agent
        code relies on.
        """
        value = self._actor.recall(key)
        return value if value is not None else default

    # ── Inter-agent messaging ──────────────────────────────────────────────

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Optional[Any]:
        """Send a TASK to another agent by name and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox
          2. Remote node via MQTT — agents/by-name/{name}/task with reply topic
          3. Returns error dict if the agent is unknown in both

        Works with local DynamicAgent/LLMAgent AND remote _RemoteAgent on any node.
        """
        registry = self._actor._registry
        if not registry:
            logger.warning(f"[{self.name}] send_to: no registry")
            return None

        target = registry.find_by_name(agent_name)

        if target:
            # ── Local path ────────────────────────────────────────────────────
            import uuid as _uuid
            task_id = str(_uuid.uuid4())[:8]
            if not hasattr(self._actor, "_result_futures"):
                self._actor._result_futures = {}
            future = asyncio.get_event_loop().create_future()
            self._actor._result_futures[task_id] = future
            _ensure_result_handler(self._actor)
            if not isinstance(payload, dict):
                payload = {"message": payload, "text": str(payload)}
            payload = dict(payload)
            payload["_task_id"]  = task_id
            payload["_reply_to"] = self._actor.actor_id
            await self._actor.send(target.actor_id, MessageType.TASK, payload)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.name}] send_to '{agent_name}' timed out after {timeout}s")
                return {"error": f"Timeout waiting for '{agent_name}'"}
            finally:
                self._actor._result_futures.pop(task_id, None)

        # ── Remote path: find agent on a known node ───────────────────────────
        remote_node = None
        main = registry.find_by_name("main") if registry else None
        if main and hasattr(main, "_known_nodes"):
            for node_name, nd in main._known_nodes.items():
                if agent_name in nd.get("agents", []):
                    remote_node = node_name
                    break

        if not remote_node:
            logger.warning(f"[{self.name}] send_to: agent '{agent_name}' not found locally or remotely")
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid as _uuid
        reply_topic = f"agents/by-name/{self.name}/reply/{_uuid.uuid4().hex[:8]}"

        if not isinstance(payload, dict):
            payload = {"message": payload, "text": str(payload)}
        payload = dict(payload)
        payload["_reply_topic"] = reply_topic
        payload["_remote_task"] = True

        future = asyncio.get_event_loop().create_future()
        if not hasattr(self._actor, "_result_futures"):
            self._actor._result_futures = {}
        self._actor._result_futures[reply_topic] = future

        await self._actor._mqtt_publish(f"agents/by-name/{agent_name}/task", payload)

        async def _wait_reply():
            try:
                import aiomqtt
                broker = getattr(self._actor, "_mqtt_broker", "localhost")
                port   = getattr(self._actor, "_mqtt_port", 1883)
                async with aiomqtt.Client(broker, port) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        try:
                            import json as _json
                            data = _json.loads(msg.payload.decode())
                            if not future.done():
                                future.set_result(data)
                        except Exception:
                            pass
                        return
            except Exception as e:
                if not future.done():
                    future.set_exception(e)

        reply_task = asyncio.create_task(_wait_reply())
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] send_to '{agent_name}' on '{remote_node}' timed out after {timeout}s")
            return {"error": f"Timeout waiting for remote '{agent_name}'"}
        finally:
            reply_task.cancel()
            self._actor._result_futures.pop(reply_topic, None)

    async def send_to_many(self, tasks: list[tuple[str, Any]], timeout: float = 60.0) -> list:
        """Send tasks to multiple agents IN PARALLEL and collect all results.

        tasks: list of (agent_name, payload) tuples
        Returns list of results in the same order.

        Example:
            results = await agent.send_to_many([
                ("weather-agent", {"city": "Athens"}),
                ("news-agent",    {"topic": "AI"}),
            ])
            weather, news = results[0], results[1]
        """
        coros = [self.send_to(name, payload, timeout) for name, payload in tasks]
        return list(await asyncio.gather(*coros, return_exceptions=True))

    def agents(self) -> list[dict]:
        """
        Return all running agents — both local and remote.

        Local agents come from the registry. Remote agents are sourced from
        main._known_nodes (populated by node heartbeats). Each entry includes
        a 'remote' flag and 'node' field so callers can route correctly.

        Example:
            available = agent.agents()
            remote_workers = [a for a in available if a.get("remote")]
        """
        registry = self._actor._registry
        result   = []
        seen     = set()

        # ── Local agents from registry ────────────────────────────────────────
        if registry:
            for actor in registry.all_actors():
                seen.add(actor.name)
                result.append({
                    "name":        actor.name,
                    "type":        type(actor).__name__,
                    "description": (
                        getattr(actor, "description", "")
                        or getattr(actor, "system_prompt", "")[:100]
                        or ""
                    ),
                    "state":  actor.state.name if hasattr(actor.state, "name") else str(actor.state),
                    "remote": False,
                    "node":   None,
                })

        # ── Remote agents from live node heartbeats ───────────────────────────
        main = registry.find_by_name("main") if registry else None
        if main and hasattr(main, "_known_nodes"):
            import time as _t
            for node_name, nd in main._known_nodes.items():
                if _t.time() - nd.get("last_seen", 0) > 30:
                    continue   # node is offline — skip
                for aname in nd.get("agents", []):
                    if aname in seen:
                        continue   # already in local registry (shouldn't happen but guard it)
                    seen.add(aname)
                    # Try to get description from _agent_manifests
                    desc = ""
                    if hasattr(main, "_agent_manifests"):
                        m    = main._agent_manifests.get(aname, {})
                        desc = m.get("description", "")
                    result.append({
                        "name":        aname,
                        "type":        "RemoteAgent",
                        "description": desc,
                        "state":       "running",
                        "remote":      True,
                        "node":        node_name,
                    })

        return result

    def nodes(self) -> list[dict]:
        """
        Return all known remote nodes with online status and running agents.
        Only available when the agent is running under a MainActor system.

        Example:
            for nd in agent.nodes():
                status = 'online' if nd['online'] else 'offline'
                await agent.log(f"{nd['node']}: {status}, agents: {nd['agents']}")
        """
        main = self._actor._registry.find_by_name("main") if self._actor._registry else None
        if main and hasattr(main, "list_nodes"):
            return main.list_nodes()
        return []

    def topics(self, keyword: str = "") -> list[dict]:
        """
        Return all known MQTT topics published by agents, optionally filtered by keyword.
        Each entry: {"topic": str, "agents": [{"name", "node", "description"}, ...]}

        Example:
            temp_topics = agent.topics("temp")   # find all temperature-related topics
            all_topics  = agent.topics()         # everything
            for t in temp_topics:
                data = await agent.mqtt_get(t["topic"])
        """
        main = self._actor._registry.find_by_name("main") if self._actor._registry else None
        if main and hasattr(main, "list_topics"):
            return main.list_topics(keyword)
        return []

    def capabilities(self, keyword: str = "") -> list[dict]:
        """
        Return all known agents with their full capability profile.
        Each entry: {"name", "description", "capabilities", "input_schema", "output_schema"}

        Example:
            weather_agents = agent.capabilities("weather")
            for a in weather_agents:
                print(a["input_schema"])   # know exactly what to send
                print(a["output_schema"])  # know exactly what to expect back
        """
        main = self._actor._registry.find_by_name("main") if self._actor._registry else None
        if main and hasattr(main, "list_capabilities"):
            return main.list_capabilities(keyword)
        return []

    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Optional[Any]:
        """Alias for send_to() — cleaner name for planner/coordinator agents."""
        return await self.send_to(agent_name, payload, timeout=timeout)

    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Optional[Any]:
        """
        Wait for one MQTT message on topic and return its parsed payload.
        Useful for reading live data published by remote agents.

        Example:
            stats = await agent.mqtt_get('rpi-room/cpu')
            cpu = stats.get('cpu_percent') if stats else None
        """
        import asyncio, json
        try:
            import aiomqtt
        except ImportError:
            return None
        actor = self._actor
        result = []
        async def _fetch():
            try:
                async with aiomqtt.Client(actor._mqtt_broker, actor._mqtt_port) as client:
                    await client.subscribe(topic)
                    async for msg in client.messages:
                        try:
                            result.append(json.loads(msg.payload.decode()))
                        except Exception:
                            result.append(msg.payload.decode())
                        return
            except Exception:
                pass
        try:
            await asyncio.wait_for(_fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return result[0] if result else None

    # ── Topic Bus API ───────────────────────────────────────────────────────

    def window(self, topic: str, seconds: float = 300,
               max_size: int = 1000):
        """
        Create a sliding time window over an MQTT topic stream.

        IMPORTANT: window() is synchronous — do NOT use await.
        CORRECT:  agent.state['w'] = agent.window('sensors/temp', seconds=60)
        WRONG:    agent.state['w'] = await agent.window(...)  # TypeError!

        Returns a StreamWindow with methods: mean, min, max, rising, falling,
        stable, absent_for, event_count, latest, count, values.

        Usage:
            async def setup(agent):
                agent.state['w'] = agent.window('sensors/temp', seconds=60)  # NO await

            async def process(agent):
                w = agent.state['w']
                avg = w.mean('value')
                mn  = w.min('value')
                mx  = w.max('value')
                if w.rising(threshold=3.0):
                    await agent.alert('Temperature rising fast!')
                if w.absent_for(60):
                    await agent.alert('Sensor stopped publishing!')
        """
        from ..core.topic_bus import get_topic_bus, StreamWindow

        class _UnawaatableWindow:
            """
            Wraps StreamWindow and raises a clear TypeError if accidentally awaited.

            We do NOT implement __await__ here. Yielding a StreamWindow from
            __await__ violates the awaitable protocol and causes
            `RuntimeError: Task got bad yield` in CPython's event loop.

            Instead, accidental `await agent.window(...)` is handled by:
              - Layer 2 (sanitizer): strips `await` from `agent.window()` at compile time
              - Layer 4 (_safe_invoke): catches TypeError in subscribe callbacks
            This wrapper exists solely for a clear error message if those layers miss it.
            """
            def __init__(self, inner):
                self._inner = inner
            def __getattr__(self, name):
                return getattr(self._inner, name)
            def __repr__(self):
                return f"StreamWindow(topic={getattr(self._inner, 'topic', '?')}, seconds={getattr(self._inner, 'seconds', '?')})"
            def __await__(self):
                raise TypeError(
                    "agent.window() is not a coroutine — do not use 'await'. "
                    "Correct: agent.state['w'] = agent.window('topic', seconds=60)  # no await"
                )
                # Make this a generator so __await__ is syntactically valid
                return
                yield  # pragma: no cover

        try:
            bus = get_topic_bus()
            if bus:
                w = bus.make_window(topic, seconds=seconds, max_size=max_size)
            else:
                w = StreamWindow(topic, seconds=seconds, max_size=max_size)
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
            if w is None:
                raise ValueError("StreamWindow construction returned None")
            return _UnawaatableWindow(w)
        except Exception as e:
            # Last resort fallback — return a minimal no-op window that won't crash
            logger.error(f"[{self.name}] agent.window() failed: {e} — returning fallback window")
            w = StreamWindow(topic, seconds=seconds, max_size=max_size)
            try:
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
            except Exception:
                pass
            return _UnawaatableWindow(w)

    def declare_contract(self, publishes=None, subscribes=None,
                         triggers_when: dict = None, produces_schema: dict = None,
                         consumes_schema: dict = None, **kwargs):
        """
        Declare this agent's topic contract — what it produces and consumes.

        Call from setup() to make this agent discoverable by the planner
        and other agents via topic-based auto-wiring.

        Accepts common LLM kwarg variants:
          schema → produces_schema
          output_schema → produces_schema
          input_schema → consumes_schema
          topics → publishes

        Usage:
            async def setup(agent):
                agent.declare_contract(
                    publishes    = ['rpi-kitchen/camera/detections'],
                    subscribes   = ['homeassistant/state_changes/#'],
                    triggers_when= {'person_detected': True},
                    produces_schema = {'person_detected': 'bool', 'confidence': 'float'},
                )
        """
        # ── Accept common LLM kwarg aliases ────────────────────────────────
        if produces_schema is None:
            produces_schema = (
                kwargs.get("schema")
                or kwargs.get("output_schema")
                or kwargs.get("produce_schema")
                or {}
            )
        if consumes_schema is None:
            consumes_schema = (
                kwargs.get("input_schema")
                or kwargs.get("consume_schema")
                or {}
            )
        if publishes is None:
            publishes = kwargs.get("topics") or kwargs.get("publish")
        if subscribes is None:
            subscribes = kwargs.get("subscribe")

        # ── Coerce strings to single-element lists ─────────────────────────
        # LLMs often write publishes="topic" instead of publishes=["topic"]
        if isinstance(publishes, str):
            publishes = [publishes]
        if isinstance(subscribes, str):
            subscribes = [subscribes]

        from ..core.topic_bus import TopicContract, get_topic_bus
        contract = TopicContract(
            name            = self.name,
            publishes       = publishes or list(self._published_topics),
            subscribes      = subscribes or [],
            triggers_when   = triggers_when or {},
            produces_schema = produces_schema or {},
            consumes_schema = consumes_schema or {},
            actor_id        = self.actor_id,
            node            = getattr(self._actor, "_node", None),
        )
        bus = get_topic_bus()
        if bus:
            bus.register_contract(contract)
        # Also include in manifest so remote agents and planner can see it
        self._actor._topic_contract = contract
        asyncio.ensure_future(self._publish_manifest())
        return _AWAITABLE_NONE           # safe to await

    async def publish_world_state(self, key: str, data: Any, retain: bool = True):
        """
        Publish a piece of world state to the shared retained state hub.
        Other agents can read this without making a request — it's always there.

        Topic: agents/{agent_name}/data/{key}

        Usage:
            await agent.publish_world_state('person_present', {'present': True, 'zone': 'kitchen'})
            await agent.publish_world_state('energy', {'kwh': 2.3, 'cost': 0.45})
        """
        from ..core.topic_bus import get_topic_bus
        bus = get_topic_bus()
        if bus:
            await bus.state_hub.publish_agent_data(self.name, key, data)
        else:
            topic = f"agents/{self.name}/data/{key}"
            await self.publish(topic, data)

    async def read_world_state(self, topic: str, timeout: float = 2.0) -> Optional[Any]:
        """
        Read a retained world state topic — returns immediately if cached,
        otherwise waits up to timeout seconds for the retained message.

        Usage:
            presence = await agent.read_world_state('home/presence/kitchen')
            energy   = await agent.read_world_state('home/energy/current')
            ha_state = await agent.read_world_state('home/state/light/light.living_room')
        """
        return await self.mqtt_get(topic, timeout=timeout)

    def wiring_opportunities(self) -> list[dict]:
        """
        Return a list of other agents this agent can be auto-wired to,
        based on topic contract compatibility.

        Usage:
            opps = agent.wiring_opportunities()
            for o in opps:
                print(f"Can receive data from {o['producer']} via {o['topic']}")
        """
        from ..core.topic_bus import get_topic_bus
        bus = get_topic_bus()
        if not bus:
            return []
        pairs = bus.registry.find_wiring_opportunities()
        return [
            {"producer": p.name, "consumer": c.name, "topic": t}
            for p, c, t in pairs
            if p.name == self.name or c.name == self.name
        ]

    # ── Time-series queries (for ML agents) ────────────────────────────────

    def query_ts(
        self,
        hours: float = 24,
        topic: Optional[str] = None,
        entity_id: Optional[str] = None,
        field: Optional[str] = None,
        limit: int = 100_000,
        as_dataframe: bool = False,
    ) -> Any:
        """
        Query historical sensor readings from the time-series store.

        Returns a list of dicts by default. Set as_dataframe=True to get
        a pandas DataFrame (requires pandas installed).

        SYNCHRONOUS — do NOT await.

        Usage:
            # Get last 24h of temperature data
            rows = agent.query_ts(hours=24, field='temp')

            # Get as pandas DataFrame for ML
            df = agent.query_ts(hours=168, entity_id='sensor.kitchen_temp', as_dataframe=True)

            # Train a model
            from sklearn.ensemble import IsolationForest
            model = IsolationForest().fit(df[['value']])
            agent.persist('anomaly_model', model)
        """
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            logger.warning(f"[{self.name}] query_ts: persistence not initialised")
            return [] if not as_dataframe else None

        rows = db.query_sensor(
            hours=hours, topic=topic, entity_id=entity_id,
            field=field, limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd
                return pd.DataFrame(rows)
            except ImportError:
                logger.warning(f"[{self.name}] pandas not installed — returning list of dicts")
                return rows
        return rows

    def query_detections(
        self,
        hours: float = 24,
        agent_name: Optional[str] = None,
        class_name: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """
        Query historical object detections (YOLO, camera agents).

        Usage:
            # All person detections in last 12 hours
            rows = agent.query_detections(hours=12, class_name='person')

            # As DataFrame for analysis
            df = agent.query_detections(hours=48, min_confidence=0.8, as_dataframe=True)
        """
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_detections(
            hours=hours, agent=agent_name, class_name=class_name,
            min_confidence=min_confidence, limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd
                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """
        Query historical Home Assistant state changes.

        Usage:
            # All light state changes in last week
            df = agent.query_ha_states(hours=168, domain='light', as_dataframe=True)

            # Specific entity history
            rows = agent.query_ha_states(hours=24, entity_id='sensor.kitchen_temp')
        """
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_ha_states(
            hours=hours, entity_id=entity_id, domain=domain, limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd
                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def ts_stats(self) -> dict:
        """
        Return row counts for all time-series tables.
        Useful for checking how much data is available before training.

        Usage:
            stats = agent.ts_stats()
            # {'sensor_readings': 145230, 'detections': 8920, ...}
        """
        from ..core.persistence import get_db
        db = get_db()
        if not db:
            return {}
        return db.stats()

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1
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
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._code           = code
        self.poll_interval   = poll_interval
        self.description     = description
        self.input_schema    = input_schema  or {}
        self.output_schema   = output_schema or {}
        self._llm_provider   = llm_provider

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
        # Give generated code a chance to clean up
        cleanup = self._ns.get("cleanup")
        if cleanup:
            try:
                await cleanup(self._api)
            except Exception:
                pass

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

    def _compile_code(self, code: Optional[str] = None) -> Optional[str]:
        """
        Sanitize then compile LLM-generated code into self._ns.

        Returns the error message string if compilation fails, None on success.
        Callers use the error string to ask the LLM to fix the code and retry
        (see on_start / _fix_syntax_with_llm).
        """
        source = code if code is not None else self._code
        clean  = self._sanitize_code(source)

        # Pre-inject the LLM shim so generated code can call agent.llm directly
        def _get_llm_shim(*args, **kwargs):
            return self._api.llm
        self._ns["get_llm"]    = _get_llm_shim
        self._ns["setup_llm"]  = _get_llm_shim
        self._ns["create_llm"] = _get_llm_shim

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

    async def _process_loop(self):
        """Continuously call the generated process() function."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            if self.state == ActorState.PAUSED:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self._fn_process(self._api)
                self._reset_error_count()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.errors += 1
                tb = traceback.format_exc()
                logger.error(f"[{self.name}] process() error: {e}\n{tb}")
                await self._publish_error(phase="process", error=e, traceback_str=tb)
                backoff = min(2 ** self._consecutive_errors, 30)
                await asyncio.sleep(backoff)
            await asyncio.sleep(self.poll_interval)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            self.metrics.messages_processed += 1
            if self._fn_handle_task:
                try:
                    result = await self._fn_handle_task(self._api, msg.payload or {})
                    if msg.sender_id and result is not None:
                        await self.send(msg.sender_id, MessageType.RESULT, result)
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
        if self._consecutive_errors > 0:
            logger.info(f"[{self.name}] Recovered — resetting error counter.")
            self._consecutive_errors = 0
            self._error_phase        = ""

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
                            except Exception as e:
                                logger.error(f"[{actor.name}] subscribe callback error: {e}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[{actor.name}] MQTT subscribe error: {e} — retrying in 5s")
                    await asyncio.sleep(5)

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

    def recall(self, key: str) -> Any:
        val = self._actor.recall(key)
        return val if val is not None else _AWAITABLE_NONE  # safe to await

    # ── Inter-agent messaging ──────────────────────────────────────────────

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Optional[Any]:
        """Send a TASK to another agent by name and wait for its result.

        Works with DynamicAgent, LLMAgent, ManualAgent — any Actor subclass.
        Uses a dedicated future keyed by a unique task_id so concurrent calls
        don't interfere with each other.
        """
        registry = self._actor._registry
        if not registry:
            logger.warning(f"[{self.name}] send_to: no registry")
            return None
        target = registry.find_by_name(agent_name)
        if not target:
            logger.warning(f"[{self.name}] send_to: agent '{agent_name}' not found")
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid
        task_id = str(uuid.uuid4())[:8]

        # Register a future in the actor's result table
        if not hasattr(self._actor, "_result_futures"):
            self._actor._result_futures = {}
        future = asyncio.get_event_loop().create_future()
        self._actor._result_futures[task_id] = future

        # Ensure the actor resolves result futures in handle_message
        _ensure_result_handler(self._actor)

        # Payload: always a dict with task_id and reply_to so target can respond
        if not isinstance(payload, dict):
            payload = {"message": payload, "text": str(payload)}
        payload = dict(payload)
        payload["_task_id"]  = task_id
        payload["_reply_to"] = self._actor.actor_id

        await self._actor.send(target.actor_id, MessageType.TASK, payload)
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] send_to '{agent_name}' timed out after {timeout}s")
            return {"error": f"Timeout waiting for '{agent_name}'"}
        finally:
            self._actor._result_futures.pop(task_id, None)

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
        Return all running agents with name, type and description.
        Use this to discover what workers are available before delegating.

        Example:
            available = agent.agents()
            workers = [a for a in available if a["name"] != "main"]
        """
        registry = self._actor._registry
        if not registry:
            return []
        result = []
        for actor in registry.all_actors():
            result.append({
                "name":        actor.name,
                "type":        type(actor).__name__,
                "description": (
                    getattr(actor, "description", "")
                    or getattr(actor, "system_prompt", "")[:100]
                    or ""
                ),
                "state": actor.state.name if hasattr(actor.state, "name") else str(actor.state),
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
            """Wraps StreamWindow and raises a clear error if accidentally awaited."""
            def __init__(self, inner):
                self._inner = inner
            def __getattr__(self, name):
                return getattr(self._inner, name)
            def __repr__(self):
                return f"StreamWindow(topic={getattr(self._inner, 'topic', '?')}, seconds={getattr(self._inner, 'seconds', '?')})"
            def __await__(self):
                logger.warning(
                    f"agent.window() was awaited — this is unnecessary but not fatal. "
                    f"Correct: agent.state['w'] = agent.window('topic', seconds=60)  # no await"
                )
                yield self._inner   # return the window object itself
                return self._inner

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

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1
"""DynamicAgent - A generic actor shell whose behavior is defined by LLM-generated code.

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
import inspect
import logging
import time
import traceback
import types
from typing import TYPE_CHECKING, Any, cast

from ...core.actor import Actor, ActorState, Message, MessageType
from ..llm_agent import accumulate_global_cost
from ..lookup import find_main_actor
from .api import AgentAPI
from .cv2_shim import resilient_cv2_module
from .safety import extract_function_body, validate_code_safety
from .sanitize import sanitize_code

if TYPE_CHECKING:
    from ..llm_agent import LLMProvider

logger = logging.getLogger(__name__)


class DynamicAgent(Actor):
    """Generic actor shell. Core behavior is provided as Python source code strings.
    The LLM writes setup/process/handle_task functions; this class runs them.
    """

    def __init__(
        self,
        code: str,  # LLM-generated Python source
        poll_interval: float = 1.0,  # seconds between process() calls
        description: str = "",  # what this agent does
        input_schema: dict[str, Any] | None = None,  # expected task payload fields
        output_schema: dict[str, Any] | None = None,  # returned result fields
        llm_provider=None,  # optional LLM for agent.llm.chat()
        trusted: bool = False,  # True = catalog agent, skip safety validator
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._code = code
        self.poll_interval = poll_interval
        self.description = description
        self.input_schema = input_schema or {}
        self.output_schema = output_schema or {}
        self._llm_provider = llm_provider
        self._trusted = trusted  # catalog agents bypass safety checks

        # Compiled functions — populated in on_start
        self._fn_setup = None
        self._fn_process = None
        self._fn_handle_task = None

        # Namespace shared across all calls (agent can store state here)
        self._ns: dict = {}

        # Cost tracking (populated by LLMInterface if LLM is used)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self._last_period_cost_usd = 0.0

        # Error tracking for health classification
        self._consecutive_errors: int = 0
        self._error_threshold: int = 3  # DEGRADED after this many
        self._last_error_time: float = 0.0
        self._error_phase: str = ""  # compile|setup|process|handle_task

        # Public API exposed to generated code via `agent` parameter
        # Owned here rather than grafted on by AgentAPI at first use: a
        # collaborator creating attributes on its host hides them from every
        # reader and from the type checker.
        #: Per-topic timestamp of the last callback error, for backoff.
        self._cb_error_last: dict[str, float] = {}
        #: Per-topic count of consecutive callback errors.
        self._cb_error_count: dict[str, int] = {}
        #: (topic, callback id) pairs already subscribed, to refuse duplicates.
        self._subscribed_topics: dict[tuple[str, int], Any] = {}
        #: The last contract this agent declared, published in its manifest.
        self._topic_contract: Any = None
        self._api = AgentAPI(self)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        # ── Compile with LLM self-correction on syntax errors ─────────────
        current_code = self._code
        error_msg = self._compile_code(current_code)

        if error_msg:
            for attempt in range(1, self._MAX_COMPILE_RETRIES + 1):
                logger.warning("[%s] Compile error (attempt %s): %s", self.name, attempt, error_msg)
                fixed = await self._fix_syntax_with_llm(current_code, error_msg)
                if fixed is None:
                    # LLM unavailable — no point retrying
                    break
                self._ns = {}  # fresh namespace for retry
                new_err = self._compile_code(fixed)
                if new_err is None:
                    # Fix worked — update stored code so restarts use the good version
                    self._code = fixed
                    error_msg = None
                    logger.info("[%s] Code fixed by LLM after %s attempt(s).", self.name, attempt)
                    # ── Write fixed code back to spawn registry so restart uses it ──
                    self._persist_fixed_code(fixed)
                    await self._mqtt_publish(
                        f"agents/{self.actor_id}/logs",
                        {
                            "type": "log",
                            "message": f"Syntax error fixed by LLM after {attempt} attempt(s).",
                            "timestamp": time.time(),
                        },
                    )
                    break
                # Fix compiled but still broken — feed it back for the next attempt
                current_code = fixed
                error_msg = new_err

        if error_msg:
            # All attempts exhausted — publish fatal and stop
            err_exc = SyntaxError(error_msg)
            logger.error("[%s] Code compilation failed permanently: %s", self.name, error_msg)
            # ── Erlang/OTP: mark FAILED so Supervisor's watch_loop detects us ──
            self.state = ActorState.FAILED
            await self._publish_error(
                phase="compile", error=err_exc, traceback_str=error_msg, fatal=True
            )
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
            self.persist(
                "_final_cost",
                {
                    "input_tokens": self.total_input_tokens,
                    "output_tokens": self.total_output_tokens,
                    "cost_usd": round(self.total_cost_usd, 6),
                    "name": self.name,
                    "stopped_at": time.time(),
                },
            )

        # ── Publish final metrics before heartbeat loop is cancelled ───────
        try:
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                self._build_metrics()
                if hasattr(self, "_build_metrics")
                else {
                    "actor_id": self.actor_id,
                    "input_tokens": getattr(self, "total_input_tokens", 0),
                    "output_tokens": getattr(self, "total_output_tokens", 0),
                    "cost_usd": round(getattr(self, "total_cost_usd", 0.0), 6),
                    "messages_processed": self.metrics.messages_processed,
                    "errors": self.metrics.errors,
                    "uptime": self.metrics.uptime,
                    "final": True,  # signals UI this is the last metrics msg
                },
            )
        except Exception:
            pass

        # ── Unregister from TopicBus so stale contracts don't accumulate ───
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                bus.unregister(self.name)
                logger.debug("[%s] Unregistered from TopicBus", self.name)
        except Exception:
            pass  # TopicBus unavailable — not fatal

        # ── Give generated code a chance to clean up ───────────────────────
        cleanup = self._ns.get("cleanup")
        if cleanup:
            try:
                await asyncio.wait_for(cleanup(self._api), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[%s] cleanup() timed out after 10s", self.name)
            except Exception as e:
                logger.warning("[%s] cleanup() error: %s", self.name, e)

        # ── Force-release common resources that LLM code may have opened ───
        # Even if cleanup() didn't run or missed something, we try to release
        # known resource types stored in agent.state OR in module-level globals
        # inside the compiled namespace (_ns). LLM-generated code frequently uses
        # globals like `_cap = None` instead of agent.state, so we must check both.
        state = getattr(self._api, "state", {}) if self._api else {}

        # Skip builtins/modules/functions — only look at plain objects
        _SKIP_TYPES = (
            type(None),
            bool,
            int,
            float,
            str,
            bytes,
            type,
            types.ModuleType,
            types.FunctionType,
            types.CoroutineType,
        )

        def _release_obj(key, obj):
            """Release a single resource object, logging the result."""
            if obj is None or isinstance(obj, _SKIP_TYPES):
                return
            # cv2.VideoCapture (and anything with release/isOpened)
            if hasattr(obj, "release") and hasattr(obj, "isOpened"):
                try:
                    if obj.isOpened():
                        obj.release()
                        logger.info("[%s] Released camera handle '%s'", self.name, key)
                except Exception:
                    pass
            # Open file handles
            elif hasattr(obj, "close") and hasattr(obj, "closed"):
                try:
                    if not obj.closed:
                        obj.close()
                        logger.debug("[%s] Closed file handle '%s'", self.name, key)
                except Exception:
                    pass

        # Scan agent.state (preferred pattern)
        for key in list(state.keys()):
            _release_obj(key, state.get(key))

        # Scan module-level globals in the compiled namespace (common LLM pattern)
        # e.g. `_cap = None` / `_model = None` at module level
        for key, obj in list(self._ns.items()):
            if key.startswith("__") or key in ("setup", "process", "cleanup", "handle_task"):
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

    # Max times on_start will ask the LLM to fix a syntax error before giving up
    _MAX_COMPILE_RETRIES = 2

    # ── Pre-exec safety validator ──────────────────────────────────────────
    # Scans sanitized code for dangerous patterns BEFORE exec().
    # This is NOT a sandbox — it's a best-effort blocklist.
    # For true isolation, run DynamicAgents in a subprocess or container.

    # Patterns that are suspicious but allowed — just logged as warnings

    # Patterns checked specifically inside process() body — cause 120s timeout crashes

    _sanitize_code = staticmethod(sanitize_code)
    _extract_function_body = staticmethod(extract_function_body)

    def _validate_code_safety(self, code: str) -> str | None:
        return validate_code_safety(code, self.name)

    def _compile_code(self, code: str | None = None) -> str | None:
        """Sanitize, validate safety, then compile LLM-generated code into self._ns.

        Returns the error message string if compilation fails, None on success.
        Callers use the error string to ask the LLM to fix the code and retry
        (see on_start / _fix_syntax_with_llm).

        Trusted agents (from the catalog) skip the safety validator — their code
        is pre-built and tested, and may legitimately use __import__, subprocess,
        etc. that the safety validator would block.
        """
        source = code if code is not None else self._code
        clean = self._sanitize_code(source) if not self._trusted else source

        # ── Safety check before exec (skipped for trusted/catalog agents) ──
        if not self._trusted:
            safety_error = self._validate_code_safety(clean)
            if safety_error:
                return safety_error
        else:
            logger.info("[%s] Trusted agent — skipping safety validator", self.name)

        # Pre-inject the LLM shim so generated code can call agent.llm directly
        def _get_llm_shim(*args, **kwargs):
            from ...llm_factory import provider_for

            # The shim hands generated code the agent's own interface, which
            # answers the same calls without being an LLMProvider subclass.
            return provider_for("dynamic", cast("LLMProvider | None", self._api.llm))

        self._ns["get_llm"] = _get_llm_shim
        self._ns["setup_llm"] = _get_llm_shim
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

        if _re.search(r"\bcv2\b", clean):
            shim = resilient_cv2_module(self.name)
            if shim is not None:
                self._ns["cv2"] = shim

        try:
            exec(compile(clean, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup = self._ns.get("setup")
            self._fn_process = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")
            fns = [f for f in ["setup", "process", "handle_task", "cleanup"] if f in self._ns]
            logger.info("[%s] Code compiled OK. Functions: %s", self.name, fns)
            if not fns:
                logger.warning("[%s] No functions found in compiled code.", self.name)
            return None  # success
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    async def _fix_syntax_with_llm(self, bad_code: str, error_msg: str) -> str | None:
        """Ask the configured LLM to fix a syntax error in agent code.

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
        logger.info("[%s] Asking LLM to fix syntax error: %s", self.name, error_msg[:120])
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": f"Syntax error — asking LLM to fix: {error_msg[:120]}",
                "timestamp": time.time(),
            },
        )
        try:
            response, usage = await self._llm_provider.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a Python syntax expert. Return only valid Python code.",
                max_tokens=4096,
            )
            # Track cost
            if hasattr(self, "total_input_tokens"):
                self._accrue_usage(usage)

            # Strip markdown fences the LLM may add despite instructions
            fixed = response.strip()
            if fixed.startswith("```"):
                fixed = "\n".join(
                    ln for ln in fixed.split("\n") if not ln.strip().startswith("```")
                ).strip()

            return fixed  # caller validates with _compile_code()

        except Exception as e:
            logger.warning("[%s] LLM fix call failed: %s", self.name, e)
            return None  # only None when LLM is truly unreachable

    # ── Setup wrapper ───────────────────────────────────────────────────────

    # Max times _run_setup will ask the LLM to fix a runtime error before giving up
    _MAX_SETUP_RETRIES = 2

    async def _run_setup(self):
        """Run setup() as a background task with LLM self-correction on failure.

        If setup() raises a runtime error (e.g. TypeError from await on sync call,
        NameError, AttributeError), the LLM is asked to fix the code and the whole
        compile-then-setup cycle is retried up to _MAX_SETUP_RETRIES times.

        - If process() is also defined, it is started AFTER setup() returns.
          For agents whose setup() never returns (e.g. aiomqtt subscription loops),
          process() is simply not started — the subscription loop IS the process.
        """
        current_code = self._code
        last_error = None

        setup = self._fn_setup
        if setup is None:  # the caller checks this; kept so the type is honest
            return

        for attempt in range(1 + self._MAX_SETUP_RETRIES):
            try:
                await setup(self._api)
                if attempt > 0:
                    logger.info("[%s] setup() succeeded after %s fix(es).", self.name, attempt)
                    # ── Write fixed code back to spawn registry so restart uses it ──
                    self._persist_fixed_code(self._code)
                    await self._mqtt_publish(
                        f"agents/{self.actor_id}/logs",
                        {
                            "type": "log",
                            "message": f"setup() runtime error fixed by LLM after {attempt} attempt(s).",
                            "timestamp": time.time(),
                        },
                    )
                else:
                    logger.info("[%s] setup() completed.", self.name)
                last_error = None
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                last_error = e
                err = traceback.format_exc()
                logger.error("[%s] setup() failed (attempt %s): %s", self.name, attempt + 1, e)

                if attempt >= self._MAX_SETUP_RETRIES:
                    break  # exhausted retries

                # Ask LLM to fix the runtime error
                fixed = await self._fix_runtime_with_llm(current_code, str(e), err)
                if fixed is None:
                    logger.warning("[%s] LLM unavailable — cannot fix setup() error", self.name)
                    break

                # Recompile the fixed code
                self._ns = {}
                compile_err = self._compile_code(fixed)
                if compile_err:
                    logger.warning(
                        "[%s] LLM fix introduced compile error: %s", self.name, compile_err
                    )
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

                self._code = fixed
                current_code = fixed
                logger.info(
                    "[%s] Retrying setup() with LLM-fixed code (attempt %s)...",
                    self.name,
                    attempt + 1,
                )

        if last_error is not None:
            err = traceback.format_exc()
            logger.error("[%s] setup() failed permanently: %s", self.name, last_error)
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
    ) -> str | None:
        """Ask the LLM to fix a runtime error in agent code (setup/process).

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
        logger.info("[%s] Asking LLM to fix runtime error: %s", self.name, error_msg[:120])
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": f"Runtime error — asking LLM to fix: {error_msg[:120]}",
                "timestamp": time.time(),
            },
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
                self._accrue_usage(usage)

            fixed = response.strip()
            if fixed.startswith("```"):
                fixed = "\n".join(
                    ln for ln in fixed.split("\n") if not ln.strip().startswith("```")
                ).strip()
            return fixed

        except Exception as e:
            logger.warning("[%s] LLM runtime-fix call failed: %s", self.name, e)
            return None

    # ── Process loop ───────────────────────────────────────────────────────

    # Max time a single process() or handle_task() call can take before
    # we assume it's stuck in a blocking call and cancel it.
    _PROCESS_TIMEOUT = 120.0  # seconds
    _HANDLE_TASK_TIMEOUT = 60.0

    # ── How many consecutive process() errors before we attempt LLM self-fix ──
    _PROCESS_LLM_FIX_THRESHOLD = 3  # try to fix after this many errors in a row
    # How many consecutive process() errors trigger state=FAILED (Supervisor sees this)
    _PROCESS_FAIL_THRESHOLD = 5

    async def _process_loop(self):
        """Continuously call the generated process() function.

        Erlang/OTP semantics:
        - Each error increments _consecutive_errors.
        - At _PROCESS_LLM_FIX_THRESHOLD consecutive errors, ask the LLM to fix the code
          and recompile in-place (self-healing).
        - At _PROCESS_FAIL_THRESHOLD consecutive errors (or after LLM fix fails),
          set state=FAILED — the Supervisor's _watch_loop will detect this and restart us.
          This is the "let it crash" principle: don't spin in degraded mode forever.
        """
        process = self._fn_process
        if process is None:  # the caller checks this; kept so the type is honest
            return

        _llm_fix_attempted = False  # only try the LLM fix once per process_loop lifetime

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            if self.state == ActorState.PAUSED:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await asyncio.wait_for(
                    process(self._api),
                    timeout=self._PROCESS_TIMEOUT,
                )
                self._reset_error_count()
                _llm_fix_attempted = False  # reset after a clean run
            except asyncio.TimeoutError:
                self.metrics.errors += 1
                logger.error(
                    "[%s] process() timed out after %ss — likely a blocking call without run_in_executor",
                    self.name,
                    self._PROCESS_TIMEOUT,
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
                        "[%s] process() timed out %sx — setting FAILED so Supervisor can restart cleanly.",
                        self.name,
                        self._consecutive_errors,
                    )
                    self.state = ActorState.FAILED
                    return
                backoff = min(2**self._consecutive_errors, 30)
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.errors += 1
                tb = traceback.format_exc()
                logger.error("[%s] process() error: %s\n%s", self.name, e, tb)
                await self._publish_error(phase="process", error=e, traceback_str=tb)

                # ── LLM self-healing: try to fix the code in-place ────────────
                if (
                    not _llm_fix_attempted
                    and self._consecutive_errors >= self._PROCESS_LLM_FIX_THRESHOLD
                    and self._llm_provider is not None
                ):
                    _llm_fix_attempted = True
                    logger.warning(
                        "[%s] %s consecutive process() errors — asking LLM to fix code in-place.",
                        self.name,
                        self._consecutive_errors,
                    )
                    fixed = await self._fix_runtime_with_llm(self._code, str(e), tb)
                    if fixed is not None:
                        self._ns = {}
                        compile_err = self._compile_code(fixed)
                        if compile_err is None:
                            self._code = fixed
                            self._consecutive_errors = 0  # give the fixed code a clean slate
                            # ── Write fixed code back to spawn registry so restart uses it ──
                            self._persist_fixed_code(fixed)
                            logger.info(
                                "[%s] LLM fixed process() code — resuming with patched version.",
                                self.name,
                            )
                            await self._mqtt_publish(
                                f"agents/{self.actor_id}/logs",
                                {
                                    "type": "log",
                                    "message": "process() runtime error fixed by LLM in-place.",
                                    "timestamp": time.time(),
                                },
                            )
                            await asyncio.sleep(self.poll_interval)
                            continue
                        logger.warning(
                            "[%s] LLM fix introduced compile error: %s", self.name, compile_err
                        )

                # ── Erlang: too many errors → FAILED → Supervisor restarts us ──
                if self._consecutive_errors >= self._PROCESS_FAIL_THRESHOLD:
                    logger.critical(
                        "[%s] %s consecutive process() errors — setting FAILED so Supervisor can restart cleanly.",
                        self.name,
                        self._consecutive_errors,
                    )
                    self.state = ActorState.FAILED
                    await self._publish_error(
                        phase="process", error=e, traceback_str=tb, fatal=True
                    )
                    return

                backoff = min(2**self._consecutive_errors, 30)
                await asyncio.sleep(backoff)
            await asyncio.sleep(self.poll_interval)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            self.metrics.messages_processed += 1

            # Correlation id for request/reply. main.delegate_task() and
            # AgentAPI.send_to() tag the TASK with "_task_id" and then block on a
            # future keyed by it; the reply MUST echo that id or the caller's
            # future never resolves. Without this, recipe/dynamic agents (e.g.
            # manual-agent) run handle_task and log everything but their RESULT
            # is un-correlatable, so the user sees no response. LLMAgent already
            # echoes it — this brings DynamicAgent to parity.
            _incoming = msg.payload if isinstance(msg.payload, dict) else {}
            _corr = _incoming.get("_task_id")

            def _with_corr(r):
                if _corr is None:
                    return r
                if isinstance(r, dict):
                    return r if "_task_id" in r else {**r, "_task_id": _corr}
                # non-dict result (str/number) — wrap so the id can ride along
                return {"result": r, "_task_id": _corr}

            if self._fn_handle_task:
                try:
                    result = await asyncio.wait_for(
                        self._fn_handle_task(self._api, msg.payload or {}),
                        timeout=self._HANDLE_TASK_TIMEOUT,
                    )
                    if msg.sender_id and result is not None:
                        await self.send(msg.sender_id, MessageType.RESULT, _with_corr(result))
                except asyncio.TimeoutError:
                    logger.error(
                        "[%s] handle_task() timed out after %ss",
                        self.name,
                        self._HANDLE_TASK_TIMEOUT,
                    )
                    await self._publish_error(
                        phase="handle_task",
                        error=TimeoutError(f"handle_task() exceeded {self._HANDLE_TASK_TIMEOUT}s"),
                        traceback_str="",
                    )
                    if msg.sender_id:
                        await self.send(
                            msg.sender_id,
                            MessageType.RESULT,
                            _with_corr(
                                {
                                    "error": f"handle_task() timed out after {self._HANDLE_TASK_TIMEOUT}s",
                                    "error_phase": "handle_task",
                                    "agent": self.name,
                                }
                            ),
                        )
                except Exception as e:
                    tb = traceback.format_exc()
                    logger.error("[%s] handle_task() error: %s\n%s", self.name, e, tb)
                    await self._publish_error(phase="handle_task", error=e, traceback_str=tb)
                    if msg.sender_id:
                        await self.send(
                            msg.sender_id,
                            MessageType.RESULT,
                            _with_corr(
                                {
                                    "error": str(e),
                                    "error_phase": "handle_task",
                                    "agent": self.name,
                                }
                            ),
                        )
            else:
                if msg.sender_id:
                    await self.send(
                        msg.sender_id,
                        MessageType.RESULT,
                        _with_corr({"info": f"{self.name} has no handle_task defined"}),
                    )

    async def _publish_error(
        self,
        phase: str,
        error: Exception,
        traceback_str: str = "",
        fatal: bool = False,
    ):
        """Publish a structured error event to agents/{id}/errors AND send
        a direct actor message to MonitorAgent so it works without MQTT.
        """
        self._consecutive_errors += 1
        self._last_error_time = time.time()
        self._error_phase = phase
        severity = (
            "critical" if fatal or self._consecutive_errors >= self._error_threshold else "warning"
        )
        event = {
            "actor_id": self.actor_id,
            "name": self.name,
            "phase": phase,
            "error": str(error),
            "traceback": traceback_str[-1200:] if traceback_str else "",
            "consecutive": self._consecutive_errors,
            "fatal": fatal,
            "severity": severity,
            "degraded": self._consecutive_errors >= self._error_threshold,
            "timestamp": time.time(),
        }
        await self._mqtt_publish(f"agents/{self.actor_id}/errors", event)
        # Direct actor message to monitor (works without MQTT broker)
        if self._registry:
            monitor = self._registry.find_by_name("monitor")
            if monitor and monitor.actor_id != self.actor_id:
                try:
                    await self.send(
                        monitor.actor_id,
                        MessageType.TASK,
                        {
                            **event,
                            "_monitor_error_event": True,
                        },
                    )
                except Exception:
                    pass
        # Mirror to /alert so the dashboard picks it up immediately
        await self._mqtt_publish(
            f"agents/{self.actor_id}/alert",
            {
                "actor_id": self.actor_id,
                "name": self.name,
                "message": f"[{phase}] {error}",
                "severity": severity,
                "timestamp": time.time(),
            },
        )

    def _reset_error_count(self):
        """Reset the process()/setup() error counter after a clean run.

        Deliberately does NOT touch _cb_error_count / _cb_error_last — those
        track subscribe callback errors which are independent of process().
        A successful process() call doesn't mean the callback is fixed.
        """
        if self._consecutive_errors > 0:
            logger.info("[%s] Recovered — resetting error counter.", self.name)
            self._consecutive_errors = 0
            self._error_phase = ""

    def _persist_fixed_code(self, fixed_code: str):
        """Write the LLM-fixed code back to:
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
                main = find_main_actor(self._registry)
                if main is not None:
                    reg = main._get_spawn_registry()
                    if self.name in reg:
                        entry = dict(reg[self.name])
                        if entry.get("code") != fixed_code:
                            entry["code"] = fixed_code
                            entry["_code_fixed_at"] = time.time()
                            main._save_to_spawn_registry(entry)
                            logger.info(
                                "[%s] Fixed code written to spawn registry (%s chars).",
                                self.name,
                                len(fixed_code),
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
                    _mqtt_port = self._mqtt_port
                    _registry = self._registry

                    async def _fixed_factory(
                        old_f=_old_factory,
                        code=_fixed,
                        mc=_mqtt_client,
                        mb=_mqtt_broker,
                        mp=_mqtt_port,
                    ):
                        # Call the original factory to get a correctly configured instance
                        actor = await old_f() if inspect.iscoroutinefunction(old_f) else old_f()
                        # Patch in the fixed code before the actor starts
                        actor._code = code
                        return actor

                    spec.factory = _fixed_factory
                    logger.info("[%s] Supervisor factory updated with fixed code.", self.name)

        except Exception as exc:
            logger.warning("[%s] Could not persist fixed code: %s", self.name, exc)

    def get_status(self) -> dict:
        s = super().get_status()
        s["description"] = self.description
        s["code"] = self._code
        s["agent_type"] = "dynamic"
        return s

    def _build_heartbeat(self) -> dict:
        hb = super()._build_heartbeat()
        hb["code"] = self._code  # include code in every heartbeat
        hb["description"] = self.description
        hb["agent_type"] = "dynamic"
        return hb

    def _current_task_description(self) -> str:
        return self.description or "running dynamic code"

    def _accrue_usage(self, usage: dict) -> None:
        if not isinstance(usage, dict):
            return
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        delta = self.total_cost_usd - self._last_period_cost_usd
        if delta > 0:
            accumulate_global_cost(delta)
            self._last_period_cost_usd = self.total_cost_usd

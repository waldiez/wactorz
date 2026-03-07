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
        llm_provider=None,                  # optional LLM for agent.llm.chat()
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._code           = code
        self.poll_interval   = poll_interval
        self.description     = description
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
        self._compile_code()
        if self._fn_setup:
            try:
                await self._fn_setup(self._api)
                logger.info(f"[{self.name}] setup() completed.")
            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"[{self.name}] setup() failed: {e}\n{err}")
                await self._publish_error(
                    phase="setup", error=e, traceback_str=err, fatal=True)
        if self._fn_process:
            self._tasks.append(asyncio.create_task(self._process_loop()))

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

        return "\n".join(result)
    @staticmethod
    def _fix_fstrings(code: str) -> str:
        """
        Rewrite Python 3.12-style f-strings (nested same-delimiter quotes)
        so they run on Python 3.10 by hoisting inner expressions to temp vars.
        e.g.: f'...{'x' if c else 'y'}...' -> _fs1 = 'x' if c else 'y'; f'...{_fs1}...'
        """
        import re
        lines = code.split('\n')
        result = []
        counter = [0]

        for line in lines:
            if "f'" not in line and 'f"' not in line:
                result.append(line)
                continue
            indent = len(line) - len(line.lstrip())
            prefix = ' ' * indent
            new_lines = []

            def hoist(m):
                expr = m.group(1)
                # Only hoist if the expression contains string literals
                if "'" in expr or '"' in expr:
                    counter[0] += 1
                    vname = f'_fs{counter[0]}'
                    new_lines.append(f'{prefix}{vname} = {expr}')
                    return '{' + vname + '}'
                return m.group(0)

            fixed = re.sub(r'\{([^{}]+)\}', hoist, line)
            result.extend(new_lines)
            result.append(fixed)

        return '\n'.join(result)

    @staticmethod
    def _fix_multiline_strings(code: str) -> str:
        """
        Convert unterminated single/double-quoted strings that span multiple lines
        into triple-quoted strings. The LLM sometimes writes f'...
...' with
        real newlines which is a SyntaxError in Python 3.10.
        """
        def has_unterminated(s):
            in_str = None
            j = 0
            while j < len(s):
                c = s[j]
                if c == "\\":
                    j += 2
                    continue
                if in_str is None:
                    if c in ('"', "'"):
                        if s[j:j+3] in ('"""', "'''"):
                            end = s.find(s[j:j+3], j + 3)
                            if end == -1:
                                return None  # already triple-quoted, spans lines
                            j = end + 3
                            continue
                        in_str = c
                elif c == in_str:
                    in_str = None
                j += 1
            return in_str

        lines = code.split("\n")
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            unclosed = has_unterminated(line.rstrip())
            if unclosed and i + 1 < len(lines):
                collected = [line.rstrip()]
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].rstrip()
                    collected.append(next_line)
                    raw = next_line.replace("\\" + unclosed, "")
                    if raw.count(unclosed) % 2 == 1:
                        break
                    j += 1
                if j > i:
                    # Find opening quote position in first collected line
                    first = collected[0]
                    # Walk backwards to find last unmatched open quote
                    open_pos = None
                    in_s = None
                    for k, c in enumerate(first):
                        if c == "\\":
                            continue
                        if in_s is None and c == unclosed:
                            open_pos = k
                            in_s = c
                        elif in_s == c:
                            in_s = None
                            open_pos = None
                    if open_pos is not None:
                        tq = unclosed * 3
                        before    = first[:open_pos]
                        after_open = first[open_pos + 1:]
                        last      = collected[-1]
                        close_pos = last.find(unclosed)
                        if close_pos >= 0:
                            before_close = last[:close_pos]
                            after_close  = last[close_pos + 1:]
                            parts = [after_open] + collected[1:-1] + [before_close]
                            result.append(before + tq + "\n".join(parts) + tq + after_close)
                            i = j + 1
                            continue
            result.append(line)
            i += 1
        return "\n".join(result)

    def _compile_code(self):
        """Compile and exec the LLM-generated code into self._ns."""
        # Step 1: sanitize — remove any self-instantiated LLM clients
        clean_code = self._sanitize_code(self._code)
        # Step 2: fix multi-line string literals (real newlines inside quotes)
        clean_code = self._fix_multiline_strings(clean_code)
        # Step 3: fix Python 3.12-style nested f-string quotes for 3.10
        clean_code = self._fix_fstrings(clean_code)

        # Pre-inject the LLM interface so generated code can use it directly
        # via agent.llm.chat() which the _AgentAPI already provides
        def _get_llm_shim(*args, **kwargs):
            return self._api.llm
        self._ns["get_llm"]    = _get_llm_shim
        self._ns["setup_llm"]  = _get_llm_shim
        self._ns["create_llm"] = _get_llm_shim

        try:
            exec(compile(clean_code, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup       = self._ns.get("setup")
            self._fn_process     = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")

            fns = [f for f in ["setup", "process", "handle_task", "cleanup"] if f in self._ns]
            logger.info(f"[{self.name}] Code compiled OK. Functions: {fns}")

            if not fns:
                logger.warning(f"[{self.name}] No functions found in code!")
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"[{self.name}] Code compilation failed: {e}\n{err}")
            asyncio.create_task(self._publish_error(
                phase="compile", error=e, traceback_str=err, fatal=True))

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

    # ── MQTT ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, data: Any):
        """Publish data to an MQTT topic. topic is used as-is."""
        await self._actor._mqtt_publish(topic, data)

    async def publish_detection(self, data: Any):
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any):
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

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

    def recall(self, key: str) -> Any:
        return self._actor.recall(key)

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

    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Optional[Any]:
        """Alias for send_to() — cleaner name for planner/coordinator agents."""
        return await self.send_to(agent_name, payload, timeout=timeout)

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1

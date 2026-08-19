"""Handing a task to another agent and waiting for its answer.

Two routes, tried in order: an agent in this process gets the task through its
mailbox, and one on another machine gets it over MQTT. An agent that is neither
is not an error to raise — the caller is told nothing came back and decides what
that means.

The reply topic is subscribed before the caller publishes. Publishing first
loses the answer of any agent quick enough to reply before the subscription
exists: the broker drops it and the caller waits out its whole timeout on work
that in fact succeeded, which reads as a flaky node rather than a bug.

Delegations arrive in two shapes. A `<delegate>` block is unambiguous. An
`@mention` is what the model writes anyway, and only some count — one starting a
line or sentence is an instruction, while one buried mid-sentence is the model
telling the *user* about an agent, and dispatching that would run something
nobody asked for and rewrite the sentence explaining it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, NamedTuple

from ..core.actor import MessageType
from ..core.mqtt import mqtt_client

if TYPE_CHECKING:
    from .hosts import DelegationHost

logger = logging.getLogger(__name__)


class _Delegation(NamedTuple):
    """One delegation found in a reply, and how it was written."""

    text: str
    agent_name: str
    payload: dict[str, Any]
    is_bare: bool


#: An `@mention` that begins a line or a sentence, and so is an instruction.
_BARE_MENTION = re.compile(r"(?:^|\n|[.!?:]\s+)(@([\w][\w\-]*)[ \t]+)")

#: The only agents a social channel may delegate to.
#:
#: An allow-list rather than a deny-list: anything added to the system is out of
#: reach from Discord and Telegram until someone decides otherwise, which is the
#: safe direction for a default to fail in.
RESTRICTED_DELEGATION_ALLOW = frozenset({"weather-agent", "home-assistant-agent"})

#: How long to wait for a reply subscription before publishing regardless.
#:
#: Publishing anyway is deliberate: the task still gets done and its side
#: effects are usually what the caller wanted, so losing the answer beats losing
#: the work. The reason is logged, so a timeout that follows is explained.
SUBSCRIBE_TIMEOUT_S = 5.0


class DelegationManager:
    """Sends tasks to other agents and turns their replies into text."""

    def __init__(self, host: DelegationHost) -> None:
        self.host = host

    @contextlib.asynccontextmanager
    async def _reply_topic(self) -> AsyncGenerator[tuple[str, asyncio.Future]]:
        """A reply topic that is already subscribed, and the future its answer lands in.

        **Subscribing before the caller publishes is the whole point.** Both
        call sites used to publish first and subscribe after, so an agent that
        answered quickly answered into nothing: the reply was published to a
        topic with no subscriber, dropped by the broker, and the caller waited
        out its full timeout for a task that had in fact succeeded. It reads as
        a flaky node, which is why it survived so long. The monitor's own copy
        of this logic was corrected years ago and the fix was never brought back
        here.

        Publishes anyway if the subscription does not come up in time. The task
        still gets done, and its side effects are usually what the caller wanted;
        losing the answer is worse than losing the work. The reason is logged, so
        a timeout that follows is explained rather than mysterious.
        """
        topic = f"main/reply/{self.host.actor_id}/{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.host._result_futures[topic] = future
        subscribed = asyncio.Event()

        async def _listen() -> None:
            try:
                async with mqtt_client(self.host._mqtt_broker, self.host._mqtt_port) as client:
                    await client.subscribe(topic)
                    subscribed.set()
                    async for msg in client.messages:
                        try:
                            if not future.done():
                                future.set_result(json.loads(msg.payload.decode()))
                        except Exception as exc:
                            logger.debug(
                                "[%s] Undecodable reply on %r: %s", self.host.name, topic, exc
                            )
                        return
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                # Whatever happened, stop the caller waiting on a listener that
                # is no longer going to subscribe.
                subscribed.set()

        task = asyncio.create_task(_listen())
        try:
            try:
                await asyncio.wait_for(subscribed.wait(), timeout=SUBSCRIBE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] reply subscription for %s did not come up in %.0fs — sending anyway, "
                    "so a reply may be missed",
                    self.host.name,
                    topic,
                    SUBSCRIBE_TIMEOUT_S,
                )
            yield topic, future
        finally:
            task.cancel()
            self.host._result_futures.pop(topic, None)

    async def delegate_task(
        self, target_name: str, task: str, timeout: float = 60.0
    ) -> dict | None:
        """Send a task to a named agent and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox path
          2. Remote node   — MQTT request/reply via agents/by-name/{name}/task
          3. Returns None if the agent is unknown in both
        """
        if not self.host._registry:
            return None

        target = self.host._registry.find_by_name(target_name)
        if target:
            # ── Local path (fast, in-process) ────────────────────────────────
            task_id = uuid.uuid4().hex
            future = asyncio.get_event_loop().create_future()
            self.host._result_futures[task_id] = future
            await self.host.send(
                target.actor_id,
                MessageType.TASK,
                {
                    "text": task,
                    "_task_id": task_id,
                    "task": task_id,
                    "reply_to": self.host.actor_id,
                },
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self.host._result_futures.pop(task_id, None)

        # ── Remote path: check if agent is on a known node ───────────────────
        remote_node = None
        for node_name, nd in self.host._known_nodes.items():
            if target_name in nd.get("agents", []):
                remote_node = node_name
                break

        if not remote_node:
            logger.warning(
                "[%s] delegate_task: %r not found locally or remotely", self.host.name, target_name
            )
            return None

        async with self._reply_topic() as (reply_topic, future):
            await self.host._mqtt_publish(
                f"agents/by-name/{target_name}/task",
                {"text": task, "payload": task, "_reply_topic": reply_topic, "_remote_task": True},
            )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] delegate_task: %r on %r timed out after %ss",
                    self.host.name,
                    target_name,
                    remote_node,
                    timeout,
                )
                return None

    async def delegate_to_installer(
        self, payload: dict[str, Any], timeout: float = 300.0
    ) -> dict[str, Any]:
        """Send a task to the installer agent and wait for the result.
        Handles node_deploy, node_install, node_run, install, check actions.
        timeout is generous (300s) because deploys involve SSH + pip installs.
        """
        if not self.host._registry:
            return {"error": "No registry available"}
        installer = self.host._registry.find_by_name("installer")
        if not installer:
            return {"error": "installer agent not found"}

        task_id = f"inst_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.host._result_futures[task_id] = future

        payload = dict(payload)
        payload["_task_id"] = task_id
        payload["task"] = task_id

        await self.host.send(installer.actor_id, MessageType.TASK, payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"Installer timed out after {timeout}s"}
        finally:
            self.host._result_futures.pop(task_id, None)

    async def _run_delegation(self, agent_name: str, payload: Any) -> str:
        """Dispatch one already-resolved delegation and format the result string.
        Shared by @mention delegations and <delegate> blocks.
        """
        json_str = json.dumps(payload)
        logger.info(
            "[%s] Executing LLM delegation → @%s %s", self.host.name, agent_name, json_str[:80]
        )
        try:
            result = await self.delegate_task(agent_name, json_str, timeout=300.0)
        except Exception as e:
            return f"[{agent_name} error: {e}]"
        # Formatting the reply is outside the guard on purpose: only the
        # delegation can fail, and reporting a formatting bug as an agent error
        # would send the reader looking at the wrong machine.
        if not result:
            return f"[{agent_name} did not respond]"
        if not isinstance(result, dict):
            return f"✅ {agent_name}: {result}"
        error = result.get("error")
        if error:
            return f"❌ {agent_name} failed: {error}"
        for key in ("pptx_path", "image_path", "result", "message", "output", "text"):
            if result.get(key):
                return f"✅ {agent_name} completed: {key}={result[key]}"
        return f"✅ {agent_name} completed: {result}"

    async def _process_delegate_commands(
        self, response: str, restricted: bool = False
    ) -> tuple[str, list[str]]:
        """Scan the LLM response for structured delegation blocks and execute them:

            <delegate>{"agent": "manual-agent", "task": "search for the Philips 2200 manual"}</delegate>
            <delegate>{"agent": "weather-agent", "payload": {"city": "Athens"}}</delegate>

        This is the orchestrator-side counterpart of <spawn>/<delete> and the
        PREFERRED way for the LLM to delegate: unlike a free-text @mention it is
        unambiguous, never truncated, and carries a structured payload.

        Accepted block fields:
          - "agent"  (required) — target agent name
          - "task"   — free-text task; wrapped as {"text": task}
          - "payload"— explicit dict payload (takes precedence over "task")

        Returns (cleaned_response, [result_strings]). The <delegate> block is
        replaced in-place with its result so the user sees the outcome.
        """
        pattern = r"<delegate>(.*?)</delegate>"
        results: list[str] = []

        matches = list(re.finditer(pattern, response, re.DOTALL))
        for m in matches:
            block = m.group(1).strip()
            try:
                cfg = json.loads(block)
            except json.JSONDecodeError:
                logger.exception(
                    "[%s] Invalid <delegate> JSON\nRaw block: %s", self.host.name, block[:200]
                )
                response = response.replace(m.group(0), "[delegate: malformed block]")
                continue

            agent_name = (cfg.get("agent") or cfg.get("name") or "").strip()
            if not agent_name:
                logger.warning(
                    "[%s] <delegate> block missing 'agent': %s", self.host.name, block[:200]
                )
                response = response.replace(m.group(0), "[delegate: no agent named]")
                continue
            if agent_name == self.host.name:
                response = response.replace(m.group(0), "")
                continue

            if isinstance(cfg.get("payload"), dict):
                payload = cfg["payload"]
            else:
                payload = {"text": str(cfg.get("task", "")).strip()}

            if restricted:
                # Resolve-only (no spawn), allow-listed targets only.
                if agent_name.lower() not in RESTRICTED_DELEGATION_ALLOW:
                    result_str = f"[{agent_name} isn't available from this channel]"
                    results.append(result_str)
                    response = response.replace(m.group(0), result_str)
                    continue
                target = (
                    self.host._registry.find_by_name(agent_name) if self.host._registry else None
                )
            else:
                target, _spawnable = await self.host._resolve_or_spawn(agent_name)
            if not target:
                result_str = f"[Could not reach {agent_name}]"
            else:
                # Use the resolved actor's real name so delegation lands even
                # when the LLM used a fuzzy alias ('smart energy agent').
                result_str = await self._run_delegation(target.name, payload)

            results.append(result_str)
            response = response.replace(m.group(0), result_str)

        return response, results

    def _json_mentions(self, response: str) -> tuple[list[_Delegation], list[tuple[int, int]]]:
        """`@agent {json}` anywhere in the text, and the spans they consume.

        The payload is found by counting braces rather than by a pattern, so a
        nested object survives. A brace inside a string value does not: the scan
        does not know which are quoted, stops early, and the block is skipped —
        a limitation rather than a decision.
        """
        found: list[_Delegation] = []
        spans: list[tuple[int, int]] = []
        for m in re.finditer(r"@([\w][\w\-]*)\s+(\{)", response):
            agent_name = m.group(1)
            if agent_name == self.host.name:
                continue
            start = m.start(2)
            depth, end = 0, start
            for i, ch in enumerate(response[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if depth != 0:
                continue
            try:
                payload = json.loads(response[start:end])
            except json.JSONDecodeError:
                continue
            found.append(_Delegation(response[m.start() : end], agent_name, payload, False))
            spans.append((m.start(), end))
        return found, spans

    def _bare_mentions(self, response: str, taken: list[tuple[int, int]]) -> list[_Delegation]:
        """`@agent do the thing` at a line or sentence boundary.

        The boundary is the whole rule. An `@name` buried mid-sentence is the
        model telling the user about an agent rather than calling one, and
        dispatching it would run something nobody asked for and rewrite the
        sentence that explains it.
        """
        found: list[_Delegation] = []
        for m in _BARE_MENTION.finditer(response):
            agent_name = m.group(2)
            if agent_name == self.host.name:
                continue
            at_start, payload_start = m.start(1), m.end(1)
            if any(s <= at_start < e for s, e in taken):
                continue  # already read as the JSON form
            if payload_start < len(response) and response[payload_start] == "{":
                continue  # the JSON form owns this one
            tail = response[payload_start:]
            stop = re.search(r"[.!?\n]", tail)
            cut = stop.start() if stop else len(tail)
            task = tail[:cut].strip()
            if not task:
                continue
            found.append(
                _Delegation(
                    response[at_start : payload_start + cut], agent_name, {"text": task}, True
                )
            )
        return found

    async def _execute_llm_delegations(self, response: str) -> str:
        """Run the `@mention` delegations in `response`, replacing each with its result."""
        json_form, spans = self._json_mentions(response)
        delegations = json_form + self._bare_mentions(response, spans)

        replacements: list[tuple[str, str]] = []
        for full_match, agent_name, payload, is_bare in delegations:
            target, spawnable = await self.host._resolve_or_spawn(agent_name)
            if target:
                replacements.append((full_match, await self._run_delegation(target.name, payload)))
                continue
            if is_bare and not spawnable:
                # Prose, almost certainly: an unknown name written without
                # braces is far more likely to be a turn of phrase than intent,
                # and rewriting the line would cost the reader a sentence.
                logger.debug(
                    "[%s] Ignoring bare mention of unknown agent %r", self.host.name, agent_name
                )
                continue
            replacements.append((full_match, f"[Could not reach {agent_name}]"))

        for original, replacement in replacements:
            response = response.replace(original, replacement)
        return response

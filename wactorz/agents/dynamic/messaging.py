"""Getting something out: publishing to MQTT, delegating, alerting, logging."""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from ...core.actor import ReplyError, ask_through
from ...core.addresses import MAIN_NODE, Address, parse
from ...core.agent_directory import AgentDirectory
from ...core.mqtt import mqtt_client, user_properties, v5_kwargs
from ...core.node_publications import admit
from ...core.topics import reply_topic, task_by_name_topic, task_topic
from ..lookup import find_main_actor

if TYPE_CHECKING:
    from .hosts import ApiHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = ApiHost
else:
    _Host = object

logger = logging.getLogger(__name__)

#: How long `send_to` waits for its reply subscription before sending anyway.
#: The same bound, and the same choice, as main's delegation: the task still
#: gets done when the subscription is slow, which beats not sending it at all.
REPLY_SUBSCRIBE_TIMEOUT_S = 5.0


class Route(NamedTuple):
    """Where a task goes: the agent's address, and the topic, "" for one in this process."""

    address: Address
    topic: str


def route_from_main(main: Any, wanted: str) -> Route | str:
    """Where a task for ``wanted`` goes, as main knows its agents and the nodes'; or why nowhere."""
    resolution = main.resolve_address(wanted)
    if resolution.ambiguous:
        return resolution.refusal()
    found = resolution.found
    if found is None:
        return Route(Address(parse(wanted).name), "")
    refusal = main.task_refusal(found)
    if refusal:
        return refusal
    return Route(found, "" if found.node == MAIN_NODE else main.task_topic_for(found))


def route_from_node(directory: AgentDirectory, wanted: str, here: str) -> Route | str:
    """Where a task from an agent on node ``here`` for ``wanted`` goes; or why nowhere.

    From what the agents' manifests say. An agent whose place is known goes on
    its node's topic, or by name to a node from before tasks went by node. One
    no manifest places -- not heard of, or whose manifest names no node, as
    main's own and an older node's do -- is sent by name, which every node
    hears, as before nodes read manifests; but a bare name is not, while an
    agent of that name is also placed somewhere, since that would be a guess.
    Main takes no task from a node, so an address on main is refused.
    """
    address = parse(wanted)
    if address.node == MAIN_NODE:
        return f"'{address.name}' is on main, which takes no task from an agent on a node."
    resolution = directory.resolve(wanted)
    if resolution.ambiguous:
        return resolution.refusal()
    found = resolution.found
    if found is None:
        if address.node:
            return f"No agent '{address.name}' is known on node '{address.node}'."
        return Route(address, task_by_name_topic(address.name))
    if found.node == here:
        return Route(found, "")
    if not address.node and directory.unplaced(found.name):
        return (
            f"An agent called '{found.name}' runs on {found.node}, and another whose manifest "
            f"does not say where, so nothing was sent. Name the one on {found.node} by its "
            f"address: `{found}`."
        )
    if directory.takes_tasks_by_node(found):
        return Route(found, task_topic(found.node, found.name))
    others = directory.nodes_running(found.name) - {found.node}
    if others:
        return (
            f"Agents called '{found.name}' also run on {' and '.join(sorted(others))}, and node "
            f"'{found.node}' takes a task only by name, which every one of them would run, so "
            f"nothing was sent to {found}."
        )
    return Route(found, task_by_name_topic(found.name))


class MessagingMixin(_Host):
    """Mixed into AgentAPI; reads the actor through `self._actor`."""

    async def publish(self, topic: str, data: Any) -> None:
        """Publish data to an MQTT topic. Auto-registers topic in capability manifest
        and TopicBus contract so the agent is discoverable without explicit declare_contract().
        On every publish, captures the actual payload schema (field names + types)
        so the planner and other agents know the real field names — not guesses.
        """
        await self._actor._mqtt_publish(topic, data)

        is_new_topic = topic not in self._published_topics

        # ── Auto-capture observed schema from real payloads ────────────────
        # This solves the "temp" vs "temperature" vocabulary mismatch:
        # the schema reflects what the code ACTUALLY publishes.
        # Uses TopicContract.update_observed() — a proper dataclass field,
        # not monkey-patched attributes.
        try:
            from ...core.topic_bus import TopicContract, get_topic_bus

            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(
                    self.name, self.actor_id, getattr(self._actor, "_node", None)
                )
                if existing:
                    if is_new_topic and topic not in existing.publishes:
                        existing.publishes.append(topic)
                    # Record actual field names on every publish (first call
                    # per topic populates; subsequent calls are no-ops if
                    # fields haven't changed, but cheap either way)
                    if isinstance(data, dict):
                        existing.update_observed(topic, data)
                        # Also keep produces_schema in sync
                        for k, v in (
                            existing.observed_samples.get(topic, {}).get("fields", {}).items()
                        ):
                            existing.produces_schema[k] = v
                    bus.registry.register(existing)
                elif is_new_topic:
                    # Create minimal contract from published topics
                    contract = TopicContract(
                        name=self.name,
                        publishes=list(self._published_topics | {topic}),
                        actor_id=self.actor_id,
                        node=getattr(self._actor, "_node", None),
                    )
                    if isinstance(data, dict):
                        contract.update_observed(topic, data)
                        # Bootstrap produces_schema from observed
                        contract.produces_schema = dict(
                            contract.observed_samples.get(topic, {}).get("fields", {})
                        )
                    bus.register_contract(contract)
        except Exception as exc:
            logger.debug(
                "[%s] Could not register the %s publication with TopicBus: %s",
                self.name,
                topic,
                exc,
            )

        if is_new_topic:
            self._published_topics.add(topic)
            await self._publish_manifest()

    async def publish_detection(self, data: Any) -> None:
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any) -> None:
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

    async def log(self, message: str, level: str = "info") -> None:
        """Add a message to the event log visible in the dashboard."""
        # Encode safely for Windows terminals that can't handle all unicode
        safe_msg = message.encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    async def alert(self, message: str, severity: str = "warning") -> None:
        """Trigger an alert visible in the dashboard."""
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/alert",
            {
                "actor_id": self._actor.actor_id,
                "name": self.name,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            },
        )

    async def notify_user(
        self,
        text: str,
        *,
        options: Sequence[str] = (),
        channel: str = "dashboard",
        user: str = "",
        **extra: Any,
    ) -> str:
        """Say ``text`` to the person, and return the message's id (see Actor.notify_user).

        Use this — not log() or alert() — when the user should see the message in
        chat, e.g. when a long task finishes or an autonomous agent has news.
        ``options`` offers a choice, shown as buttons or a numbered list; the
        pick comes back on ``agents/<id>/answer`` naming the id returned.
        ``channel`` ("dashboard", "telegram", "discord", "whatsapp") says where,
        and ``user`` who to reach there.
        """
        return await self._actor.notify_user(
            text, options=options, channel=channel, user=user, **extra
        )

    async def ask_operator(
        self,
        text: str,
        *,
        options: Sequence[str],
        channel: str = "dashboard",
        user: str = "",
        timeout: float | None = None,
        label: dict[str, Any] | None = None,
        context: str = "",
    ) -> str:
        """Ask the person ``text`` with ``options`` and return the one picked (see Actor.ask_operator).

        Waits for the pick, up to ``timeout`` seconds (None: as long as the
        agent runs). A question outlives a restart; an answer that arrives
        when nothing waits goes to the program's ``on_answer(agent, question,
        option)`` if it defines one. ``label`` makes the answer a label:
        ``{"source": "sensors/ahu-1", "start": ts, "values": {"Maintenance":
        "maintenance", "Real problem": "fault"}}``, an option left out making
        none. ``context`` says why it is being asked, for the dashboard's Inbox.
        """
        return await self._actor.ask_operator(
            text,
            options=options,
            channel=channel,
            user=user,
            timeout=timeout,
            label=label,
            context=context,
        )

    async def label(
        self,
        source: str,
        value: str,
        *,
        start: Any,
        end: Any = None,
        note: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Record that ``source`` was ``value`` from ``start`` to ``end`` (event times; see Actor.label).

        Returns the label as a dict. On main only: an agent on a node gets its
        labels from its questions' answers.
        """
        written = await self._actor.label(
            source, value, start=start, end=end, note=note, model=model
        )
        return written.to_dict()

    async def wait_for_answer(self, question_id: str, *, timeout: float | None = None) -> str:
        """The option picked for an earlier question, waiting up to ``timeout`` seconds."""
        return await self._actor.wait_for_answer(question_id, timeout=timeout)

    def open_questions(self) -> list[dict[str, Any]]:
        """The questions still waiting for an answer, oldest first, as dicts (SYNC, no await)."""
        return [q.to_dict() for q in self._actor.open_questions()]

    async def _send_to_remote(
        self, address: Address, payload: Any, timeout: float, topic: str
    ) -> dict[str, Any] | None:
        """Send a task for the agent at ``address`` on ``topic`` and wait for its reply.

        ``address`` names no node for an agent sent to by name alone, wherever
        among the nodes it runs.
        """
        agent_name = address.name
        remote_node = address.node or None
        main = find_main_actor(self._actor._registry)
        refusal = main.nodes.refuses_tasks(remote_node) if main and remote_node else None
        if refusal:
            return {"error": refusal}

        here = getattr(self._actor, "_node", None) or MAIN_NODE
        reply_to = reply_topic(here, self.name, uuid.uuid4().hex[:8])

        if not isinstance(payload, dict):
            payload = {"message": payload, "text": str(payload)}
        payload = dict(payload)
        payload["_reply_topic"] = reply_to
        payload["_remote_task"] = True

        future = asyncio.get_running_loop().create_future()
        if not hasattr(self._actor, "_result_futures"):
            self._actor._result_futures = {}
        self._actor._result_futures[reply_to] = future

        # Subscribed before the task goes out: the broker drops a reply nobody
        # is subscribed to yet, so a node that answers at once would answer into
        # nothing and the caller would wait out its timeout for work that
        # succeeded.
        subscribed = asyncio.Event()
        reply_task = asyncio.create_task(
            await_remote_reply(
                future,
                reply_to,
                self._actor,
                agent_name,
                timeout,
                subscribed=subscribed,
                node=remote_node,
            )
        )
        try:
            await _until_subscribed(subscribed, self.name, agent_name)
            await self._actor._mqtt_publish(topic, payload)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] send_to '%s' on '%s' timed out after %ss",
                self.name,
                agent_name,
                remote_node or "whichever host has it",
                timeout,
            )
            return {"error": f"Timeout waiting for remote '{agent_name}'"}
        except Exception as exc:
            # The reply channel failed -- a refused or dropped connection, which
            # the waiter puts on the future. Told as a result, like a timeout,
            # rather than raised into the agent's code.
            logger.warning("[%s] send_to '%s': no reply can arrive: %s", self.name, agent_name, exc)
            return {"error": f"Could not receive a reply from remote '{agent_name}': {exc}"}
        finally:
            reply_task.cancel()
            self._actor._result_futures.pop(reply_to, None)

    def _where(self, wanted: str) -> Route | str:
        """Where a task for ``wanted``, a name or ``name@node``, goes; or why it goes nowhere.

        Main knows where its agents and the nodes' are. A node knows what the
        agents' manifests say (`wactorz.core.agent_directory`). Anywhere else, only
        the agents in this process are reached.
        """
        main = find_main_actor(self._actor._registry)
        if main is not None:
            return route_from_main(main, wanted)
        directory = getattr(getattr(self._actor, "_runner", None), "directory", None)
        if not isinstance(directory, AgentDirectory):
            return Route(parse(wanted), "")
        here = self._actor._node
        address = parse(wanted)
        registry = self._actor._registry
        if (
            address.node in ("", here)
            and registry is not None
            and registry.find_by_name(address.name)
        ):
            # Here, whatever the manifests say: this node's own record is first-hand,
            # and the agent's manifest may not have come back to it yet.
            return Route(Address(address.name, here), "")
        return route_from_node(directory, wanted, here)

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Send a TASK to another agent, by name or ``name@node``, and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox
          2. Remote node via MQTT — the task topic of the agent's node, or its
             name's for a node from before tasks went by node, with a reply topic
          3. Returns an error dict for an agent found nowhere, and for a name
             more than one agent has, with their addresses

        Works for an agent on main and for one on any node.
        """
        registry = self._actor._registry
        # `is None`, not falsiness: `ActorRegistry` defines `__len__`, so an
        # empty one is false, and an agent that holds one would be told it has
        # none. The answer for an agent that really has no registry is
        # unchanged — there is nowhere to send and nothing to report it to.
        if registry is None:
            logger.warning("[%s] send_to: no registry", self.name)
            return None

        where = self._where(agent_name)
        if isinstance(where, str):
            return {"error": where}
        address, topic = where
        agent_name = address.name
        # One here is asked in-process, whatever a manifest has said or not yet said.
        here = getattr(self._actor, "_node", None) or MAIN_NODE
        target = registry.find_by_name(agent_name) if address.node in ("", here) else None

        if target:
            # ── Local path ────────────────────────────────────────────────────
            # Generated code reads "text" or "message" from a plain value.
            if not isinstance(payload, dict):
                payload = {"message": payload, "text": str(payload)}
            if not hasattr(self._actor, "_result_futures"):
                self._actor._result_futures = {}
            try:
                return await ask_through(self._actor, agent_name, payload, timeout=timeout)
            except ReplyError as exc:
                # This API answers with error dicts, never raises: generated
                # code is written against that.
                return exc.reply
            except RuntimeError:
                return {"error": f"'{agent_name}' is not taking messages: its mailbox is full"}
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] send_to '%s' timed out after %ss", self.name, agent_name, timeout
                )
                return {"error": f"Timeout waiting for '{agent_name}'"}

        if not topic:
            logger.warning(
                "[%s] send_to: agent '%s' not found locally or remotely", self.name, agent_name
            )
            return {"error": f"Agent '{agent_name}' not found"}
        return await self._send_to_remote(address, payload, timeout, topic)

    async def send_to_many(self, tasks: list[tuple[str, Any]], timeout: float = 60.0) -> list[Any]:
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

    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Alias for send_to() — cleaner name for planner/coordinator agents."""
        return await self.send_to(agent_name, payload, timeout=timeout)


async def _until_subscribed(subscribed: asyncio.Event, name: str, agent_name: str) -> None:
    """Wait for the reply subscription, but never past `REPLY_SUBSCRIBE_TIMEOUT_S`.

    The task is sent either way; a slow subscription only risks the reply, and
    the warning explains a timeout that follows.
    """
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=REPLY_SUBSCRIBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "[%s] send_to '%s': the reply subscription did not come up in %.0fs; "
            "sending anyway, so the reply may be missed",
            name,
            agent_name,
            REPLY_SUBSCRIBE_TIMEOUT_S,
        )


async def await_remote_reply(
    future: asyncio.Future,
    reply_topic: str,
    actor: Any,
    agent_name: str,
    timeout: float,
    *,
    subscribed: asyncio.Event | None = None,
    node: str | None = None,
) -> dict[str, Any] | None:
    """Wait for a remote agent's reply, cleaning up the pending future either way.

    ``subscribed`` is set once the reply topic is subscribed, so the caller can
    publish the task only then -- and set as well if that never happens, so the
    caller is not left waiting on a subscription that is not coming.

    ``node`` is the node the task went to, when the caller is main and knows
    it: the reply is then the first message signed by that node, since any node
    can publish on the topic. On a node there is no key to check one with, and
    the first message is the reply.
    """
    try:
        broker = getattr(actor, "_mqtt_broker", "localhost")
        port = getattr(actor, "_mqtt_port", 1883)
        async with mqtt_client(broker, port, **v5_kwargs()) as client:
            await client.subscribe(reply_topic)
            if subscribed is not None:
                subscribed.set()
            async for msg in client.messages:
                raw = bytes(msg.payload or b"")
                if node and not admit(node, reply_topic, raw, user_properties(msg), "a reply"):
                    continue
                try:
                    data = json.loads(raw.decode())
                    if not future.done():
                        future.set_result(data)
                except Exception as exc:
                    logger.debug("[%s] Unreadable reply on %s: %s", agent_name, reply_topic, exc)
                return
    except Exception as e:
        if not future.done():
            future.set_exception(e)
    finally:
        # Whatever happened, stop the caller waiting on a subscription that is
        # no longer going to be made.
        if subscribed is not None:
            subscribed.set()

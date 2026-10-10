"""Chat routing for the monitor.

Decides where a user message goes — slash command, @mention, the orchestrator
behind ``runtime.orchestrator`` (main, unless the deployment installed another),
or a plain ``handle_message`` agent — and exposes the REST chat endpoints plus
the in-flight task tracker that ``POST /chat/stop`` cancels through.
"""

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from aiohttp import web
from aiohttp.web import Response
from aiomqtt import MqttError

from ..agents.llm.attachments import to_blocks
from ..agents.lookup import MAIN_ACTOR_NAME, find_main_actor
from ..core.actor import ActorState, Message, MessageType
from ..core.mqtt import mqtt_client
from ..core.task_text import reply_text, task_payload
from ..core.turns import acting_as, turn_scope
from ..monitoring import chat_metrics
from ..orchestration import DASHBOARD
from . import runtime, uploads

if TYPE_CHECKING:
    from ..orchestration import Orchestrator

logger = logging.getLogger(__name__)

# In-flight chat-generation tasks (WebSocket + REST paths) so POST /chat/stop can
# cancel a turn mid-stream.
inflight_chat_tasks: set = set()


def track_chat_task(task):
    """Register an in-flight chat-generation task so /chat/stop can cancel it."""
    inflight_chat_tasks.add(task)
    task.add_done_callback(inflight_chat_tasks.discard)
    return task


async def no_op_async() -> None:
    """No op. To use instead of lambdas."""


async def discard_reply(_text: str) -> None:
    """Drop a reply chunk, for fire-and-forget callers with nowhere to send it.

    ``route_chat`` does ``await reply_fn(text)``, so this must be a coroutine
    function taking one argument — a bare lambda raises TypeError on the first
    chunk and silently abandons the stream after the tokens are paid for.
    """


def parse_mention(content: str) -> tuple[str, str]:
    """Split a leading ``@agent`` mention off a message.

    Returns ``(target, remaining_text)``; target is ``""`` when unmentioned.
    """
    if content.startswith("@"):
        parts = content[1:].split(None, 1)
        return parts[0], (parts[1].strip() if len(parts) > 1 else "")
    return MAIN_ACTOR_NAME, content


#: How long since a node last reported before its agents stop counting as
#: reachable. Heartbeats arrive far more often than this.
NODE_FRESH_SECONDS = 30

#: Why a command about remote nodes has nowhere to go without main: the nodes
#: are reached through main, and a profile without it has none.
NO_MAIN_FOR_NODES = (
    "No main runs in this profile, so there are no remote nodes to deploy to or move agents to."
)


def remote_node_for(name: str) -> str | None:
    """The node running ``name``, or None if no node recently said it has it."""
    # Main itself, not the orchestrator: the node table is main's.
    main_actor = find_main_actor(runtime.registry)
    if not main_actor:
        return None
    for node_name, nd in main_actor._known_nodes.items():
        if time.time() - nd.get("last_seen", 0) < NODE_FRESH_SECONDS and name in nd.get(
            "agents", []
        ):
            return node_name
    return None


def routable(name: str) -> bool:
    """Whether a message addressed to ``name`` has somewhere to go."""
    if not name:
        return False
    if runtime.registry is not None and runtime.registry.find_by_name(name):
        return True
    return remote_node_for(name) is not None


def turn_attribution(content: str, declared: str = "") -> str:
    """Which agent a turn belongs to — its reply frames and its ``chat_log`` rows.

    The mention when it can be routed to, so a reply is filed with the agent that
    answers it. Otherwise the thread the sender says it is in: a mention that
    resolves to nothing is answered by the transport, and that answer belongs
    where the user is looking rather than in a thread for an agent that does not
    exist, which no view would ever show. ``declared`` is not checked against the
    running agents on purpose — a node that has gone quiet leaves the dashboard
    offering a thread this process will not route to, and that thread is still
    where the exchange belongs.
    """
    if content.startswith("/"):
        return MAIN_ACTOR_NAME
    mentioned, _ = parse_mention(content)
    if routable(mentioned):
        return mentioned
    return declared or MAIN_ACTOR_NAME


# ── Catalog / experimental-agent presentation ──────────────────────────────


def catalog_agent_line(agent: dict[str, Any]) -> str:
    name = agent.get("name", "unknown")
    description = agent.get("description", "")
    return f"- `{name}` - {description}" if description else f"- `{name}`"


def format_catalog_agents_response(payload: dict[str, Any]) -> str:
    agents = payload.get("agents", [])
    if not isinstance(agents, list):
        return str(payload)

    show_experimental = bool(payload.get("show_experimental", False))
    recommended = [a for a in agents if isinstance(a, dict) and not a.get("experimental")]
    experimental = [a for a in agents if isinstance(a, dict) and a.get("experimental")]
    total = len(recommended) + len(experimental)

    lines = [
        "**Catalog agents**",
        f"`{total}` total - `{len(recommended)}` recommended, "
        f"`{len(experimental)}` experimental beta",
    ]

    if recommended:
        lines.extend(
            [
                "",
                "### Recommended",
                *(catalog_agent_line(agent) for agent in recommended),
            ]
        )

    if experimental:
        if show_experimental:
            lines.extend(
                [
                    "",
                    "### Experimental / Beta",
                    *(catalog_agent_line(agent) for agent in experimental),
                ]
            )
        else:
            # Hidden by default — nudge the user toward the opt-in instead of
            # listing beta agents in the normal view.
            lines.extend(
                [
                    "",
                    f"_{len(experimental)} experimental/beta agent(s) hidden — "
                    f"say `list experimental` to show them._",
                ]
            )

    return "\n".join(lines)


# Agents already warned in this process — so the beta banner shows on the first
# user message to an experimental agent, not on every turn.
beta_warned_agents: set = set()


def experimental_first_use_banner(agent_name: str) -> str | None:
    """One-time beta banner for the first user message to an experimental agent.

    Returns the banner the first time ``agent_name`` is messaged in this process,
    then None afterwards so the warning isn't repeated every turn. Non-experimental
    or unknown agents always return None. The experimental flag and per-agent
    warning come from main's manifest, populated by the catalog at startup.
    """
    if agent_name in beta_warned_agents:
        return None
    # Main itself, not the orchestrator: the manifests are main's.
    main = find_main_actor(runtime.registry)
    manifest = main._agent_manifests.get(agent_name) if main else None
    if not manifest or not manifest.get("experimental"):
        return None
    beta_warned_agents.add(agent_name)
    from ..agents.catalog_agent import BETA_WARNING

    warning = manifest.get("warning") or BETA_WARNING
    return f"⚠️ **{agent_name}** is an experimental/beta agent. {warning}\n\n"


# ── Slash commands ─────────────────────────────────────────────────────────
# Every handler receives a `reply_fn` coroutine — callers supply either an
# MQTT publisher or a WebSocket sender.  No global state, no monkey-patching.


#: The slash commands only main answers, whichever orchestrator is installed.
MAIN_COMMANDS = frozenset({"/deploy", "/migrate"})


async def handle_slash(text: str, reply_fn, stream_fn=None, stream_end_fn=None) -> bool:
    """Dispatch a slash command. Returns True if recognised.

    `reply_fn` is an async callable that sends a string back to the user as a
    message of its own. Main's commands answer through `stream_fn` instead, one
    message built up chunk by chunk and closed with `stream_end_fn`, so /deploy
    shows its progress as it goes; without them each chunk is a reply.
    """
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/clear-plans":
        # Main itself, not the orchestrator: the plan cache is main's.
        main_actor = find_main_actor(runtime.registry)
        if main_actor:
            main_actor.persist("_plan_cache", {})
        await reply_fn("[System: Plan cache cleared.]")
        return True

    if cmd == "/agents":
        if runtime.registry is None:
            await reply_fn("[agents] Registry not available.")
            return True
        lines = []
        for actor in runtime.registry.all_actors():
            status = actor.get_status() if hasattr(actor, "get_status") else {}
            st = status.get("state", "?")
            protected = " [protected]" if getattr(actor, "protected", False) else ""
            node = f" [{status['node']}]" if status.get("node") else ""
            lines.append(f"  [{st:8s}] @{actor.name:<22s} {actor.actor_id[:8]}{protected}{node}")
        await reply_fn("Agents:\n" + "\n".join(lines) if lines else "No agents running.")
        return True

    if cmd == "/nodes" and find_main_actor(runtime.registry) is None:
        # Main answers /nodes, remote nodes included. Without it (the minimal
        # profile) there are no remote nodes, and this process is the one node.
        local = [a.name for a in runtime.registry.all_actors()] if runtime.registry else []
        await reply_fn(
            f"Nodes:\n  local    online   {', '.join('@' + n for n in local) or '(none)'}"
        )
        return True

    if cmd in MAIN_COMMANDS:
        # Main itself, not the orchestrator: nodes, and moving agents between
        # them, are main's, and these are main's own commands as any channel
        # reaches them.
        main_actor = find_main_actor(runtime.registry)
        if main_actor is None:
            await reply_fn(f"[error] {NO_MAIN_FOR_NODES}")
            return True
        chunk_fn = stream_fn or reply_fn
        async for chunk in main_actor.process_user_input_stream(text):
            if isinstance(chunk, dict):
                continue
            await chunk_fn(str(chunk))
        if stream_end_fn is not None:
            await stream_end_fn()
        return True

    return False


def _takes_attachments(fn: Callable[..., Any]) -> bool:
    """Whether `fn` accepts an `attachments` argument.

    The dispatch below reaches four differently-shaped entry points, and only
    the LLM ones grew a parameter for this: the Gmail, Calendar and Home
    Assistant agents override `chat` with routing of their own, and a dynamic
    agent's `chat` takes `(prompt, system)` entirely. Asking is what keeps a
    file from becoming a TypeError on an agent that never wanted one.
    """
    try:
        return "attachments" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class Destination(NamedTuple):
    """Where a chat message is going, worked out once.

    Routing acts on it and the turn metrics label the turn with its `kind`, so
    the two cannot disagree about where a message went.
    """

    #: `chat_metrics.COMMAND`, `LOCAL`, `REMOTE` or `UNROUTED`.
    kind: str
    #: The agent named, and the text with the mention taken off. Empty for a command.
    name: str = ""
    text: str = ""
    #: The agent in this process, when it is here.
    target: Any = None
    #: The node running it, when it is not here but a node is.
    remote_node: str | None = None


def command_word(content: str) -> str:
    """The command ``content`` calls, by its first word: ``/help()`` and ``/help x`` are ``/help``."""
    words = content.split()
    return words[0].rstrip("()") if words else ""


def destination_of(content: str) -> Destination:
    """Where ``content`` is going: a command, the orchestrator, an agent here, one on a node, or nowhere.

    A message that names no agent is the orchestrator's, whichever one is
    installed. Until one is (``runtime.orchestrator`` is None before the system
    has started, and in a process that runs none), it is for whatever is
    registered under main's name, as the ``@main`` form below would route it.
    """
    if content.startswith("/"):
        return Destination(chat_metrics.COMMAND)
    if not content.startswith("@") and runtime.orchestrator is not None:
        return Destination(chat_metrics.ORCHESTRATOR, MAIN_ACTOR_NAME, content)
    name, text = parse_mention(content)
    target = runtime.registry.find_by_name(name) if runtime.registry else None
    if target is not None:
        return Destination(chat_metrics.LOCAL, name, text, target)
    # None without main, which is what relays a message to a node.
    remote_node = remote_node_for(name)
    if remote_node:
        return Destination(chat_metrics.REMOTE, name, text, remote_node=remote_node)
    return Destination(chat_metrics.UNROUTED, name, text)


async def _answer_through(
    orchestrator: "Orchestrator",
    text: str,
    blocks: list[dict[str, Any]],
    chunk_fn: Callable[[str], Awaitable[Any]],
    end_fn: Callable[[], Awaitable[Any]],
) -> None:
    """Stream the orchestrator's answer to ``text`` through ``chunk_fn``, then end the turn.

    The orchestrator labels its own work and decides what to do with the
    attachments; the dashboard is the channel, whichever orchestrator answers.
    """
    try:
        async for chunk in orchestrator.handle_turn_stream(
            text, channel=DASHBOARD, attachments=blocks or None
        ):
            await chunk_fn(str(chunk))
    finally:
        await end_fn()


async def route_chat(
    content: str,
    reply_fn,
    stream_fn=None,
    stream_end_fn=None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Route one chat turn, and record how long the person waited for it.

    Timed here because every way a message reaches an agent from the dashboard
    passes through, and every one of them ends by sending the reply. A turn the
    person stops is cancelled, and not recorded; nor is one that raises, which
    the caller reports as the failure it is.
    """
    destination = destination_of(content)
    timer = chat_metrics.TurnTimer(destination.kind)
    # The turn starts here for the dashboard and the REST chat route, so every
    # agent it reaches, main or another, works inside it.
    with turn_scope():
        await _route_chat(
            content,
            destination,
            timer.watch(reply_fn),
            timer.watch(stream_fn) if stream_fn is not None else None,
            stream_end_fn,
            attachments,
        )
    timer.finish()


async def _route_chat(
    content: str,
    destination: Destination,
    reply_fn,
    stream_fn=None,
    stream_end_fn=None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Core chat routing — slash commands, @mentions, or the orchestrator's stream.

    reply_fn(text)        — send a complete message (slash commands, errors)
    stream_fn(chunk)      — send one streaming chunk (optional; falls back to reply_fn)
    stream_end_fn()       — signal that streaming is done (optional)
    attachments           — stored records for this turn, resolved by the caller

    The records are read into content blocks here rather than by the agent: the
    files are the web layer's, and only this side knows how to reach them. Every
    route that cannot carry them says so instead of answering as though the user
    attached nothing.
    """
    _chunk_fn = stream_fn or reply_fn
    _end_fn = stream_end_fn or no_op_async
    blocks = to_blocks(attachments, uploads.read_bytes) if attachments else []

    async def _say_files_not_sent(why: str) -> None:
        names = ", ".join(str(a.get("name") or "attachment") for a in attachments or [])
        await _chunk_fn(f"[note] {names} not sent — {why}.")

    if content.startswith("/"):
        if blocks:
            await _say_files_not_sent("a command does not take attachments")
        handled = await handle_slash(content, reply_fn, stream_fn, stream_end_fn)
        if not handled:
            # The rest of the command set (/help, /plans, /memory, /rules,
            # /topics, ...) is the orchestrator's, if it says it answers it.
            orchestrator = runtime.orchestrator
            if orchestrator is not None and command_word(content) in orchestrator.commands():
                await _answer_through(orchestrator, content, [], _chunk_fn, _end_fn)
            elif orchestrator is None and (main_actor := find_main_actor(runtime.registry)):
                # No orchestrator installed: main from the registry, as before.
                async for chunk in main_actor.process_user_input_stream(content):
                    if isinstance(chunk, dict):
                        continue
                    await _chunk_fn(str(chunk))
                await _end_fn()
            else:
                await reply_fn("Unknown command. Type /help for available commands.")
        return

    if destination.kind == chat_metrics.ORCHESTRATOR:
        orchestrator = runtime.orchestrator
        if orchestrator is None:
            # Gone between deciding the destination and acting on it, which a
            # shutdown under way can do: say so, rather than answer as nobody.
            await reply_fn("[error] No orchestrator is running.")
            await _end_fn()
            return
        logger.info("[io-gateway] → orchestrator: %r", destination.text[:60])
        await _answer_through(orchestrator, destination.text, blocks, _chunk_fn, _end_fn)
        return

    target_name, text = destination.name, destination.text
    target = destination.target

    if target is None:
        # Main itself, not the orchestrator: a node is reached over main's broker link.
        main_actor = find_main_actor(runtime.registry)
        # ── Remote agent fallback ─────────────────────────────────────────────
        # Agent not in local registry — check if it's running on a remote node.
        # If so, route the message via MQTT and stream the reply back.
        if main_actor:
            remote_node = destination.remote_node

            if remote_node:
                if blocks:
                    # The files are on this disk; a remote node cannot read them,
                    # and shipping them means megabytes of base64 through the
                    # broker and into its outbox. The broker is the wrong pipe.
                    await _say_files_not_sent(f"@{target_name} runs on {remote_node}")
                reply_topic = f"main/reply/io-gateway/{uuid.uuid4().hex[:8]}"
                payload = {
                    "text": text,
                    "payload": text,
                    "_reply_topic": reply_topic,
                    "_remote_task": True,
                }
                try:
                    async with mqtt_client(
                        main_actor._mqtt_broker,
                        main_actor._mqtt_port,
                    ) as client:
                        # Subscribe first, then publish — avoids race condition
                        await client.subscribe(reply_topic)
                        await main_actor._mqtt_publish(
                            f"agents/by-name/{target_name}/task",
                            payload,
                        )
                        logger.info(
                            "[io-gateway] Routed @%s → %s via MQTT", target_name, remote_node
                        )
                        try:

                            async def _get_reply():
                                async for msg in client.messages:
                                    try:
                                        data = json.loads(msg.payload.decode())
                                        text_out = reply_text(data)
                                    except Exception:
                                        text_out = msg.payload.decode()
                                    return str(text_out)
                                return None

                            text_out = await asyncio.wait_for(_get_reply(), timeout=150.0)
                            await reply_fn(text_out)
                            await _end_fn()
                        except asyncio.TimeoutError:
                            await reply_fn(
                                f"[error] @{target_name} on {remote_node} did not reply within 150s."
                            )
                            await _end_fn()
                            return
                        else:
                            return
                except (OSError, MqttError) as exc:
                    # The broker is not there to carry it. That is an outage,
                    # which every listener is already reporting: a warning with
                    # the reason, and no traceback of code that did its job.
                    logger.warning(
                        "[io-gateway] Could not reach @%s on %s: %s", target_name, remote_node, exc
                    )
                    await reply_fn(
                        f"[error] Could not reach @{target_name} on {remote_node}: {exc}"
                    )
                    await _end_fn()
                    return
                except Exception as exc:
                    logger.exception("[io-gateway] Remote @%s routing failed", target_name)
                    await reply_fn(
                        f"[error] Could not reach @{target_name} on {remote_node}: {exc}"
                    )
                    await _end_fn()
                    return

        # A turn nothing answered still ends: the caller waits on the ending
        # rather than on the reply, and holds its composer until one arrives.
        await reply_fn(f"Agent @{target_name} not found.")
        await _end_fn()
        return

    # Every path below reaches into the agent directly rather than through its
    # mailbox, so a state that ends the mailbox does not stop it answering on its
    # own: a stopped agent keeps replying after its message loop is cancelled
    # unless the state is checked here.
    #
    # ``==`` not ``is``: ActorState is a str-enum compared by value everywhere
    # else in the codebase, and identity is not safe here — the test suite has
    # wactorz.core.actor loaded under two module identities, so the enum members
    # are distinct objects with equal values.
    _unavailable = {
        ActorState.STOPPED.value: "is stopped. Start it to send messages.",
        ActorState.FAILED.value: "has failed. It should restart shortly.",
    }
    reason = _unavailable.get(getattr(target.state, "value", target.state))
    if reason is not None:
        await reply_fn(f"@{target.name} {reason}")
        await _end_fn()
        return

    logger.info("[io-gateway] → %s: %r", target.name, text[:60])

    # First user message to an experimental/beta agent gets a one-time warning
    # banner, emitted through the same channel the reply will use.
    banner = experimental_first_use_banner(target_name)
    if banner:
        await _chunk_fn(banner)

    # The agent's work, though it does not come through its mailbox.
    with acting_as(target.name):
        gen_fn = getattr(target, "process_user_input_stream", None) or getattr(
            target, "chat_stream", None
        )
        if gen_fn:
            if blocks and not _takes_attachments(gen_fn):
                await _say_files_not_sent(f"@{target.name} cannot read attachments")
            kwargs = {"attachments": blocks} if blocks and _takes_attachments(gen_fn) else {}
            try:
                async for chunk in gen_fn(text, **kwargs):  # pylint: disable=not-callable
                    if isinstance(chunk, dict):
                        continue
                    await _chunk_fn(str(chunk))
            finally:
                await _end_fn()
        elif hasattr(target, "process_user_input"):
            if blocks:
                await _say_files_not_sent(f"@{target.name} cannot read attachments")
            result = await target.process_user_input(text)  # pyright: ignore[reportAttributeAccessIssue]
            await reply_fn(str(result))
            await _end_fn()
        else:
            # Agents that only speak via handle_task/TASK+RESULT message passing:
            # - catalog-agent (no LLM)
            # - dynamic agents (generated code, timeseries-collector, etc.)
            # - manual-agent (fallback if chat() not present)
            #
            # Strategy: call handle_message() directly, with a reply slot of the
            # registry as the address the RESULT is sent to.

            # manual-agent: prefer its native chat() — it handles plain text well
            if hasattr(target, "chat") and not hasattr(target, "_fn_handle_task"):
                if blocks:
                    await _say_files_not_sent(f"@{target.name} cannot read attachments")
                try:
                    result = await target.chat(text)  # pyright: ignore[reportAttributeAccessIssue]
                    await reply_fn(str(result))
                except Exception as exc:
                    logger.exception("[io-gateway] chat() on %s failed", target.name)
                    await reply_fn(f"[error] {target.name}: {exc}")
                await _end_fn()
                return

            # All other message-passing agents: a chat turn is not an actor, so the
            # reply address is a slot of the registry, which a RESULT sent to it
            # settles; the slot is gone when the turn ends, however it ends.
            if blocks:
                await _say_files_not_sent(f"@{target.name} cannot read attachments")
            # The target came out of this registry, so it is there; said for the
            # type checker, which only knows the attribute may be unset.
            registry = runtime.registry
            if registry is None:
                await reply_fn("[error] registry not available")
                await _end_fn()
                return
            try:
                async with registry.reply_slot() as (slot_id, reply):
                    msg = Message(
                        type=MessageType.TASK,
                        sender_id=slot_id,
                        reply_to=slot_id,
                        payload=task_payload(text),
                    )
                    await target.handle_message(msg)
                    payload = await asyncio.wait_for(reply, timeout=150.0)

                text_out = reply_text(payload)
                if (
                    isinstance(payload, dict)
                    and "agents" in payload
                    and isinstance(payload["agents"], list)
                ):
                    text_out = format_catalog_agents_response(payload)

                await reply_fn(text_out)

            except asyncio.TimeoutError:
                await reply_fn(f"[error] @{target_name} did not reply within 150s.")
            except Exception as exc:
                logger.exception("[io-gateway] task dispatch to %s failed", target.name)
                await reply_fn(f"[error] {target.name}: {exc}")
            finally:
                await _end_fn()


# ── REST chat endpoints ────────────────────────────────────────────────────


async def rest_chat_handler(request: web.Request) -> Response:
    """POST /chat — fire-and-forget a message to a named agent."""
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    message = data.get("message", "").strip()
    agent_name = data.get("agent_name", MAIN_ACTOR_NAME)
    if not message:
        return web.json_response({"error": "message required"}, status=400)
    target = runtime.registry.find_by_name(agent_name)
    if target is None:
        return web.json_response({"error": f"agent '{agent_name}' not found"}, status=404)
    # As above: route to the named agent, since route_chat would otherwise
    # default to main when the message carries no @mention.
    routed = message if message.startswith(("@", "/")) else f"@{target.name} {message}"
    track_chat_task(asyncio.create_task(route_chat(routed, discard_reply)))
    return web.json_response({"status": "sent", "agent": agent_name})


async def rest_chat_stop_handler(request: web.Request | None) -> Response:
    """POST /chat/stop — cancel any in-flight generation. No request body needed.

    Cancels the in-process generation task(s); the cancelled stream finalizes and
    posts "⏹ Stopped." over the WebSocket. The user-facing confirmation rides the
    usual chat reply path, so the UI needs no extra subscription.
    """
    tasks = [t for t in inflight_chat_tasks if not t.done()]
    for t in tasks:
        t.cancel()

    return web.json_response(
        {
            "status": "stopped",
            "cancelled": len(tasks),
        }
    )

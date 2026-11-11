# SPDX-License-Identifier: Apache-2.0
"""Bringing an agent back from the node that was running it.

A migration home asks the node for the agent's persistent state, and spawns the
agent locally from main's own config for it with that state loaded into it: its
spawn registry entry, or for a library agent the plugin main has installed.

**Only the state is taken from the node, never the program.** The node's message
carries the config it was running too, a library agent's bundle included, but a
node is not trusted to say what main or another node executes: the registry
entry is what main placed there, kept current by `code_refresh` when the node
repaired it, and a bundle is made again from main's own copy of the code for
each node it goes to. So main migrates only an agent it holds a program for. The token decides whether the state is
accepted at all: main mints one per migration it starts, and only a message
quoting it, from the node it was asked of and about the agent it was asked
about, is acted on; it is consumed on use and expires, so a replayed message
spawns nothing.

An agent's blobs -- its models, arrays and bytes -- travel beside its state, on
topics of their own (see `wactorz.core.blob_transfer`). Main sends them with an
agent it places on a node, and receives them with one a node hands back.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import secrets
import sys
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import plugins
from ...config import MIGRATION_MAX_BLOB_BYTES, MIGRATION_MAX_STATE_BYTES
from ...core import topic_bus
from ...core.addresses import MAIN_NODE
from ...core.blob_transfer import (
    INBOX_DIRNAME,
    BlobsTooLarge,
    BlobsUnusable,
    Inbox,
    blob_bytes,
    cannot_travel,
    is_reference,
    json_part,
    pack,
    references,
    send,
    sha256_of_topic,
    to_node_topic,
    unpack,
    wait_for,
)
from ...core.blobs import BLOB_MARK, encoder_named
from ...core.manifests import AgentManifest
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.paths import resolve_state_dir
from ...core.state_snapshot import json_safe, why_it_cannot_travel
from ...core.topics import NODES, node_topic, own_topic_parts
from ...plugins import MODULE_TYPE, is_module_config
from ..mixins import installed_packages
from .addresses import registered_agent
from .spawn_registry import SpawnRegistry, address_of, without_transient_keys
from .spawns import why_a_node_cannot_run, with_code_for

if TYPE_CHECKING:
    from .hosts import MigrationHost, NodeReaders

logger: logging.Logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How long a migration's return token stays usable.
#:
#: A node that never answers would otherwise leave its token in the pending map
#: for the life of the process. Five minutes is far longer than a migration
#: takes and short enough that the map stays small.
TOKEN_TTL_S = 300.0

#: How often to look for migrations that stalled. Half the token lifetime, so a
#: stalled one is recovered within roughly one and a half TTLs rather than
#: waiting for a message that is never coming.
SWEEP_INTERVAL_S = TOKEN_TTL_S / 2

#: How long a blob that arrived and was never used is kept.
INBOX_KEEP_S = 3600.0

#: Identifies the code synthesized for an LLM agent sent out to a node, so it
#: can be dropped when the agent comes back. Hand-written code never has it.
BRIDGE_CODE_MARKER = "Auto-generated LLM bridge"


def library_agent_config(agent_name: str, registry: Any) -> dict[str, Any] | None:
    """The spawn config of a library agent, as a ``type: module`` spawn would state it.

    None when no plugin goes by ``agent_name``, or the one that does was
    registered in code under no import path, which nothing elsewhere could
    name. The running actor's options go with it, those that can be written
    as JSON: a path, a threshold. One starting with an underscore is the
    actor's own working value -- a loaded model -- and is left.
    """
    plugin = plugins.for_name(agent_name)
    if plugin is None or not plugin.target:
        return None
    local = registry.find_by_name(agent_name) if registry else None
    held = getattr(local, "options", None)
    options: dict[str, Any] = {}
    if isinstance(held, dict):
        options, _dropped = json_safe({k: v for k, v in held.items() if not k.startswith("_")})
    return {"name": agent_name, "type": MODULE_TYPE, "target": plugin.target, "options": options}


#: `from_node` for an agent that was running here rather than on a node. The
#: source of a migration is a node name everywhere else, so the sentinel is what
#: tells the completion and rollback paths to act on this process instead of
#: publishing to a node topic that does not exist.
LOCAL_SOURCE = "local"

#: Where the migrations in flight are recorded, so a restart does not drop them.
PENDING_MIGRATIONS_KEY = "_pending_migrations"

#: Entry fields that cannot outlive the process, and so are never written.
#:
#: `local_actor` is the stopped local instance, carried so the ack can purge its
#: persistence through the actor's own API. A restart loses it, and there is
#: nothing to fall back to: `_purge_local_agent_persistence` takes the agent name
#: for its log lines only, and every deletion goes through the actor. So a
#: restored local→remote migration purges through whatever copy startup brought
#: back instead, and leaves the state behind only when there is none — see
#: `_purge_local_source`.
_TRANSIENT_ENTRY_FIELDS = ("local_actor",)

#: A move as the guard against a second one knows it: the agent's name, the
#: place it is leaving and the place it is going, "" for main.
Move = tuple[str, str, str]


def _place(node: Any) -> str:
    """A node name as a place a move leaves or reaches: "" for main, however it was written."""
    text = str(node or "").strip()
    return "" if text.lower() in ("local", "main") else text


def _move_of(pending: Mapping[str, Any]) -> Move:
    """The move a pending return or spawn is part of."""
    return (
        str(pending.get("agent_name") or ""),
        _place(pending.get("from_node")),
        _place(pending.get("target_node")),
    )


class Migration:
    """Agents in flight between main and a node."""

    def __init__(self, host: MigrationHost | None = None, nodes: NodeReaders | None = None) -> None:
        self.host = host
        #: The live node view. Read for "is the target up" and "where is this
        #: agent" — questions the heartbeat table answers and nothing else can.
        self.host_nodes = nodes
        #: return token -> the migration main started and is waiting on.
        self.pending_returns: dict[str, dict[str, Any]] = {}
        #: Migrations waiting for a target node to confirm the spawn started.
        self.pending_spawns: dict[str, dict[str, Any]] = {}
        #: Where blobs from nodes arrive; made when first used.
        self._inbox: Inbox | None = None
        #: State returns waiting on their blobs, held so none is collected.
        self._returns: set[asyncio.Task[None]] = set()
        #: Moves being started, or finished, with no pending entry to show it:
        #: a move asked for until it is sent, and a return or a confirmation
        #: from when its entry is taken until the agent is placed or put back.
        #: Counted, as a return waiting on its blobs holds its move twice.
        self._in_hand: Counter[Move] = Counter()

    @property
    def inbox(self) -> Inbox:
        """Where the blobs a node hands back arrive, before they are used."""
        if self._inbox is None:
            self._inbox = Inbox(
                Path(resolve_state_dir()) / INBOX_DIRNAME, MIGRATION_MAX_BLOB_BYTES or sys.maxsize
            )
        return self._inbox

    def _save_pending(self) -> None:
        """Record both maps. Called after every change to either.

        A failure here is logged and swallowed: losing the record degrades a
        restart back to the behaviour that existed before this, and that is not
        a reason to fail the migration currently in flight.
        """
        if self.host is None:
            return
        try:
            self.host.persist(
                PENDING_MIGRATIONS_KEY,
                {
                    "returns": self.pending_returns,
                    "spawns": {
                        token: {
                            key: value
                            for key, value in entry.items()
                            if key not in _TRANSIENT_ENTRY_FIELDS
                        }
                        for token, entry in self.pending_spawns.items()
                    },
                },
            )
        except Exception:
            logger.exception("[main] Could not record the migrations in flight")

    def restore(self) -> None:
        """Take back the migrations that were in flight when the process stopped.

        Without this a restart drops the token, and the two halves of the
        choreography both go quiet: an ack arriving afterwards finds nothing
        waiting and is ignored, and no rollback ever fires. The agent comes back
        locally from the spawn registry while the target may also be running it —
        the duplicate the confirmation work exists to prevent, reached through
        the one door it left open.

        Nothing is resolved here. The sweep already runs on a timer and already
        knows the rules, so an entry that is past its TTL is handled on its first
        pass rather than needing a second implementation at startup.
        """
        if self.host is None:
            return
        try:
            stored = self.host.recall(PENDING_MIGRATIONS_KEY) or {}
        except Exception:
            logger.exception("[main] Could not read back the migrations in flight")
            return
        if not isinstance(stored, dict):
            return
        returns = stored.get("returns") or {}
        spawns = stored.get("spawns") or {}
        if isinstance(returns, dict):
            self.pending_returns.update(returns)
        if isinstance(spawns, dict):
            self.pending_spawns.update(spawns)
        if self.pending_returns or self.pending_spawns:
            logger.info(
                "[main] Took back %s migration(s) in flight: %s waiting to come back, "
                "%s waiting to be confirmed",
                len(self.pending_returns) + len(self.pending_spawns),
                len(self.pending_returns),
                len(self.pending_spawns),
            )

    async def state_return_listener(self) -> None:
        """Follow `nodes/+/state_return` until the actor stops."""
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "migration"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe(node_topic("+", "state_return"), qos=1)
                    await client.subscribe(node_topic("+", "spawn_ack"), qos=1)
                    await client.subscribe(node_topic("+", "blob_return", "+"), qos=1)
                    logger.info("[main] Subscribed to state_return topics.")
                    last_error = None
                    async for message in client.messages:
                        topic = str(message.topic)
                        if "/blob_return/" in topic:
                            await self._accept_blob(topic, message.payload)
                            continue
                        if topic.endswith("/spawn_ack"):
                            await self.receive_spawn_ack(topic, message.payload)
                        else:
                            await self.receive_state_return(topic, message.payload)
                        await self.expire_pending_spawns()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] state_return listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] state_return listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _accept_blob(self, topic: str, payload: Any) -> None:
        sha = sha256_of_topic(topic)
        if sha is None or not isinstance(payload, (bytes, bytearray)):
            return
        try:
            await self.inbox.accept(sha, bytes(payload))
        except OSError as exc:
            logger.warning("[main] Could not keep a blob that arrived: %s", exc)

    async def receive_state_return(self, topic: str, payload: bytes | None) -> None:
        """Take an agent back from the node that was running it.

        Only the state in this message is used. The agent is placed from main's
        own registry entry, so what runs is the program main sent out, not one
        the node supplies. The token decides whether the state is accepted:
        main mints one per migration it starts, and only a message quoting it is
        acted on. Consumed on use, so a replay finds nothing waiting.
        """
        if not payload:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire_tokens()
        token = data.get("return_token", "")
        if not token or token not in self.pending_returns:
            logger.warning(
                "[main] state_return with unknown/expired token %r from %s — ignoring",
                f"{token[:8]}…",
                topic,
            )
            return

        started = self.pending_returns[token]
        if _misaddressed(topic, data, started["agent_name"], started["from_node"]):
            return
        self.pending_returns.pop(token)
        self._save_pending()
        # Still under way until the agent is placed, though its entry is gone.
        with self._holding(_move_of(started)):
            await self._take_returned(started, data)

    async def _take_returned(self, started: dict[str, Any], data: dict[str, Any]) -> None:
        """Place an agent its node handed back, or leave it there if the node refused."""
        agent_name = started["agent_name"]
        from_node = started["from_node"]
        if data.get("refused"):
            # The node kept the agent running and said why on `migrate_result`
            # too, which is what the user is told; repeating it here would be a
            # second notice for one refusal.
            logger.info("[main] %r stays on %r: %s", agent_name, from_node, data.get("refused"))
            return
        state = data.get("state") or {}
        left_behind = [str(k) for k in data.get("state_keys_dropped") or []]

        cfg = self._registry_config(agent_name, from_node, data.get("config"))
        if cfg is None:
            return

        if isinstance(state, dict) and references(state):
            # Off the listener: the blobs arrive through it, and it would
            # otherwise be waiting for itself. The task holds the move until
            # it ends, from before it is started.
            self._in_hand[_move_of(started)] += 1
            task = asyncio.create_task(
                self._place_returned_or_restart(agent_name, started, cfg, state, left_behind)
            )
            self._returns.add(task)
            task.add_done_callback(self._returns.discard)
            return
        await self._place_returned(agent_name, started, cfg, state, left_behind, blobs=False)

    async def _ready_for(
        self, config: dict[str, Any], target_node: str
    ) -> tuple[dict[str, Any], str | None]:
        """``config`` as ``target_node`` will be sent it, and why it cannot be, or None.

        A library agent's code goes as the bundle main makes from its own copy,
        every model the agent names is pinned, and the packages it installs are
        cleared, which may ask the operator. Done before the agent stops
        wherever it is: a node handed a config with no program starts an empty
        agent and confirms it, and a model that is not stored would otherwise be
        found by the spawn, with the agent running nowhere.
        """
        if self.host is None or self.host_nodes is None:
            return config, "main is not running"
        targets = self.host_nodes.targets_of(target_node)
        config, problem = await with_code_for(
            config, targets, self.host_nodes.takes_bundles(target_node)
        )
        unrunnable = problem or why_a_node_cannot_run(config, targets)
        if unrunnable:
            return config, unrunnable
        packages = installed_packages(config)
        if packages:
            refusal = await self.host.spawns._packages_cleared(
                str(config.get("name", "")), packages
            )
            if refusal:
                return config, refusal
        return await self.host.spawns.pinned_for(config, target_node)

    def _registry_config(
        self, agent_name: str, from_node: str, returned: Any
    ) -> dict[str, Any] | None:
        """The config a returning agent is placed from: main's own.

        Its spawn registry entry, or for a library agent started with the
        system, the config main makes from the plugin it has installed.
        ``returned`` is the config the node sent back, its program or bundle
        included. It is compared, never used: a node that differs may have
        repaired the program before the `code_refresh` exchange caught up, or
        may be trying to have main run a program of its choosing, and from here
        the two look the same. The repair is lost either way, which the notice
        says.

        None when main holds neither. `migrate_agent` refuses such a move before
        anything stops, so this is an entry that went away while the agent was
        in flight; the agent is left stopped on its node, with its state.
        """
        if self.host is None:
            return None
        entry = _entry_on(self.host._get_spawn_registry(), agent_name, from_node) or (
            library_agent_config(agent_name, self.host._registry)
        )
        if not entry:
            logger.warning(
                "[main] %r came back from %r, but main holds no program for it",
                agent_name,
                from_node,
            )
            self._announce(
                f"'{agent_name}' came back from '{from_node}', but main holds no program for it, "
                f"so it was not started anywhere. Its state is still on '{from_node}'.",
                "warning",
            )
            return None
        # What main sent: an LLM agent's entry holds no code, and the node was
        # given bridge code synthesized from it, which is rebuilt the same way.
        sent = self.host._inject_llm_bridge_code(dict(entry)).get("code")
        if _another_program(returned, sent) or _another_bundle(returned, entry):
            logger.warning(
                "[main] %r came back from %r with a program main did not send — discarded",
                agent_name,
                from_node,
            )
            self._announce(
                f"'{agent_name}' came back from '{from_node}' with a program that differs from "
                "the one main holds. Main's copy is used; the node's was discarded.",
                "warning",
            )
        return dict(entry)

    async def _place_returned_or_restart(
        self,
        agent_name: str,
        started: dict[str, Any],
        cfg: dict[str, Any],
        state: dict[str, Any],
        left_behind: list[str],
    ) -> None:
        """Place a handed-back agent whose blobs are on their way, in a task of its own.

        Nobody awaits the task, so a failure there would end it with the agent
        stopped on its node and only a line in the log. Started again there
        instead, as when its blobs do not arrive -- unless it got as far as
        being placed, when starting it there as well would make two.
        """
        try:
            await self._place_returned(agent_name, started, cfg, state, left_behind, blobs=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[main] Placing %r after its hand-back failed", agent_name)
            if not self._placed(agent_name):
                await self._restart_on_source(
                    agent_name, started.get("from_node", "?"), cfg, f"{type(exc).__name__}: {exc}"
                )
        finally:
            self._release(_move_of(started))

    def moving_to(self, agent_name: str) -> str:
        """The node a move under way is starting ``agent_name`` on, or "" for none.

        The spawn registry names the target only once it confirms, and the
        agent speaks from there before that: its manifest and its first replies.
        """
        return next(
            (
                str(p.get("target_node") or "")
                for p in self.pending_spawns.values()
                if p.get("agent_name") == agent_name
            ),
            "",
        )

    def move_under_way(self, move: Move) -> Move | None:
        """The move under way that ``move`` would cross, or None.

        Under way from the moment a move is asked for until the agent is
        confirmed where it went or put back where it was. Two moves cross when
        they are of agents of one name and share a place: one leaving it or
        arriving there while the other does. Agents of one name on two other
        hosts move apart.
        """
        name, leaving, going = move
        under_way = [
            *(_move_of(p) for p in self.pending_returns.values()),
            *(_move_of(p) for p in self.pending_spawns.values()),
            *(m for m, held in self._in_hand.items() if held > 0),
        ]
        return next(
            (
                other
                for other in under_way
                if other[0] == name and {other[1], other[2]} & {leaving, going}
            ),
            None,
        )

    @contextlib.contextmanager
    def _holding(self, move: Move) -> Iterator[None]:
        """Count ``move`` as under way while the block runs."""
        self._in_hand[move] += 1
        try:
            yield
        finally:
            self._release(move)

    def _release(self, move: Move) -> None:
        self._in_hand[move] -= 1
        if self._in_hand[move] <= 0:
            del self._in_hand[move]

    def _placed(self, agent_name: str) -> bool:
        """Whether a handed-back agent is running here, or waiting on a target to confirm it.

        Either way it has a copy beyond the stopped one on its source, and the
        source must not be started as well: a target that never confirms is
        rolled back by `expire_pending_spawns`, which starts the source then.
        """
        registry = getattr(self.host, "_registry", None)
        if registry is not None and registry.find_by_name(agent_name) is not None:
            return True
        return any(p.get("agent_name") == agent_name for p in self.pending_spawns.values())

    async def _place_returned(
        self,
        agent_name: str,
        started: dict[str, Any],
        cfg: dict[str, Any],
        state: dict[str, Any],
        left_behind: list[str],
        *,
        blobs: bool,
    ) -> None:
        """Put a handed-back agent where it was going: here, or on another node."""
        from_node = started.get("from_node", "?")
        target_node = started.get("target_node", "")
        if target_node:
            # Sent on as a move from main is: the bundle main makes from its
            # own copy of the code, never the one the node handed back.
            ready, problem = await self._ready_for(cfg, target_node)
            if problem:
                await self._restart_on_source(agent_name, from_node, cfg, problem)
                return
            cfg = ready
        # A node does not send main, or through main another node, what runs
        # code when it loads; one that sent it anyway has it left behind here,
        # before anything waits for it to arrive.
        refused = [key for key, value in state.items() if is_reference(value) and _runs_code(value)]
        if refused:
            state = {k: v for k, v in state.items() if k not in refused}
            left_behind = [*left_behind, *refused]
            blobs = bool(references(state))
        received: dict[str, Path] = {}
        if blobs:
            try:
                received = await self._receive_blobs(state)
            except BlobsUnusable as exc:
                await self._restart_on_source(agent_name, from_node, cfg, str(exc))
                return

        if target_node:
            await self._place_on_target(
                agent_name, from_node, target_node, cfg, state, left_behind, received
            )
            return
        if received:
            try:
                state, _ = await asyncio.to_thread(unpack, state, received, code_allowed=False)
            except BlobsUnusable as exc:
                await self._restart_on_source(agent_name, from_node, cfg, str(exc))
                return
            finally:
                await self.inbox.release(received)
        if await self._respawn_locally(agent_name, from_node, cfg, state, left_behind):
            # Local again, and confirmed: the source may now drop its copy, and
            # its desired state must stop listing it. That list is retained and
            # read again whenever the node reconnects, so an agent left in it
            # comes back there beside this one.
            await self.update_desired_state(from_node, remove_name=agent_name)
            await self._tell_source_to_delete(agent_name, from_node)

    async def _receive_blobs(self, state: dict[str, Any]) -> dict[str, Path]:
        """The files of the blobs ``state`` refers to, once they have all arrived."""
        total = blob_bytes(state)
        if MIGRATION_MAX_BLOB_BYTES and total > MIGRATION_MAX_BLOB_BYTES:
            raise BlobsTooLarge(total, MIGRATION_MAX_BLOB_BYTES)
        shas = {ref["sha256"] for ref in references(state).values()}
        return await self.inbox.wait_for(shas, wait_for(total))

    async def _restart_on_source(
        self, agent_name: str, from_node: str, cfg: dict[str, Any], reason: str
    ) -> None:
        """Start a handed-back agent again where it was, when it cannot be placed.

        The node stopped it and kept its state, so starting it there again
        loses nothing.
        """
        if self.host is None:
            return
        restored = {k: v for k, v in cfg.items() if k not in ("_initial_state", "replace")}
        restored["name"] = agent_name
        restored["node"] = from_node
        await self.host._spawn_remote(restored, from_node, save=True, restoring=True)
        self._announce(
            f"Migration of '{agent_name}' from '{from_node}' failed: {reason}. "
            f"It is running on '{from_node}' again.",
            "warning",
        )

    async def _send_blobs(self, blobs: Mapping[str, bytes], target_node: str) -> None:
        """Send blobs to a node, on topics of their own."""
        if self.host is None or not blobs:
            return
        publish = functools.partial(self.host._mqtt_publish, qos=1)
        await send(blobs, functools.partial(to_node_topic, target_node), publish)

    async def _place_on_target(
        self,
        agent_name: str,
        from_node: str,
        target_node: str,
        cfg: dict[str, Any],
        state: dict[str, Any],
        left_behind: list[str] | None = None,
        received: Mapping[str, Path] | None = None,
    ) -> None:
        """Spawn a returned agent on another node, and wait to be told it started.

        The source still holds its state file: nothing is deleted until the
        target confirms, so a migration that fails here leaves the agent exactly
        where it was rather than between two nodes. That file is also why the
        pending entry keeps the config without the snapshot: a rollback
        restarts the agent from it, and only the spawn message needs the copy.
        """
        if self.host is None:
            return
        blobs = await asyncio.to_thread(_read_all, received or {}, state)
        if received:
            await self.inbox.release(received)
        token = secrets.token_hex(8)
        config = dict(cfg)
        config["name"] = agent_name
        config["node"] = target_node
        config.pop("replace", None)
        if state:
            config["_initial_state"] = state
        config["_migration_token"] = token

        self.pending_spawns[token] = {
            "agent_name": agent_name,
            "from_node": from_node,
            "target_node": target_node,
            "config": without_transient_keys(config),
            "left_behind": list(left_behind or []),
            "started_at": time.time(),
            "ttl": _ttl(sum(len(data) for data in blobs.values())),
        }
        self._save_pending()
        await self.host._spawn_remote(config, target_node, save=False)
        await self._send_blobs(blobs, target_node)
        logger.info(
            "[%s] Placed %r on %r; waiting for it to confirm it started",
            self.host.name,
            agent_name,
            target_node,
        )

    async def receive_spawn_ack(self, topic: str, payload: bytes | None) -> None:
        """A target node confirms an agent it was handed is running, or says it is not.

        Only a confirmation completes the migration: the registry moves, both
        nodes' desired state is rewritten, and the source is told to drop its
        copy. An answer carrying ``refused`` puts the agent back on its source
        now, as the sweep would once the token ran out.
        """
        if not payload or self.host is None:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire_tokens()
        token = data.get("migration_token", "")
        pending = self.pending_spawns.get(token) if token else None
        if pending is None:
            logger.debug("[main] spawn_ack with unknown/expired token from %s — ignoring", topic)
            return
        if _misaddressed(topic, data, pending["agent_name"], pending["target_node"]):
            return
        self.pending_spawns.pop(token)
        self._save_pending()
        # Still under way until it is committed or put back, though its entry is gone.
        with self._holding(_move_of(pending)):
            await self._confirmed(pending, data)

    async def _confirmed(self, pending: dict[str, Any], data: dict[str, Any]) -> None:
        """Commit a move its target confirmed, or put the agent back if the target refused it."""
        if self.host is None:
            return
        if data.get("refused"):
            await self._put_back(pending, str(data["refused"]))
            return

        agent_name = pending["agent_name"]
        from_node = pending["from_node"]
        target_node = pending["target_node"]
        config = pending["config"]

        committed = {k: v for k, v in config.items() if not k.startswith("_migration")}
        self.host._save_to_spawn_registry(committed)
        if from_node != LOCAL_SOURCE:
            # There is no `nodes/local/desired_state`: a local source is this
            # process, and dropping the agent from it is the purge below.
            await self.update_desired_state(from_node, remove_name=agent_name)
        await self.update_desired_state(target_node, committed)
        await self._release_source(pending)
        self._announce(
            f"Migration of '{agent_name}' from '{from_node}' → '{target_node}' complete."
            + _left_behind_note(pending.get("left_behind")),
            "warning" if pending.get("left_behind") else "info",
        )

    async def _release_source(self, pending: dict[str, Any]) -> None:
        """Drop the source's copy, now that the agent is confirmed elsewhere.

        Where that copy lives is the only difference between the legs: a node
        is told to delete, and a local source is purged here.
        """
        from_node = pending.get("from_node", "")
        if from_node == LOCAL_SOURCE:
            await self._purge_local_source(pending)
            return
        await self._tell_source_to_delete(pending["agent_name"], from_node)

    async def _purge_local_source(self, pending: dict[str, Any]) -> None:
        """Wipe what the local instance persisted, once the target has it.

        Held until the ack for the reason a node's copy is: until the target
        says it started, this is the only intact copy of the agent. Purged at
        all because the snapshot has moved on — left behind, it would merge
        with the state that comes home on a migrate-back and duplicate
        everything in it.

        The stopped actor is carried in the pending entry rather than looked up
        again: it is unregistered by now, and its own persistence API is what
        knows which backends to clear.
        """
        actor = pending.get("local_actor")
        if self.host is None:
            return
        agent_name = pending.get("agent_name", "?")
        if actor is None:
            # The migration outlived the process that started it, so the
            # instance it stopped is gone — but startup may have brought the
            # agent back. The registry was never moved to the target (that waits
            # for this ack), so restoring spawned agents saw a local agent and
            # restarted it. Left alone, the agent then runs here *and* on the
            # target, for good. Stopped the way `migrate_agent` stopped the
            # original, and then purged through: a stopped actor is exactly what
            # the purge needs, so this also settles the purge instead of skipping.
            registry = self.host._registry
            revived = registry.find_by_name(agent_name) if registry else None
            if revived is None:
                logger.info(
                    "[%s] %r moved to %r across a restart — its local state is left "
                    "in place, and will merge if it ever migrates back",
                    self.host.name,
                    agent_name,
                    pending.get("target_node", "?"),
                )
                return
            try:
                await registry.unregister(revived.actor_id)
                await revived.stop()
            except Exception as exc:
                # Not purged: an instance that failed to stop may still be
                # running, and wiping its state underneath it is worse.
                logger.warning(
                    "[%s] %r is confirmed on %r but the copy restarted here would not stop: %s",
                    self.host.name,
                    agent_name,
                    pending.get("target_node", "?"),
                    exc,
                )
                return
            self.host._agent_manifests.pop(agent_name, None)
            logger.info(
                "[%s] Stopped the copy of %r restarted here — it is confirmed on %r",
                self.host.name,
                agent_name,
                pending.get("target_node", "?"),
            )
            actor = revived
        try:
            await self.host._purge_local_agent_persistence(actor, agent_name)
        except Exception as exc:
            logger.warning(
                "[%s] Could not purge local persistence for %r after it moved to %r: %s",
                self.host.name,
                agent_name,
                pending.get("target_node", "?"),
                exc,
            )

    async def _tell_source_to_delete(self, agent_name: str, from_node: str) -> None:
        """Drop the source's copy, now that the agent is confirmed elsewhere."""
        if self.host is None or not from_node or from_node == LOCAL_SOURCE:
            return
        await self.host._mqtt_publish(
            node_topic(from_node, "stop"),
            {"name": agent_name, "delete": True},
            qos=1,
        )

    async def expire_pending_spawns(self) -> None:
        """Put back any agent whose target never confirmed it started.

        The source was stopped but not deleted, so recovery is a re-spawn there
        rather than anything to reconstruct -- which is what makes "a failed
        migration leaves the system where it started" structural rather than
        something rollback logic has to get right.
        """
        if self.host is None:
            return
        now = time.time()
        for token, pending in list(self.pending_spawns.items()):
            if now - pending.get("started_at", 0) <= pending.get("ttl", TOKEN_TTL_S):
                continue
            self.pending_spawns.pop(token, None)
            self._save_pending()
            with self._holding(_move_of(pending)):
                await self._put_back(pending, None)

    async def _put_back(self, pending: dict[str, Any], refused: str | None) -> None:
        """Restore an agent on its source after its target did not take it.

        ``refused`` is the target's reason when it said the agent did not start,
        and None when it never answered. A restore that fails is said on the
        dashboard, naming where the agent's state is kept: silence here is an
        agent running nowhere that nobody is told about, and it must not stop
        the sweep that found it from reaching the next.
        """
        if self.host is None:
            return
        agent_name = pending["agent_name"]
        from_node = pending["from_node"]
        target_node = pending["target_node"]
        try:
            await self._put_back_on_source(pending, refused)
        except Exception as exc:
            logger.exception(
                "[%s] Could not put %r back on %r", self.host.name, agent_name, from_node
            )
            where = (
                "its state is kept in main's state directory"
                if from_node == LOCAL_SOURCE
                else f"its state is kept on '{from_node}'"
            )
            self._announce(
                f"Migration of '{agent_name}' to '{target_node}' failed"
                + (f": {refused}" if refused else "")
                + f", and it could not be started on '{from_node}' again: {exc}. "
                f"It is running nowhere; {where}. Fix the cause and spawn it again.",
                "error",
            )

    async def _put_back_on_source(self, pending: dict[str, Any], refused: str | None) -> None:
        if self.host is None:
            return
        agent_name = pending["agent_name"]
        from_node = pending["from_node"]
        target_node = pending["target_node"]
        logger.warning(
            "[%s] %r %s on %r — putting it back on %r",
            self.host.name,
            agent_name,
            f"did not start ({refused})" if refused else "never confirmed",
            target_node,
            from_node,
        )
        # Clear the target first, best effort. The ack may have been lost
        # rather than never sent, in which case the target is running the
        # agent and putting it back on the source would leave two -- the
        # duplicate this whole choreography exists to avoid. An agent that
        # never started ignores the stop.
        #
        # Its retained topics are kept. An actor id comes from the name, so
        # they are the topics of the copy being restored below: cleared, its
        # manifest reads as that copy withdrawing, and the registry forgets
        # the agent this rollback exists to keep.
        await self.host._mqtt_publish(
            node_topic(target_node, "stop"),
            {"name": agent_name, "delete": True, "keep_topics": True},
            qos=1,
        )
        # The stop is a one-off, but placing the agent added it to the
        # target's desired state, and that message is retained. Left there,
        # the next time the node reboots it reconciles the agent back into
        # existence beside the copy this rollback is restoring, and the two
        # of them carry the same name and diverge from the same state.
        await self.update_desired_state(target_node, remove_name=agent_name)
        if from_node == LOCAL_SOURCE:
            await self._restore_local(pending)
        else:
            restored = {k: v for k, v in pending["config"].items() if k != "_migration_token"}
            restored["node"] = from_node
            await self.host._spawn_remote(restored, from_node, save=True, restoring=True)
        self._announce(
            f"Migration of '{agent_name}' to '{target_node}' failed"
            + (f": {refused}" if refused else "")
            + f" — it is back on '{from_node}'.",
            "warning",
        )

    async def _restore_local(self, pending: dict[str, Any]) -> None:
        """Start the agent here again after its target never confirmed.

        No snapshot is carried back in. The local state was never purged --
        that is what the ack was holding up -- so what is on disk is both
        intact and newer than the copy that was shipped out.
        """
        if self.host is None:
            return
        restored = {
            k: v
            for k, v in pending["config"].items()
            if k not in ("_migration_token", "_initial_state", "node")
        }
        restored["replace"] = True
        await self.host._spawn_from_config(restored, save=True)

    def _expire_tokens(self) -> None:
        """Forget return tokens whose migration never completed.

        Only forgetting them. Putting the agent back is
        :meth:`expire_pending_returns`, which needs to publish and so cannot run
        from the synchronous paths that call this.
        """
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) > TOKEN_TTL_S:
                self.pending_returns.pop(token, None)
                self._save_pending()

    async def expire_pending_returns(self) -> None:
        """Restart an agent whose node was asked to hand it back and never did.

        The source stops the agent before publishing `state_return`, so a source
        that dies in between leaves it stopped and intact but running nowhere.
        Forgetting the token is not enough: the registry still places the agent
        on that node, so the recovery is to spawn it there again.
        """
        if self.host is None:
            return
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) <= TOKEN_TTL_S:
                continue
            self.pending_returns.pop(token, None)
            self._save_pending()
            agent_name = started.get("agent_name", "")
            from_node = started.get("from_node", "")
            if not agent_name or not from_node:
                continue
            config = _entry_on(self.host._get_spawn_registry(), agent_name, from_node)
            if not config:
                logger.warning(
                    "[%s] %r never came back from %r and is not in the registry — "
                    "it cannot be restarted automatically",
                    self.host.name,
                    agent_name,
                    from_node,
                )
                continue
            logger.warning(
                "[%s] %r never came back from %r — restarting it there",
                self.host.name,
                agent_name,
                from_node,
            )
            restored = dict(config)
            restored["node"] = from_node
            with self._holding(_move_of(started)):
                await self.host._spawn_remote(restored, from_node, save=True, restoring=True)
            self._announce(
                f"'{agent_name}' did not come back from '{from_node}' — "
                f"it has been restarted there.",
                "warning",
            )

    async def stalled_migration_watcher(self) -> None:
        """Sweep for stalled migrations on a timer, until the actor stops.

        Sweeps before the first wait, not after it: a migration taken back by
        `restore` may have run out its time while the process was down, and
        waiting a full interval would leave its rollback pending for no reason.
        """
        while self.host is not None and self.host.state.value not in ("stopped", "failed"):
            try:
                await self.sweep_stalled_migrations()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[main] Migration sweep failed")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def sweep_stalled_migrations(self) -> None:
        """Recover from either leg going quiet.

        Called on a timer rather than when a message arrives: `state_return` and
        `spawn_ack` carry migration traffic and nothing else, so the very
        failure this recovers from -- a node going away mid-migration -- produces
        no message to trigger it. Driven by the message loop alone, the net
        would spring only on the next unrelated migration, which may never come.
        """
        await self.expire_pending_returns()
        await self.expire_pending_spawns()
        await self.inbox.sweep(older_than_s=INBOX_KEEP_S)

    @staticmethod
    def local_spawn_config(
        agent_name: str, cfg: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """The returned config, rewritten to run here.

        The node is dropped because the agent is coming home, and leaving it on
        would send it straight back. `replace` is set so a leftover local
        instance of the same name is swapped rather than colliding.

        An LLM agent loses the code synthesized for it on the way out: locally
        the type routes to the real class and the code is ignored, so keeping it
        would put something misleading in the spawn registry. The marker comment
        identifies it; hand-written code never carries one.
        """
        local_cfg = dict(cfg)
        local_cfg.pop("node", None)
        local_cfg.pop("_initial_state", None)
        if state:
            local_cfg["_initial_state"] = state
        local_cfg["replace"] = True
        local_cfg.setdefault("name", agent_name)
        if local_cfg.get("type") == "llm" and BRIDGE_CODE_MARKER in (local_cfg.get("code") or ""):
            local_cfg.pop("code", None)
        return local_cfg

    async def _respawn_locally(
        self,
        agent_name: str,
        from_node: str,
        cfg: dict[str, Any],
        state: dict[str, Any],
        left_behind: list[str] | None = None,
    ) -> bool:
        """Spawn the returned agent here, and say whether it worked.

        The answer decides whether the source may drop its copy: if this fails,
        the node's stopped agent is the only one left, and deleting it would
        lose the agent. It is started again there instead, unless the failed
        spawn left a copy running here; then the failure is announced, and the
        source keeps its stopped copy.
        """
        host = self.host
        if host is None:
            return False
        local = self.local_spawn_config(agent_name, cfg, state)
        earned = host._restore_earned_trust(agent_name, local)
        logger.info(
            "[main] Received state_return for %r from %r (%s state key(s)) — spawning locally",
            agent_name,
            from_node,
            len(state) if isinstance(state, dict) else 0,
        )
        try:
            await host._spawn_from_config(local, save=True, from_registry=earned)
        except Exception as exc:
            logger.exception("[main] Local re-spawn after state_return failed for %r", agent_name)
            if self._placed(agent_name):
                self._announce(
                    f"Migration of '{agent_name}' from '{from_node}' → local FAILED: {exc}",
                    "warning",
                )
            else:
                await self._restart_on_source(
                    agent_name, from_node, cfg, f"it could not start here: {exc}"
                )
            return False
        self._announce(
            f"Migration of '{agent_name}' from '{from_node}' → local succeeded."
            + _left_behind_note(left_behind),
            "warning" if left_behind else "info",
        )
        return True

    def _announce(self, message: str, severity: str) -> None:
        host = self.host
        if host is None:
            return
        host._queue_notification(
            {
                "_monitor_notification": True,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            }
        )

    async def migrate_agent(
        self, agent_name: str, target_node: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Move a running agent to a different node; see `_migrate_agent`.

        Returns {"success": bool, "message": str}. The move counts as under way
        from its checks until it is sent, so a second one asked meanwhile is
        refused (`move_under_way`); once sent, its pending entry says so.
        """
        held: list[Move] = []
        try:
            return await self._migrate_agent(agent_name, target_node, force=force, held=held)
        finally:
            for move in held:
                self._release(move)

    async def _migrate_agent(
        self, agent_name: str, target_node: str, *, force: bool, held: list[Move]
    ) -> dict[str, Any]:
        """Move a running agent to a different node.

        A move that would lose state is refused before anything stops: keys
        that cannot go with it unless ``force`` is set, and a snapshot over
        ``MIGRATION_MAX_STATE_BYTES`` always. Main checks a local agent itself;
        a node checks its own, from the same two values sent with the request.

        Sources of truth, in priority order:
          1. Spawn registry — has the full config including code; with the
             agent running in main's own process, settles that it is on main.
          2. Agent manifest — has node, description, schemas (no code).
          3. Node heartbeats — minimum: tells us which node the agent runs on.

        Any of them locates the agent, but an agent on a node moves only with a
        registry entry: the node hands back its state through the '@main'
        sentinel, and main places the agent from its own copy of the program,
        never from one the node supplies.

        Returns {"success": bool, "message": str}
        """
        host, nodes = self.host, self.host_nodes
        if host is None or nodes is None:
            return {}
        named, refusal = registered_agent(host, agent_name)
        if refusal:
            return _refused(refusal)
        found = _locate(host, nodes, named)
        # "local", "main" or nothing at all names main as the target.
        target = "" if target_node.strip().lower() in ("", "local", "main") else target_node

        # Before anything waits: two moves asked at once must not both pass.
        move = (found.name, _place(found.node), target)
        refusal = self._refusal_before_holding(found, move)
        if refusal:
            return _refused(refusal)
        self._in_hand[move] += 1
        held.append(move)
        refusal = self._refusal_of_target(host, nodes, found, target)
        if refusal:
            return _refused(refusal)

        if found.node and target:
            return await self._move_between_nodes(host, found, target, force)
        if found.node:
            return await self._bring_home(host, found, force)
        return await self._send_out(host, found, target, force)

    def _refusal_before_holding(self, found: _Found, move: Move) -> str | None:
        """Why the move cannot even be held: nothing to move, another move under way, or nowhere to go."""
        # If we found nothing — not in registry, not in manifest, not heart-
        # beating from any node, not in the local registry — we genuinely
        # can't migrate what doesn't exist.
        if not found.config and not found.manifest and not found.node and not found.local_alive:
            return (
                f"Agent '{found.name}' not found anywhere (no registry "
                f"entry, no manifest, no heartbeat). Nothing to migrate."
            )
        crossed = self.move_under_way(move)
        if crossed is not None:
            # Until a move is confirmed, the registry still places the agent
            # where it left, while it may already run where it is going: a
            # second move would start from the wrong one.
            return (
                f"'{found.name}' is moving from '{crossed[1] or 'local'}' to "
                f"'{crossed[2] or 'local'}'; ask again once that move is done."
            )
        if found.node == move[2]:
            return f"Agent '{found.name}' is already on '{found.node or 'local'}'."
        kept = (found.config or {}).get("actor_id")
        if self.host is not None and self.host._get_spawn_registry().is_stopped(kept):
            # Its node hands over a running agent's state; a stopped one is not
            # there to hand anything over.
            return (
                f"'{found.name}' is stopped on '{found.node}'. Start it there first "
                f"(/start {found.name}@{found.node}), then move it."
            )
        return None

    def _refusal_of_target(
        self, host: MigrationHost, nodes: NodeReaders, found: _Found, target: str
    ) -> str | None:
        """Why the agent cannot go to ``target`` ("" for main), checked before anything stops."""
        where = found.node or "local"
        if found.node and not found.config:
            # Wherever it goes, it is placed from main's registry entry; the
            # node supplies only its state. Refused now, while it still runs.
            return (
                f"Cannot migrate '{found.name}': main holds no program for it "
                f"(it is not in the spawn registry), and a node's own copy is not run "
                f"elsewhere. It stays on '{found.node}'."
            )
        occupied = self._occupied(
            found.name, target, str((found.config or {}).get("actor_id") or "")
        )
        if occupied:
            return f"Cannot migrate '{found.name}': {occupied} It stays on '{where}'."
        if not target:
            return None
        # The target must exist and be online BEFORE anything destructive.
        # Otherwise a typo'd or offline target would make the agent vanish: the
        # source would stop it (deleting its state file) and publish the
        # hand-off to a node topic nobody is listening on. Refused up front,
        # the agent keeps running where it is.
        if not host._node_is_online(target):
            return (
                f"Cannot migrate '{found.name}': {_why_offline(host, nodes, target)} "
                f"Migration aborted — the agent stays on '{where}'."
            )
        mismatch = host._node_version_mismatch(target)
        unheard = host._node_refuses_tasks(target)
        mismatch = mismatch or (f"{unheard}." if unheard else None)
        if mismatch:
            # Same footing as an offline target: nothing has moved yet, so the
            # agent simply stays where it is.
            return (
                f"Cannot migrate '{found.name}': {mismatch} "
                f"Migration aborted — the agent stays on '{where}'."
            )
        return None

    async def _move_between_nodes(
        self, host: MigrationHost, found: _Found, target_node: str, force: bool
    ) -> dict[str, Any]:
        """Move an agent from the node it runs on to another node, through main.

        Main needs to update BOTH nodes' desired_state retained messages so
        neither tries to re-spawn the agent in the wrong place after a
        restart: the source must forget the agent, the target must remember it.
        """
        agent_name, current_node = found.name, found.node
        logger.info(
            "[%s] Migrating %r from node %r → %r", host.name, agent_name, current_node, target_node
        )
        # Checked before the source stops it, as a move from main is: the
        # target is sent main's own config, so main can tell now whether
        # the target can take it.
        _ready, unrunnable = await self._ready_for(dict(found.config or {}), target_node)
        if unrunnable:
            return _refused(
                f"Cannot migrate '{agent_name}' to '{target_node}': {unrunnable}. "
                f"It stays on '{current_node}'."
            )
        # Routed through main, in two legs: ask the source to hand the agent
        # back (the `@main` machinery), then place it on the target ourselves.
        # The source never publishes to another node's spawn topic: that would
        # let one node run code on another, and a node holding a signing key
        # refuses a spawn not signed by main.
        await self._ask_for_hand_back(host, agent_name, current_node, force, target_node)
        # Nothing is pre-staged. The registry and both nodes' desired state
        # move only once the target says the agent started -- until then the
        # agent is stopped but intact on the source, which is what a
        # rollback needs.
        msg = (
            f"Migrating '{agent_name}' from '{current_node}' → '{target_node}' "
            f"(via main; waiting for the source to hand it over)."
        )
        logger.info("[%s] %s", host.name, msg)
        return {"success": True, "message": msg}

    async def _bring_home(self, host: MigrationHost, found: _Found, force: bool) -> dict[str, Any]:
        """Move an agent from the node it runs on back to main.

        Always through the '@main' sentinel, so the node ships the agent's
        persistent state (conversation history, counters, calibration, etc.)
        back to main BEFORE the agent is stopped. Stopping it and spawning it
        here again from the registry entry would be one round-trip quicker,
        and would lose everything the agent accumulated: an LLM agent its
        whole history. The state_return listener handles the spawn once the
        node replies.
        """
        agent_name, current_node = found.name, found.node
        logger.info(
            "[%s] Migrating %r from node %r → local (via @main sentinel)",
            host.name,
            agent_name,
            current_node,
        )
        await self._ask_for_hand_back(host, agent_name, current_node, force)
        msg = (
            f"Migration of '{agent_name}' from '{current_node}' → local "
            f"initiated (waiting for state from remote node)."
        )
        logger.info("[%s] %s", host.name, msg)
        return {"success": True, "message": msg}

    async def _ask_for_hand_back(
        self,
        host: MigrationHost,
        agent_name: str,
        from_node: str,
        force: bool,
        target_node: str | None = None,
    ) -> None:
        """Ask ``from_node`` to hand ``agent_name`` back, under a token that marks the reply as this move's.

        The pending entry names the node the agent then goes to, when it is not main.
        """
        return_token = secrets.token_hex(8)
        self.pending_returns[return_token] = {
            "agent_name": agent_name,
            "from_node": from_node,
            **({"target_node": target_node} if target_node is not None else {}),
            "started_at": time.time(),
        }
        self._save_pending()
        await host._mqtt_publish(
            node_topic(from_node, "migrate"),
            self._hand_back_request(agent_name, return_token, force),
            qos=1,
        )

    async def _send_out(
        self, host: MigrationHost, found: _Found, target_node: str, force: bool
    ) -> dict[str, Any]:
        """Move an agent running here to ``target_node``, with its state and live topic contract."""
        agent_name = found.name
        # Requires the agent to be running locally. If it isn't, the
        # spawn-registry config would also be useless (no code shipped
        # over MQTT) so error early.
        if not found.local_alive and not found.have_code:
            return _refused(
                f"Agent '{agent_name}' not running locally and no config in registry — cannot migrate."
            )
        # Before anything stops: a node handed a config with no program starts
        # an empty agent and confirms it, and the migration would then purge
        # the only copy that worked.
        config, unrunnable = await self._ready_for(dict(found.config or {}), target_node)
        if unrunnable:
            return _refused(
                f"Cannot migrate '{agent_name}' to '{target_node}': {unrunnable}. "
                f"It stays on 'local'."
            )
        logger.info(
            "[%s] Migrating LOCAL agent %r → remote node %r", host.name, agent_name, target_node
        )

        local = host._registry.find_by_name(agent_name) if host._registry else None
        # Asked while the agent still runs, so a refusal stops nothing. The
        # snapshot that is shipped is taken again after the stop, below.
        before = self._local_values(agent_name, local)
        refusal = why_it_cannot_travel(
            json_part(before),
            # A node runs whatever code main sends it, so nothing is held back for that.
            cannot_travel(before, code_allowed=True),
            force=force,
            max_bytes=MIGRATION_MAX_STATE_BYTES,
        )
        if refusal:
            return _refused(f"Cannot migrate '{agent_name}': {refusal} It stays on 'local'.")

        live_contract = _live_contract(host, agent_name)
        stopped = None
        if host._registry and local:
            failed = await _stop_local(host, agent_name, local)
            if failed:
                return _refused(
                    f"Cannot migrate '{agent_name}': it could not be stopped here ({failed}). "
                    "It stays on 'local'."
                )
            stopped = local

        # Taken after the stop: `on_stop` and the save that follows it are the
        # agent's last writes, and a snapshot taken before them would leave
        # them in the local copy that is purged once the target confirms.
        # Encoding a model takes as long as writing it: off the loop. The
        # values are read here, on it, since the database is not the thread's.
        values = self._local_values(agent_name, local)
        packed = await asyncio.to_thread(pack, values, code_allowed=True)
        if packed.left_behind:
            # Only what the agent wrote while stopping can land here: anything
            # earlier was refused above unless the move was forced.
            logger.warning(
                "[%s] Local→remote migrate %r: %s cannot go with it and is left behind",
                host.name,
                agent_name,
                ", ".join(packed.left_behind),
            )
        too_large = why_it_cannot_travel(
            packed.state,
            [],
            force=True,
            max_bytes=MIGRATION_MAX_STATE_BYTES,
            blob_bytes=packed.blob_bytes,
            max_blob_bytes=MIGRATION_MAX_BLOB_BYTES,
        )
        if too_large:
            return await self._keep_local(agent_name, config, too_large)

        new_config = _outgoing_config(config, agent_name, target_node, packed.state, live_contract)
        token = secrets.token_hex(8)
        new_config["_migration_token"] = token
        self.pending_spawns[token] = {
            "agent_name": agent_name,
            "from_node": LOCAL_SOURCE,
            "target_node": target_node,
            # Without the snapshot: a rollback restarts from the local copy,
            # which is kept until the ack, so only the spawn message needs it.
            "config": without_transient_keys(new_config),
            "left_behind": packed.left_behind,
            "local_actor": stopped,
            "started_at": time.time(),
            "ttl": _ttl(packed.blob_bytes),
        }
        self._save_pending()
        # Nothing is committed yet: the registry still places this agent here,
        # so a restart before the ack brings it back rather than losing it.
        await host._spawn_remote(new_config, target_node, save=False)
        await self._send_blobs(packed.blobs, target_node)
        msg = (
            f"Migrating '{agent_name}' from 'local' → '{target_node}' "
            f"(waiting for it to confirm it started)."
        )
        logger.info("[%s] %s", host.name, msg)
        return {"success": True, "message": msg}

    def _occupied(self, agent_name: str, target_node: str, moving_id: str = "") -> str | None:
        """Why ``target_node`` ("" for main) cannot take ``agent_name``: it runs one of the name already.

        A name is unique on a host, so a move there would leave two agents
        answering to one address. Renaming one on the way is not offered: it
        would break every address that names it. What runs under the moving
        agent's own id, ``moving_id``, is that agent -- main keeps its copy
        until a move out is confirmed -- and is not another one.
        """
        if self.host is None:
            return None
        here = self.host._registry.find_by_name(agent_name) if self.host._registry else None
        if here is not None and moving_id and here.actor_id == moving_id:
            here = None
        kept = self.host._get_spawn_registry().at(agent_name, target_node)
        if kept is not None and moving_id and kept["actor_id"] == moving_id:
            kept = None
        listed = (
            bool(target_node)
            and self.host_nodes is not None
            and (agent_name in self.host_nodes.known.get(target_node, {}).get("agents", []))
        )
        if kept is None and not listed and (target_node or here is None):
            return None
        return f"{address_of({'name': agent_name, 'node': target_node})} already runs."

    @staticmethod
    def _hand_back_request(agent_name: str, return_token: str, force: bool) -> dict[str, Any]:
        """What a source node is sent to hand an agent back to main.

        Carries the terms the node checks the state against before it stops
        the agent, so both legs refuse on main's settings. A node older than
        these fields ignores them and ships what it can, naming what it left.
        """
        return {
            "name": agent_name,
            "target_node": "@main",
            "return_token": return_token,
            "force": force,
            "max_state_bytes": MIGRATION_MAX_STATE_BYTES,
            "max_blob_bytes": MIGRATION_MAX_BLOB_BYTES,
        }

    def _local_values(self, agent_name: str, local: Any) -> dict[str, Any]:
        """Everything a local agent has persisted, as it holds it."""
        api = getattr(local, "_persistence_api", None) if local is not None else None
        if api is None:
            return {}
        try:
            return dict(api.all())
        except Exception as exc:
            logger.warning(
                "[%s] Could not snapshot local state for %r: %s",
                self.host.name if self.host else "main",
                agent_name,
                exc,
            )
            return {}

    async def _keep_local(
        self, agent_name: str, config: dict[str, Any] | None, reason: str
    ) -> dict[str, Any]:
        """Start a stopped local agent again, and say why it did not move.

        Reached only when the agent's last writes while stopping took its state
        over the limit, since the size was checked before the stop. Nothing has
        been sent and nothing purged, so starting it from its config brings it
        back with everything it wrote.
        """
        if self.host is None:
            return {}
        message = f"Cannot migrate '{agent_name}': {reason}"
        if config and (config.get("code") or is_module_config(config)):
            await self._restore_local({"config": dict(config)})
            return {"success": False, "message": f"{message} It is running on 'local' again."}
        logger.warning(
            "[%s] %r is stopped and has no config to restart from", self.host.name, agent_name
        )
        return {
            "success": False,
            "message": f"{message} It is stopped on 'local' with its state kept.",
        }

    async def update_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        """Maintain nodes/{node}/desired_state as a retained MQTT message containing
        ALL agents that should run on this node.

        **The list must be complete.** The node starts what it names and stops
        what it leaves out, so a list that is short by accident stops agents
        that should be running. It is built from the whole spawn registry for
        the node, which is read from storage when asked for rather than loaded
        after startup; anything that ever publishes it another way has to be
        just as complete.

        An agent kept stopped is left out, which is what keeps it stopped
        through the node's next reboot. ``new_config`` is listed whatever the
        registry says of it: it is the agent being started now.
        """
        if self.host is None:
            return
        # Build desired state from spawn registry filtered to this node
        agents = {cfg["name"]: cfg for cfg in self.host._get_spawn_registry().running_on(node)}

        # Apply pending change before publishing. Stripped, because this message
        # is retained: the runner reconciles from it on every reboot, and a
        # migration snapshot left in it would be re-applied each time.
        if new_config:
            agents[new_config["name"]] = without_transient_keys(new_config)
        if remove_name:
            agents.pop(remove_name, None)

        # The spawn registry holds CLEAN configs (no synthesized bridge code).
        # But the runner that consumes desired_state on reboot will compile
        # whatever code field it finds and call handle_task() on it. So pass
        # every config through _inject_llm_bridge_code() here, which is a
        # no-op for non-llm-type agents and idempotent if code is already
        # present. Without this, restarting a remote node would silently
        # bring back all LLM agents in a non-functional state — same as the
        # original "no handle_task" bug, just delayed by one reboot.
        wire_agents = [self.host._inject_llm_bridge_code(cfg) for cfg in agents.values()]

        await self.host._mqtt_publish(
            node_topic(node, "desired_state"),
            {"node": node, "agents": wire_agents, "timestamp": time.time()},
            retain=True,
            qos=1,
        )
        logger.info("[%s] Desired state for %r: %s", self.host.name, node, list(agents.keys()))


def _entry_on(registry: SpawnRegistry, agent_name: str, node: str) -> dict[str, Any] | None:
    """The registry entry of ``agent_name`` on ``node``, or else its only entry, or None."""
    return registry.at(agent_name, node) or registry.only(agent_name)


def _misaddressed(topic: str, data: Mapping[str, Any], agent: str, node: str) -> bool:
    """Whether a node's answer quoting a migration's token is not that migration's.

    The token is one migration's: one agent, answered by one node -- the source
    handing it back, or the target confirming it started. An answer naming
    another agent, or arriving on another node's topics, is not the answer to
    it. The topic is the part a node cannot choose: with an account of its own,
    the broker lets it write under its own ``nodes/<node>/`` and nowhere else.

    Asked before the token is spent, and that order is the point. The token
    travels in the clear, so anyone who can read the topics it crosses can
    quote it. Spending it on a misaddressed answer would let them cancel the
    migration: the real answer, a moment later, would find nothing waiting.
    Dropped without spending, the migration stands and the right answer lands.
    """
    parts = own_topic_parts(topic) or []
    sender = parts[1] if len(parts) > 2 and parts[0] == NODES else ""
    if data.get("agent") == agent and sender == node:
        return False
    logger.warning(
        "[main] %s quotes the token for %r on %r but names %r on %r — ignoring",
        topic.rsplit("/", 1)[-1],
        agent,
        node,
        data.get("agent"),
        sender,
    )
    return True


def _left_behind_note(keys: list[str] | None) -> str:
    """The sentence an announcement ends with when some state did not travel."""
    if not keys:
        return ""
    return f" Left behind, since they cannot go with it: {', '.join(sorted(keys))}."


def _ttl(blob_bytes: int) -> float:
    """How long a placement may take to be confirmed, with ``blob_bytes`` of blobs to send."""
    return TOKEN_TTL_S + (wait_for(blob_bytes) if blob_bytes else 0.0)


def _another_bundle(returned: Any, held: dict[str, Any]) -> bool:
    """Whether a node's returned config carries a bundle other than the one main placed.

    A library agent main builds from its plugin holds none in its config: main
    makes the bundle again for each node it sends the agent to, so any bundle
    a node hands back for one is not main's.
    """
    bundle = returned.get("bundle") if isinstance(returned, dict) else None
    if not isinstance(bundle, dict):
        return False
    placed = held.get("bundle")
    return not isinstance(placed, dict) or bundle.get("sha256") != placed.get("sha256")


def _another_program(returned: Any, sent: Any) -> bool:
    """Whether a node's returned config carries a program other than ``sent``."""
    code = returned.get("code") if isinstance(returned, dict) else None
    if not isinstance(code, str) or not code.strip():
        return False
    return code.strip() != str(sent or "").strip()


def _runs_code(reference: dict[str, Any]) -> bool:
    """Whether the blob ``reference`` names is in a format that runs code when it loads.

    A format this side does not know counts as one: nothing says it does not.
    """
    encoder = encoder_named(reference[BLOB_MARK])
    return encoder is None or encoder.runs_code


def _read_all(received: Mapping[str, Path], state: Mapping[str, Any]) -> dict[str, bytes]:
    """The bytes of each blob ``state`` refers to, from the files they arrived in."""
    wanted = {ref["sha256"] for ref in references(state).values()}
    return {sha: path.read_bytes() for sha, path in received.items() if sha in wanted}


@dataclasses.dataclass(frozen=True)
class _Found:
    """Where an agent asked to move is, and what main holds for it."""

    name: str
    #: Main's registry entry for it, or the config a library agent's spawn would have.
    config: dict[str, Any] | None
    have_code: bool
    #: Its manifest, which may be one retained from where it ran before.
    manifest: AgentManifest
    #: Whether it runs in main's own process.
    local_alive: bool
    #: The node it runs on, "" for main.
    node: str


def _refused(message: str) -> dict[str, Any]:
    """The answer to a move that is not made."""
    return {"success": False, "message": message}


def _locate(host: MigrationHost, nodes: NodeReaders, named: Any) -> _Found:
    """Where the agent ``named`` is, from every source main has.

    What main holds first: a registry entry says where the agent is, its empty
    node meaning main itself, and an agent running in this process is here.
    Only an agent main has neither for is looked for in the manifests, then
    the heartbeats: a manifest retained from where the agent ran before would
    otherwise send the move to a node that no longer has it. A name kept in
    more than one place always has its entry here: `registered_agent` takes
    only an address for it.
    """
    agent_name = named.name
    config = named.entry
    if config is None:
        # A library agent started with the system has no entry: its config
        # is what a `type: module` spawn of it would say.
        config = library_agent_config(agent_name, host._registry)
    manifest = host._agent_manifests.get(agent_name, {})
    local_alive = bool(host._registry and host._registry.find_by_name(agent_name))
    current_node = named.node
    placed_by_main = named.entry is not None or local_alive
    if not placed_by_main and manifest:
        current_node = (manifest.get("node") or "").strip()
    if not current_node and not placed_by_main:
        # Last resort — the live heartbeats, which may know a node the spawn
        # registry does not record.
        current_node = nodes.running_agent(agent_name)
    return _Found(
        name=agent_name,
        config=config,
        have_code=bool(config and config.get("code")),
        manifest=manifest,
        local_alive=local_alive,
        node=current_node,
    )


def _why_offline(host: MigrationHost, nodes: NodeReaders, target_node: str) -> str:
    """Why ``target_node`` cannot take a move now, and which nodes could."""
    if target_node in nodes.known:
        reason = f"node '{target_node}' is known but offline (no heartbeat in the last 30s)"
    else:
        reason = f"node '{target_node}' does not exist (never sent a heartbeat)"
    online = host._online_node_names()
    hint = (
        f"Online nodes: {', '.join(online)}." if online else "No remote nodes are currently online."
    )
    return f"{reason}. {hint}"


def _live_contract(host: MigrationHost, agent_name: str) -> dict[str, Any]:
    """The topic contract the agent running here has now, from the TopicBus; empty when there is none.

    The spawn registry only has what was REQUESTED at spawn time, but the
    agent may have learned new topics at runtime (via publish/subscribe
    auto-registration or declare_contract). Shipped without them, those
    topics would be lost across the migration and the remote agent would
    publish a manifest missing them — breaking auto-wiring.
    """
    live_contract: dict[str, Any] = {}
    try:
        bus = topic_bus.get_topic_bus()
        c = bus.registry.at(agent_name, MAIN_NODE) if bus else None
        if c is not None:
            live_contract = {
                "publishes": list(c.publishes or []),
                "subscribes": list(c.subscribes or []),
                "triggers_when": dict(c.triggers_when or {}),
                "produces_schema": dict(c.produces_schema or {}),
                "consumes_schema": dict(c.consumes_schema or {}),
            }
            # observed_samples is a separate field on the contract
            if getattr(c, "observed_samples", None):
                live_contract["observed_samples"] = dict(c.observed_samples)
            logger.info(
                "[%s] Captured live contract for %r: pub=%s sub=%s",
                host.name,
                agent_name,
                live_contract["publishes"],
                live_contract["subscribes"],
            )
    except Exception as exc:
        logger.debug("[%s] Could not capture live contract: %s", host.name, exc)
    return live_contract


async def _stop_local(host: MigrationHost, agent_name: str, local: Any) -> str | None:
    """Stop the agent running here and keep everything it persisted; why it could not be, or None.

    The snapshot taken after this is the copy being shipped out; what the
    agent persisted is what the migration comes back to if the target never
    confirms. Purging it here would put the agent nowhere the moment a spawn
    failed on the node -- the same window the other legs close by deleting
    only on the ack -- so it is purged there instead, in `_purge_local_source`.

    A stop that fails puts the agent back as it was, registered and
    supervised, so the move can stop there: sent on, it would leave a copy
    running here that nothing knows of beside the one started on the target.
    """
    registry = host._registry
    # Released while it stops, so the watchdog does not read the stop as a
    # crash; the spec is kept until the stop has succeeded, which is what lets
    # a failed one be undone.
    supervisor = getattr(registry, "_supervisor_ref", None)
    if supervisor is not None:
        supervisor.release(agent_name)
    try:
        await registry.unregister(local.actor_id)
        await local.stop()
    except Exception as exc:
        logger.warning("[%s] Could not stop local %r: %s", host.name, agent_name, exc)
        await _put_back(registry, supervisor, agent_name, local)
        return str(exc) or type(exc).__name__
    if supervisor is not None:
        # Forgotten now, as main's delete does: a rollback spawns the agent
        # afresh, which gives it a new entry.
        supervisor.drop_supervised(agent_name)
    host._agent_manifests.pop(agent_name, None)
    await asyncio.sleep(0.3)
    return None


async def _put_back(registry: Any, supervisor: Any, agent_name: str, local: Any) -> None:
    """Register ``local`` again and link it back to its supervisor, after a stop that failed.

    Supervised again, a half-stopped agent is one the supervisor restarts from
    its factory, and one the stop never reached goes on as it was.
    """
    try:
        await registry.register(local)
        if supervisor is not None:
            supervisor.resupervise(agent_name, local)
    except Exception:
        logger.exception("[migration] Could not put %r back after its stop failed", agent_name)


def _outgoing_config(
    config: dict[str, Any] | None,
    agent_name: str,
    target_node: str,
    initial_state: Any,
    live_contract: dict[str, Any],
) -> dict[str, Any]:
    """The spawn config an agent going out is sent with: placed on ``target_node``, with its state.

    Live contract values take precedence over stale spawn config values (a
    topic the local agent actually published to is more authoritative than
    what was declared at spawn time), but an empty one overwrites nothing.
    """
    new_config = dict(config or {})
    new_config.setdefault("name", agent_name)
    new_config["node"] = target_node
    new_config.pop("replace", None)
    if initial_state:
        new_config["_initial_state"] = initial_state
    for k, v in live_contract.items():
        if v:
            new_config[k] = v
    return new_config

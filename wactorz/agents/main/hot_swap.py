"""Reloading the agents that follow a model pointer, when it moves.

A spawn that named a model by its bare name, or as ``NAME:active`` or
``NAME:shadow``, is pinned with ``follows`` saying which pointer it meant
(`wactorz.core.artifacts`). When that pointer moves -- a promotion, a rollback,
``train.py --set``, the dashboard -- every running agent whose pin follows it
is re-pinned to where the pointer stands now and reloaded in place: the old
version is served until the new one is in memory, then the two change in one
assignment, and the program is told (`Actor.swap_model`). Nothing restarts, and
an agent that named a version is left alone. A pointer cleared moves nothing:
a pin keeps the version it has while there is nothing to follow.

On main the pointer watcher hands the changes straight here. For an agent on a
node, main sends the node the new pin, signed on a control topic::

    main → nodes/<node>/swap_model   {agent, key, entry, token}
    node → nodes/<node>/swap_ack     {token, agent, key, ok, reason, from, to}

Main sends the pin rather than having the node follow the pointer topic: the
topic says the version and its hash, not the format or the size a node
needs to fetch and check it, and a command to a node is signed where a topic
anything may read is not. The spawn registry and the node's desired state are
updated first, so the node may fetch the version and a reboot runs it.

A swap that landed is an ``action`` node in the provenance graph, joined from
the move that caused it (`wactorz.core.model_swaps`), and one line on the feed.
One that failed -- the version would not load, the node could not fetch it --
is a line on the feed saying what the agent keeps and why, and nothing in the
graph: the agent serves what it served.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...core.artifacts import ArtifactError, ArtifactStore, Pointer, pinned_in
from ...core.model_swaps import record_swap
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.paths import resolve_state_dir
from ...core.persistence.stores import get_db
from ...core.provenance import Provenance

if TYPE_CHECKING:
    from .artifact_service import Change
    from .hosts import SwapHost

logger = logging.getLogger(__name__)

#: How long main waits for a node's answer before forgetting it asked. A node
#: that is away gets the command when it is back, held by the broker; one away
#: longer than this comes back to a desired state that already names the new
#: version, and a swap it then reports is served but not recorded.
TOKEN_TTL_S = 3600.0

#: How long to wait before reconnecting a dropped subscription.
RECONNECT_DELAY_S = 5.0

#: The host name a swap on main is recorded under.
MAIN = "main"


def _reference(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('name')}:{entry.get('version')}"


class HotSwap:
    """Re-pins and reloads the agents whose pins follow a pointer that moved."""

    def __init__(self, host: SwapHost | None = None) -> None:
        self.host = host
        #: token -> the swap main sent a node and is waiting to hear about.
        self.pending: dict[str, dict[str, Any]] = {}

    @property
    def store(self) -> ArtifactStore:
        """The store, read where the state directory is at the time of asking."""
        return ArtifactStore.in_state_dir(resolve_state_dir())

    # ── Following a move ──────────────────────────────────────────────────

    async def follow(self, changes: list[Change]) -> None:
        """Reload every agent whose pin follows a pointer ``changes`` says moved.

        Called by the pointer watcher with what it found, the first look
        included: an agent on main was re-pinned when it was restored and is
        passed over, while one on a node that stayed up through main's
        absence is sent the version everyone else moved to. Agents on main
        are found among those running, so a library agent the system started
        follows as a spawned one does; agents on nodes through their records.
        """
        host = self.host
        if host is None:
            return
        moved = [(name, which, now) for name, which, now, _ in changes if now is not None]
        if not moved:
            return
        self._expire()
        registry = host._get_spawn_registry()
        # Agents on nodes are reached through their records; main does not
        # hold them.
        for config in registry:
            if not str(config.get("node") or "").strip():
                continue
            try:
                entries = pinned_in(config)
            except ArtifactError:
                continue
            for key, entry in entries.items():
                for name, which, now in moved:
                    if entry.get("name") == name and entry.get("follows") == which:
                        await self._guarded(self._follow_on_node, config, key, entry, now)
        # Agents here are the ones running, whatever placed them: a library
        # agent started with the system has no record, and follows all the same.
        for actor in host._registry.all_actors() if host._registry else []:
            artifacts = getattr(actor, "artifacts", None)
            if not isinstance(artifacts, dict):
                continue
            for key, entry in artifacts.items():
                for name, which, now in moved:
                    if entry.get("name") == name and entry.get("follows") == which:
                        config = registry.at(actor.name, "") if registry else None
                        await self._guarded(self._follow_here, actor, config, key, entry, now)

    async def _guarded(self, follow: Any, *args: Any) -> None:
        """Run one agent's follow; its trouble is not the others', which follow too."""
        host = self.host
        try:
            await follow(*args)
        except Exception as exc:
            logger.warning(
                "[%s] cannot follow a moved pointer: %s", host.name if host else "main", exc
            )

    async def _follow_on_node(
        self, config: dict[str, Any], key: str, entry: dict[str, Any], now: Pointer
    ) -> None:
        """Re-pin an agent on a node and send the node its new version."""
        host = self.host
        if host is None:
            return
        agent = str(config["name"])
        node = str(config.get("node") or "").strip()
        try:
            repinned = await asyncio.to_thread(self.store.pin, entry)
        except ArtifactError as exc:
            await self._say(f"{agent} keeps {_reference(entry)} as {key}: {exc}")
            return
        if repinned["sha256"] == entry["sha256"]:
            return
        updated = {**config, "artifacts": {**pinned_in(config), key: repinned}}
        # The registry is what says a node may have a version, and the
        # desired state what a reboot runs: both before the node is asked.
        host._save_to_spawn_registry(updated)
        await host._update_node_desired_state(node, updated)
        token = secrets.token_hex(8)
        self.pending[token] = {
            "agent": agent,
            "node": node,
            "key": key,
            "from": entry.get("version"),
            "pointer": now,
            "sent_at": time.time(),
        }
        # Signed on the way out, because it is a node control topic: see
        # `MainActor._publish_properties`.
        await host._mqtt_publish(
            f"nodes/{node}/swap_model",
            {
                "agent": agent,
                "key": key,
                "entry": repinned,
                "token": token,
                "timestamp": time.time(),
            },
            qos=1,
        )

    async def _follow_here(
        self,
        actor: Any,
        config: dict[str, Any] | None,
        key: str,
        entry: dict[str, Any],
        now: Pointer,
    ) -> None:
        """Swap the version into an agent running on main, and keep its record true."""
        host = self.host
        if host is None:
            return
        try:
            repinned = await asyncio.to_thread(self.store.pin, entry)
        except ArtifactError as exc:
            await self._say(f"{actor.name} keeps {_reference(entry)} as {key}: {exc}")
            return
        recorded = pinned_in(config).get(key, {}).get("sha256") if config is not None else None
        if repinned["sha256"] == entry["sha256"]:
            # Already on the version, as a restore leaves it; its record may lag.
            if config is not None and recorded != repinned["sha256"]:
                host._save_to_spawn_registry(
                    {**config, "artifacts": {**pinned_in(config), key: repinned}}
                )
            return
        try:
            path = await asyncio.to_thread(self.store.readable_path, repinned)
            await actor.swap_model(key, {**repinned, "path": str(path)})
        except Exception as exc:
            await self._say(f"{actor.name} on {MAIN} keeps {_reference(entry)} as {key}: {exc}")
            return
        if config is not None:
            host._save_to_spawn_registry(
                {**config, "artifacts": {**pinned_in(config), key: repinned}}
            )
        await self._landed(secrets.token_hex(8), actor.name, MAIN, key, entry.get("version"), now)

    # ── Hearing from a node ───────────────────────────────────────────────

    async def receive_ack(self, topic: str, payload: bytes | None) -> None:
        """A node says how a swap went; act on it for a token main minted."""
        if self.host is None or not payload:
            return
        try:
            data = json.loads(payload.decode())
        except (UnicodeDecodeError, ValueError):
            return
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "nodes" or not isinstance(data, dict):
            return
        node = parts[1]
        self._expire()
        token = str(data.get("token") or "")
        sent = self.pending.get(token)
        if sent is None or sent["node"] != node:
            logger.debug(
                "[%s] swap_ack from %r with a token main did not mint; ignoring",
                self.host.name,
                node,
            )
            return
        del self.pending[token]
        agent, key = sent["agent"], sent["key"]
        pointer: Pointer = sent["pointer"]
        if data.get("ok") is True:
            await self._landed(token, agent, node, key, sent.get("from"), pointer)
            return
        kept = f"{pointer.name}:{sent.get('from')}"
        await self._say(
            f"{agent} on {node} keeps {kept} as {key}: {data.get('reason') or 'no reason given'}"
        )

    def _expire(self) -> None:
        cutoff = time.time() - TOKEN_TTL_S
        for token in [t for t, sent in self.pending.items() if sent["sent_at"] < cutoff]:
            sent = self.pending.pop(token)
            logger.info(
                "[main] %r on %r never said how its swap of %s went; no longer waiting",
                sent["agent"],
                sent["node"],
                sent["key"],
            )

    # ── Saying and recording ──────────────────────────────────────────────

    async def _landed(
        self, token: str, agent: str, where: str, key: str, from_version: int | None, now: Pointer
    ) -> None:
        was = f" (was {now.name}:{from_version})" if from_version is not None else ""
        await self._say(f"{agent} on {where} now serves {now.reference} as {key}{was}")
        db = get_db()
        if db is None:
            return
        try:
            await asyncio.to_thread(
                record_swap,
                Provenance(db),
                token=token,
                agent=agent,
                host=where,
                key=key,
                name=now.name,
                pointer=now.pointer,
                from_version=from_version,
                to_version=now.version,
                sha256=now.sha256,
                by=now.by,
                at=time.time(),
                moved_at=now.moved_at,
            )
        except Exception as exc:
            logger.warning("cannot record the model swap: %s", exc)

    async def _say(self, message: str) -> None:
        """One line on the feed, as a pointer's move is one."""
        host = self.host
        if host is None:
            return
        logger.info("[%s] %s", host.name, message)
        await host._mqtt_publish(
            f"agents/{host.actor_id}/logs",
            {"type": "model", "message": message, "timestamp": time.time()},
        )

    # ── The subscription ──────────────────────────────────────────────────

    async def listener(self) -> None:
        """Follow the nodes' answers, until the actor stops."""
        host = self.host
        if host is None:
            return
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "swaps"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("nodes/+/swap_ack", qos=1)
                    last_error = None
                    async for message in client.messages:
                        await self.receive_ack(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[%s] swap listener error: %s. Reconnecting in %ss…",
                        host.name,
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                await asyncio.sleep(RECONNECT_DELAY_S)

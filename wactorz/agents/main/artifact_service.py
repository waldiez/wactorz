"""Sending a node the artifacts its agents name, when it asks.

A spawn config may name artifacts -- models kept with `wactorz-artifacts` -- as
``{"artifacts": {"model": "imu-detector:3"}}``. Before main sends such a config
to a node, each name is pinned to the version it means now and the bytes that
version is: name, version, SHA-256, format and size. That pinned form is what
the node is sent and what main keeps, so an agent restarted or moved later runs
the same model, not whatever is newest by then.

The node keeps what it fetches across reboots and asks only for what it lacks:

    node → nodes/<node>/artifact_request   {agent, sha256: [...]}
    main → nodes/<node>/blob/<sha256>      the file, in chunks

**A node gets only what one of its agents names.** Main answers a request for a
hash only when an agent placed on that node -- in the spawn registry, in a
migration waiting for it, or in a spawn sent this run -- names it. The topic is
what says which node asks: with an account of its own, a node writes under its
own ``nodes/<node>/`` and nowhere else. What the node does with the bytes is
check them against the hash its signed spawn named, so a chunk from anyone else
is a file nobody uses.

**Main says where the pointers stand.** Each name's ``active`` and ``shadow``
pointers (see `wactorz.core.artifacts`) are published retained on
``models/<name>/active`` and ``models/<name>/shadow``, the pointer as the store
keeps it, or an empty payload when it is unset, so a subscriber that comes up
late sees the same as one that was there. Main publishes what the index says:
everything at start, and from then on each change it finds, on a short interval,
so a move the CLI makes beside the server reaches the broker without the two
talking. A change found is also one line on the feed, and is handed to the hot
swap (`hot_swap`), which reloads the agents whose pins follow the pointer.
Nobody else publishes there: a pointer is moved through the store, never by
writing to its topic.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from ...config import ARTIFACT_MAX_BYTES
from ...core.artifacts import POINTERS, ArtifactError, ArtifactStore, Pointer, pinned_in
from ...core.blob_transfer import send_file, to_node_topic
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.paths import resolve_state_dir
from ...core.persistence.stores import get_db
from ...core.pointer_moves import record_clear, record_move
from ...core.provenance import Provenance

if TYPE_CHECKING:
    from ...core.persistence.db import WactorzDB
    from .hosts import ArtifactHost

#: One pointer's change: name, pointer, where it stands now, where it stood.
Change = tuple[str, str, "Pointer | None", "Pointer | None"]

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting a dropped subscription.
RECONNECT_DELAY_S = 5.0

#: The most hashes one request may ask for. An agent names a few models; a
#: request for more is not one an agent made.
MAX_REQUESTED = 64

#: How often main reads the index for a pointer moved beside it, by the CLI.
#: The index is one small file, so the read costs nothing worth saving.
POINTER_POLL_S = 5.0


def pointer_topic(name: str, which: str) -> str:
    """The retained topic that says where ``name``'s ``which`` pointer stands."""
    return f"models/{name}/{which}"


def pointer_message(name: str, which: str, now: Pointer | None, was: Pointer | None) -> str:
    """One line for the feed: what a pointer's change means."""
    if now is None:
        gone = f" (was {was.reference})" if was is not None else ""
        return f"{name} has no {which} version now{gone}"
    previous = f" (was {name}:{now.previous})" if now.previous is not None else ""
    reason = f": {now.reason}" if now.reason else ""
    return f"{now.reference} is now {which}{previous}, by {now.by}{reason}"


def record_pointer_changes(
    db: WactorzDB,
    store: ArtifactStore,
    changes: list[Change],
    standing: Mapping[tuple[str, str], Pointer | None],
) -> None:
    """Write each change into the provenance graph, as `wactorz.core.pointer_moves` keeps them.

    A shadow that went because its version became active is the promotion's,
    by whoever promoted; any other clear is by whoever this process saw clear
    it, or unknown.
    """
    prov = Provenance(db)
    for name, which, now, was in changes:
        if now is not None:
            record_move(prov, now)
            continue
        if was is None:
            continue
        active = standing.get((name, "active"))
        if which == "shadow" and active is not None and active.version == was.version:
            record_clear(prov, was, at=active.moved_at, by=active.by, promoted=True)
        else:
            record_clear(prov, was, at=time.time(), by=store.cleared_by(name, which))


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TooLargeForNodeError(ArtifactError):
    """Artifacts over the size a node accepts."""

    def __init__(self, too_large: list[str]) -> None:
        super().__init__(
            f"{', '.join(too_large)} is over the {ARTIFACT_MAX_BYTES}-byte limit for an "
            "artifact sent to a node (WACTORZ_ARTIFACT_MAX_BYTES)"
        )


class ArtifactService:
    """Pins the artifacts a spawn names, and sends them to the nodes that ask."""

    def __init__(
        self,
        host: ArtifactHost | None = None,
        *,
        follow: Callable[[list[Change]], Awaitable[None]] | None = None,
    ) -> None:
        self.host = host
        #: Told each change the watcher finds, after it is published and
        #: recorded: the hot swap, which reloads the agents that follow it.
        self._follow = follow
        #: node -> the hashes main has sent it a spawn naming, this run. The
        #: registry says the same once a spawn is saved; this covers the moment
        #: before, when the node may already be asking.
        self.offered: dict[str, set[str]] = {}
        #: Where each pointer stood when it was last published, by (name,
        #: pointer); None for one that is unset. Empty until the first look,
        #: which publishes everything and says nothing on the feed.
        self.announced: dict[tuple[str, str], Pointer | None] = {}
        self._looked = False

    @property
    def store(self) -> ArtifactStore:
        """The store, read where the state directory is at the time of asking."""
        return ArtifactStore.in_state_dir(resolve_state_dir())

    # ── Pinning ───────────────────────────────────────────────────────────

    async def pin_for_node(self, wanted: Any, node: str) -> dict[str, dict[str, Any]]:
        """``wanted`` -- a config's ``artifacts`` -- pinned, and offered to ``node``.

        Raises `ArtifactError` before anything is sent: an artifact that is not
        stored, or one larger than a node accepts, would leave the agent
        waiting on the node for something that is never coming.
        """
        pinned = await asyncio.to_thread(self.store.pin_all, wanted)
        too_large = [
            f"{entry['name']}:{entry['version']}"
            for entry in pinned.values()
            if ARTIFACT_MAX_BYTES and entry["size"] > ARTIFACT_MAX_BYTES
        ]
        if too_large:
            raise TooLargeForNodeError(too_large)
        self.offered.setdefault(node, set()).update(entry["sha256"] for entry in pinned.values())
        return pinned

    def allowed(self, node: str) -> set[str]:
        """Every hash an agent placed on ``node`` names."""
        allowed = set(self.offered.get(node, ()))
        if self.host is None:
            return allowed
        configs = [
            cfg
            for cfg in self.host._get_spawn_registry().values()
            if str(cfg.get("node", "")).strip() == node
        ]
        configs += [
            pending["config"]
            for pending in self.host.migration.pending_spawns.values()
            if pending.get("target_node") == node and isinstance(pending.get("config"), dict)
        ]
        for cfg in configs:
            try:
                allowed.update(entry["sha256"] for entry in pinned_in(cfg).values())
            except ArtifactError:
                continue
        return allowed

    # ── Answering ─────────────────────────────────────────────────────────

    async def receive_request(self, topic: str, payload: bytes | None) -> None:
        """Send a node what it asked for, of what its agents name."""
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
        asked = data.get("sha256")
        if not isinstance(asked, list):
            return
        wanted = [
            sha for sha in asked[:MAX_REQUESTED] if isinstance(sha, str) and _SHA256.match(sha)
        ]
        allowed = self.allowed(node)
        refused = [sha for sha in wanted if sha not in allowed]
        if refused:
            logger.warning(
                "[%s] %r asked for %s artifact(s) none of its agents names; not sent",
                self.host.name,
                node,
                len(refused),
            )
        publish = functools.partial(self.host._mqtt_publish, qos=1)
        for sha in wanted:
            if sha in refused:
                continue
            path = await asyncio.to_thread(self.store.object_path, sha)
            if path is None:
                logger.warning(
                    "[%s] %r asked for %s…, which is no longer stored",
                    self.host.name,
                    node,
                    sha[:12],
                )
                continue
            logger.info(
                "[%s] Sending %s… to %r for %r", self.host.name, sha[:12], node, data.get("agent")
            )
            await send_file(path, to_node_topic(node, sha), publish)

    # ── Where the pointers stand ──────────────────────────────────────────

    def _standing(self) -> dict[tuple[str, str], Pointer | None]:
        """Every stored name's pointers, set or not, as the store has them now."""
        store = self.store
        standing: dict[tuple[str, str], Pointer | None] = {}
        for name in store.names():
            pointers = store.pointers(name)
            for which in POINTERS:
                standing[(name, which)] = pointers.get(which)
        return standing

    async def publish_pointers(self) -> None:
        """Publish each pointer that moved since the last look, and say so on the feed.

        The first look publishes every pointer of every stored name, set or
        not, so the broker agrees with the store after a restart, and says
        nothing on the feed: nothing moved, main only came back. Called by
        the watcher on its interval, and by whatever moves a pointer through
        main, so that move is heard at once.
        """
        host = self.host
        if host is None:
            return
        standing = await asyncio.to_thread(self._standing)
        first = not self._looked
        changes: list[Change] = []
        for key in sorted(set(standing) | set(self.announced)):
            now = standing.get(key)
            was = self.announced.get(key)
            known = key in self.announced
            if not first and known and now == was:
                continue
            name, which = key
            payload: Any = b"" if now is None else now.payload()
            await host._mqtt_publish(pointer_topic(name, which), payload, retain=True, qos=1)
            # The first look records where each pointer stands, silently: a
            # move made while main was away is written once, one already held
            # is passed over (`wactorz.core.pointer_moves`).
            if now is not None or (known and not first):
                changes.append((name, which, now, was))
            # A model just stored has pointers to publish and nothing to say:
            # none of them moved.
            if first or (not known and now is None):
                continue
            message = pointer_message(name, which, now, was)
            logger.info("[%s] %s", host.name, message)
            await host._mqtt_publish(
                f"agents/{host.actor_id}/logs",
                {
                    "type": "model",
                    "message": message,
                    "model": name,
                    "pointer": which,
                    "timestamp": time.time(),
                },
            )
        self.announced = standing
        self._looked = True
        # After the look is taken, so a database that refuses the record does
        # not make every later look announce the same change again.
        await self._record(changes, standing)
        if changes and self._follow is not None:
            try:
                await self._follow(changes)
            except Exception as exc:
                logger.warning(
                    "[%s] cannot reload the agents that follow a pointer: %s", host.name, exc
                )

    async def _record(
        self, changes: list[Change], standing: Mapping[tuple[str, str], Pointer | None]
    ) -> None:
        """Write ``changes`` into the provenance graph, where main has a database."""
        db = get_db()
        if not changes or db is None:
            return
        try:
            await asyncio.to_thread(record_pointer_changes, db, self.store, changes, standing)
        except Exception as exc:
            logger.warning("cannot record the model pointer moves: %s", exc)

    async def pointer_watcher(self) -> None:
        """Keep the pointer topics true to the index, until the actor stops."""
        host = self.host
        if host is None:
            return
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                await self.publish_pointers()
                last_error = None
            except asyncio.CancelledError:
                break
            except Exception as exc:
                text = str(exc)
                if text != last_error:
                    logger.warning("[%s] cannot publish the model pointers: %s", host.name, exc)
                    last_error = text
            await asyncio.sleep(POINTER_POLL_S)

    # ── The subscription ──────────────────────────────────────────────────

    async def listener(self) -> None:
        """Follow the nodes' requests, until the actor stops."""
        host = self.host
        if host is None:
            return
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "artifacts"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("nodes/+/artifact_request", qos=1)
                    last_error = None
                    async for message in client.messages:
                        await self.receive_request(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[%s] artifact listener error: %s. Reconnecting in %ss…",
                        host.name,
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                await asyncio.sleep(RECONNECT_DELAY_S)

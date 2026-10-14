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
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ...config import ARTIFACT_MAX_BYTES
from ...core.artifacts import ArtifactError, ArtifactStore, pinned_in
from ...core.blob_transfer import send_file, to_node_topic
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from ...core.paths import resolve_state_dir

if TYPE_CHECKING:
    from .hosts import ArtifactHost

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting a dropped subscription.
RECONNECT_DELAY_S = 5.0

#: The most hashes one request may ask for. An agent names a few models; a
#: request for more is not one an agent made.
MAX_REQUESTED = 64

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactService:
    """Pins the artifacts a spawn names, and sends them to the nodes that ask."""

    def __init__(self, host: ArtifactHost | None = None) -> None:
        self.host = host
        #: node -> the hashes main has sent it a spawn naming, this run. The
        #: registry says the same once a spawn is saved; this covers the moment
        #: before, when the node may already be asking.
        self.offered: dict[str, set[str]] = {}

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
            raise ArtifactError(
                f"{', '.join(too_large)} is over the {ARTIFACT_MAX_BYTES}-byte limit for an "
                "artifact sent to a node (WACTORZ_ARTIFACT_MAX_BYTES)"
            )
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

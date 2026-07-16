"""Shared ports between core and extensions: app-state keys and identity protocols.

Core owns these ports; extensions provide the implementations. Living in core
lets any core module (monitor_server, fuseki) depend on the *interface* without a
backwards core→ext import — the swid extension supplies a concrete minter that
structurally satisfies :class:`IdentityMinter`.

Wiring rule: core PROVIDES core keys before ``setup_all()``; extensions PROVIDE
their keys in ``setup()``; everyone CONSUMES shared keys only inside startup
hooks or request handlers (all setups finish before any on_startup hook fires,
so discovery order never matters). Absent providers are normal — read with
``app.get(KEY)``.
"""

from __future__ import annotations

from typing import Protocol

from aiohttp import web


class IdentityRecord(Protocol):
    """Outcome of minting: a readable handle always, a DID when minting is on.

    Structural, so any implementation (e.g. the swid extension's ``MintResult``)
    satisfies it without importing this module or subclassing anything.
    """

    @property
    def did(self) -> str | None: ...

    @property
    def handle(self) -> str: ...


class IdentityMinter(Protocol):
    """Mints (or looks up) a stable identity for an entity; idempotent by handle."""

    async def ensure_did(
        self,
        entity_class: str,
        namespace: str,
        natural_key: str,
        *,
        name: str | None = None,
    ) -> IdentityRecord: ...


# Actor registry — core-provided before setup_all() (may hold None in standalone mode).
ACTOR_REGISTRY: web.AppKey = web.AppKey("actor_registry", object)
# Identity minter — provided by the swid extension's setup(); absent without it.
IDENTITY_MINTER: web.AppKey[IdentityMinter] = web.AppKey("identity_minter", IdentityMinter)
# actor_id → IdentityRecord — filled by the swid extension at startup; optional.
AGENT_IDENTITY: web.AppKey = web.AppKey("agent_identity", dict)

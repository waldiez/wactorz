"""Shared app-state keys and protocols between core and extensions.

Core PROVIDES core keys before setup_all(); extensions PROVIDE their keys in
setup(); everyone CONSUMES shared keys only inside startup hooks or request
handlers (all setups finish before any on_startup hook fires, so discovery
order never matters). Absent providers are normal: read with app.get(KEY).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aiohttp import web


@dataclass(frozen=True)
class IdentityRecord:
    """Entry identity record dataclass."""

    did: str | None
    handle: str


# pylint: disable=too-few-public-methods,unnecessary-ellipsis
class IdentityMinter(Protocol):
    """Identity minter class."""

    async def ensure_did(
        self, entity_class, namespace, natural_key, *, name=None
    ) -> IdentityRecord:
        """Handle minting a did."""
        ...


# core-provided (may hold None)
ACTOR_REGISTRY: web.AppKey = web.AppKey("actor_registry", object)
# identity-ext-provided, optional
IDENTITY_MINTER: web.AppKey = web.AppKey("identity_minter", object)
# identity-ext-provided, optional
AGENT_IDENTITY: web.AppKey = web.AppKey("agent_identity", dict)

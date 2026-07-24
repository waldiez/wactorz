"""Identity ports shared by the identity-related extensions.

App-state keys and structural protocols for did:swid identities: the minter and
the actor→identity map, plus the ``IdentityMinter``/``IdentityRecord`` protocols
an implementation satisfies. They live at the ``wactorz.ext`` level — a neutral
home both the *provider* (``ext/swid``) and *consumers* (``ext/fuseki``) import,
so no extension imports another. ``core.contract`` stays generic (registry only);
``ACTOR_REGISTRY`` is re-exported here for one-import convenience.
"""

from __future__ import annotations

from typing import Protocol

from aiohttp import web

# Generic registry key, re-exported (same object the web layer sets).
from wactorz.core.contract import ACTOR_REGISTRY

__all__ = [
    "ACTOR_REGISTRY",
    "AGENT_IDENTITY",
    "IDENTITY_MINTER",
    "IdentityMinter",
    "IdentityRecord",
]


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


# Identity minter — provided by the swid extension's setup(); absent without it.
IDENTITY_MINTER: web.AppKey[IdentityMinter] = web.AppKey("identity_minter", IdentityMinter)
# actor_id → IdentityRecord — filled by the swid extension at startup; optional.
AGENT_IDENTITY: web.AppKey = web.AppKey("agent_identity", dict)

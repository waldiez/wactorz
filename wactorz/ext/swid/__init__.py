"""did:swid extension — mints and resolves Spatial Web identities.

An additive extension (``wactorz/ext/``): its ``setup`` provides the minting
service and the per-agent identity map on the aiohttp app (via the keys in
:mod:`wactorz.core.contract`), mounts the DIF resolver routes, and registers a
startup hook that mints a DID for every local actor. Nothing here imports
``monitor_server`` — the actor registry arrives through app-state.

Wraps the ``waldiez-swid`` library: the file-backed CEL registry, the minting
service (encrypted keystore + handle index), and the resolver routes. Readable
handles live separately in :mod:`wactorz.core.handles`.
"""

# pylint: disable=import-outside-toplevel
import logging
import os
from pathlib import Path
from typing import cast

from aiohttp.web import Application

from wactorz.core import contract
from wactorz.core.registry import ActorRegistry

from .registry import FileSWIDRegistry
from .routes import swid_routes
from .service import MintResult, SwidMinter

logger = logging.getLogger(__name__)


async def mint_agent_dids(app: Application) -> None:
    """Mint (or look up) a did:swid for every local actor at startup.

    Best-effort: minting failure must never abort server startup. With an empty
    keystore passphrase the minter is disabled and agents get handles only.
    Populates ``app[AGENT_IDENTITY]`` (actor_id → MintResult), which the
    ``/api/actors`` handler reads display-only. Agents spawned *after* startup
    are not minted until restart — minting is idempotent, so wiring this into
    the spawn/heartbeat path is a safe future follow-up (out of scope here).

    Note: linking agent DIDs onto Fuseki graph nodes is deferred to the fuseki
    extraction (the fuseki extension will read AGENT_IDENTITY and link them);
    device/space linkage already happens inside the HA bridge via the minter.
    """
    app_registry = app.get(contract.ACTOR_REGISTRY)
    if app_registry is None:
        return
    registry = cast(ActorRegistry, app_registry)
    app_minter = app[contract.IDENTITY_MINTER]
    if not app_minter:
        return
    minter = cast(SwidMinter, app_minter)
    id_map = app[contract.AGENT_IDENTITY]
    namespace = os.getenv("SWID_NAMESPACE", "home")
    for actor in registry.all_actors():
        try:
            id_map[actor.actor_id] = await minter.ensure_did(
                "agent", namespace, actor.actor_id, name=actor.name
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[swid] minting DID for agent '%s' failed: %s", actor.name, exc)


def setup(app: Application) -> None:
    """Provide the minter + identity map, mount routes, register the mint hook.

    Env: SWID_KEYSTORE_PASSPHRASE / SWID_DATA_DIR / SWID_HSTP_BASE (empty
    passphrase ⇒ minting disabled, handles-only).

    Parameters:
        app: Application. The aiohttp web app.
    """
    data_dir = Path(os.getenv("SWID_DATA_DIR", "data/swid"))
    minter = SwidMinter(
        data_dir,
        os.getenv("SWID_KEYSTORE_PASSPHRASE", ""),
        os.getenv("SWID_HSTP_BASE", "https://hstp.waldiez.io"),
    )
    app[contract.IDENTITY_MINTER] = minter
    app[contract.AGENT_IDENTITY] = {}
    app.add_routes(swid_routes(FileSWIDRegistry(data_dir), minter))
    app.on_startup.append(mint_agent_dids)


__all__ = ["FileSWIDRegistry", "MintResult", "SwidMinter", "setup", "swid_routes"]

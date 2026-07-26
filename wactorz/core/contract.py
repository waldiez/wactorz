"""Shared app-state keys between core and extensions.

Core provides its keys on the aiohttp app before ``setup_all()`` runs;
extensions provide their own keys in ``setup()``. Consume a key with
``app.get(KEY)`` — an absent provider is normal (e.g. the actor registry is
``None`` in standalone MQTT mode), never an error.

Only *generic* keys live here. Extension-specific keys (identity minter, etc.)
belong to the extension that owns them, not to core.
"""

from aiohttp import web

# The live ActorRegistry, set by the web layer before setup_all() so extensions
# can reach it. Holds None in standalone/legacy MQTT mode.
ACTOR_REGISTRY: web.AppKey = web.AppKey("actor_registry", object)

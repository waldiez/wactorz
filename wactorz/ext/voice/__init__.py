"""Choosing which branch listens, speaks and wakes, while running.

The settings themselves are in `settings`, which the extensions they steer
read. This adds the one route that changes them, ``POST /api/voice``, and tells
the browser what each may be set to.

A change has consequences only those extensions know -- a microphone to warn
about, a wake loop to start or stop -- so each registers its own reaction with
`on_change` from its ``setup``. This package calls them without knowing what
they are, and none of them is imported here.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from . import settings

#: What a change is answered with, by whoever registered it. Sync or async.
Reaction = Callable[[], "Awaitable[None] | None"]

_REACTIONS: web.AppKey[list[Reaction]] = web.AppKey("voice_reactions", list)


def setup(app: web.Application) -> None:
    """Register the route that changes the voice settings."""
    app.router.add_post("/api/voice", voice_settings_handler)


def on_change(app: web.Application, reaction: Reaction) -> None:
    """Have `reaction` run after every change ``POST /api/voice`` makes, reset included.

    Kept on the app rather than in this module, so two apps in one process --
    tests build many -- do not answer each other's changes.
    """
    if _REACTIONS not in app:
        app[_REACTIONS] = []
    app[_REACTIONS].append(reaction)


def public_config(_app: web.Application) -> dict[str, Any]:
    """What Settings may offer for each branch.

    The ceiling is the environment's, so the page shows what exists and which of
    it this deployment keeps out of a browser's reach, rather than offering a
    choice the server will refuse.
    """
    return {"allowed": _allowed()}


async def voice_settings_handler(request: web.Request) -> web.Response:
    """POST /api/voice — change which branch listens or speaks, while running.

    The environment says what a deployment starts as; this is how someone tries
    another without restarting. Addresses are not settable here on purpose: they
    name services this process dials, and one a browser can write is one it can
    point at anything reachable from this machine.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "expected a JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected a JSON object"}, status=400)

    # `is True`, not truthiness: "false" is a truthy string, and a client sending
    # the word would have every choice dropped without having asked for it.
    if body.get("reset") is True:
        try:
            settings.forget()
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        # Settled like any other change: reset decides which branch is in force
        # just as a named setting does, so it decides whether the loop should be
        # running and who holds the microphone.
        await _settle(request.app)
        return web.json_response(_now())

    changed = {k: v for k, v in body.items() if k in {"listening", "speaking", "waking", "voice"}}
    if not changed:
        return web.json_response(
            {"error": "nothing to change: listening, speaking, waking or voice"}, status=400
        )
    # Checked before any of it is kept: a request naming one good setting and one
    # bad one would otherwise leave the deployment half-changed, in a state
    # nobody asked for and the answer does not describe.
    for setting, value in changed.items():
        problem = settings.refuses(setting, str(value))
        if problem:
            return web.json_response({"error": problem}, status=400)
    try:
        for setting, value in changed.items():
            settings.choose(setting, str(value))
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)

    await _settle(request.app)
    return web.json_response(_now())


async def _settle(app: web.Application) -> None:
    """Make the running deployment match the settings it now has, in registration order."""
    for reaction in app.get(_REACTIONS, []):
        outcome = reaction()
        if inspect.isawaitable(outcome):
            await outcome


def _now() -> dict[str, Any]:
    """What the voice settings resolve to at this moment, and what they may be."""
    return {
        "listening": settings.listening(),
        "speaking": settings.speaking(),
        "waking": settings.waking(),
        "voice": settings.voice(),
        "allowed": _allowed(),
    }


def _allowed() -> dict[str, list[str]]:
    """Each branch setting's choices here, up to the ceiling its variable sets."""
    return {setting: list(settings.allowed(setting)) for setting in settings.SETTINGS}

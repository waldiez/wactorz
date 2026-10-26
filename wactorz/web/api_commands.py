"""``GET /api/commands``: the slash commands the dashboard's chat can send.

Each way to type a command, with what it does, for the chat box to offer as
it is typed. They come from the orchestrator answering chat
(`wactorz.orchestration.command_forms`) -- main's commands, the model-free
profile's few, or an orchestrator of your own by name -- and from this server,
which answers a few itself before the orchestrator sees them
(`wactorz.web.chat.server_forms`). So the list is what will be answered. The dashboard is the operator's channel, so every command is listed;
a social channel never asks.
"""

from aiohttp import web

from ..agents.lookup import find_main_actor
from ..orchestration import MainOrchestrator, command_forms
from . import runtime
from .chat import server_forms


def listing() -> list[dict[str, str]]:
    """Every form, as ``{"usage", "typed", "summary"}``."""
    forms = []
    orchestrator = runtime.orchestrator
    if orchestrator is not None:
        forms = command_forms(orchestrator)
    elif runtime.registry is not None and find_main_actor(runtime.registry) is not None:
        # No orchestrator installed: chat reaches main from the registry, as
        # `web.chat` routes it.
        forms = command_forms(MainOrchestrator(runtime.registry))
    listed = {form.usage for form in forms}
    forms += [form for form in server_forms() if form.usage not in listed]
    return [{"usage": f.usage, "typed": f.typed, "summary": f.summary} for f in forms]


async def commands_handler(_request: web.Request) -> web.Response:
    """The commands the chat can send, each way to type them."""
    return web.json_response({"commands": listing()})

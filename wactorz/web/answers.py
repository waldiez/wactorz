"""The dashboard's answer to a question an agent asked.

A chat frame with options is shown on the dashboard as a card with buttons.
A press arrives here over the websocket, as ``{"type": "answer", ...}``, and
goes out on the agent's ``agents/<id>/answer`` topic over the server's broker
link, the way a Telegram tap or a WhatsApp reply does from its channel. The
agent that asked hears it there, wherever it runs, and so does every open
dashboard, which marks the question answered.

An agent in this process whose answers are permissions -- the installer,
asking whether to install a package -- does not listen on that topic, which
anything on the broker could write to. It is handed the press here as well.
"""

import json
import logging
import time
from typing import Any

from ..core.outbound import Answer, TakesDashboardAnswers, check_options
from ..core.topics import topic_name_error
from . import runtime

logger = logging.getLogger(__name__)


def answer_from_frame(data: dict[str, Any]) -> Answer | str:
    """The :class:`Answer` a websocket frame carries, or what is wrong with it.

    ``agent_id`` becomes one level of the topic the answer goes on, so it is
    checked as a topic level, not trusted as an id; ``option`` is checked as an
    option is, since it is shown back to people as the pick.
    """
    agent_id = str(data.get("agent_id") or "").strip()
    if not agent_id or "/" in agent_id or topic_name_error(agent_id):
        return "an answer names the agent it is for"
    message_id = str(data.get("id") or "").strip()
    if not message_id:
        return "an answer names the message it answers"
    try:
        (option,) = check_options([data.get("option")])
    except (TypeError, ValueError) as exc:
        return f"not an option: {exc}"
    try:
        index = int(data.get("index", 0))
    except (TypeError, ValueError):
        return "an answer's index is a number"
    if index < 0:
        return "an answer's index is a number"
    return Answer(
        id=message_id,
        agent_id=agent_id,
        option=option,
        index=index,
        channel="dashboard",
        timestamp=time.time(),
    )


async def answer_from_dashboard(data: dict[str, Any]) -> str | None:
    """Publish the pick in ``data`` for its agent. Returns what went wrong, or None."""
    answer = answer_from_frame(data)
    if isinstance(answer, str):
        return answer
    agent = runtime.registry.get(answer.agent_id) if runtime.registry is not None else None
    if isinstance(agent, TakesDashboardAnswers):
        # Handed over here, not heard from the broker: this agent's answers are permissions.
        await agent.dashboard_answer(answer)
    client = runtime.mqtt_client_ref
    if client is None:
        return "The broker is not connected, so the answer could not be sent."
    try:
        # QoS 1: the person pressed a button, and a pick that reaches nobody
        # is a question asked again.
        await client.publish(answer.topic, json.dumps(answer.to_payload()), qos=1)
    except Exception as exc:
        logger.warning("[answers] could not publish an answer for %s: %s", answer.agent_id[:8], exc)
        return "The answer could not be sent to the agent."
    logger.info("[answers] %s picked %r for %s", "dashboard", answer.option, answer.agent_id[:8])
    return None

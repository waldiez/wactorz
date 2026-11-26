# SPDX-License-Identifier: Apache-2.0
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

Before either, the server's question ledger (`wactorz.web.questions`) checks
the pick against the question: still open, and one of its options. The same
pick can come as ``POST /api/questions/{agent_id}/{question_id}/answer`` with
``{"option": "..."}``, for a question the ledger knows.

A question asked for words (``free_text``) is answered on that route alone,
with ``{"text": "..."}``. Words cannot be checked against options, so they are
never sent on the broker for the agent to hear: the text is cleaned once
(`questions.typed_text`) and handed to the agent in this process
(`Actor.take_free_text`), which records it and publishes it for the other
dashboards. An agent not in this process cannot be answered so, and is refused.
"""

import json
import logging
import time
from typing import Any

from aiohttp import web

from ..agents.lookup import find_main_actor
from ..agents.main.typed_answers import BEING_MOVED, forward_words
from ..core.labels import vouch
from ..core.outbound import Answer, TakesDashboardAnswers, check_options, note_text
from ..core.questions import MAX_TEXT_CHARS, typed_text
from ..core.topics import topic_name_error
from . import questions, runtime

logger: logging.Logger = logging.getLogger(__name__)


def answer_from_frame(data: dict[str, Any]) -> Answer | str:
    """The :class:`Answer` a websocket frame carries, or what is wrong with it.

    ``agent_id`` becomes one level of the topic the answer goes on, so it is
    checked as a topic level, not trusted as an id; ``option`` is checked as an
    option is, since it is shown back to people as the pick. ``note`` is
    optional text the person adds, cut to `MAX_NOTE_CHARS`.
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
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        return "an answer's note is text"
    return Answer(
        id=message_id,
        agent_id=agent_id,
        option=option,
        index=index,
        channel="dashboard",
        timestamp=time.time(),
        note=note_text(note),
    )


async def answer_from_dashboard(data: dict[str, Any]) -> str | None:
    """Publish the pick in ``data`` for its agent. Returns what went wrong, or None."""
    answer = answer_from_frame(data)
    if isinstance(answer, str):
        return answer
    try:
        answer = questions.checked(answer)
    except questions.AnswerRefusedError as exc:
        return str(exc)
    return await _send(answer)


async def _send(answer: Answer) -> str | None:
    """Hand ``answer`` to its agent here if it takes them so, and publish it. What went wrong, or None."""
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
    # Once it is sent, and only then: a label from this answer is a person's
    # signed in to the dashboard, not anyone's on the broker. The agent's book
    # saying it was answered comes back over the broker, after this.
    vouch(answer.agent_id, answer.id, answer.option)
    logger.info("[answers] %s picked %r for %s", "dashboard", answer.option, answer.agent_id[:8])
    return None


async def _free_text_answer(agent_id: str, question_id: str, body: dict[str, Any]) -> web.Response:
    """Answer the free-text question ``question_id`` with the words ``body`` carries."""
    text = typed_text(body.get("text"))
    if not text:
        return web.json_response(
            {"error": f"This question is answered in words: up to {MAX_TEXT_CHARS} characters."},
            status=400,
        )
    question = questions.find(agent_id, question_id)
    if question is not None and not question.open:
        return web.json_response({"error": "This question was already answered."}, status=409)
    answer = Answer(
        id=question_id,
        agent_id=agent_id,
        option=text,
        index=0,
        channel="dashboard",
        timestamp=time.time(),
    )
    main = find_main_actor(runtime.registry) if runtime.registry is not None else None
    if main is not None and main.migration.being_moved(questions.name_of(agent_id)):
        # Here or on a node: the copy being stopped would keep words no one runs.
        return web.json_response({"error": BEING_MOVED, "retry": True}, status=409)
    agent = runtime.registry.get(agent_id) if runtime.registry is not None else None
    # The registry holds this process's actors only; an agent on a node is not among them.
    if agent is None:
        return await _words_to_a_node(agent_id, answer)
    refused = await agent.take_free_text(answer)
    if refused is not None:
        return web.json_response({"error": refused}, status=409)
    # The agent took the words: this server's browsers close the question now,
    # on what this route knows, rather than on the agent's echo, which the
    # broker may bring them before the book that lets it through.
    from .mqtt import broadcast_mqtt_msg  # a genuine circular import: ws imports this module

    await broadcast_mqtt_msg(answer.topic, json.dumps(answer.to_payload()))
    logger.info("[answers] dashboard answered %s's question in words", agent_id[:8])
    return web.json_response({"answer": answer.to_payload()})


async def _words_to_a_node(agent_id: str, answer: Answer) -> web.Response:
    """Send the words to the agent ``agent_id`` on a node, through main; or say why not.

    Main checks where the agent runs and whether that node may be given them,
    each time (`typed_answers.forward_words`), and sends them signed. The node's
    agent records them and publishes them, which closes the question on every
    dashboard; this answers once they are on their way.
    """
    main = find_main_actor(runtime.registry) if runtime.registry is not None else None
    if main is None:
        return web.json_response(
            {"error": "The agent that asked does not run in this server's process."}, status=503
        )
    refused = await forward_words(main, questions.name_of(agent_id), answer)
    if refused is not None:
        return web.json_response({"error": refused}, status=503)
    logger.info("[answers] dashboard answered %s's question in words, through main", agent_id[:8])
    return web.json_response({"answer": answer.to_payload(), "forwarded": True})


async def answer_handler(request: web.Request) -> web.Response:
    """``POST /api/questions/{agent_id}/{question_id}/answer``: pick an option, as the button does.

    A question asked for words takes ``{"text": ...}`` instead (`_free_text_answer`).
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "the body is not JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "the body is a JSON object"}, status=400)
    asked = questions.find(request.match_info["agent_id"], request.match_info["question_id"])
    if asked is not None and asked.free_text:
        return await _free_text_answer(
            request.match_info["agent_id"], request.match_info["question_id"], body
        )
    answer = answer_from_frame(
        {
            "agent_id": request.match_info["agent_id"],
            "id": request.match_info["question_id"],
            "option": body.get("option"),
            "note": body.get("note"),
        }
    )
    if isinstance(answer, str):
        return web.json_response({"error": answer}, status=400)
    try:
        answer = questions.checked(answer, known_only=True)
    except questions.AnswerRefusedError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    problem = await _send(answer)
    if problem is not None:
        return web.json_response({"error": problem}, status=503)
    return web.json_response({"answer": answer.to_payload()})

"""Labels from the answers to agents' questions, recorded by main.

An agent that asks the operator with a label spec (``ask_operator(...,
label=...)``) keeps the spec in its question book, and publishes the book,
retained, on ``agents/<id>/questions`` whenever a question is asked or
answered. Main follows those books and writes a label into the provenance graph
for every answered question whose option maps to a value. One path for every
agent: one in main's process and one on a node publish their books alike, and
main holds the database either way.

A book counts only from where its agent is, as a manifest does
(`wactorz.agents.main.manifests`): for main's own agent the book is read from
the agent itself, never the broker's copy, and for an agent on a node it must be
signed by the node main places it on. The answer inside a book is still only
what the agent heard on its answer topic, which anything on the broker can
write to; a label is marked verified only when this process sent that answer
itself, from the dashboard (`wactorz.core.labels.vouch`).

Writing a label for a question again is harmless -- its id is minted from the
question -- so a book republished when its agent starts, or redelivered when
this listener reconnects, changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...core.labels import LabelError, Labels, vouched
from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
    user_properties,
)
from ...core.node_publications import admit
from ...core.persistence.stores import get_db
from ...core.provenance import ProvenanceError
from ...core.questions import QUESTIONS_LEAF, Question, questions_in_frame
from .voices import placed_node, speaks_for

if TYPE_CHECKING:
    from .hosts import LabelHost

logger = logging.getLogger(__name__)

#: What this listener subscribes to: every agent's question book.
BOOKS_TOPIC = f"agents/+/{QUESTIONS_LEAF}"

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How many (agent, question) pairs are remembered as recorded, so a book
#: republished in full is not walked through the database again.
RECORDED_KEPT = 10_000


class LabelRecorder:
    """Writes the labels answered questions make, from the books agents publish."""

    def __init__(self, host: LabelHost | None = None) -> None:
        self.host = host
        self._recorded: dict[tuple[str, str], None] = {}

    async def listener(self) -> None:
        """Follow every agent's question book until the actor stops, reconnecting as needed."""
        host = self.host
        if host is None:
            return
        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "labels"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe(BOOKS_TOPIC, qos=1)
                    last_error = None
                    async for message in client.messages:
                        await self.receive(
                            str(message.topic), message.payload, user_properties(message)
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] Label listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def receive(
        self, topic: str, payload: Any, properties: Mapping[str, str] | None = None
    ) -> list[str]:
        """Take one question book and record the labels its answers make.

        ``properties`` are the user properties the message arrived with; None
        for one that did not come off the broker. Returns the ids of the labels
        written or found already written, for the record.
        """
        book = _book(topic, payload)
        if book is None:
            return []
        actor_id, name, raw, frame = book
        trusted = self._trusted(topic, raw, frame, name, properties)
        if trusted is None:
            return []
        node, questions = trusted
        # In the order they were answered, so two answers from one book keep
        # their own order when both are written now.
        pending = sorted(
            (
                q
                for q in questions
                if not q.open and q.label and (actor_id, q.id) not in self._recorded
            ),
            key=lambda q: q.answered_at,
        )
        if not pending:
            return []
        db = get_db()
        if db is None:
            return []
        written = await asyncio.to_thread(_record, db, pending, actor_id, name, node)
        for question in pending:
            self._recorded[(actor_id, question.id)] = None
        while len(self._recorded) > RECORDED_KEPT:
            self._recorded.pop(next(iter(self._recorded)))
        return written

    def _trusted(
        self,
        topic: str,
        payload: bytes,
        frame: dict[str, Any],
        name: str,
        properties: Mapping[str, str] | None,
    ) -> tuple[str, list[Question]] | None:
        """The node an agent's book comes from and its questions, or None when it is not the agent's word.

        Main's own agent: the questions it keeps itself, whatever arrived. An
        agent on a node: the book, as long as the node main places it on signed
        it -- or, for one main placed nowhere, the node whose heartbeat lists it.
        """
        host = self.host
        if host is None:
            return "", questions_in_frame(frame)
        local = host._registry.find_by_name(name) if host._registry is not None else None
        if local is not None:
            # The name in the frame is anyone's to write; the agent it names is
            # read only when the topic is that agent's own, or a book published
            # under another id would be recorded as its answers.
            if getattr(local, "actor_id", "") != _actor_id_from(topic):
                return None
            own = getattr(local, "questions", None)
            kept = own() if callable(own) else None
            return ("", list(kept.values())) if isinstance(kept, dict) else None
        if properties is None:
            return "", questions_in_frame(frame)
        node = placed_node(host, name) or _listing_node(host._known_nodes, name)
        if not speaks_for(host, node, name):
            logger.warning(
                "[main] Ignoring a question book for %r from %r: main does not place it there",
                name,
                node or "an unknown node",
            )
            return None
        if not admit(node, topic, payload, properties, "a question book"):
            return None
        return node, questions_in_frame(frame)


def _book(topic: str, payload: Any) -> tuple[str, str, bytes, dict[str, Any]] | None:
    """The asking agent's id and name, the raw payload and the frame, or None for no book.

    A cleared retained topic, a payload that is not a JSON object, and one that
    names no agent all carry nothing to record.
    """
    if not payload:
        return None
    raw = payload if isinstance(payload, bytes) else str(payload).encode()
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(frame, dict):
        return None
    actor_id = _actor_id_from(topic)
    name = str(frame.get("agent") or "")
    if not actor_id or not name:
        return None
    return actor_id, name, raw, frame


def _record(db: Any, questions: list[Question], actor_id: str, name: str, node: str) -> list[str]:
    """Write the labels ``questions`` make; blocking, so run in a thread.

    One question whose label cannot be written is logged and passed over, so
    the others in the book are still recorded.
    """
    labels = Labels(db)
    written: list[str] = []
    for question in questions:
        try:
            label = labels.from_answer(
                question,
                agent=name,
                node=node,
                verified=vouched(actor_id, question.id, question.answer),
            )
        except (LabelError, ProvenanceError) as exc:
            logger.warning(
                "[main] Could not record a label from %r's answer to %r: %s",
                name,
                question.text[:60],
                exc,
            )
            continue
        if label is not None:
            written.append(label.id)
    return written


def _actor_id_from(topic: str) -> str:
    """The actor id in ``agents/<id>/questions``, or "" when the topic is not one."""
    parts = topic.split("/")
    return parts[1] if len(parts) == 3 and parts[0] == "agents" else ""


def _listing_node(known_nodes: dict[str, dict[str, Any]], name: str) -> str:
    """The node whose heartbeat lists ``name``, for an agent main placed nowhere."""
    return next((node for node, info in known_nodes.items() if name in info.get("agents", [])), "")

# SPDX-License-Identifier: Apache-2.0
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
from dataclasses import dataclass
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
from ...core.questions import QUESTIONS_LEAF, Question, book_frame, questions_in_frame
from ...core.topics import AGENTS, agent_topic, own_topic_parts
from .voices import placed_node, speaks_for

if TYPE_CHECKING:
    from ...core.registry import ActorRegistry
    from .hosts import LabelHost

logger: logging.Logger = logging.getLogger(__name__)

#: What this listener subscribes to: every agent's question book.
BOOKS_TOPIC = agent_topic("+", QUESTIONS_LEAF)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How many (agent, question) pairs are remembered as recorded, so a book
#: republished in full is not walked through the database again.
RECORDED_KEPT = 10_000


@dataclass(frozen=True)
class AdmittedBook:
    """A question book as its agent's word: who it is, where, and what it asked.

    ``name`` is the agent's own (an agent in this process) or the one its node
    was found to speak for, never only what the payload claimed. ``node`` is ""
    for an agent in this process. A book with no questions is a clear.
    """

    actor_id: str
    name: str
    node: str
    questions: tuple[Question, ...]

    @property
    def cleared(self) -> bool:
        """Whether the book holds nothing: the agent's retained topic is to be taken as empty."""
        return not self.questions

    def frame(self) -> dict[str, Any]:
        """The book as the questions topic carries it, with the admitted name."""
        return book_frame(self.name, {q.id: q for q in self.questions})


def admit_book(
    topic: str,
    payload: bytes,
    properties: Mapping[str, str] | None,
    *,
    host: LabelHost | None,
    registry: ActorRegistry | None = None,
    last: Mapping[str, tuple[str, str]] | None = None,
) -> AdmittedBook | None:
    """The book on ``topic`` as its agent's word, or None when it is not.

    One rule for every reader of the books, so labels and what a person is
    shown cannot disagree:

    - an agent in this process, found by the topic's id in ``registry`` (the
      host's when it has one), is read from the agent itself: whatever the
      payload says, and an empty payload included, the book is the agent's own;
    - an agent on a node must be spoken for by the node main places it on, or,
      for one main placed nowhere, the node whose heartbeat lists it; and the
      message must be that node's (`admit`: signed, or let through with a
      warning when signing is not enforced);
    - with no host (a profile that runs no main) there is nothing to place a
      node's agent by, and its book is taken as it came, unless it names an
      agent of this process under another id.

    An empty payload is a clear. It names no agent, so ``last`` -- actor id to
    the name and node last admitted for it -- says whose it is; with nothing
    admitted before, a clear has nothing to clear and is None. A clear for an
    agent of this process that is not registered now -- stopped, restarting or
    gone -- is refused like any clear no node speaks for: anything on the
    broker can send an empty payload, and a deleted agent's book goes when its
    manifest is withdrawn. A payload that is not a book is None, so the last
    admitted book stands.

    ``properties`` are the user properties the message came with: ``{}`` for
    an unsigned one off the broker, None for one that did not come off it.
    """
    actor_id = _actor_id_from(topic)
    if not actor_id:
        return None
    local_registry = host._registry if host is not None else registry
    local = local_registry.get(actor_id) if local_registry is not None else None
    if local is not None:
        return _own_book(local, actor_id)
    claimed = _claimed(topic, payload, actor_id, last or {})
    if claimed is None:
        return None
    name, node, frame = claimed
    questions = tuple(questions_in_frame(frame))
    if host is None:
        named = local_registry.find_by_name(name) if local_registry is not None else None
        return None if named is not None else AdmittedBook(actor_id, name, node, questions)
    if properties is None:
        return AdmittedBook(actor_id, name, node, questions)
    node = _speaking_node(host, name, actor_id, node)
    if node is None or not admit(node, topic, payload, properties, "a question book"):
        return None
    return AdmittedBook(actor_id, name, node, questions)


def _own_book(agent: Any, actor_id: str) -> AdmittedBook | None:
    """The book an agent of this process keeps itself, whatever was published for it."""
    own = getattr(agent, "questions", None)
    kept = own() if callable(own) else None
    if not isinstance(kept, dict):
        return None
    return AdmittedBook(actor_id, agent.name, "", tuple(kept.values()))


def _claimed(
    topic: str, payload: bytes, actor_id: str, last: Mapping[str, tuple[str, str]]
) -> tuple[str, str, dict[str, Any]] | None:
    """Who a book from the broker says it is, where it was last admitted from, and its frame.

    A clear says nothing of who it is, so it is whoever ``last`` has for the id,
    and nothing when that is no one.
    """
    if not payload:
        known = last.get(actor_id)
        return None if known is None else (known[0], known[1], {})
    book = _book(topic, payload)
    if book is None:
        return None
    _, name, _, frame = book
    return name, "", frame


def _speaking_node(host: LabelHost, name: str, actor_id: str, last_node: str) -> str | None:
    """The node that may speak for the agent ``name``, or None when none may."""
    node = placed_node(host, name, actor_id) or _listing_node(host._known_nodes, name) or last_node
    if speaks_for(host, node, name):
        return node
    logger.warning(
        "[main] Ignoring a question book for %r from %r: main does not place it there",
        name,
        node or "an unknown node",
    )
    return None


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
        actor_id, _, raw, _ = book
        host = self.host
        admitted = admit_book(topic, raw, properties, host=host) if host is not None else None
        if host is None:
            # A recorder with no host to place agents by is a test's: the book as it came.
            admitted = AdmittedBook(actor_id, book[1], "", tuple(questions_in_frame(book[3])))
        if admitted is None:
            return []
        name, node, questions = admitted.name, admitted.node, admitted.questions
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
    parts = own_topic_parts(topic)
    return parts[1] if parts is not None and len(parts) == 3 and parts[0] == AGENTS else ""


def _listing_node(known_nodes: dict[str, dict[str, Any]], name: str) -> str:
    """The node whose heartbeat lists ``name``, for an agent main placed nowhere."""
    return next((node for node, info in known_nodes.items() if name in info.get("agents", [])), "")

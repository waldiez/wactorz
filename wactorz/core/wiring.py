# SPDX-License-Identifier: Apache-2.0
"""Who publishes what, who reads it, and where the data stops: the wiring, read from the registry.

`wiring` turns main's topic registry (`wactorz.core.topic_bus`) into a graph a
person can read: the agents, each topic with who publishes and who reads it,
the topics fed from outside the system, and what looks wrong. It works on what
the registry holds at the time of asking; it subscribes to nothing and keeps
nothing of its own.

Three things look wrong:

- **a topic nobody reads**: an agent publishes it, and no agent subscribes;
  what a model on trial publishes (under the ``shadow`` root) is for the trial, and
  is marked as such instead;
- **a subscriber with no producer**: an agent reads a topic no agent publishes
  and nothing has been seen on, outside the world's own roots (`WORLD_ROOTS`),
  where a sensor, Home Assistant or a script is expected to publish;
- **a field mismatch**: an agent declares a field it reads (``consumes_schema``)
  that the topic does not carry. What a topic carries is what was seen on it,
  from an agent or from outside; before anything is seen, what its producer
  declares (``produces_schema``). A field declared as ``any`` names nothing,
  and a topic nothing is known about raises no mismatch.

What is known of each topic is its fields, the last payload seen on it (cut
to `EXAMPLE_MAX_CHARS` of JSON) and when it came. Topics under the roots main
does not sample are said to be so, rather than guessed at.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .topic_bus import TopicContract, is_machinery, topic_matches
from .topics import AGENTS, SHADOW, WORLD_ROOTS, own_topic_parts

#: The roots main keeps samples from (`wactorz.agents.main.manifests`), so a
#: topic under any other has no fields to show unless an agent published it.
SAMPLED_ROOTS = ("sensors", "custom", AGENTS)
#: The longest last example given whole, as JSON text; a longer one is cut.
EXAMPLE_MAX_CHARS = 2048

#: A model's pointers as the store has them: ``{"active": 3, "shadow": 4}``, None where unset.
Pointers = Callable[[str], Mapping[str, int | None]]


def root_of(topic: str) -> str:
    """The root ``topic`` is under: ``sensors`` for ``sensors/imu/left``.

    For one of Wactorz's own that is its root, whatever the namespace in front
    of it; for any other topic, its first level.
    """
    parts = own_topic_parts(topic)
    return parts[0] if parts is not None else topic.split("/", 1)[0]


def agent_id(contract: TopicContract) -> str:
    """How the graph names an agent: its id, or its name for one whose id is not known yet."""
    return contract.actor_id or contract.name


def _example(sample: Mapping[str, Any]) -> dict[str, Any]:
    """A sample's last payload and when it came, cut to `EXAMPLE_MAX_CHARS` when long."""
    example = sample.get("example")
    text = json.dumps(example, default=str)
    if len(text) <= EXAMPLE_MAX_CHARS:
        shown: dict[str, Any] = {"example": example}
    else:
        shown = {"example": None, "example_text": text[:EXAMPLE_MAX_CHARS], "example_cut": True}
    at = sample.get("at")
    return {**shown, "seen_at": float(at) if isinstance(at, (int, float)) else None}


def _read_fields(contract: TopicContract) -> list[str]:
    """The fields ``contract``'s agent says it reads, leaving out any declared as ``any``."""
    return sorted(
        str(key)
        for key, kind in (contract.consumes_schema or {}).items()
        if str(kind).strip().lower() != "any"
    )


def _published(contracts: Iterable[TopicContract]) -> dict[str, list[TopicContract]]:
    """Each topic some agent publishes, with its producers.

    A topic is what an agent declares, and what it was seen publishing; a
    declared pattern that a seen topic fills is shown as the topics seen.
    """
    topics: dict[str, list[TopicContract]] = {}
    for contract in contracts:
        seen = [t for t in contract.observed_samples if not is_machinery(t)]
        declared = [
            t
            for t in contract.publishes
            if not is_machinery(t) and not any(topic_matches(t, s) and t != s for s in seen)
        ]
        for topic in dict.fromkeys([*declared, *seen]):
            topics.setdefault(topic, []).append(contract)
    return topics


def _reads(contract: TopicContract, topic: str) -> bool:
    return any(topic_matches(f, topic) for f in contract.subscribes if not is_machinery(f))


def _known(
    topic: str, producers: list[TopicContract], outside: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """What is known of what ``topic`` carries: from a sample if one was seen, else from a declaration."""
    samples = [p.observed_samples[topic] for p in producers if topic in p.observed_samples]
    if topic in outside:
        samples.append(outside[topic])
    if samples:
        newest = max(samples, key=lambda s: float(s.get("at") or 0.0))
        return {
            "fields": dict(newest.get("fields") or {}),
            "fields_from": "seen",
            **_example(newest),
        }
    declared: dict[str, Any] = {}
    for producer in producers:
        declared.update(producer.produces_schema or {})
    if declared:
        return {"fields": declared, "fields_from": "declared", "example": None, "seen_at": None}
    return {"fields": None, "fields_from": None, "example": None, "seen_at": None}


def _agent(
    contract: TopicContract, pointers: Pointers, asked: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    models = []
    for key, name in sorted(contract.models.items()):
        stands = pointers(name)
        models.append(
            {
                "key": key,
                "name": name,
                "active": stands.get("active"),
                "shadow": stands.get("shadow"),
            }
        )
    return {
        "id": agent_id(contract),
        "name": contract.name,
        "node": contract.place,
        "description": contract.description,
        "publishes": list(contract.publishes),
        "subscribes": list(contract.subscribes),
        "reads_fields": _read_fields(contract),
        "models": models,
        "asks": dict(asked[contract.actor_id])
        if contract.actor_id and contract.actor_id in asked
        else None,
    }


def _mismatches(
    topic: str, known: Mapping[str, Any], consumers: list[TopicContract]
) -> list[dict[str, Any]]:
    fields = known.get("fields")
    if not fields:
        return []
    found = []
    for consumer in consumers:
        missing = [f for f in _read_fields(consumer) if f not in fields]
        if missing:
            found.append(
                {
                    "kind": "field_mismatch",
                    "topic": topic,
                    "consumer": agent_id(consumer),
                    "missing": missing,
                    "carried": sorted(fields),
                    "carried_from": known.get("fields_from"),
                }
            )
    return found


#: Topics and warnings as each part of the graph adds them.
_Found = tuple[list[dict[str, Any]], list[dict[str, Any]]]


def _published_topics(
    kept: list[TopicContract],
    published: Mapping[str, list[TopicContract]],
    outside: Mapping[str, Mapping[str, Any]],
) -> _Found:
    """The topics agents publish, with their readers; one nobody reads, or that misses a field, is a warning."""
    topics: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for topic, producers in sorted(published.items()):
        # An agent that reads its own topic is one of its readers, as any other is.
        consumers = [c for c in kept if _reads(c, topic)]
        known = _known(topic, producers, outside)
        named = [agent_id(p) for p in producers]
        # What a model on trial publishes is for the trial: nobody reading it is no warning.
        trial = root_of(topic) == SHADOW
        topics.append(
            {
                "topic": topic,
                "producers": named,
                "consumers": [agent_id(c) for c in consumers],
                "outside": False,
                "shadow": trial,
                "sampled": True,
                **known,
            }
        )
        if not consumers and not trial:
            warnings.append({"kind": "unread", "topic": topic, "producers": named})
        warnings.extend(_mismatches(topic, known, consumers))
    return topics, warnings


def _outside_topics(
    kept: list[TopicContract],
    published: Mapping[str, list[TopicContract]],
    outside: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
    """The topics seen from outside that agents read, and each (agent, subscription) they fill."""
    topics: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    filled: set[tuple[str, str]] = set()
    for topic in sorted(outside):
        consumers = [c for c in kept if _reads(c, topic)]
        if topic in published or is_machinery(topic) or not consumers:
            continue
        known = _known(topic, [], outside)
        topics.append(
            {
                "topic": topic,
                "producers": [],
                "consumers": [agent_id(c) for c in consumers],
                "outside": True,
                "shadow": False,
                "sampled": True,
                **known,
            }
        )
        warnings.extend(_mismatches(topic, known, consumers))
        filled.update(
            (agent_id(c), f) for c in consumers for f in c.subscribes if topic_matches(f, topic)
        )
    return topics, warnings, filled


def _unfilled_subscriptions(
    kept: list[TopicContract],
    published: Mapping[str, list[TopicContract]],
    filled: set[tuple[str, str]],
) -> _Found:
    """What agents subscribe to that nothing publishes or was seen on, by pattern, with its readers.

    Under the world's roots that is an outside source; anywhere else, a warning.
    """
    unfilled: dict[str, list[str]] = {}
    for contract in kept:
        for pattern in contract.subscribes:
            if is_machinery(pattern) or (agent_id(contract), pattern) in filled:
                continue
            if any(topic_matches(pattern, t) or topic_matches(t, pattern) for t in published):
                continue
            unfilled.setdefault(pattern, []).append(agent_id(contract))
    topics: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for pattern, readers in sorted(unfilled.items()):
        world = root_of(pattern) in WORLD_ROOTS
        topics.append(
            {
                "topic": pattern,
                "producers": [],
                "consumers": readers,
                "outside": world,
                "shadow": False,
                "sampled": root_of(pattern) in SAMPLED_ROOTS,
                "fields": None,
                "fields_from": None,
                "example": None,
                "seen_at": None,
            }
        )
        if not world:
            warnings.append({"kind": "no_producer", "topic": pattern, "consumers": readers})
    return topics, warnings


def wiring(
    contracts: Iterable[TopicContract],
    outside: Mapping[str, Mapping[str, Any]],
    *,
    pointers: Pointers,
    asked: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The graph of ``contracts``, with the payloads seen from ``outside``, by topic.

    ``pointers`` says where a model's pointers stand, and ``asked`` what each
    agent, by id, has asked a person (`wactorz.web.questions.asked_by`).
    """
    kept = list(contracts)
    published = _published(kept)
    ours, ours_warned = _published_topics(kept, published, outside)
    theirs, theirs_warned, filled = _outside_topics(kept, published, outside)
    unfilled, unfilled_warned = _unfilled_subscriptions(kept, published, filled)
    topics = [*ours, *theirs, *unfilled]
    sources: dict[str, list[str]] = {}
    for entry in topics:
        if entry["outside"]:
            sources.setdefault(root_of(entry["topic"]), []).append(entry["topic"])
    return {
        "agents": [
            _agent(c, pointers, asked) for c in sorted(kept, key=lambda c: (c.name, c.place))
        ],
        "topics": topics,
        "sources": [{"root": root, "topics": names} for root, names in sorted(sources.items())],
        "warnings": [*ours_warned, *theirs_warned, *unfilled_warned],
    }

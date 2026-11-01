"""What an actor's id is: derived from its name, or given to it and checked.

A leaf module, so that anything -- persistence, which ``core.actor`` itself
imports, among them -- can know the formula without importing the actor.
"""

import uuid


def derive_actor_id(name: str) -> str:
    """The id an actor gets from its name alone.

    What main's own agents run under, and every agent spawned before ids were
    kept, whose registry entries and chat history are upgraded to it. A new
    spawn is given an id of its own instead (see docs/design/agent-identity.md).

    Deterministic, so a name comes back as the same id at every start. Spelled
    once: a copy of the formula that drifted would raise nothing, and the
    agents upgraded to this id would lose their retained topics and their
    broker sessions.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))


def given_actor_id(value: object) -> str | None:
    """The id a spawn config's ``actor_id`` gives its agent, or None for one derived from the name.

    Main sends the id its spawn registry keeps. It becomes a level of every
    topic the agent uses (``agents/<id>/…``), so only a well-formed UUID is
    taken, in its canonical spelling; anything else is treated as no id at all,
    as a spawn from a main older than ids is.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None

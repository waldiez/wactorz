"""Agent addresses: ``name@node``, and which agents a name or an address finds.

An agent's name is unique on the host it runs on, and main is the node
``main``. ``name@node`` is an agent's full address; a bare name is an address
while exactly one live agent has it. When more than one does, nothing is
guessed: a person is told the addresses to choose from, and a program is
refused with them. See docs/design/agent-identity.md.

Nothing here knows where agents run. The caller lists the addresses it knows
of (main's own agents, the ones nodes report), and `resolve` says what a name
or an address finds among them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from ..config import RESERVED_NODE_NAMES
from .topics import topic_name_error

#: The node an agent in main's own process is on.
MAIN_NODE = "main"

#: Separates an agent's name from its node, and so is allowed in neither.
SEPARATOR = "@"


class Address(NamedTuple):
    """An agent's name and the node it is on; ``node`` is "" for a bare name."""

    name: str
    node: str = ""

    def __str__(self) -> str:
        return f"{self.name}{SEPARATOR}{self.node}" if self.node else self.name


def node_of(node: str) -> str:
    """The node ``node`` names: `MAIN_NODE` for every name that already means main."""
    node = node.strip()
    return MAIN_NODE if node.lower() in RESERVED_NODE_NAMES else node


def parse(text: str) -> Address:
    """The address ``text`` spells: ``counter@pi`` is counter on pi, ``counter`` a bare name.

    It splits at the first separator, since neither a new name nor a node name
    may contain one. ``counter@`` names no node, and is the bare name.
    """
    name, _, node = text.strip().partition(SEPARATOR)
    return Address(name.strip(), node_of(node))


def agent_name_error(name: str) -> str | None:
    """Why a new agent may not be called ``name``, or None if it may.

    A level of every topic the agent uses, so first whatever a topic level
    refuses; then the separator, so that ``name@node`` splits one way only.
    Checked where an agent is first made: one already running under such a
    name keeps running, and is still found by its whole name.
    """
    problem = topic_name_error(name)
    if problem:
        return problem
    if SEPARATOR in name:
        return (
            f"the name contains {SEPARATOR!r}, which separates an agent's name from its node "
            f"in an address (name{SEPARATOR}node)"
        )
    return None


class Resolution(NamedTuple):
    """What a name or an address found: no agent, one, or several to choose between."""

    wanted: str
    matches: tuple[Address, ...]

    @property
    def found(self) -> Address | None:
        """The one agent it names, or None for none or several."""
        return self.matches[0] if len(self.matches) == 1 else None

    @property
    def ambiguous(self) -> bool:
        """Whether more than one agent answers to it."""
        return len(self.matches) > 1

    def choices(self, *, mention: bool = False) -> str:
        """The addresses to choose from, as a person types them (``@counter@pi``) or as a program passes them."""
        prefix = "@" if mention else ""
        spelled = [f"`{prefix}{address}`" for address in self.matches]
        if len(spelled) <= 2:
            return " or ".join(spelled)
        return ", ".join(spelled[:-1]) + f" or {spelled[-1]}"

    def refusal(self, *, mention: bool = False) -> str:
        """What a sender is told when the name it used is ambiguous."""
        return (
            f"More than one agent is called '{self.wanted}', so nothing was done. "
            f"Name the one you mean by its address: {self.choices(mention=mention)}."
        )


def resolve(wanted: str, known: Iterable[Address]) -> Resolution:
    """Which of the ``known`` addresses ``wanted`` names.

    An agent whose whole name is ``wanted`` comes first: one named before the
    separator was refused in a name is still reached by that name. Otherwise
    ``wanted`` is an address, and a bare name finds every agent of that name,
    on whichever node. The order of ``known`` is kept, without repeats.
    """
    candidates = list(dict.fromkeys(known))
    wanted = wanted.strip()
    whole = tuple(a for a in candidates if a.name == wanted)
    if whole or SEPARATOR not in wanted:
        return Resolution(wanted, whole)
    address = parse(wanted)
    matches = tuple(
        a
        for a in candidates
        if a.name == address.name and (not address.node or node_of(a.node) == address.node)
    )
    return Resolution(address.name if not address.node else wanted, matches)

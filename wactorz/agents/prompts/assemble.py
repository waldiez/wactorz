"""Prompts assembled from a core and the fragments an installation's integrations add.

The prompts main and the planner work from describe what the whole system can
do. Part of that depends on which integrations are configured: an installation
with Home Assistant should be told about its devices, one without it should not
hear about lights at all. So a prompt is a *template*, a sequence of literal
text and :class:`Slot` markers, and each integration present contributes a
:class:`PromptFragment` saying what text goes into which slots. Rendering joins
them in a fixed order, so the result for a given set of fragments is the same
every time and can be pinned in a test.

A slot shows what the fragments insert into it, in the fragments' order, or its
own default when none of them inserts anything. Most slots have an empty default
and simply disappear; a few carry the wording the core uses on its own, such as
an example topic, which a fragment replaces with one of its integration's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Slot:
    """A place in a template where fragments put text."""

    name: str
    #: What the slot shows when no fragment inserts anything into it.
    default: str = ""


@dataclass(frozen=True)
class PromptFragment:
    """What one integration adds to the shared prompts.

    Each mapping is slot name to the text inserted there, for one template.
    """

    name: str
    orchestrator: Mapping[str, str] = field(default_factory=dict)
    intent_classifier: Mapping[str, str] = field(default_factory=dict)
    #: The intents the classifier may answer with when this fragment is present.
    #: They are listed before the core intents, and the fragment's
    #: ``intent_classifier`` inserts are expected to define them.
    intents: tuple[str, ...] = ()


#: A piece of a template: literal text, a slot, or a function of the fragments
#: present, for text that is computed from them rather than inserted by one.
Part = str | Slot | Callable[[Sequence[PromptFragment]], str]
Template = tuple[Part, ...]


def slot_names(template: Template) -> set[str]:
    return {part.name for part in template if isinstance(part, Slot)}


def render(
    template: Template,
    fragments: Sequence[PromptFragment],
    inserts_of: Callable[[PromptFragment], Mapping[str, str]],
) -> str:
    """The template with the fragments' text in its slots.

    ``inserts_of`` picks the mapping of a fragment that belongs to this
    template. A fragment naming a slot the template has not got is a mistake
    in the fragment, and is refused rather than silently dropped.
    """
    known = slot_names(template)
    for fragment in fragments:
        unknown = set(inserts_of(fragment)) - known
        if unknown:
            msg = f"fragment {fragment.name!r} fills slots the template has not got: {sorted(unknown)}"
            raise ValueError(msg)
    out: list[str] = []
    for part in template:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, Slot):
            texts = [
                inserts_of(fragment)[part.name]
                for fragment in fragments
                if part.name in inserts_of(fragment)
            ]
            out.append("".join(texts) if texts else part.default)
        else:
            out.append(part(fragments))
    return "".join(out)


def one_of(items: Sequence[str]) -> str:
    """``items`` as English alternatives: ``A``, ``A or B``, ``A, B, or C``."""
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"

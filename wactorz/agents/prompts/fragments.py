"""Every prompt fragment there is, in one place.

Main's and the planner's prompt modules both render with "every fragment" by
default, so the list lives here rather than in either of them. An integration
that adds a fragment adds it to this tuple; which of these an installation
actually gets is decided where the system is built, from its configuration.
"""

from __future__ import annotations

from .assemble import PromptFragment
from .home_assistant_prompts import HOME_ASSISTANT_FRAGMENT

#: In the order their text appears in the prompts. The default for a prompt
#: built with no say in the matter, so an actor constructed on its own is told
#: about everything.
DEFAULT_FRAGMENTS: tuple[PromptFragment, ...] = (HOME_ASSISTANT_FRAGMENT,)

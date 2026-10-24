"""A transcript as it would be written, for recognisers that answer in capitals.

Some streaming models are trained on upper-case text and answer that way:
"WHAT TIME IS IT". Shown in the chat and handed to the model as typed, it reads
as shouting and is matched against nothing written normally. A transcript with
no lower-case letter at all is put in sentence case; one that has any is left
exactly as the recogniser wrote it, since its capitals are then deliberate.
"""

import re

#: "I" on its own and in its contractions, which sentence case would lower.
_FIRST_PERSON = re.compile(r"\bi\b")

#: The first letter of the text, and the first after each full stop, question
#: mark or exclamation mark: where a written sentence starts.
_SENTENCE_START = re.compile(r"(^\W*|[.!?]\s+\W*)([a-z])")


def as_written(text: str) -> str:
    """`text` in sentence case if the recogniser wrote it all in capitals, else as it is."""
    if not any(c.isalpha() for c in text) or any(c.islower() for c in text):
        return text
    lowered = _FIRST_PERSON.sub("I", text.lower())
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), lowered)

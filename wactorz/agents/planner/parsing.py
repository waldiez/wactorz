"""Reading structure back out of an LLM response, and keying it for the cache."""

import hashlib
import json
import re
from typing import Any

#: A backslash that does not start a JSON escape. Generated Python lands in
#: the plan as a JSON string, and a model writing a regex (``\d``), a quote
#: (``\'``) or a Windows path inside it escapes for Python, not for JSON —
#: ``json.loads`` then refuses the whole plan with "Invalid \escape".
_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def loads_lenient(text: str) -> Any:
    """``json.loads`` for model output.

    Two things a model gets wrong that do not change what it meant: a raw
    newline or tab inside a string (``strict=False`` accepts those), and a
    backslash that is valid in the Python it was writing but not in JSON.
    Doubling such a backslash yields the same Python source the model wrote,
    so the repair cannot alter a plan that was already valid. Anything else
    still raises the original ``JSONDecodeError``.
    """
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as first:
        repaired = _INVALID_JSON_ESCAPE.sub(r"\\\\", text)
        if repaired == text:
            raise
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            raise first from None


def extract_json_array(response: str) -> str:
    """Pull a JSON array out of an LLM response that may be fenced or padded
    with prose. Strips a leading ``` / ```json fence and any trailing fence,
    then slices the outermost [...] so stray commentary on either side does
    not break json.loads(). Returns '' if no array delimiters are found.

    Shared by both decompose paths so they parse identically.
    """
    clean = (response or "").strip()
    # Drop an opening fence line (``` or ```json) if present.
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else ""
    # Drop everything from a trailing fence onward.
    if "```" in clean:
        clean = clean[: clean.rfind("```")]
    # Slice to the outermost array.
    start = clean.find("[")
    end = clean.rfind("]")
    if start != -1 and end != -1 and end > start:
        clean = clean[start : end + 1]
    return clean.strip()


def extract_json_object(response: str) -> str:
    """Like extract_json_array but slices the outermost {...} object."""
    clean = (response or "").strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else ""
    if "```" in clean:
        clean = clean[: clean.rfind("```")]
    start = clean.find("{")
    end = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        clean = clean[start : end + 1]
    return clean.strip()


def task_hash(task: str) -> str:
    """Stable short hash of a normalized task string for cache keying."""
    normalized = " ".join(task.lower().split())
    return hashlib.md5(normalized.encode(), usedforsecurity=False).hexdigest()[:12]

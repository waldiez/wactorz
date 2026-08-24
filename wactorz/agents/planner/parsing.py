"""Reading structure back out of an LLM response, and keying it for the cache."""

import hashlib


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

# SPDX-License-Identifier: Apache-2.0
"""Between what a person types for an agent and what the agent is sent, and back.

Shared by the dashboard's chat and the model-free orchestrator, which both hand
a typed message to a message-passing agent as a task and show its reply as text.
"""

import json
from typing import Any, TypedDict


class TaskEnvelope(TypedDict, total=False):
    """The tags `ask_through` adds around a request, and the ones a reply carries back.

    Everything else in a TASK or RESULT payload is the agent's own business --
    free-form, and shaped by whatever that agent's `input_schema` declares. Only
    these keys are fixed across every agent: `_task_id`/`_reply_to` route the
    reply, and `error`/`error_phase` are how an agent reports failure instead of
    an answer (see `wactorz.core.actor.ReplyError`).

    `task` is deliberately not one of these keys, though many agents' replies
    carry it: some echo the request's *text* under it (an answer, kept as-is),
    others the older convention of echoing the request's *id* (stripped, since
    `_task_id` already carries it) -- `reply_value` tells the two apart by
    comparing the value to the task id, not by the key alone.
    """

    _task_id: str
    _reply_to: str
    error: str
    error_phase: str


#: Where a reply keeps its words, most likely first. `result` leads because that
#: is the field the prompts tell a generated agent to fill -- "for agents that
#: return plain text, use {"result": ...}" -- and what every other reader in the
#: tree looks for before anything else.
_REPLY_FIELDS = ("result", "reply", "text", "message", "content")


def task_payload(text: str) -> dict[str, Any]:
    """What a message-passing agent is handed for the text typed after its name.

    A JSON object is the payload itself, so ``@imu-anomaly {"ax": 9, "ay": 0,
    "az": 1}`` reaches a function declared with ``@wactorz.agent`` as the
    reading its input schema describes, the way another agent's ``send_to``
    would deliver it. Anything else travels as ``{"text": ...}``, which is what
    an agent that reads natural language expects.
    """
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"text": text}


def reply_text(payload: Any) -> str:
    """The words in an agent's reply, whatever shape the agent chose.

    Every reply is read here, whether the agent runs in this process or answers
    from a node, so an agent moved between the two shows the same words. The
    fields are tried in one order, and the first with something in it wins.

    A dict with none of them is shown as JSON: a function declared with
    ``@wactorz.agent`` answers with its return value, which is data, and JSON
    can be read and pasted on where a Python repr can be neither. It is still
    visibly not prose, so an agent that never learned to answer still gets
    noticed. Anything that is not a dict is returned as it is.
    """
    if isinstance(payload, dict):
        for field in _REPLY_FIELDS:
            value = payload.get(field)
            if value:
                return str(value)
        return json.dumps(payload, default=str)
    return str(payload)


__all__ = ["TaskEnvelope", "reply_text", "task_payload"]

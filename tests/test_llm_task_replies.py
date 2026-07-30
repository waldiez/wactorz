"""`LLMAgent._handle_task` must always answer the caller.

A task arrives with `_reply_to` / `_task_id` so the sender's `send_to()` future
can resolve. If the handler returns without sending a RESULT, that future never
resolves and the caller blocks until its own timeout — with nothing to say what
went wrong, because the failure was logged on the callee's side.
"""

import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from wactorz.core.actor import Message, MessageType


class _Boom:
    """A provider whose failure is *not* a RuntimeError."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def complete(self, messages=None, system=None, **_kw):
        raise self._exc


def _make_agent(llm=None):
    from wactorz.agents.llm_agent import LLMAgent

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        agent = LLMAgent(name="task-agent", llm_provider=llm, persistence_dir=tmp)
    agent.recall = lambda key, default=None: default
    agent.persist = MagicMock()
    agent.publish_manifest = AsyncMock()
    agent._mqtt_publish = AsyncMock()
    agent._persist_cost = MagicMock()
    agent.send = AsyncMock(return_value=True)
    return agent


def _task(**extra) -> Message:
    payload = {"text": "do the thing", "_reply_to": "caller-1", "_task_id": "t-42", **extra}
    return Message(type=MessageType.TASK, sender_id="caller-1", payload=payload)


def _sent_results(agent) -> list[dict]:
    # Compared by value, not identity: another test in the suite causes
    # `wactorz.core.actor` to be loaded twice, so this module's MessageType can
    # be a different class object than the agent's. `MessageType` subclasses
    # `str`, so equality still matches across the duplicates — `is` does not.
    return [c.args[2] for c in agent.send.await_args_list if c.args[1] == MessageType.RESULT]


class HandleTaskAlwaysRepliesTest(unittest.IsolatedAsyncioTestCase):
    async def test_replies_when_the_provider_raises_a_non_runtime_error(self):
        agent = _make_agent(_Boom(ValueError("model exploded")))

        await agent._handle_task(_task())

        results = _sent_results(agent)
        self.assertTrue(results, "no RESULT sent — the caller waits out its own timeout")
        self.assertEqual(results[0]["_task_id"], "t-42", "future cannot resolve without the id")
        self.assertIn("model exploded", results[0]["text"])

    async def test_replies_when_no_provider_is_configured(self):
        agent = _make_agent(llm=None)

        await agent._handle_task(_task())

        results = _sent_results(agent)
        self.assertTrue(results, "a misconfigured provider silently hung the caller")
        self.assertEqual(results[0]["_task_id"], "t-42")

    async def test_a_timeout_is_reported_rather_than_swallowed(self):
        agent = _make_agent(_Boom(TimeoutError("provider timed out")))

        await agent._handle_task(_task())

        self.assertTrue(_sent_results(agent))

    async def test_failure_reply_still_counts_the_task_as_failed(self):
        agent = _make_agent(_Boom(ValueError("nope")))

        await agent._handle_task(_task())

        self.assertEqual(agent.metrics.tasks_failed, 1)

    async def test_success_still_replies_normally(self):
        class _Ok:
            async def complete(self, messages=None, system=None, **_kw):
                return "the answer", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

        agent = _make_agent(_Ok())

        await agent._handle_task(_task())

        results = _sent_results(agent)
        self.assertEqual(results[0]["text"], "the answer")
        self.assertEqual(results[0]["_task_id"], "t-42")

    async def test_no_reply_target_means_no_send(self):
        """Fire-and-forget tasks must not gain a phantom reply."""
        agent = _make_agent(_Boom(ValueError("nope")))
        msg = Message(type=MessageType.TASK, sender_id="", payload={"text": "x"})

        await agent._handle_task(msg)

        self.assertEqual(_sent_results(agent), [])

"""Tests for chat result presentation at the IO gateway."""

import unittest
from unittest import mock

from wactorz.agents.io_agent import IOAgent
from wactorz.core.actor import Message, MessageType


class IOAgentResultTest(unittest.IsolatedAsyncioTestCase):
    """Reachy speaks its own turn, so its result must not also become a bubble.

    Both cases clear the request by its correlation id, like any other reply —
    a suppressed one included, or it would sit in ``_pending_replies`` until
    ``_expire_pending`` reclaimed it (see test_io_agent_reply_correlation).
    """

    async def test_opt_in_suppressed_result_is_consumed_without_a_chat_bubble(self):
        agent = object.__new__(IOAgent)
        agent._pending_replies = {"t1": ("user", 0.0)}
        agent._reply = mock.AsyncMock()

        await agent.handle_message(
            Message(
                type=MessageType.RESULT,
                sender_id="reachy-mini",
                payload={"ok": True, "_task_id": "t1", "_suppress_reply": True},
            )
        )

        self.assertEqual(agent._pending_replies, {})
        agent._reply.assert_not_awaited()

    async def test_ordinary_result_still_reaches_chat(self):
        agent = object.__new__(IOAgent)
        agent._pending_replies = {"t1": ("user", 0.0)}
        agent._reply = mock.AsyncMock()

        await agent.handle_message(
            Message(
                type=MessageType.RESULT,
                sender_id="reachy-mini",
                payload={"ok": True, "_task_id": "t1", "result": "Done."},
            )
        )

        self.assertEqual(agent._pending_replies, {})
        agent._reply.assert_awaited_once_with("Done.", to="user")

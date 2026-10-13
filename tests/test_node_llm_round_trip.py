"""What a node agent's model requests take, there through main and back.

The model call itself is timed on main, where it is made. A node agent waits
for the whole round trip, the broker both ways included, and a node serves no
`/metrics`, so the agent's metrics frame carries it: the recent p50 and p95,
and how many requests main never answered in time.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.node.agent import NodeAgent
from wactorz.node.llm import BridgeProvider
from wactorz.node.runner import NodeRunner


def _agent(tmp_path: Path) -> NodeAgent:
    runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
    return NodeAgent({"name": "asker", "code": ""}, runner)


class TestTheFrame:
    async def test_an_answered_request_is_timed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        assert "llm_round_trip_p50_s" not in agent._build_metrics()

        async def _answers(_topic: str, _payload: Any, timeout: float) -> Any:
            return {"text": "high tide at noon", "usage": {}}

        monkeypatch.setattr(agent, "ask_main", _answers)

        text, _usage = await BridgeProvider(agent)._complete([{"role": "user", "content": "tide?"}])

        frame = agent._build_metrics()
        assert text == "high tide at noon"
        assert {"llm_round_trip_p50_s", "llm_round_trip_p95_s"} <= frame.keys()
        assert frame["llm_timeouts"] == 0

    async def test_a_request_main_never_answered_is_a_timeout(self, tmp_path: Path) -> None:
        # Through the real request path: it waits with `asyncio.wait_for`, whose
        # timeout is asyncio's own TimeoutError on Python 3.10 and the builtin
        # from 3.11, and turns either into no reply.
        agent = _agent(tmp_path)

        text, _usage = await BridgeProvider(agent)._complete(
            [{"role": "user", "content": "tide?"}], timeout=0.05
        )

        frame = agent._build_metrics()
        assert text == ""
        assert frame["llm_timeouts"] == 1
        assert frame["llm_round_trip_p50_s"] >= 0.05

    async def test_a_request_that_could_not_be_sent_is_counted_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not a timeout: main never received it.
        agent = _agent(tmp_path)

        async def _broker_gone(_topic: str, _payload: Any, timeout: float) -> Any:
            raise OSError("the broker is not there")

        monkeypatch.setattr(agent, "ask_main", _broker_gone)

        with pytest.raises(OSError):
            await BridgeProvider(agent)._complete([{"role": "user", "content": "tide?"}])

        frame = agent._build_metrics()
        assert frame["llm_unsent"] == 1
        assert frame["llm_timeouts"] == 0

"""A main and a node kept busy for a while, watched for anything that only grows.

The other tests here show that each thing works once. This one does the same
few things over and over -- an agent spawned on the node and one on main, each
asked a few questions, both deleted -- and looks at what is left behind after
every round. A leak is a number that should come back to where it was and does
not: a task nobody awaits, a broker connection nobody closed, a supervisor
entry for an agent that is gone, a file descriptor, memory.

What counts as a failure is decided here, before the run:

- every question is answered, and every spawn and delete completes;
- after each round the registries, the supervisors, the node's agent list, the
  pending replies and the outbox are exactly as they were before it;
- nothing was dropped from the outbox or refused by a mailbox;
- the event loop never stopped for as long as the lag monitor reports on, at
  any point in a round and not only when the round was looked at;
- tasks, threads and open files end no higher than they were after the first
  rounds, within a small allowance;
- memory, over a run long enough to tell, grows by less than a set amount per
  round.

Skipped unless ``WACTORZ_SOAK_SECONDS`` says how long to run. ``make soak``
starts the broker and sets it; ``WACTORZ_SOAK_REPORT`` names a file to write
the samples to.
"""

import asyncio
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.monitoring.loop_lag import REPORT_AFTER_S, LoopLagMonitor

from .conftest import FastNode, until

SECONDS = float(os.environ.get("WACTORZ_SOAK_SECONDS", "0") or 0)

pytestmark = [
    pytest.mark.real_mqtt_client,
    pytest.mark.skipif(SECONDS <= 0, reason="set WACTORZ_SOAK_SECONDS, or run `make soak`"),
    # The run itself, and room to start, settle and stop around it.
    pytest.mark.timeout(SECONDS + 300),
]

#: Rounds before the numbers are taken as the ones to hold to: imports, caches
#: and connection pools fill up during the first few, and that is not a leak.
WARM_UP_ROUNDS = 5

#: How far above its level after the warm-up each may end. Not zero: a
#: heartbeat or a reconnect can be in flight at the moment of a sample.
ALLOWED_MORE = {"tasks": 5, "threads": 4, "open_files": 6}

#: Memory growth per round, after the warm-up, that is taken for a leak, and
#: how many rounds it takes before the figure means anything.
LEAK_KB_PER_ROUND = 50.0
ROUNDS_TO_JUDGE_MEMORY = 100

#: An agent that answers a task with what it was sent, and remembers how many.
ECHO = """
async def setup(agent):
    agent.state["seen"] = int(agent.recall("seen") or 0)


async def handle_task(agent, payload):
    agent.state["seen"] += 1
    agent.persist("seen", agent.state["seen"])
    return {"result": "echo:" + str(payload.get("text", ""))}
"""

QUESTIONS_PER_AGENT = 3


@dataclass(frozen=True)
class Sample:
    """What is left once a round has ended and settled."""

    round: int
    seconds: float
    tasks: int
    threads: int
    open_files: int
    rss_kb: int
    main_actors: int
    main_supervised: int
    node_agents: int
    node_supervised: int
    spawn_registry: int
    pending_replies: int
    outbox: int
    outbox_lost: int
    mailbox_refused: int
    loop_lag_s: float


def _open_files(process: psutil.Process) -> int:
    # Windows has handles, not descriptors, and counts far more things as one.
    return process.num_fds() if hasattr(process, "num_fds") else 0


def _sample(
    number: int,
    started: float,
    main: MainActor,
    node: FastNode,
    process: psutil.Process,
    lag: LoopLagMonitor,
) -> Sample:
    registry = main._registry
    assert registry is not None
    publisher: Any = main._mqtt_client
    supervisor: Any = getattr(registry, "_supervisor_ref", None)
    return Sample(
        round=number,
        seconds=round(time.monotonic() - started, 1),
        tasks=len(asyncio.all_tasks()),
        threads=threading.active_count(),
        open_files=_open_files(process),
        rss_kb=process.memory_info().rss // 1024,
        main_actors=len(registry.all_actors()),
        main_supervised=len(supervisor._specs) if supervisor is not None else 0,
        node_agents=len(node.agents),
        node_supervised=len(node.supervisor._specs),
        spawn_registry=len(main._get_spawn_registry()),
        pending_replies=len(main._result_futures),
        outbox=publisher.queue_depth + publisher.backlog_depth,
        outbox_lost=publisher.dropped + publisher.discarded,
        mailbox_refused=sum(actor.metrics.messages_refused for actor in registry.all_actors()),
        loop_lag_s=round(lag.longest, 3),
    )


async def _ask(main: MainActor, name: str, number: int) -> None:
    for question in range(QUESTIONS_PER_AGENT):
        text = f"{number}-{question}"
        reply = await asyncio.wait_for(
            main.delegation.delegate_task(name, text, timeout=30), timeout=60
        )
        assert reply is not None, f"round {number}: '{name}' never answered {text!r}"
        assert reply.get("result") == f"echo:{text}", f"round {number}: '{name}' said {reply!r}"


async def _one_round(main: MainActor, node: FastNode, number: int) -> None:
    """Spawn an agent on the node and one on main, question both, delete both."""
    registry = main._registry
    assert registry is not None
    remote, local = f"soak-remote-{number}", f"soak-local-{number}"

    await main._spawn_remote(
        {"name": remote, "type": "dynamic", "code": ECHO}, node.node_name, save=True
    )
    await main._spawn_local_from_config(
        {"name": local, "type": "dynamic", "code": ECHO}, blocking_install=True
    )
    seen = lambda: main._known_nodes.get(node.node_name, {}).get("agents", [])  # noqa: E731
    await until(lambda: remote in seen(), f"round {number}: main seeing '{remote}' on the node")
    await until(
        lambda: registry.find_by_name(local) is not None, f"round {number}: '{local}' running"
    )

    await _ask(main, remote, number)
    await _ask(main, local, number)

    await main.delete_spawned_agent(remote)
    await main.delete_spawned_agent(local)
    await until(lambda: node.get(remote) is None, f"round {number}: the node deleting '{remote}'")
    await until(lambda: remote not in seen(), f"round {number}: main seeing '{remote}' gone")
    await until(lambda: registry.find_by_name(local) is None, f"round {number}: '{local}' gone")


async def _settled(main: MainActor) -> None:
    """Wait for what a round set going to finish: the outbox empty, replies collected."""
    publisher: Any = main._mqtt_client
    await until(lambda: publisher.queue_depth + publisher.backlog_depth == 0, "the outbox emptying")
    await until(lambda: not main._result_futures, "every pending reply being collected")
    await asyncio.sleep(0.2)


def _problems(samples: list[Sample]) -> list[str]:
    """Everything the run did that the rules at the top do not allow."""
    first, steady = samples[0], samples[WARM_UP_ROUNDS:]
    problems: list[str] = []
    if not steady:
        return [f"the run ended after {len(samples) - 1} round(s), before the warm-up was over"]
    baseline, last = steady[0], samples[-1]

    for name in (
        "main_actors",
        "main_supervised",
        "node_agents",
        "node_supervised",
        "spawn_registry",
        "pending_replies",
        "outbox",
    ):
        wrong = [s for s in samples if getattr(s, name) != getattr(first, name)]
        if wrong:
            problems.append(
                f"{name} was {getattr(first, name)} before the first round and "
                f"{getattr(wrong[0], name)} after round {wrong[0].round}"
            )
    for name in ("outbox_lost", "mailbox_refused"):
        if getattr(last, name):
            problems.append(f"{name} ended at {getattr(last, name)}")
    slowest = max(samples, key=lambda s: s.loop_lag_s)
    if slowest.loop_lag_s >= REPORT_AFTER_S:
        problems.append(
            f"the event loop stopped for {slowest.loop_lag_s}s around round {slowest.round}"
        )
    for name, allowed in ALLOWED_MORE.items():
        if getattr(last, name) > getattr(baseline, name) + allowed:
            problems.append(
                f"{name} went from {getattr(baseline, name)} after the warm-up to "
                f"{getattr(last, name)} after round {last.round}"
            )
    rounds = last.round - baseline.round
    if rounds >= ROUNDS_TO_JUDGE_MEMORY:
        per_round = (last.rss_kb - baseline.rss_kb) / rounds
        if per_round > LEAK_KB_PER_ROUND:
            problems.append(
                f"memory grew {per_round:.0f} KB a round over {rounds} rounds "
                f"({baseline.rss_kb // 1024} MB to {last.rss_kb // 1024} MB)"
            )
    return problems


def _report(samples: list[Sample], problems: list[str]) -> str:
    shown = [samples[0], *samples[WARM_UP_ROUNDS :: max(1, len(samples) // 12)], samples[-1]]
    lines = ["round  secs  tasks threads files  rss MB  actors node-agents outbox lag s"]
    lines += [
        f"{s.round:5d} {s.seconds:5.0f} {s.tasks:6d} {s.threads:7d} {s.open_files:5d} "
        f"{s.rss_kb / 1024:7.1f} {s.main_actors:7d} {s.node_agents:11d} {s.outbox:6d} "
        f"{s.loop_lag_s:5.2f}"
        for s in dict.fromkeys(shown)
    ]
    lines.append(f"{samples[-1].round} rounds in {samples[-1].seconds:.0f}s")
    lines += [f"PROBLEM: {problem}" for problem in problems]
    return "\n".join(lines)


async def test_nothing_grows_while_agents_come_and_go(main: MainActor, node: FastNode) -> None:
    process = psutil.Process()
    lag = LoopLagMonitor(interval=0.5)
    lag.start()
    started = time.monotonic()
    await _settled(main)
    samples = [_sample(0, started, main, node, process, lag)]
    lag.longest = 0.0
    try:
        number = 0
        while time.monotonic() - started < SECONDS:
            number += 1
            await _one_round(main, node, number)
            await _settled(main)
            samples.append(_sample(number, started, main, node, process, lag))
            # Each sample then holds the longest stop of its own round, however
            # many times the loop was timed during it.
            lag.longest = 0.0
    finally:
        lag.stop()
        problems = _problems(samples)
        report = _report(samples, problems)
        print("\n" + report)
        wanted = os.environ.get("WACTORZ_SOAK_REPORT", "").strip()
        if wanted:
            Path(wanted).write_text(
                json.dumps(
                    {"problems": problems, "samples": [asdict(s) for s in samples]}, indent=1
                ),
                encoding="utf-8",
            )

    assert problems == [], report

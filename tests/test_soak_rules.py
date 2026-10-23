"""The soak run's verdict, checked on runs that never happened.

`tests/broker/test_soak.py` keeps a main and a node busy and decides from the
samples it takes whether anything leaked. That decision is only worth having if
it would notice a leak, and a run that passes cannot show that it would. So the
rules are given made-up samples here: a clean run, and one for each thing they
exist to catch.
"""

from dataclasses import replace

import pytest

from tests.broker.test_soak import (
    ALLOWED_MORE,
    LEAK_KB_PER_ROUND,
    ROUNDS_TO_JUDGE_MEMORY,
    WARM_UP_ROUNDS,
    Sample,
    _problems,
    _report,
)
from wactorz.monitoring.loop_lag import REPORT_AFTER_S

CLEAN = Sample(
    round=0,
    seconds=0.0,
    tasks=37,
    threads=9,
    open_files=24,
    rss_kb=110_000,
    main_actors=1,
    main_supervised=1,
    node_agents=0,
    node_supervised=0,
    spawn_registry=0,
    pending_replies=0,
    outbox=0,
    outbox_lost=0,
    mailbox_refused=0,
    loop_lag_s=0.0,
)


def _run(rounds: int, **growth_per_round: float) -> list[Sample]:
    """A run of ``rounds`` rounds in which each named number grows by that much a round."""
    return [
        replace(
            CLEAN,
            round=number,
            seconds=float(number),
            **{
                name: type(getattr(CLEAN, name))(getattr(CLEAN, name) + per_round * number)
                for name, per_round in growth_per_round.items()
            },
        )
        for number in range(rounds + 1)
    ]


def test_a_run_where_everything_comes_back_has_no_problems() -> None:
    assert _problems(_run(200)) == []


def test_a_run_too_short_to_judge_says_so() -> None:
    (problem,) = _problems(_run(WARM_UP_ROUNDS - 1))

    assert "before the warm-up was over" in problem


@pytest.mark.parametrize(
    "name",
    [
        "main_actors",
        "main_supervised",
        "node_agents",
        "node_supervised",
        "spawn_registry",
        "pending_replies",
    ],
)
def test_something_left_behind_by_one_round_is_a_problem(name: str) -> None:
    # These come back exactly: an agent that was deleted is in none of them.
    samples = _run(50)
    samples[20] = replace(samples[20], **{name: getattr(CLEAN, name) + 1})

    (problem,) = _problems(samples)

    assert problem.startswith(f"{name} was {getattr(CLEAN, name)} before the first round")
    assert "after round 20" in problem


def test_a_message_queued_as_a_round_was_sampled_is_not_a_problem() -> None:
    # A heartbeat can be queued between the outbox being seen empty and the
    # sample; an outbox that never empties fails the run where it waits instead.
    samples = _run(50)
    samples[20] = replace(samples[20], outbox=2)

    assert _problems(samples) == []


@pytest.mark.parametrize("name", ["outbox_lost", "mailbox_refused"])
def test_anything_lost_or_refused_is_a_problem(name: str) -> None:
    samples = _run(50)
    samples[-1] = replace(samples[-1], **{name: 1})

    assert _problems(samples) == [f"{name} ended at 1"]


@pytest.mark.parametrize("name", sorted(ALLOWED_MORE))
def test_a_resource_that_climbs_is_a_problem_and_one_that_wobbles_is_not(name: str) -> None:
    climbing = _run(100, **{name: 0.5})
    wobbling = _run(100)
    wobbling[-1] = replace(wobbling[-1], **{name: getattr(CLEAN, name) + ALLOWED_MORE[name]})

    (problem,) = _problems(climbing)
    assert problem.startswith(f"{name} went from")
    assert _problems(wobbling) == []


def test_what_the_first_rounds_fill_up_is_not_counted() -> None:
    # Imports, caches and connection pools: there after the warm-up, and no more after.
    samples = [
        replace(sample, tasks=CLEAN.tasks + min(sample.round, WARM_UP_ROUNDS) * 4)
        for sample in _run(100)
    ]

    assert _problems(samples) == []


def test_memory_that_grows_every_round_is_a_problem_over_a_long_run() -> None:
    rounds = ROUNDS_TO_JUDGE_MEMORY + WARM_UP_ROUNDS

    (problem,) = _problems(_run(rounds, rss_kb=LEAK_KB_PER_ROUND * 2))

    assert problem.startswith("memory grew 100 KB a round")
    assert _problems(_run(rounds, rss_kb=LEAK_KB_PER_ROUND / 2)) == []


def test_memory_is_not_judged_on_a_run_too_short_to_tell() -> None:
    assert _problems(_run(ROUNDS_TO_JUDGE_MEMORY // 2, rss_kb=LEAK_KB_PER_ROUND * 10)) == []


def test_an_event_loop_that_stopped_is_a_problem() -> None:
    samples = _run(50)
    samples[30] = replace(samples[30], loop_lag_s=REPORT_AFTER_S + 1)

    (problem,) = _problems(samples)

    assert problem == f"the event loop stopped for {REPORT_AFTER_S + 1}s around round 30"


def test_the_report_shows_the_run_and_names_each_problem() -> None:
    samples = _run(50, tasks=1)
    problems = _problems(samples)

    report = _report(samples, problems)

    assert "50 rounds in 50s" in report
    assert report.count("PROBLEM: ") == len(problems) == 1

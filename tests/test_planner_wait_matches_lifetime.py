"""The caller must not outwait the planner it is waiting on.

A planner self-terminates at its lifetime cap and does not answer on the way
out, so once the cap has passed no reply can arrive. The caller waited 180s
against a 90s cap, spending the last 90 seconds of every stuck plan waiting for
something that could no longer be sent.

Both numbers now come from one value, so they cannot drift apart again.
"""

import inspect

from wactorz.agents.main import planning
from wactorz.agents.planner.agent import PlannerAgent


def test_the_wait_exceeds_the_planner_lifetime() -> None:
    # It must still be longer: a reply produced just before the cap needs time
    # to arrive.
    wait = PlannerAgent.DEFAULT_MAX_LIFETIME_S + planning.PLANNER_REPLY_GRACE_S
    assert wait > PlannerAgent.DEFAULT_MAX_LIFETIME_S


def test_the_wait_does_not_outlast_the_planner_by_much() -> None:
    # The grace covers delivery, not more planning. Anything approaching a
    # second lifetime means the caller is again waiting on a dead planner.
    assert planning.PLANNER_REPLY_GRACE_S < PlannerAgent.DEFAULT_MAX_LIFETIME_S / 2


def test_the_default_lifetime_is_what_a_planner_actually_gets() -> None:
    # The constant is only useful if it is the value the constructor applies.
    default = inspect.signature(PlannerAgent).parameters["max_lifetime_s"].default
    assert default == PlannerAgent.DEFAULT_MAX_LIFETIME_S


def test_the_caller_pins_the_lifetime_it_waits_on() -> None:
    # Passing it explicitly is what stops the two drifting: a later change to
    # the constructor default cannot silently outlive the caller's wait.
    source = inspect.getsource(planning)
    assert "max_lifetime_s=lifetime_s" in source
    assert "timeout=180" not in source

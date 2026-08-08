"""A sibling crash must not resurrect an actor that was deliberately stopped.

ONE_FOR_ALL and REST_FOR_ONE restart siblings of the crashed actor. Both walked
the whole order and restarted every entry, and `_restart_one` only guarded the
budget — so a spec retired for any other reason was spawned again.

`release()` is the common path there: it is how a deliberate stop, a delete, and
a planner self-terminating all end. Under either strategy, one unrelated crash
undid every one of those decisions.
"""

import pytest

from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
from wactorz.core.registry import ActorRegistry, Supervisor


class _Worker(Actor):
    """A minimal actor that does nothing but exist."""

    async def handle_message(self, message: Message) -> None:
        return None


def _inject(_actor: Actor) -> None:
    """Stand-in for ActorSystem's MQTT injection, which needs no broker here."""


@pytest.fixture(name="supervisor")
def supervisor_fixture() -> Supervisor:
    """A supervisor over a real registry, with no watch loop running."""
    return Supervisor(ActorRegistry(), _inject, poll_interval=0.01)


def _supervise(sup: Supervisor, name: str, strategy: SupervisorStrategy) -> None:
    sup.supervise(
        name,
        lambda n=name: _Worker(name=n),  # pyright: ignore[reportUnknownLambdaType]
        strategy=strategy,
        restart_delay=0,
    )
    sup._specs[name].actor = _Worker(name=name)


async def _crash(sup: Supervisor, name: str) -> None:
    spec = sup._specs[name]
    assert spec.actor is not None
    spec.actor.state = ActorState.FAILED
    await sup._supervise_one(name, spec)


@pytest.mark.parametrize(
    "strategy",
    [SupervisorStrategy.ONE_FOR_ALL, SupervisorStrategy.REST_FOR_ONE],
)
class TestADeliberatelyStoppedSibling:
    async def test_it_is_not_restarted_when_another_actor_crashes(
        self, supervisor: Supervisor, strategy: SupervisorStrategy
    ) -> None:
        # Registration order matters for REST_FOR_ONE: the stopped one must come
        # after the crasher, or it would be outside the affected set anyway and
        # the test would pass without proving anything.
        _supervise(supervisor, "crasher", strategy)
        _supervise(supervisor, "stopped", strategy)
        supervisor.release("stopped")
        assert supervisor._specs["stopped"].retired is True

        await _crash(supervisor, "crasher")

        stopped = supervisor._specs["stopped"]
        assert stopped.retired is True, "a deliberate stop was undone"
        assert stopped.actor is None, "a stopped actor was brought back to life"

    async def test_the_crashed_actor_itself_is_still_restarted(
        self, supervisor: Supervisor, strategy: SupervisorStrategy
    ) -> None:
        # The guard must not cost us the restart the strategy exists to perform.
        _supervise(supervisor, "crasher", strategy)
        _supervise(supervisor, "stopped", strategy)
        supervisor.release("stopped")

        await _crash(supervisor, "crasher")

        assert supervisor._specs["crasher"].actor is not None

    async def test_a_live_sibling_is_still_restarted(
        self, supervisor: Supervisor, strategy: SupervisorStrategy
    ) -> None:
        _supervise(supervisor, "crasher", strategy)
        _supervise(supervisor, "healthy", strategy)
        before = supervisor._specs["healthy"].actor

        await _crash(supervisor, "crasher")

        healthy = supervisor._specs["healthy"]
        assert healthy.retired is False
        assert healthy.actor is not None
        assert healthy.actor is not before, "the sibling should have been replaced"


class TestResupervisingClearsRetirement:
    async def test_a_re_supervised_spec_is_restarted_again(self, supervisor: Supervisor) -> None:
        # Retirement is not permanent — starting an agent again clears it, and
        # from then on the strategy should cover it as before.
        _supervise(supervisor, "crasher", SupervisorStrategy.ONE_FOR_ALL)
        _supervise(supervisor, "stopped", SupervisorStrategy.ONE_FOR_ALL)
        supervisor.release("stopped")
        supervisor.resupervise("stopped", _Worker(name="stopped"))
        assert supervisor._specs["stopped"].retired is False

        await _crash(supervisor, "crasher")

        assert supervisor._specs["stopped"].actor is not None

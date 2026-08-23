"""One interrupt, and it is gone in well under a second.

Two claims, and the second is the one that gets broken. Shutdown that eventually
works is shutdown that a container runtime kills at the ten-second mark, which
means state written on the way out is sometimes written and sometimes not - a
class of bug that reproduces once a fortnight and never on a developer machine.

Its own backend, obviously: the shared one has to survive for the scenarios after
this.
"""

from collections.abc import Callable

from harness import backend

#: What "well under a second" is allowed to mean. Generous against the claim -
#: the point is to catch a shutdown that waits on a timeout, and one that does
#: takes seconds, not milliseconds.
BUDGET_SECONDS = 1.0


def test_one_interrupt_is_enough(own_app: Callable[..., backend.Backend]) -> None:
    """A single signal, not a second one and not a kill.

    `interrupt` sends exactly one SIGINT and raises if the process needs more, so
    a shutdown that only works when pressed twice fails here rather than being
    quietly retried into a pass.
    """
    instance: backend.Backend = own_app()
    instance.interrupt()
    assert not instance.alive, "the backend was still running after it reported exiting"


def test_it_exits_quickly_and_quietly(own_app: Callable[..., backend.Backend]) -> None:
    instance: backend.Backend = own_app()
    elapsed = instance.interrupt()

    assert elapsed < BUDGET_SECONDS, (
        f"the backend took {elapsed:.2f}s to exit on one interrupt, over the {BUDGET_SECONDS}s "
        f"budget - long enough for a container runtime to kill it mid-write"
    )
    assert instance.process.returncode == 0, (
        f"an intentional stop exited with code {instance.process.returncode}; "
        f"whatever supervises the process will read that as a crash\n"
        f"{instance.console()[-2000:]}"
    )

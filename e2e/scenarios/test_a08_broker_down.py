"""A local command takes effect with the broker stopped.

The broker is how agents on other machines are reached, and it is easy to end up
routing everything through it - at which point losing it means losing control of
the agents running in this very process. This is the scenario that says local
stays local.

It skips when the reachable broker is not the development container this
repository starts. The suite may not stop a broker it does not own: the one on
port 1883 might be running somebody's house.
"""

from collections.abc import Iterator

import pytest
from harness import backend, broker, waiting


@pytest.fixture(autouse=True, name="broker_restored")
def broker_restored_fixture() -> Iterator[None]:
    """Whatever this scenario does, the broker is running when it ends.

    Autouse and unconditional: a failure part-way through would otherwise leave
    every scenario after this one running against no broker, and the report would
    blame them.
    """
    if not broker.controllable():
        pytest.skip(
            f"the broker on {broker.HOST}:{broker.PORT} is not the {broker.CONTAINER} "
            f"container, so this suite must not stop it"
        )
    try:
        yield
    finally:
        if not broker.reachable():
            broker.start()


def test_a_local_command_lands_with_the_broker_stopped(
    app: backend.Backend, spare_agent: str
) -> None:
    broker.stop()

    # Sent over the dashboard socket, which is the path the pause button uses and
    # has nothing to do with the broker. Deliberately not the REST route: that one
    # pauses the actor and leaves the reported state to arrive by MQTT, so with the
    # broker down it succeeds and is invisible - which would make this scenario
    # fail against a working system.
    app.rest.socket_command(spare_agent, "pause")

    waiting.until(
        lambda: app.rest.state_of(spare_agent) == "paused",
        what=f"{spare_agent!r} to actually pause with the broker down",
        timeout=60.0,
        interval=0.25,
    )


def test_the_system_recovers_when_the_broker_returns(
    app: backend.Backend, spare_agent: str
) -> None:
    """It reconnects on its own, and the agent it paused is still paused.

    The second half matters as much as the first: a reconnect that resynchronises
    from a retained message can quietly undo a command that was given while the
    broker was away, which is a worse failure than not accepting it.
    """
    broker.start()

    waiting.until(
        lambda: app.rest.ok("/health"),
        what="the backend to still be serving after the broker returned",
        timeout=60.0,
        interval=0.5,
    )
    assert app.rest.state_of(spare_agent) == "paused", (
        f"{spare_agent!r} was paused while the broker was down and is now "
        f"{app.rest.state_of(spare_agent)!r} - the reconnect undid it"
    )
    assert app.rest.command(spare_agent, "resume").ok, (
        "resuming after the broker returned was refused"
    )

import argparse
import asyncio
import json
import pathlib
import tempfile
import time
from dataclasses import dataclass

from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
from wactorz.core.registry import ActorSystem, Supervisor

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "parity_fixtures" / "backend_supervisor_parity.json"


@dataclass
class ActorTracker:
    name: str
    crash_remaining: int
    starts: int = 0


# A fixed path under /tmp is shared by every run at once — a second run of the
# harness, or a second checkout, reads and writes the same actor state. Held at
# module scope so it is cleaned up when the process exits.
_STATE = tempfile.TemporaryDirectory(prefix="wactorz-parity-")
STATE_DIR = _STATE.name


class ProbeActor(Actor):
    def __init__(self, tracker: ActorTracker, **kwargs):
        super().__init__(name=tracker.name, persistence_dir=STATE_DIR, **kwargs)
        self._tracker = tracker

    async def on_start(self):
        self._tracker.starts += 1
        if self._tracker.crash_remaining > 0:
            self._tracker.crash_remaining -= 1
            self.state = ActorState.FAILED

    async def handle_message(self, msg: Message):
        return None


#: How often the Supervisor under test sweeps for actors to restart. Named
#: because settling has to outlast it: a group restart that has begun but not
#: finished looks exactly like a settled system until the next sweep lands.
SUPERVISOR_POLL = 0.05


def _make_system() -> ActorSystem:
    system = ActorSystem()

    class _NoOpMQTT:
        async def publish(self, topic, payload):
            return None

        async def disconnect(self):
            return None

    system._mqtt_client = _NoOpMQTT()  # pyright: ignore[reportAttributeAccessIssue]
    system._supervisor = Supervisor(system.registry, system._inject, poll_interval=SUPERVISOR_POLL)
    return system


def _normalize_state(actor) -> str:
    """The actor's state, or `missing` when it is not in the registry at all.

    Reporting the absence rather than raising keeps a failure legible: the
    comparison against the fixture then shows `missing` where `running` was
    expected, instead of a KeyError several frames from the thing that is wrong.
    """
    if actor is None:
        return "missing"
    value = actor.state.value
    return "running" if value == "running" else value.lower()


async def _wait_until_settled(
    system: ActorSystem,
    trackers: dict[str, "ActorTracker"],
    *,
    timeout: float = 10.0,
    quiet: float = 0.15,
) -> None:
    """Wait for the supervisor to finish reacting, rather than for a fixed delay.

    A restart is not instantaneous: the supervisor stops an actor, saves its
    state and starts a replacement, and the save alone has been seen taking
    longer on CI than any sleep worth writing. Reading the registry part-way
    through finds an actor missing or stopped, which looks like a supervision
    bug and is not one.

    Settled means **every supervised actor is registered and running**, and has
    stayed that way briefly. The state check is what makes this reliable: while a
    restart is in flight the actor is typically still registered but `stopped` or
    `failed`, so presence alone — or a snapshot that merely stops changing —
    reports a system caught mid-restart as a settled one.

    The quiet window is the secondary guard, for `one_for_all`, where every actor
    is momentarily running again between the first restart and the rest.

    Returns on timeout rather than raising, so a system that never settles is
    reported by the fixture comparison, which can say which actor is wrong,
    rather than by an exception here.

    ⚠ Assumes every scenario ends with its actors running, which is what the
    fixture describes today. A scenario whose expected end state is `failed` —
    an actor that exhausts `max_restarts`, say — would sit here until the
    timeout; give it its own predicate rather than widening this one, or the
    wait starts asserting the thing the fixture is supposed to assert.
    """
    expected = set(trackers)
    deadline = time.monotonic() + timeout
    previous: tuple | None = None
    unchanged_since = time.monotonic()

    while time.monotonic() < deadline:
        actors = {actor.name: actor for actor in system.registry.all_actors()}
        running = {name for name, actor in actors.items() if actor.state is ActorState.RUNNING}
        snapshot = (tuple(sorted(running)), tuple(t.starts for t in trackers.values()))
        if snapshot != previous:
            previous, unchanged_since = snapshot, time.monotonic()
        elif expected <= running and time.monotonic() - unchanged_since >= quiet:
            return
        await asyncio.sleep(0.02)


async def _run_scenario(scenario: dict) -> dict:
    system = _make_system()
    strategy = SupervisorStrategy(scenario["strategy"])
    trackers: dict[str, ActorTracker] = {}

    for actor_cfg in scenario["actors"]:
        tracker = ActorTracker(
            name=actor_cfg["name"],
            crash_remaining=actor_cfg.get("crash_count", 0),
        )
        trackers[tracker.name] = tracker

        def factory(tracker=tracker):
            return ProbeActor(tracker=tracker)

        system.supervisor.supervise(
            tracker.name,
            factory,
            strategy=strategy,
            max_restarts=3,
            restart_delay=0.0,
        )

    await system.supervisor.start()
    await _wait_until_settled(system, trackers)

    status_rows = {row["name"]: row for row in system.supervisor.status()}
    registry_rows = {actor.name: actor for actor in system.registry.all_actors()}

    result = {
        "scenario": scenario["name"],
        "actors": {
            name: {
                "starts": tracker.starts,
                "restart_count": int(status_rows[name]["restarts_used"]),
                "final_state": _normalize_state(registry_rows.get(name)),
            }
            for name, tracker in trackers.items()
        },
    }

    await system.supervisor.stop()
    return result


async def run_fixtures(path: pathlib.Path = FIXTURE_PATH) -> dict:
    payload = json.loads(path.read_text())
    results = []
    for scenario in payload["scenarios"]:
        results.append(await _run_scenario(scenario))
    return {"contract": payload["contract"], "results": results}


def _expected_payload(path: pathlib.Path = FIXTURE_PATH) -> dict:
    payload = json.loads(path.read_text())
    results = []
    for scenario in payload["scenarios"]:
        results.append(
            {
                "scenario": scenario["name"],
                "actors": {
                    name: {
                        "starts": starts,
                        "restart_count": scenario["expected"]["restart_counts"][name],
                        "final_state": scenario["expected"]["final_states"][name],
                    }
                    for name, starts in scenario["expected"]["start_counts"].items()
                },
            }
        )
    return {"contract": payload["contract"], "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(FIXTURE_PATH))
    parser.add_argument("--assert-expected", action="store_true")
    args = parser.parse_args()

    fixture_path = pathlib.Path(args.fixture)
    actual = asyncio.run(run_fixtures(fixture_path))
    if args.assert_expected:
        expected = _expected_payload(fixture_path)
        if actual != expected:
            print(json.dumps({"expected": expected, "actual": actual}, indent=2, sort_keys=True))
            return 1
    print(json.dumps(actual, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

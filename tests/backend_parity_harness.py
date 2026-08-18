import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "parity_fixtures" / "backend_supervisor_parity.json"


def _ensure_importable(name: str) -> None:
    """Guarantee ``name`` can be imported by the hand-loaded core modules.

    ``actor.py`` and ``registry.py`` import ``aiomqtt`` / ``psutil`` at load
    time. Both are core dependencies so they are normally installed; only when
    one is genuinely missing do we insert an empty placeholder so ``_load``
    below can still exec the module. We never replace a real, importable module:
    leaving a bare stub in ``sys.modules`` would shadow it for every other test
    in this shared process.
    """
    if name in sys.modules:
        return
    try:
        importlib.import_module(name)
    except Exception:
        sys.modules[name] = types.ModuleType(name)


for _module in ("aiomqtt", "psutil"):
    _ensure_importable(_module)


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_load("wactorz.core.actor", ROOT / "wactorz" / "core" / "actor.py")
_load("wactorz.core.registry", ROOT / "wactorz" / "core" / "registry.py")

from wactorz.core.actor import Actor, ActorState, Message, SupervisorStrategy
from wactorz.core.registry import ActorSystem, Supervisor


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
    value = actor.state.value
    return "running" if value == "running" else value.lower()


async def _settled(
    system, trackers: dict[str, ActorTracker], poll: float = 0.02, timeout: float = 10.0
) -> None:
    """Wait until the supervisor has finished restarting things, then return.

    This replaced a flat 0.35s sleep. Crash-and-restart is work, not a duration,
    so a fixed wait encodes how fast the machine happened to be: run the suite
    across every core (`make test` uses `-n auto`) and the wait that was ample
    on an idle machine expires mid-restart, reporting a short start count as a
    contract violation.

    Settled means every actor is running again with its scripted crashes spent —
    read from the harness's own inputs, never from the fixture's expected
    numbers, which would leave the check marking its own work. That alone is not
    enough under the group strategies: `one_for_all` and `rest_for_one` restart
    siblings too, and between the crashed actor coming back and its siblings
    going down the system briefly looks finished. So the state also has to hold
    still across more than one supervisor sweep before it is believed.

    The timeout is only a backstop against a supervisor that never settles, so
    it is far longer than the work can need.
    """
    quiet_polls = max(3, int(SUPERVISOR_POLL * 2 / poll))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: tuple | None = None
    stable = 0
    while loop.time() < deadline:
        await asyncio.sleep(poll)
        states = {actor.name: _normalize_state(actor) for actor in system.registry.all_actors()}
        snapshot = (
            tuple(t.starts for t in trackers.values()),
            tuple(sorted((r["name"], int(r["restarts_used"])) for r in system.supervisor.status())),
        )
        stable = stable + 1 if snapshot == last else 0
        last = snapshot
        done = all(t.crash_remaining == 0 for t in trackers.values()) and all(
            states.get(name) == "running" for name in trackers
        )
        if done and stable >= quiet_polls:
            return


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
    await _settled(system, trackers)

    status_rows = {row["name"]: row for row in system.supervisor.status()}
    registry_rows = {actor.name: actor for actor in system.registry.all_actors()}

    result = {
        "scenario": scenario["name"],
        "actors": {
            name: {
                "starts": tracker.starts,
                "restart_count": int(status_rows[name]["restarts_used"]),
                "final_state": _normalize_state(registry_rows[name]),
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

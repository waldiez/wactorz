import argparse
import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from dataclasses import dataclass


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "parity_fixtures" / "backend_supervisor_parity.json"


def _stub(name: str) -> None:
    sys.modules.setdefault(name, types.ModuleType(name))


for _module in [
    "aiomqtt",
    "psutil",
]:
    _stub(_module)


_psutil = sys.modules["psutil"]


class _FakeProc:
    def cpu_percent(self, interval=None):
        return 0.0

    def memory_info(self):
        return type("m", (), {"rss": 0})()


_psutil.Process = lambda: _FakeProc()

def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
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


class ProbeActor(Actor):
    def __init__(self, tracker: ActorTracker, **kwargs):
        super().__init__(name=tracker.name, persistence_dir="/tmp/wactorz_backend_parity", **kwargs)
        self._tracker = tracker

    async def on_start(self):
        self._tracker.starts += 1
        if self._tracker.crash_remaining > 0:
            self._tracker.crash_remaining -= 1
            self.state = ActorState.FAILED

    async def handle_message(self, msg: Message):
        return None


def _make_system() -> ActorSystem:
    system = ActorSystem()

    class _NoOpMQTT:
        async def publish(self, topic, payload):
            return None

        async def disconnect(self):
            return None

    system._mqtt_client = _NoOpMQTT()
    system._supervisor = Supervisor(system.registry, system._inject, poll_interval=0.05)
    return system


def _normalize_state(actor) -> str:
    value = actor.state.value
    return "running" if value == "running" else value.lower()


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
    await asyncio.sleep(0.35)

    status_rows = {
        row["name"]: row
        for row in system.supervisor.status()
    }
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

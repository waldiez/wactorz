"""Liveness and readiness probes, on the monitor and on the REST interface.

Liveness answers whenever the process can answer, whatever it depends on is
doing; readiness answers 503 naming the check that failed until the process
should be sent traffic. Both are reachable without a key and under any host
name, and neither may be cached.
"""

import asyncio
import threading
from collections.abc import AsyncGenerator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.config import CONFIG
from wactorz.core.actor import ActorState
from wactorz.core.persistence import WactorzDB
from wactorz.core.registry import ActorRegistry, ActorSystem, Supervisor
from wactorz.interfaces.chat.rest import RESTInterface
from wactorz.web import probes, runtime
from wactorz.web.app import build_app as build_monitor_app

KEY = "a-key-long-enough-to-not-be-warned-about"

LIVENESS = sorted(probes.LIVENESS_PATHS)
READINESS = sorted(probes.READINESS_PATHS)
ALL_PROBES = sorted(probes.PROBE_PATHS)


class _Main:
    """The part of an actor the main check reads."""

    def __init__(self, state: ActorState = ActorState.RUNNING) -> None:
        self.state = state


class _Registry:
    def __init__(self, main: _Main | None) -> None:
        self.main = main

    def find_by_name(self, name: str) -> _Main | None:
        return self.main if name == "main" else None


class _Supervisor:
    def __init__(self, running: bool) -> None:
        self.running = running


class _System:
    """What the readiness checks read off an ActorSystem, each settable."""

    def __init__(
        self,
        *,
        running: bool = True,
        stopping: bool = False,
        main: _Main | None = None,
        connected: bool = True,
    ) -> None:
        self.supervisor = _Supervisor(running)
        self.stopping = stopping
        self.registry = _Registry(main if main is not None else _Main())
        self.connected = connected

    def mqtt_status(self) -> dict[str, Any]:
        return {"connected": self.connected}


def _as_system(fake: _System) -> ActorSystem:
    return cast(ActorSystem, fake)


class _RestMain:
    """The parts of MainActor that RESTInterface reaches for at build time."""

    _registry = None


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    """A real database, installed as the one the database check asks."""
    db = WactorzDB(tmp_path / "wactorz.db")
    monkeypatch.setattr(probes, "get_db", lambda: db)
    yield db
    db.close()


@pytest.fixture(name="monitor")
async def monitor_fixture(
    monkeypatch: pytest.MonkeyPatch, db: WactorzDB
) -> AsyncGenerator[TestClient, Any]:
    """The real monitor app, with a key set."""
    monkeypatch.setattr("wactorz.config.CONFIG", replace(CONFIG, api_key=KEY))
    monkeypatch.setattr(runtime, "system", None)
    monkeypatch.setattr(runtime, "mqtt_connected", False)
    async with TestClient(TestServer(build_monitor_app())) as client:
        yield client


def _rest_client(system: _System | None, api_key: str | None = KEY) -> TestClient:
    iface = RESTInterface(
        cast(Any, _RestMain()),
        port=0,
        api_key=api_key,
        system=None if system is None else _as_system(system),
    )
    return TestClient(TestServer(iface.build_app()))


class TestLiveness:
    @pytest.mark.parametrize("path", LIVENESS)
    async def test_the_monitor_answers_without_a_key(self, monitor: TestClient, path: str) -> None:
        resp = await monitor.get(path)

        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}

    @pytest.mark.parametrize("path", LIVENESS)
    async def test_the_rest_interface_answers_without_a_key(self, path: str) -> None:
        async with _rest_client(_System()) as client:
            resp = await client.get(path)

            assert resp.status == 200
            assert await resp.json() == {"status": "ok"}

    async def test_it_does_not_depend_on_the_broker_or_the_system(
        self, monitor: TestClient
    ) -> None:
        # A liveness failure is a restart; restarting over a broker outage fixes nothing.
        runtime.system = _as_system(_System(running=False, connected=False, stopping=True))

        assert (await monitor.get("/health")).status == 200

    async def test_head_is_answered_too(self, monitor: TestClient) -> None:
        assert (await monitor.head("/healthz")).status == 200


class TestReadinessOfARunningSystem:
    async def test_ready_when_every_check_passes(self, db: WactorzDB) -> None:
        checks = await probes.readiness(_as_system(_System()))

        assert checks == {"supervisor": "ok", "main": "ok", "broker": "ok", "database": "ok"}

    async def test_not_ready_before_the_supervisor_has_started(self, db: WactorzDB) -> None:
        checks = await probes.readiness(_as_system(_System(running=False)))

        assert checks["supervisor"] == "not started"

    async def test_not_ready_once_shutdown_has_begun(self, db: WactorzDB) -> None:
        # Out of rotation first, so nothing new arrives while the agents stop.
        checks = await probes.readiness(_as_system(_System(stopping=True)))

        assert checks["supervisor"] == "stopping"

    async def test_not_ready_without_main(self, db: WactorzDB) -> None:
        fake = _System()
        fake.registry.main = None

        checks = await probes.readiness(_as_system(fake))

        assert checks["main"] == "missing"

    @pytest.mark.parametrize("state", [ActorState.FAILED, ActorState.IDLE, ActorState.STOPPED])
    async def test_not_ready_while_main_is_not_running(
        self, db: WactorzDB, state: ActorState
    ) -> None:
        checks = await probes.readiness(_as_system(_System(main=_Main(state))))

        assert checks["main"] == state.value

    async def test_not_ready_while_the_broker_is_disconnected(self, db: WactorzDB) -> None:
        checks = await probes.readiness(_as_system(_System(connected=False)))

        assert checks["broker"] == "disconnected"

    async def test_not_ready_without_a_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(probes, "get_db", lambda: None)

        checks = await probes.readiness(_as_system(_System()))

        assert checks["database"] == "not open"

    async def test_not_ready_while_the_database_is_closed(self, db: WactorzDB) -> None:
        db.close()

        checks = await probes.readiness(_as_system(_System()))

        assert checks["database"] == "unavailable"

    async def test_a_database_held_by_a_writer_is_reported_without_waiting_for_it(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(probes, "DB_PING_TIMEOUT_S", 0.05)
        release = threading.Event()
        held = threading.Event()

        def hold() -> None:
            with db.transaction():
                held.set()
                release.wait(5)

        writer = threading.Thread(target=hold)
        writer.start()
        try:
            held.wait(5)
            checks = await asyncio.wait_for(probes.readiness(_as_system(_System())), timeout=2)
        finally:
            release.set()
            writer.join()

        assert checks["database"] == "unavailable"


class TestReadinessOverHttp:
    @pytest.mark.parametrize("path", READINESS)
    async def test_the_rest_interface_answers_200_when_ready(
        self, db: WactorzDB, path: str
    ) -> None:
        async with _rest_client(_System()) as client:
            resp = await client.get(path)

            assert resp.status == 200
            assert (await resp.json())["status"] == "ready"

    @pytest.mark.parametrize("path", READINESS)
    async def test_the_rest_interface_answers_503_naming_the_check(
        self, db: WactorzDB, path: str
    ) -> None:
        async with _rest_client(_System(connected=False)) as client:
            resp = await client.get(path)

            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "not ready"
            assert body["checks"]["broker"] == "disconnected"
            assert body["checks"]["main"] == "ok"

    async def test_a_rest_interface_with_no_system_is_never_ready(self) -> None:
        async with _rest_client(None) as client:
            resp = await client.get("/ready")

            assert resp.status == 503

    @pytest.mark.parametrize("path", READINESS)
    async def test_the_monitor_reports_its_system(self, monitor: TestClient, path: str) -> None:
        runtime.system = _as_system(_System())
        assert (await monitor.get(path)).status == 200

        runtime.system = _as_system(_System(main=_Main(ActorState.FAILED)))
        resp = await monitor.get(path)

        assert resp.status == 503
        assert (await resp.json())["checks"]["main"] == "failed"

    async def test_a_monitor_on_its_own_reports_its_broker_link(self, monitor: TestClient) -> None:
        # No actors in its process: the link it listens on is all it depends on.
        runtime.mqtt_connected = False
        resp = await monitor.get("/readyz")
        assert resp.status == 503
        assert (await resp.json())["checks"] == {"broker": "disconnected"}

        runtime.mqtt_connected = True
        assert (await monitor.get("/readyz")).status == 200


class TestEveryProbe:
    @pytest.mark.parametrize("path", ALL_PROBES)
    async def test_is_never_cached(self, monitor: TestClient, path: str) -> None:
        # A cached "ready" outlives the moment it described.
        assert (await monitor.get(path)).headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize("path", ALL_PROBES)
    async def test_the_monitor_answers_under_any_host_name(
        self, monitor: TestClient, path: str
    ) -> None:
        # An orchestrator or load balancer asks under a name of its own.
        resp = await monitor.get(path, headers={"Host": "wactorz.default.svc:8888"})

        assert resp.status != 403

    async def test_an_ordinary_route_still_refuses_an_unknown_host(
        self, monitor: TestClient
    ) -> None:
        resp = await monitor.get(
            "/api/actors", headers={"Host": "attacker.example:8888", "X-API-Key": KEY}
        )

        assert resp.status == 403

    @pytest.mark.parametrize("path", ALL_PROBES)
    async def test_the_open_rest_interface_answers_under_any_host_name(
        self, db: WactorzDB, path: str
    ) -> None:
        async with _rest_client(_System(), api_key=None) as client:
            resp = await client.get(path, headers={"Host": "lb.internal:8000"})

            assert resp.status == 200


class TestWhatTheChecksRead:
    async def test_a_supervisor_runs_from_start_until_stop(self) -> None:
        supervisor = Supervisor(ActorRegistry(), lambda _actor: None)
        assert not supervisor.running

        await supervisor.start()
        assert supervisor.running

        await supervisor.stop()
        assert not supervisor.running

    async def test_a_system_is_stopping_once_stop_all_begins(self, tmp_path: Path) -> None:
        system = ActorSystem(state_dir=str(tmp_path))
        assert not system.stopping

        await system.stop_all()

        assert system.stopping

    def test_ping_answers_on_an_open_database(self, db: WactorzDB) -> None:
        assert db.ping(timeout=1)

    def test_ping_fails_on_a_closed_one(self, db: WactorzDB) -> None:
        db.close()

        assert not db.ping(timeout=1)

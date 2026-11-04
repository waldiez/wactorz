"""The imu_anomaly example: the model stored and served by version, its scripts, its triage, and the FastAPI app.

What it needs that is not installed for the suite is stood in for.
"""

import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.examples import EXAMPLES, load, no_wait
from wactorz.agents.function_agent import spec_of
from wactorz.config import CONFIG
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.labels import LabelSpec
from wactorz.core.outbound import check_options
from wactorz.core.paths import resolve_state_dir
from wactorz.web.app import build_app


class TestTheFastAPIExample:
    async def test_the_app_starts_the_system_in_its_lifespan_and_stops_it_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        import wactorz

        served: dict[str, Any] = {}

        async def fake_serve(**kwargs: Any) -> None:
            served["kwargs"] = kwargs
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                served["stopped"] = True
                raise

        monkeypatch.setattr(wactorz, "serve", fake_serve)
        app_module = load("imu_anomaly", "fastapi_app")

        async with app_module.lifespan(app_module.app):
            assert served["kwargs"]["minimal"] is True
            assert "stopped" not in served
        assert served["stopped"] is True

    async def test_a_refused_start_fails_the_app_rather_than_its_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        import wactorz

        async def refusing_serve(**kwargs: Any) -> None:
            raise wactorz.StartupError("exposed")

        monkeypatch.setattr(wactorz, "serve", refusing_serve)
        app_module = load("imu_anomaly", "fastapi_app")

        with pytest.raises(wactorz.StartupError):
            async with app_module.lifespan(app_module.app):
                pass


class TestTheImuModel:
    """`train.py` keeps the model on main; the detector names it and loads it as arrays."""

    @staticmethod
    def _imported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        # Another example's `model` or `agent` may be imported already, under the same name.
        for name in ("model", "agent"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        train = importlib.import_module("train")
        monkeypatch.delitem(sys.modules, "train")
        monkeypatch.setattr(train, "MODEL_FILE", tmp_path / "imu_model.npz")
        return train

    def test_training_stores_a_new_version_each_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("numpy")
        train = self._imported(monkeypatch, tmp_path)
        state = tmp_path / "state"

        first = train.train(state)
        train.main(["--state-dir", str(state)])

        store = ArtifactStore.in_state_dir(state)
        assert first.reference == "imu-detector:1"
        assert [a.version for a in store.versions("imu-detector")] == [1, 2]
        assert first.format == "npz" and not first.runs_code

    async def test_the_detector_flags_a_jolt_and_not_rest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("numpy")
        train = self._imported(monkeypatch, tmp_path)
        train.train(resolve_state_dir())
        detect = importlib.import_module("agent").detect
        spec = spec_of(detect)
        assert spec is not None and spec.models == {"model": "imu-detector"}
        me = spec.build(persistence_dir=str(tmp_path / "agents"))

        jolt = await detect({"ax": 9.0, "ay": -7.5, "az": 1.0}, me)
        rest = await detect({"ax": 0.1, "ay": 0.0, "az": 1.0}, me)

        assert jolt is not None and jolt["score"] > 20
        assert rest is None
        assert me.recall("anomalies_total") == 1

    def test_the_scripts_keep_state_beside_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = TestTheImuRunScript._recorded_run(monkeypatch)

        load("imu_anomaly", "run").main([])

        assert Path(called["state_dir"]) == EXAMPLES / "imu_anomaly" / "state"


class TestTheImuVersions:
    """Each version keeps its held-out score; a pointer moved from the API is served on restart."""

    async def test_each_version_is_scored_on_the_same_held_out_readings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("numpy")
        train = TestTheImuModel._imported(monkeypatch, tmp_path)
        state = resolve_state_dir()

        steady = train.train(state)
        jumpy = train.train(state, seed=1, samples=10, pointer="shadow")

        for version in (steady, jumpy):
            assert {"seed", "samples", "caught", "false_alarms"} <= set(version.metadata)
        # Fitted on ten readings, it flags far more normal motion than one fitted on 2000.
        assert jumpy.metadata["false_alarms"] > 10 * steady.metadata["false_alarms"]
        shadow = ArtifactStore.in_state_dir(state).pointer("imu-detector", "shadow")
        assert shadow is not None and (shadow.version, shadow.by) == (2, "train.py")

    def test_no_training_seed_draws_the_held_out_readings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        np = pytest.importorskip("numpy")
        train = TestTheImuModel._imported(monkeypatch, tmp_path)
        size = (1000, 3)

        held_out = np.random.default_rng(train.HELD_OUT).normal(size=size)

        for seed in (0, 1, 7):
            assert not np.array_equal(np.random.default_rng(seed).normal(size=size), held_out)

    async def test_active_moved_from_the_api_is_what_a_restarted_agent_serves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("numpy")
        train = TestTheImuModel._imported(monkeypatch, tmp_path)
        state = resolve_state_dir()
        train.train(state)
        train.main(
            ["--state-dir", str(state), "--seed", "11", "--samples", "5000", "--set", "active"]
        )
        spec = spec_of(importlib.import_module("agent").detect)
        assert spec is not None
        running = spec.build(persistence_dir=str(tmp_path / "running"))
        await running.load_model("model")
        assert running.artifacts["model"]["version"] == 2

        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            moved = await client.post(
                "/api/models/imu-detector/active", json={"version": 1, "reason": "back"}
            )
        finally:
            await client.close()

        assert moved.status == 200
        # The running agent keeps the version it loaded; a restart builds it afresh.
        await running.load_model("model")
        assert running.artifacts["model"]["version"] == 2
        restarted = spec.build(persistence_dir=str(tmp_path / "restarted"))
        await restarted.load_model("model")
        assert restarted.artifacts["model"]["version"] == 1


class TestTheImuTriage:
    """`triage` asks about a strong anomaly once per device per cooldown, with a label spec."""

    @staticmethod
    def _triage(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str | Exception = "Maintenance"
    ) -> tuple[types.ModuleType, Any, list[dict[str, Any]]]:
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        monkeypatch.delitem(sys.modules, "triage", raising=False)
        triage = importlib.import_module("triage")
        spec = spec_of(triage.triage)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        asked: list[dict[str, Any]] = []

        async def fake_ask(text: str, **kwargs: Any) -> str:
            asked.append({"text": text, **kwargs})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(me, "ask_operator", fake_ask)
        return triage, me, asked

    @staticmethod
    def _jolt(score: float = 39.75, ts: float = 1_700_000_000.0) -> dict[str, Any]:
        reading = {"device": "left-wrist", "ts": ts, "ax": 9.0, "ay": -7.5, "az": 1.0}
        return {"score": score, "reading": reading}

    async def test_a_strong_anomaly_is_asked_about_as_a_label_from_its_own_time(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, me, asked = self._triage(monkeypatch, tmp_path)

        await triage.triage(self._jolt(), me)

        (question,) = asked
        assert question["options"] == ["Maintenance", "Real problem", "Not sure"]
        assert question["label"] == {
            "source": "sensors/imu/left-wrist",
            "start": 1_700_000_000.0,
            "values": {"Maintenance": "maintenance", "Real problem": "fault"},
        }

    def test_the_label_spec_is_one_ask_operator_takes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, _, _ = self._triage(monkeypatch, tmp_path)
        label = {"source": "sensors/imu/left-wrist", "start": 1.0, "values": triage.VALUES}

        LabelSpec.parse(label, check_options(triage.OPTIONS))

    async def test_a_weak_anomaly_is_not_asked_about(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, me, asked = self._triage(monkeypatch, tmp_path)

        await triage.triage(self._jolt(score=5.0), me)

        assert asked == []

    async def test_a_device_is_asked_about_again_only_after_the_cooldown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, me, asked = self._triage(monkeypatch, tmp_path)

        await triage.triage(self._jolt(), me)
        await triage.triage(self._jolt(), me)
        assert len(asked) == 1

        me.persist("asked:sensors/imu/left-wrist", time.time() - triage.COOLDOWN_S - 1)
        await triage.triage(self._jolt(), me)
        assert len(asked) == 2

    async def test_jolts_handled_side_by_side_ask_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, me, asked = self._triage(monkeypatch, tmp_path)

        await asyncio.gather(*(triage.triage(self._jolt(), me) for _ in range(4)))

        assert len(asked) == 1

    async def test_an_unanswered_question_ends_the_call_quietly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        triage, me, asked = self._triage(monkeypatch, tmp_path, asyncio.TimeoutError())

        assert await triage.triage(self._jolt(), me) is None
        assert len(asked) == 1


class TestTheImuPublisher:
    """`publish_imu.py` reaches the broker Wactorz is configured for, with each reading's time."""

    async def test_readings_carry_their_device_and_time_and_now_and_then_a_jolt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("numpy")
        publisher = load("imu_anomaly", "publish_imu")
        dialled: list[tuple[str, int]] = []
        sent: list[tuple[str, dict[str, Any]]] = []

        class _Client:
            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def publish(self, topic: str, payload: str) -> None:
                sent.append((topic, json.loads(payload)))

        def fake_client(host: str, port: int) -> _Client:
            dialled.append((host, port))
            return _Client()

        monkeypatch.setattr(publisher, "mqtt_client", fake_client)
        monkeypatch.setattr(asyncio, "sleep", no_wait(asyncio.sleep))

        await publisher.main()

        assert dialled == [(CONFIG.mqtt_host, CONFIG.mqtt_port)]
        assert {topic for topic, _ in sent} == {"sensors/imu/left-wrist"}
        assert all(r["device"] == "left-wrist" and isinstance(r["ts"], float) for _, r in sent)
        jolts = [i for i, (_, r) in enumerate(sent) if r["ax"] == 9.0]
        assert jolts[:2] == [24, 49]


class TestTheImuRunScript:
    """`run.py` starts the minimal profile unless asked for the full system."""

    @staticmethod
    def _recorded_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        import wactorz

        called: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> None:
            called.update(kwargs)

        monkeypatch.setattr(wactorz, "run", fake_run)
        return called

    def test_by_default_it_is_the_minimal_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = self._recorded_run(monkeypatch)
        load("imu_anomaly", "run").main([])
        assert called["minimal"] is True
        assert called["llm"] is None
        specs = [spec_of(a) for a in called["agents"]]
        assert [spec.name for spec in specs if spec is not None] == ["imu-anomaly", "triage"]

    def test_with_main_starts_the_full_system_on_the_named_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = self._recorded_run(monkeypatch)
        load("imu_anomaly", "run").main(["--with-main", "--llm", "fake"])
        assert called["minimal"] is False
        assert called["llm"] == "fake"


class TestAskingTheImuDetector:
    """`ask.py` and the FastAPI route ask through `wactorz.ask`; the system is stood in for."""

    @staticmethod
    def _stand_in(monkeypatch: pytest.MonkeyPatch, answer: Any) -> dict[str, Any]:
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        import wactorz

        seen: dict[str, Any] = {"asked": [], "served": None, "stopped": False}

        async def fake_serve(**kwargs: Any) -> None:
            seen["served"] = kwargs
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                seen["stopped"] = True
                raise

        async def fake_ask(target: str, payload: Any, *, timeout: float = 60.0) -> Any:
            seen["asked"].append((target, payload))
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(wactorz, "serve", fake_serve)
        monkeypatch.setattr(wactorz, "ask", fake_ask)
        monkeypatch.setattr(asyncio, "sleep", no_wait(asyncio.sleep))
        return seen

    async def test_the_script_starts_asks_twice_and_stops(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = self._stand_in(monkeypatch, {"result": None})

        await load("imu_anomaly", "ask").main()

        assert seen["served"]["minimal"] is True
        assert [t for t, _ in seen["asked"]] == ["imu-anomaly", "imu-anomaly"]
        assert seen["stopped"] is True
        assert "jolt    ->" in capsys.readouterr().out

    async def test_the_route_answers_from_the_agents_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        self._stand_in(monkeypatch, {"score": 39.75, "reading": {"ax": 9}})
        app_module = load("imu_anomaly", "fastapi_app")

        assert await app_module.detect_now({"ax": 9}) == {
            "anomaly": True,
            "score": 39.75,
            "reading": {"ax": 9},
        }

    async def test_a_normal_reading_is_no_anomaly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fastapi")
        self._stand_in(monkeypatch, {"result": None})
        app_module = load("imu_anomaly", "fastapi_app")

        assert await app_module.detect_now({"ax": 0.1}) == {"anomaly": False}

    @pytest.mark.parametrize(
        ("failure", "status"),
        [
            (LookupError("no agent named 'imu-anomaly' is running"), 503),
            (RuntimeError("'imu-anomaly' answered with an error: boom"), 502),
            (asyncio.TimeoutError("'imu-anomaly' did not answer within 30s"), 504),
        ],
        ids=["not running", "failed", "silent"],
    )
    async def test_each_failure_is_its_own_status(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception, status: int
    ) -> None:
        fastapi = pytest.importorskip("fastapi")
        self._stand_in(monkeypatch, failure)
        app_module = load("imu_anomaly", "fastapi_app")

        with pytest.raises(fastapi.HTTPException) as raised:
            await app_module.detect_now({"ax": 9})
        assert raised.value.status_code == status

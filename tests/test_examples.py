"""The example programs under ``examples/`` do what their READMEs say.

Each is loaded from its folder by path, under a name of its own, since every
example calls its module ``agent.py``. The models, cameras and web framework
they need are stood in for, so the suite needs none of them installed.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wactorz.agents.function_agent import spec_of
from wactorz.agents.llm.providers.fake import FakeProvider

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(example: str, module: str = "agent") -> types.ModuleType:
    """``examples/<example>/<module>.py`` as a module named for both."""
    path = EXAMPLES / example / f"{module}.py"
    name = f"examples_{example}_{module}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


class TestTheLLMNotesExample:
    @pytest.fixture
    def summarise(self) -> Any:
        return _load("llm_notes").summarise

    async def test_a_note_is_summarised_with_the_model_and_the_cost_kept(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        spec = spec_of(summarise)
        assert spec is not None
        assert spec.subscribes == ("notes/raw/#",) and spec.publishes == "notes/summary"
        model = FakeProvider(script={"pump": "The pump filter clogs nightly; replace it Friday."})
        actor = spec.build(persistence_dir=str(tmp_path), llm_provider=model)

        result = await actor.call({"text": "The pump on line 2 drops to 2.1 l/min at 19:00."})

        assert result is not None
        assert result["summary"] == "The pump filter clogs nightly; replace it Friday."
        assert result["cost_usd"] >= 0
        system, messages = model.calls[0]
        assert "one sentence" in system
        assert messages[-1]["content"].startswith("The pump")
        assert actor.recall("notes_total") == 1
        # The call through `me.llm` landed on the card by itself.
        assert actor.metrics.llm_calls == 1
        assert actor.metrics.llm_cost_usd == pytest.approx(result["cost_usd"])

    async def test_plain_text_is_accepted_as_it_arrives(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        actor = spec_of(summarise).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path),
            llm_provider=FakeProvider(script={"dentist": "Call back."}),
        )

        result = await actor.call({"raw": "Call the dentist back on Tuesday."})

        assert result is not None and result["summary"] == "Call back."

    async def test_nothing_is_published_for_an_empty_note_or_without_a_model(
        self, summarise: Any, tmp_path: Path
    ) -> None:
        spec = spec_of(summarise)
        assert spec is not None
        with_model = spec.build(persistence_dir=str(tmp_path), llm_provider=FakeProvider())
        assert await with_model.call({"text": "   "}) is None

        without = spec.build(persistence_dir=str(tmp_path / "b"))
        assert await without.call({"text": "A note."}) is None
        assert without.recall("notes_total") is None


class _Boxes:
    """What ``Results.boxes`` offers, as lists that answer ``tolist()``."""

    def __init__(self, rows: list[tuple[list[float], float, int]]) -> None:
        self.xyxy = _Tensor([r[0] for r in rows])
        self.conf = _Tensor([r[1] for r in rows])
        self.cls = _Tensor([r[2] for r in rows])


class _Tensor(list):
    def tolist(self) -> list:
        return list(self)


class _Result:
    names: ClassVar[dict[int, str]] = {0: "person", 16: "dog"}

    def __init__(self, rows: list[tuple[list[float], float, int]]) -> None:
        self.boxes = _Boxes(rows)


class _Model:
    """Stands in for ``ultralytics.YOLO``: sees a person and a dog in anything."""

    def __init__(self, rows: list[tuple[list[float], float, int]] | None = None) -> None:
        self.rows = (
            rows
            if rows is not None
            else [
                ([10.0, 20.0, 110.0, 220.0], 0.91, 0),
                ([200.0, 50.0, 260.0, 120.0], 0.42, 16),
            ]
        )
        self.calls = 0

    def __call__(self, image: Any, verbose: bool = False) -> list[_Result]:
        self.calls += 1
        return [_Result(self.rows)]


class TestTheYoloWatchExample:
    @pytest.fixture
    def yolo(self) -> types.ModuleType:
        return _load("yolo_watch")

    def test_results_become_plain_records_above_the_confidence_floor(
        self, yolo: types.ModuleType
    ) -> None:
        found = yolo.detections_from(_Model()(None))

        assert found == [{"label": "person", "confidence": 0.91, "box": [10.0, 20.0, 110.0, 220.0]}]
        assert len(yolo.detections_from(_Model()(None), min_confidence=0.4)) == 2

    async def test_a_snapshot_is_decoded_and_run_through_the_model_once_loaded(
        self, yolo: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        decoded: list[bytes] = []
        monkeypatch.setattr(yolo, "decode_image", lambda raw: decoded.append(raw) or "image")
        loaded: list[str] = []

        def load_model(path: str) -> _Model:
            loaded.append(path)
            return _Model()

        monkeypatch.setattr(yolo, "load_model", load_model)
        spec = spec_of(yolo.detect_in_snapshot)
        assert spec is not None
        assert spec.subscribes == ("camera/+/snapshot",)
        actor = spec.build(persistence_dir=str(tmp_path), options={"model": "mine.pt"})

        first = await actor.call({"raw": b"\xff\xd8jpeg"})
        second = await actor.call({"raw": b"\xff\xd8jpeg"})

        assert first == second
        assert first is not None and first["count"] == 1
        assert first["detections"][0]["label"] == "person"
        assert decoded == [b"\xff\xd8jpeg", b"\xff\xd8jpeg"]
        assert loaded == ["mine.pt"], "loaded once, kept on the actor"
        # A message that is not an image is ignored.
        assert await actor.call({"text": "hello"}) is None

    async def test_nothing_is_published_when_nothing_is_seen(
        self, yolo: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yolo, "decode_image", lambda raw: "image")
        actor = spec_of(yolo.detect_in_snapshot).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path), options={"_model": _Model(rows=[])}
        )
        assert await actor.call({"raw": b"\xff\xd8"}) is None

    async def test_the_camera_watcher_reads_frames_and_publishes_detections(
        self, yolo: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeCapture:
            def __init__(self, frames: int) -> None:
                self.left = frames
                self.released = False

            def read(self) -> tuple[bool, Any]:
                if self.left == 0:
                    return False, None
                self.left -= 1
                return True, "frame"

            def release(self) -> None:
                self.released = True

        capture = FakeCapture(frames=6)
        opened: list[Any] = []

        def open_camera(source: Any) -> FakeCapture | None:
            opened.append(source)
            return capture if len(opened) == 1 else None

        model = _Model()
        monkeypatch.setattr(yolo, "open_camera", open_camera)
        monkeypatch.setattr(yolo, "load_model", lambda path: model)
        monkeypatch.setattr(yolo.CameraWatcher, "REOPEN_DELAY_S", 0.01)
        watcher = yolo.CameraWatcher(persistence_dir=str(tmp_path), source=7, every=3)
        published: list[tuple[str, Any]] = []

        async def publish(topic: str, payload: Any, **_: Any) -> None:
            published.append((topic, payload))

        said: list[str] = []

        async def say(message: str, level: str = "info") -> None:
            said.append(message)

        monkeypatch.setattr(watcher, "publish", publish)
        monkeypatch.setattr(watcher, "say", say)
        monkeypatch.setattr(watcher, "publish_manifest", _nothing)

        await watcher.on_start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(opened) >= 2:
                break
        task = next(t for t in watcher._tasks if t.get_name().endswith(":camera"))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await watcher.on_stop()

        assert opened[0] == 7
        assert model.calls == 2, "every third frame of six"
        assert [topic for topic, _ in published] == [yolo.DETECTIONS_TOPIC] * 2
        assert published[0][1]["frame"] == 3 and published[1][1]["frame"] == 6
        assert published[0][1]["detections"][0]["label"] == "person"
        assert capture.released, "the camera is released when its frames stop"
        assert watcher.last[0]["label"] == "person"
        # The feed says what happened, since a data topic is not shown there.
        assert any(s.startswith("Watching camera 7") for s in said)
        assert sum(s.startswith("→ vision/detections: person 0.91") for s in said) == 2
        assert any("stopped delivering" in s for s in said)

    async def test_the_watcher_survives_a_model_that_fails_on_a_frame(
        self, yolo: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Capture:
            def read(self) -> tuple[bool, Any]:
                return True, "frame"

            def release(self) -> None:
                return None

        class FailingModel:
            calls = 0

            def __call__(self, image: Any, verbose: bool = False) -> list[Any]:
                self.calls += 1
                raise RuntimeError("CUDA out of memory")

        model = FailingModel()
        monkeypatch.setattr(yolo, "open_camera", lambda source: Capture())
        monkeypatch.setattr(yolo, "load_model", lambda path: model)
        monkeypatch.setattr(yolo.CameraWatcher, "REOPEN_DELAY_S", 0.01)
        watcher = yolo.CameraWatcher(persistence_dir=str(tmp_path), source=0, every=1)
        said: list[tuple[str, str]] = []

        async def say(message: str, level: str = "info") -> None:
            said.append((level, message))

        monkeypatch.setattr(watcher, "say", say)
        monkeypatch.setattr(watcher, "publish_manifest", _nothing)

        await watcher.on_start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if model.calls >= 2:
                break
        task = next(t for t in watcher._tasks if t.get_name().endswith(":camera"))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert model.calls >= 2, "tried again after the failure"
        assert any(level == "error" and "CUDA out of memory" in m for level, m in said)

    def test_the_watcher_starts_when_handed_to_run(self, yolo: types.ModuleType) -> None:
        """`run.py` adds it only with a camera named, so once added it must start."""
        from wactorz.plugins import plugin_from

        assert plugin_from(yolo.CameraWatcher).autostart is True


async def _nothing(**_: Any) -> None:
    return None


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
        app_module = _load("imu_anomaly", "fastapi_app")

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
        app_module = _load("imu_anomaly", "fastapi_app")

        with pytest.raises(wactorz.StartupError):
            async with app_module.lifespan(app_module.app):
                pass


class TestTheLangGraphExample:
    async def test_a_ticket_goes_through_the_graph_without_a_model(self, tmp_path: Path) -> None:
        pytest.importorskip("langgraph")
        triage = _load("langgraph_triage").triage
        spec = spec_of(triage)
        assert spec is not None and spec.concurrency == 4
        actor = spec.build(persistence_dir=str(tmp_path))

        result = await actor.call({"id": "T-1", "text": "The pump stopped, the line is down"})
        again = await actor.call({"id": "T-2", "text": "How do I change a threshold?"})

        assert result == {
            "id": "T-1",
            "category": "outage",
            "priority": "high",
            "reply": result["reply"],  # pyright: ignore[reportOptionalSubscript]
        }
        assert again is not None and (again["category"], again["priority"]) == ("question", "low")
        assert actor.options["_graph"] is not None, "compiled once, kept on the actor"
        assert actor.recall("tickets_total") == 2
        assert await actor.call({"id": "T-3"}) is None

    async def test_the_graph_uses_the_systems_model_when_it_has_one(self, tmp_path: Path) -> None:
        pytest.importorskip("langgraph")
        triage = _load("langgraph_triage").triage
        model = FakeProvider(
            script={"invoice": "billing", "reply": "We will refund it today. Sorry."}
        )
        actor = spec_of(triage).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path), llm_provider=model
        )

        result = await actor.call({"id": "T-9", "text": "Charged twice on the invoice"})

        assert result is not None and result["category"] == "billing"
        assert result["priority"] == "medium"
        assert len(model.calls) == 2, "one call to classify, one to draft"
        assert actor.metrics.llm_calls == 2, "both counted on the card"
        assert actor.metrics.llm_input_tokens > 0


class TestTheAG2Example:
    async def test_without_a_model_the_draft_comes_back_after_one_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("ag2")
        review = _load("ag2_review").review
        actor = spec_of(review).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]
        said: list[str] = []

        async def log(message: str, level: str = "info") -> None:
            said.append(level)

        monkeypatch.setattr(actor, "log", log)

        result = await actor.call({"id": "d1", "text": "We is pleased to announce the pump."})

        assert result is not None
        assert result["id"] == "d1" and result["turns"] == 2
        assert result["cost_usd"] == 0.0, "no model, no spend"
        assert said == ["warning"], "and it says so on the feed"
        assert actor.recall("drafts_total") == 1
        assert await actor.call({"id": "d2", "text": "  "}) is None

    async def test_the_critic_sends_the_writer_back_until_it_approves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AG2's test client stands in for the model, with the turns scripted."""
        pytest.importorskip("ag2")
        from ag2 import Agent  # pyright: ignore[reportMissingImports]
        from ag2.testing import TestConfig  # pyright: ignore[reportMissingImports]

        module = _load("ag2_review")

        def scripted(config: Any) -> Any:
            assert config == "scripted"
            writer = Agent(
                "writer", "You improve drafts.", config=TestConfig("Version one.", "Version two.")
            )
            critic = Agent(
                "critic",
                "You review.",
                config=TestConfig("Too long.", f"Good now. {module.APPROVED}"),
            )
            return writer, critic

        monkeypatch.setattr(module, "make_agents", scripted)
        actor = spec_of(module.review).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path), options={"config": "scripted"}
        )

        result = await actor.call({"id": "d3", "text": "Draft."})

        assert result is not None
        assert result["text"] == "Version two."
        assert result["turns"] == 4, "write, critique, rewrite, approve"


class TestTheImuDetectorKeepsItsModel:
    """The model is read from the file once and kept in the agent's state."""

    @staticmethod
    def _model_file(tmp_path: Path) -> Path:
        numpy = pytest.importorskip("numpy")
        import pickle

        model_module = _load("imu_anomaly", "model")
        normal = numpy.random.default_rng(seed=7).normal(
            loc=(0.0, 0.0, 1.0), scale=(0.3, 0.3, 0.2), size=(500, 3)
        )
        path = tmp_path / "imu_model.pkl"
        path.write_bytes(pickle.dumps(model_module.MahalanobisModel.fit(normal)))
        return path

    async def test_it_scores_without_the_file_once_it_has_seen_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.syspath_prepend(str(EXAMPLES / "imu_anomaly"))
        path = self._model_file(tmp_path)
        spec = spec_of(_load("imu_anomaly").detect)
        assert spec is not None
        state = tmp_path / "state"
        jolt = {"ax": 9.0, "ay": -7.5, "az": 1.0}

        first = spec.build(persistence_dir=str(state), options={"model": str(path)})
        assert (await first.call(jolt))["score"] > 4.0
        assert first.recall("model") == path.read_bytes()

        path.unlink()
        again = spec.build(persistence_dir=str(state), options={"model": str(path)})
        await again._load_persistent_state()

        assert (await again.call(jolt))["score"] > 4.0
        assert await again.call({"ax": 0.0, "ay": 0.0, "az": 1.0}) is None


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
        _load("imu_anomaly", "run").main([])
        assert called["minimal"] is True
        assert called["llm"] is None
        specs = [spec_of(a) for a in called["agents"]]
        assert [spec.name for spec in specs if spec is not None] == ["imu-anomaly"]

    def test_with_main_starts_the_full_system_on_the_named_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = self._recorded_run(monkeypatch)
        _load("imu_anomaly", "run").main(["--with-main", "--llm", "fake"])
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
        monkeypatch.setattr(asyncio, "sleep", _no_wait(asyncio.sleep))
        return seen

    async def test_the_script_starts_asks_twice_and_stops(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen = self._stand_in(monkeypatch, {"result": None})

        await _load("imu_anomaly", "ask").main()

        assert seen["served"]["minimal"] is True
        assert [t for t, _ in seen["asked"]] == ["imu-anomaly", "imu-anomaly"]
        assert seen["stopped"] is True
        assert "jolt    ->" in capsys.readouterr().out

    async def test_the_route_answers_from_the_agents_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastapi")
        self._stand_in(monkeypatch, {"score": 39.75, "reading": {"ax": 9}})
        app_module = _load("imu_anomaly", "fastapi_app")

        assert await app_module.detect_now({"ax": 9}) == {
            "anomaly": True,
            "score": 39.75,
            "reading": {"ax": 9},
        }

    async def test_a_normal_reading_is_no_anomaly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("fastapi")
        self._stand_in(monkeypatch, {"result": None})
        app_module = _load("imu_anomaly", "fastapi_app")

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
        app_module = _load("imu_anomaly", "fastapi_app")

        with pytest.raises(fastapi.HTTPException) as raised:
            await app_module.detect_now({"ax": 9})
        assert raised.value.status_code == status


def _no_wait(real_sleep: Any) -> Any:
    """A sleep that yields once: the examples wait for a system that is stood in for."""

    async def _sleep(_delay: float, *args: Any) -> None:
        await real_sleep(0)

    return _sleep

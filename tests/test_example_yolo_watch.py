# SPDX-License-Identifier: Apache-2.0
"""The yolo_watch example: a YOLO model as a function and as an actor, the model and camera stood in for.

What it needs that is not installed for the suite is stood in for.
"""

import asyncio
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.examples import load
from wactorz.agents.function_agent import spec_of


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
        return load("yolo_watch")

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

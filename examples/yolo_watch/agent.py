"""Object detection with a YOLO model, two ways.

`detect_in_snapshot` is a function: a JPEG arrives on MQTT, it answers with
what the model sees. `CameraWatcher` is an actor that reads a camera itself
and publishes detections as they happen. Both keep the model loaded on the
actor and run inference on a worker thread, so a slow frame never holds the
event loop. Frames are not streamed over MQTT: the actor sits where the camera
is and publishes decisions.

Any model `ultralytics.YOLO()` loads works: a `.pt` from a release, your own
fine-tune, an exported `.onnx`.
"""

import asyncio
import logging
import os
import time
from typing import Any, ClassVar

import wactorz
from wactorz import Message, MessageType

logger = logging.getLogger(__name__)

#: The weights, unless the `model` option says otherwise. Ultralytics fetches a
#: release model by name on first use.
DEFAULT_MODEL = "yolo11n.pt"

#: Detections below this confidence are not reported.
MIN_CONFIDENCE = 0.5

DETECTIONS_TOPIC = "vision/detections"


def load_model(path: str) -> Any:
    """The YOLO model at ``path``, loaded once per process by the callers."""
    # Optional dependency: `pip install 'wactorz[ml]'`. Imported here so the
    # module, and the tests, do not need it.
    from ultralytics import YOLO

    return YOLO(path)


def decode_image(raw: bytes) -> Any:
    """A JPEG or PNG as the array the model takes."""
    # Optional dependency: `pip install 'wactorz[vision]'`. Imported here for
    # the same reason as the model's.
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("not an image the decoder recognises")
    return image


def open_camera(source: int | str) -> Any:
    """The capture for a camera index or a stream URL, opened, or ``None``."""
    import cv2  # optional dependency, as in `decode_image`

    capture = cv2.VideoCapture(source)
    return capture if capture.isOpened() else None


def detections_from(results: Any, min_confidence: float = MIN_CONFIDENCE) -> list[dict[str, Any]]:
    """The model's answer as plain records: label, confidence, box.

    Reads the `Results` Ultralytics returns, one per image; a fake with the
    same attributes does for a test.
    """
    found: list[dict[str, Any]] = []
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        names = result.names
        for xyxy, conf, cls in zip(
            boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True
        ):
            if conf < min_confidence:
                continue
            found.append(
                {
                    "label": names.get(int(cls), str(int(cls))),
                    "confidence": round(float(conf), 3),
                    "box": [round(float(v), 1) for v in xyxy],
                }
            )
    return found


def _model_for(actor: Any, options: dict[str, Any]) -> Any:
    """The model kept on ``actor``'s options, loaded on first use."""
    model = options.get("_model")
    if model is None:
        model = load_model(str(options.get("model") or DEFAULT_MODEL))
        options["_model"] = model
    return model


@wactorz.agent(
    name="yolo-snapshot",
    subscribes="camera/+/snapshot",
    publishes=DETECTIONS_TOPIC,
    description="Says what the YOLO model sees in each snapshot published to it.",
    capabilities=["object_detection", "vision"],
    input_schema={"raw": "bytes (JPEG or PNG)"},
    output_schema={"detections": "list", "count": "int"},
    requires={"ram_mb": 1024, "packages": ["ultralytics", "opencv-python"]},
)
def detect_in_snapshot(frame: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Detect objects in one image; nothing is published when nothing is seen.

    The image is the message's bytes, which arrive as ``{"raw": bytes}``. A
    plain function, so this runs on a worker thread.
    """
    raw = frame.get("raw")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    model = _model_for(me, me.options)
    results = model(decode_image(bytes(raw)), verbose=False)
    found = detections_from(results, float(me.options.get("min_confidence", MIN_CONFIDENCE)))
    if not found:
        return None
    return {"detections": found, "count": len(found)}


class CameraWatcher(wactorz.Actor):
    """Reads a camera and publishes what the model sees, every few frames.

    Spawned under another name with ``options``: ``source`` (a camera index
    or a stream URL), ``model``, ``every`` and ``min_confidence``. From
    ``run.py``, ``CAMERA`` in the environment names the source, and the
    watcher is only started when it is set, since it needs a camera.
    """

    DESCRIPTION = "Watches a camera with a YOLO model and publishes detections."
    CAPABILITIES: ClassVar[list[str]] = ["object_detection", "vision", "camera"]
    PUBLISHES: ClassVar[list[str]] = [DETECTIONS_TOPIC]
    OUTPUT_SCHEMA: ClassVar[dict[str, str]] = {"detections": "list", "count": "int", "frame": "int"}
    REQUIRES: ClassVar[dict[str, Any]] = {
        "ram_mb": 1024,
        "packages": ["ultralytics", "opencv-python"],
    }
    #: How long to wait before trying a camera that could not be opened again.
    REOPEN_DELAY_S = 5.0

    #: After this many frames through the model with nothing seen, say so on the
    #: feed, so a quiet feed is explained rather than mistaken for a stuck agent.
    QUIET_FRAMES = 100

    def __init__(
        self,
        name: str = "camera-watcher",
        persistence_dir: str | None = None,
        source: int | str | None = None,
        model: str = DEFAULT_MODEL,
        every: int = 5,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        super().__init__(name=name, persistence_dir=persistence_dir)
        if source is None:
            raw = os.environ.get("CAMERA", "0")
            source = int(raw) if raw.isdigit() else raw
        self.source = source
        self.model_path = model
        self.every = max(1, int(every))
        self.min_confidence = float(min_confidence)
        self.options: dict[str, Any] = {"model": model}
        self._capture: Any = None
        self.frames = 0
        self.last: list[dict[str, Any]] = []
        self._quiet = 0

    async def on_start(self) -> None:
        await self.publish_manifest(
            description=self.DESCRIPTION,
            publishes=list(self.PUBLISHES),
            capabilities=list(self.CAPABILITIES),
            output_schema=dict(self.OUTPUT_SCHEMA),
        )
        self.run_detached(self._watch(), name=f"{self.name}:camera")

    async def on_stop(self) -> None:
        await self._release()

    async def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            await asyncio.to_thread(capture.release)

    async def say(self, message: str, level: str = "info") -> None:
        """A line on the dashboard feed, under this agent's name.

        A data topic is not shown on the feed, so an actor that only publishes
        looks idle there; this is what the decorated agents do for each publish.
        """
        getattr(logger, level, logger.info)("[%s] %s", self.name, message)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    async def handle_message(self, msg: Message) -> None:
        """A task from chat (`@camera-watcher status`) is answered with what was last seen."""
        if msg.type != MessageType.TASK:
            return
        reply: dict[str, Any] = {
            "source": str(self.source),
            "watching": self._capture is not None,
            "frames": self.frames,
            "last": self.last,
        }
        if isinstance(msg.payload, dict) and "_task_id" in msg.payload:
            reply["_task_id"] = msg.payload["_task_id"]
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, reply)

    async def _watch(self) -> None:
        """Read frames for as long as the actor runs, reopening a camera that drops.

        Nothing here ends the loop but a stop: a camera that cannot be opened,
        one that stops delivering, and a failure inside the model are each said
        on the feed and tried again after a pause.
        """
        try:
            model = await asyncio.to_thread(_model_for, self, self.options)
        except Exception as exc:
            logger.exception("[%s] The model could not be loaded", self.name)
            await self.say(f"Model {self.model_path} could not be loaded: {exc}", "error")
            return
        await self.say(f"Model {self.model_path} loaded; opening camera {self.source!r}")
        while True:
            try:
                self._capture = await asyncio.to_thread(open_camera, self.source)
                if self._capture is None:
                    await self.say(f"Camera {self.source!r} not available; retrying", "warning")
                else:
                    await self.say(
                        f"Watching camera {self.source!r}: every {self.every} frames "
                        f"through the model, detections above {self.min_confidence:.2f} "
                        f"to {DETECTIONS_TOPIC}"
                    )
                    await self._read_until_lost(model)
                    await self.say(f"Camera {self.source!r} stopped delivering frames", "warning")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[%s] The camera loop failed", self.name)
                await self.say(f"Camera loop failed: {exc}; retrying", "error")
            await self._release()
            await asyncio.sleep(self.REOPEN_DELAY_S)

    async def _read_until_lost(self, model: Any) -> None:
        while self._capture is not None:
            ok, frame = await asyncio.to_thread(self._capture.read)
            if not ok:
                return
            self.frames += 1
            if self.frames % self.every:
                continue
            results = await asyncio.to_thread(model, frame, verbose=False)
            found = detections_from(results, self.min_confidence)
            self.metrics.messages_processed += 1
            self.last = found
            if not found:
                self._quiet += 1
                if self._quiet % self.QUIET_FRAMES == 0:
                    await self.say(
                        f"{self._quiet} frames through the model, nothing above "
                        f"{self.min_confidence:.2f}"
                    )
                continue
            self._quiet = 0
            await self.publish(
                DETECTIONS_TOPIC,
                {"detections": found, "count": len(found), "frame": self.frames},
            )
            seen = ", ".join(f"{d['label']} {d['confidence']:.2f}" for d in found[:5])
            await self.say(f"→ {DETECTIONS_TOPIC}: {seen}" + (" …" if len(found) > 5 else ""))

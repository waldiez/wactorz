"""
MLAgent - Base class for non-LLM ML/DL agents.
Example: YOLO object detection, anomaly detection, forecasting models.
These actors run 24/7 processing data streams.
"""

import asyncio
import logging
import time
from abc import abstractmethod
from typing import Any, Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)


class MLAgent(Actor):
    """
    Base for ML/DL agents that don't use LLMs.
    Override `load_model()` and `predict()`.
    Can run in continuous loop mode (e.g. anomaly detection 24/7).
    """

    def __init__(
        self,
        continuous: bool = False,
        poll_interval: float = 1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.continuous = continuous
        self.poll_interval = poll_interval
        self._model: Any = None
        self._model_loaded = False

    async def on_start(self):
        logger.info(f"[{self.name}] Loading model...")
        self._model = await asyncio.get_event_loop().run_in_executor(None, self.load_model)
        self._model_loaded = True
        logger.info(f"[{self.name}] Model loaded.")
        if self.continuous:
            self._tasks.append(asyncio.create_task(self._continuous_loop()))

    @abstractmethod
    def load_model(self) -> Any:
        """Load and return the ML model (runs in thread executor)."""
        pass

    @abstractmethod
    async def predict(self, input_data: Any) -> Any:
        """Run inference. Override this."""
        pass

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            payload = msg.payload
            if not self._model_loaded:
                await self.send(msg.sender_id, MessageType.RESULT, {"error": "Model not loaded"})
                return
            try:
                result = await self.predict(payload)
                self.metrics.tasks_completed += 1
                if msg.sender_id:
                    await self.send(msg.sender_id, MessageType.RESULT, result)
            except Exception as e:
                self.metrics.tasks_failed += 1
                logger.error(f"[{self.name}] Prediction failed: {e}")

    async def _continuous_loop(self):
        """Runs predict() in a loop - for streaming/background agents."""
        logger.info(f"[{self.name}] Starting continuous inference loop.")
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            if self.state == ActorState.PAUSED:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                data = await self.fetch_data()
                if data is not None:
                    result = await self.predict(data)
                    await self._on_continuous_result(result)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.name}] Continuous loop error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def fetch_data(self) -> Optional[Any]:
        """Override to provide data for continuous mode."""
        return None

    async def _on_continuous_result(self, result: Any):
        """Override to handle continuous results (e.g. publish alert)."""
        await self._mqtt_publish(f"agents/{self.actor_id}/result", {"result": str(result), "timestamp": time.time()})


# ─── Example: YOLO Object Detection Agent ────────────────────────────────────

class YOLOAgent(MLAgent):
    """
    Example ML agent using YOLO for object detection.
    Plug in any YOLO variant (ultralytics, yolov5, etc.)
    """

    def __init__(self, model_path: str = "yolov8n.pt", confidence: float = 0.5, **kwargs):
        kwargs.setdefault("name", "yolo-detector")
        super().__init__(**kwargs)
        self.model_path = model_path
        self.confidence = confidence

    def load_model(self):
        try:
            from ultralytics import YOLO
            return YOLO(self.model_path)
        except ImportError:
            logger.warning("[YOLOAgent] ultralytics not installed. pip install ultralytics")
            return None

    async def predict(self, input_data: Any) -> dict:
        """input_data: image path, numpy array, or URL."""
        if self._model is None:
            return {"error": "Model not available"}

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: self._model(input_data, conf=self.confidence)
        )
        detections = []
        for r in results:
            for box in r.boxes:
                detections.append({
                    "class": r.names[int(box.cls)],
                    "confidence": float(box.conf),
                    "bbox": box.xyxy[0].tolist(),
                })
        return {"detections": detections, "count": len(detections)}


# ─── Example: Anomaly Detection Agent ────────────────────────────────────────

class AnomalyDetectorAgent(MLAgent):
    """
    Simple statistical anomaly detection agent.
    Runs 24/7 in continuous mode watching a data stream.
    """

    def __init__(self, threshold: float = 3.0, window_size: int = 100, **kwargs):
        kwargs.setdefault("name", "anomaly-detector")
        kwargs.setdefault("continuous", True)
        super().__init__(**kwargs)
        self.threshold = threshold
        self.window_size = window_size
        self._values: list[float] = []

    def load_model(self):
        return {"type": "zscore", "threshold": self.threshold}

    async def predict(self, input_data: Any) -> dict:
        value = float(input_data) if not isinstance(input_data, dict) else input_data.get("value", 0.0)
        self._values.append(value)
        if len(self._values) > self.window_size:
            self._values.pop(0)

        if len(self._values) < 10:
            return {"anomaly": False, "value": value, "reason": "warming up"}

        import statistics
        mean = statistics.mean(self._values)
        stdev = statistics.stdev(self._values) or 1e-9
        zscore = abs(value - mean) / stdev
        is_anomaly = zscore > self.threshold

        result = {
            "anomaly": is_anomaly,
            "value": value,
            "zscore": zscore,
            "mean": mean,
            "stdev": stdev,
        }

        if is_anomaly:
            logger.warning(f"[{self.name}] ANOMALY DETECTED: z={zscore:.2f} value={value}")
            await self._mqtt_publish(f"agents/{self.actor_id}/anomaly", result)

        return result

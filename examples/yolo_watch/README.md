# Object detection with a YOLO model

A vision model as a Wactorz agent, two ways: a function that answers snapshots
published on MQTT, and an actor that reads a camera itself. Both publish what
the model sees as JSON on `vision/detections`, where a rule, a pipeline step or
the planner can act on it.

## Files

| File | Role |
| ---- | ---- |
| `agent.py` | `detect_in_snapshot`, a function subscribed to `camera/+/snapshot`; `CameraWatcher`, an `Actor` that reads a camera; the model loading and result parsing they share. |
| `run.py` | Starts Wactorz with the snapshot detector, and the camera watcher when `CAMERA` is set. |
| `publish_snapshot.py` | Publishes an image file as a snapshot. |

## Run it

You need a broker on `localhost:1883`, the package with its vision extras, and a
model, which Ultralytics fetches by name on first use:

```bash
pip install 'wactorz[ml,vision]'       # ultralytics, torch, opencv
cd examples/yolo_watch
python run.py                          # snapshots only
python publish_snapshot.py photo.jpg   # in another terminal, any JPEG or PNG
mosquitto_sub -t 'vision/detections'
```

No photo to hand? With OpenCV installed, one from the webcam:

```bash
python -c "import cv2; c = cv2.VideoCapture(0); ok, f = c.read(); cv2.imwrite('photo.jpg', f); print(ok)"
```

With a camera attached:

```bash
CAMERA=0 python run.py                 # or CAMERA=rtsp://user:pass@host/stream
```

The `ultra` image (`wactorz:<version>-ultra`) has all of this installed, with
GStreamer for cameras that need it.

## How it works

The snapshot detector is the IMU example's shape with an image instead of a
reading. Bytes published on MQTT arrive as `{"raw": bytes}`:

```python
@wactorz.agent(name="yolo-snapshot", subscribes="camera/+/snapshot", publishes="vision/detections")
def detect_in_snapshot(frame: dict, me: wactorz.FunctionAgent) -> dict | None:
    raw = frame.get("raw")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    model = _model_for(me, me.options)  # loaded once, kept on the actor
    found = detections_from(model(decode_image(bytes(raw)), verbose=False))
    return {"detections": found, "count": len(found)} if found else None
```

A plain `def`, so decoding and inference run on a worker thread. The `model`
option names the weights (`{"type": "module", "target": "agent:detect_in_snapshot",
"options": {"model": "my-finetune.pt"}}` from a spawn config); any model
`ultralytics.YOLO()` loads works, an exported `.onnx` included.

The camera watcher is an `Actor` subclass, because it has a lifecycle of its
own: a capture opened when it starts, a loop that reads frames while it runs,
a release when it stops. `on_start` hands the loop to `run_detached`, so the
actor owns the task and stopping the actor stops the loop. Every `every`-th
frame goes through the model on a thread, and a frame with detections is
published. A camera that cannot be opened, or that stops delivering, is tried
again after a pause rather than crashing the actor. It needs a camera, so
`run.py` adds it only when `CAMERA` is set.

Frames stay on the machine the camera is attached to. MQTT carries decisions,
and a snapshot now and then; a live stream goes over WebRTC or RTSP to whoever
needs the pixels.

## Acting on detections

A rule turns a detection into something that happens, with no code:

```python
wactorz.pipeline(
    "door-watch",
    steps=[detect_in_snapshot],
    rules=[
        wactorz.RuleConfig(
            triggers=["vision/detections"],
            conditions=[wactorz.RuleCondition(field="detections.0.label", op="eq", value="person")],
            actions=[
                wactorz.RuleAction(type="publish", topic="alerts/door", payload={"who": "person"})
            ],
            cooldown_seconds=60,
        )
    ],
)
```

# Examples

Small, complete programs that use Wactorz as a library: your own agents, your
own model, the supervision, dashboard and MQTT that come with it, and nothing
you do not need.

| Example | What it shows |
| ------- | ------------- |
| [`imu_anomaly/`](imu_anomaly/README.md) | A trained anomaly model watching IMU readings on MQTT, declared with one decorator, with no Home Assistant and no LLM. Also as a pipeline, from a notebook, and inside a FastAPI app. |
| [`llm_notes/`](llm_notes/README.md) | An agent that calls the system's language model: notes in, one-line summaries out, with the cost kept. |
| [`yolo_watch/`](yolo_watch/README.md) | A YOLO model as an agent, two ways: a function answering snapshots on MQTT, and an `Actor` reading a camera. |

Each example has a `README.md` with the commands to run it.

# Examples

Small, complete programs that use Wactorz as a library: your own agents, your
own model, the supervision, dashboard and MQTT that come with it, and nothing
you do not need.

| Example | What it shows |
| ------- | ------------- |
| [`imu_anomaly/`](imu_anomaly/README.md) | A trained anomaly model watching IMU readings on MQTT, declared with one decorator, with no Home Assistant and no LLM. Also as a pipeline, from a notebook, and inside a FastAPI app. |
| [`air_quality/`](air_quality/README.md) | A model of what each room is usually like at each hour, an agent that asks a person about the rest, their answers kept as labels, and a dashboard tab, all from one installable package. A stricter version can then be tried against those labels. |
| [`household_energy/`](household_energy/README.md) | One power meter, an agent that finds when an appliance ran, a classifier that names it, and a person asked whether it was right, with a dashboard tab of its own icon, all from one installable package. A version that also looks at how long a run lasted is then judged against the answers as a classifier. |
| [`llm_notes/`](llm_notes/README.md) | An agent that calls the system's language model: notes in, one-line summaries out, with the cost kept. |
| [`yolo_watch/`](yolo_watch/README.md) | A YOLO model as an agent, two ways: a function answering snapshots on MQTT, and an `Actor` reading a camera. |
| [`langgraph_triage/`](langgraph_triage/README.md) | A LangGraph graph as an agent: tickets in, category, priority and a draft reply out, several at once, with the model's spend on the dashboard. |
| [`ag2_review/`](ag2_review/README.md) | An AG2 1.x writer–critic conversation as an agent, on the system's model, with its spend reported. |

Each example has a `README.md` with the commands to run it.

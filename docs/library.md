# Using Wactorz as a library

Wactorz is an application, the `wactorz` command with its dashboard, planner
and catalogue, and it is a library: the supervision, persistence, MQTT wiring
and dashboard underneath, for agents you write yourself. This guide is the
library side, from one function to a declared pipeline, with no Home Assistant
and no language model required.

Everything here is in `examples/imu_anomaly/`, runnable end to end.

## Install

```bash
pip install wactorz
docker compose up -d mosquitto     # or any MQTT broker on localhost:1883
```

## One function is an agent

```python
import wactorz

@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    description="Flags IMU readings the trained model calls abnormal.",
    requires={"ram_mb": 128, "packages": ["numpy"]},
)
def detect(reading: dict) -> dict | None:
    score = MODEL.score(reading)
    return {"score": score, "reading": reading} if score > 4.0 else None
```

`detect` is still a function: call it in a test, import it elsewhere. As an
agent it is called once per message on `sensors/imu/#` with the decoded
payload, and once per task sent to it by chat or by another agent. What it
returns is published to `anomalies/imu`; `None` publishes nothing. A plain
function runs on a worker thread, so a slow model never holds the event loop
every other agent shares; a coroutine function runs on the loop.

A function that wants the actor takes it as a second parameter, for state,
options and publishing elsewhere:

```python
@wactorz.agent(subscribes="sensors/imu/#", publishes="anomalies/imu")
def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None:
    model = me.options.get("_model")           # loaded once, kept on the actor
    if model is None:
        model = load(me.options.get("model", "imu.onnx"))
        me.options["_model"] = model
    score = model.score(reading)
    if score < 4.0:
        return None
    me.persist("anomalies_total", int(me.recall("anomalies_total", 0)) + 1)
    return {"score": score, "reading": reading}
```

`persist` and `recall` keep small values across restarts and migrations:
counters, thresholds, calibration. Keep the model itself in a file and load it;
do not persist the model object.

## Start it

From a script, the dashboard on, nothing else:

```python
wactorz.run(agents=[detect], minimal=True)        # http://localhost:8888/
```

`minimal=True` starts the monitor, the dashboard and your agents: no
orchestrator, no catalogue, no installer, so no model, no API key and no
provider SDK; a plain `pip install wactorz` is enough. A model is built under
it only when one is named (`llm="ollama"`). Leave it out to
run beside the full system, where chat can ask your agent questions
(`@imu-anomaly {"ax": 9, "ay": 0, "az": 1}`) and the planner can wire new
agents to what it publishes.

Inside a program that already has an event loop, a notebook, a web framework,
a ROS node, await `serve` instead; the host keeps its signals, and cancelling
the task stops the system cleanly:

```python
# Jupyter: `serve` returns when the system stops, so it runs as a task, and the
# notebook's own server is on 8888, the dashboard's default, so pick another port.
system_task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True, monitor_port=8890))
await asyncio.sleep(4)  # the broker connection, the dashboard and the agent come up
actor = wactorz.system().registry.find_by_name("imu-anomaly")
...
system_task.cancel()  # to stop; the actors are stopped on the way out
```

```python
# FastAPI
from contextlib import asynccontextmanager, suppress

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task  # the system is stopped, and its state written, before the host exits

app = FastAPI(lifespan=lifespan)
```

`run()` is `asyncio.run(serve(...))` with signal handling on; the two take the
same arguments: `web`, `minimal`, `monitor_port`, `mqtt_broker`, `mqtt_port`,
`llm`, `state_dir`. The dashboard's default port is 8888, which Jupyter also
uses; inside a notebook, or beside any other server on that port, pass
`monitor_port` or set `MONITOR_PORT`.

`serve` behaves as a library call inside someone else's program: it leaves the
host's logging configuration alone (`configure_logging=True` asks for the
command's console and file handlers, which `run()` does), `state_dir` is set
for the run rather than written to the environment, and a configuration that
cannot be started, an exposed bind address with no API key, a chat interface
whose token is missing, a provider whose SDK is not installed, raises
`wactorz.StartupError` with the reason instead of exiting the process, and
leaves the process as it found it: no thread of ours left watching the loop,
no handler of ours on the root logger. The host's own tasks, started before
`serve` or while it runs, are left alone at shutdown: Wactorz marks the tasks
it creates and stops only those. Cancelling the `serve` task raises
`CancelledError` once the system has stopped, so a timeout or a task group
around it sees the cancellation as one.
`wactorz.system()` is the running `ActorSystem`, with the registry your actors
are in, and `None` outside a run.

## Or register it, and start `wactorz` as usual

For a deployment that runs the `wactorz` command, name the agent instead of
writing a script. Three ways, all equivalent:

| How | Where | For |
| --- | ----- | --- |
| `WACTORZ_AGENTS=mypkg.agents:detect,mypkg.agents:Sink` | the environment | a configured deployment |
| `[project.entry-points."wactorz.agents"]` in `pyproject.toml` | your package | anything `pip install`ed |
| `wactorz.run(agents=[...])` or `serve(...)` | a script | development |

The target must be importable from where `wactorz` starts: a package that is
installed, or a folder on `PYTHONPATH`. One that is not is reported at startup
and left out. A registered agent is supervised, listed by `@catalog list`,
restored after a restart, and may be spawned under another name with a
`type: "module"` spawn config; only registered targets are accepted there,
because a spawn config can be written by the model.

## An `Actor` subclass

For an agent with its own lifecycle, subclass `Actor`. The base class
subscribes, keeps rolling windows and publishes for you, on one broker
connection per actor that closes when the actor stops:

```python
from wactorz import Actor, Message, MessageType

class PumpWatch(Actor):
    DESCRIPTION = "Watches the pump's flow."
    SUBSCRIBES = ["sensors/pump/#"]
    PUBLISHES = ["alerts/pump"]

    async def on_start(self) -> None:
        self.subscribe("sensors/pump/#", self.on_reading)        # plain or async callback
        self.flow = self.window("sensors/pump/flow", seconds=60)  # rolling window

    async def on_reading(self, payload: dict) -> None:
        if self.flow.falling(threshold=2.0):
            await self.publish("alerts/pump", {"flow": payload})

    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return
        result = {"flow_mean": self.flow.mean("value")}
        if isinstance(msg.payload, dict) and "_task_id" in msg.payload:
            result["_task_id"] = msg.payload["_task_id"]
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, result)
```

`DESCRIPTION`, `CAPABILITIES`, `REQUIRES`, `SUBSCRIBES`, `PUBLISHES`,
`INPUT_SCHEMA` and `OUTPUT_SCHEMA` on the class become the catalogue entry and
the wiring a pipeline checks against; `AGENT_NAME` names it (default
`PumpWatch` → `pump-watch`); `AUTOSTART = False` keeps it in the catalogue
until asked for. A subclass that takes `llm_provider` in its constructor is
given the system's model.

A message on a subscribed topic arrives decoded: a dict or list when it was
JSON, `{"raw": text}` for text, `{"raw": bytes}` for anything else, such as a
camera frame. Messages on one topic are handled one at a time, in order.

## A pipeline

Steps that work together are declared together. Each step is still an agent
with its own card; the pipeline groups them, adds a schedule and rules, and is
recorded beside the planner's pipelines so `/rules` lists it:

```python
import wactorz
from wactorz import RuleAction, RuleCondition, RuleConfig

@wactorz.agent(subscribes="anomalies/imu")
async def notify(anomaly: dict, me: wactorz.FunctionAgent) -> None:
    await me.notify_user(f"IMU anomaly, score {anomaly['score']}")

@wactorz.agent(subscribes="pipelines/imu-watch/tick", publishes="reports/imu")
def report(tick: dict, me: wactorz.FunctionAgent) -> dict:
    return {"total": int(me.recall("anomalies_total", 0))}

alert = RuleConfig(
    triggers=("anomalies/imu",),
    conditions=(RuleCondition("score", "gt", 20),),
    actions=(RuleAction("publish", topic="alerts/imu", payload={"level": "high"}),),
    cooldown_seconds=30,
)

watch = wactorz.pipeline(
    "imu-watch",
    steps=[detect, notify, report],
    schedule={"type": "interval", "seconds": 300},
    rules=[alert],
)
```

The schedule becomes a scheduled agent ticking `pipelines/imu-watch/tick`,
which `report` listens to. The rule becomes a rule agent: when a message on
`anomalies/imu` has `score` above 20, it publishes to `alerts/imu`, at most
every thirty seconds. Rules are the typed dataclasses above or the equivalent
dicts, which are the spelling chat and JSON use:

```python
rules=[{"triggers": ["anomalies/imu"],
        "conditions": [{"field": "score", "op": "gt", "value": 20}],
        "actions": [{"type": "publish", "topic": "alerts/imu", "payload": {"level": "high"}}],
        "cooldown_seconds": 30}]
```

Actions are `publish`, `task` (sent to an agent by name) and `webhook`.
Conditions take `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`,
`exists`, `absent`, with dotted fields such as `reading.ax`; an action's
payload may name trigger fields in braces and carries the trigger under
`trigger`.

Wiring is checked when the pipeline is declared: a step or rule listening on a
topic nothing in the pipeline publishes, and not named in `inputs`, raises
`ValueError` before anything starts. The first step may listen to the outside
world without saying so. A pipeline declared at module level is found through
`WACTORZ_PIPELINES=mypkg.flows:watch`, a `wactorz.pipelines` entry point, or
`wactorz.run(pipelines_=[watch])`.

## Profiles

Without Home Assistant configured (`HA_URL` and `HA_TOKEN`) its agents do not
start; `WACTORZ_HA_AGENTS=on|off` decides outright. `wactorz --minimal`,
`WACTORZ_MINIMAL=1` or `minimal=True` starts the monitor, the dashboard and
your agents only. The minimal profile runs no planner, no generated code and
no runtime package install, which is the reproducible mode a pinned
environment wants.

## Testing

A decorated function is tested as a function. The actor around it is built
without a broker and exercised directly:

```python
import wactorz

def test_a_jolt_is_flagged(tmp_path):
    actor = wactorz.spec_of(detect).build(persistence_dir=str(tmp_path))
    assert detect({"ax": 9.0, "ay": -7.5, "az": 1.0}, actor)["score"] > 4
    assert detect({"ax": 0.1, "ay": 0.0, "az": 1.0}, actor) is None
```

`actor.call(payload)` runs it the way a message would, on a thread for a plain
function; `actor.handle_message(...)` answers a task. Nothing connects until
`on_start`. `spec_of` is the `AgentSpec` the decorator recorded: `name`,
`subscribes`, `publishes`, `options`, and `build()` for the actor.

## Where things go

- **Streams stay where they are produced.** An agent that watches a camera or
  a microphone reads it on the machine the device is attached to, with
  OpenCV, GStreamer, WebRTC or `sounddevice`, and publishes decisions. MQTT
  carries events; a `{"raw": bytes}` payload is for a snapshot or a clip, not
  a feed.
- **Models live in files.** Load `.pt`, `.onnx`, `.pkl` or anything else
  yourself, once, and keep the object on the actor. `persist()` is for what
  the agent learns or counts, in JSON-sized pieces.
- **State** lives under `WACTORZ_STATE_DIR` (default `./state`), one folder
  per agent, with the SQLite database beside them.

## Stable API

What this guide uses is the surface you can rely on:

| Name | Role |
| ---- | ---- |
| `wactorz.agent` | declare a function as an agent |
| `wactorz.pipeline` | declare steps, a schedule and rules together |
| `wactorz.run`, `wactorz.serve` | start the system from a script, or on a running loop |
| `wactorz.system` | the running `ActorSystem` (`registry`, `supervisor`), `None` outside a run |
| `wactorz.spec_of` | the `AgentSpec` behind a decorated function, with `build()` for tests |
| `wactorz.StartupError` | what `serve` and `run` raise for a configuration that cannot start |
| `wactorz.Actor` with `subscribe`, `window`, `publish`, `persist`, `recall`, `send`, `notify_user`, `on_start`, `on_stop`, `handle_message` | the base class |
| `wactorz.FunctionAgent` | the actor behind a decorated function: `call`, `options`, `log` |
| `wactorz.RuleConfig`, `RuleCondition`, `RuleAction`, `wactorz.RuleAgent` | typed rules |
| `wactorz.Message`, `MessageType` | what `handle_message` receives |
| `WACTORZ_AGENTS`, `WACTORZ_PIPELINES`, `WACTORZ_MINIMAL`, `WACTORZ_HA_AGENTS` | the settings above |

Other names the package exports, the built-in agents among them, are the
application's own and may change between releases.

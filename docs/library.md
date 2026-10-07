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
command's console and file handlers, which `run()` does; a host that has
configured no logging at all still sees warnings and errors on stderr, as it
would without Wactorz), `state_dir` is set
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

On Windows the loop matters: the broker client needs a selector loop, and a
proactor loop, which uvicorn builds there by default, cannot serve it. `serve`
refuses to start on one with a `StartupError` that says what to do: start
uvicorn with `--loop asyncio:SelectorEventLoop`, or set
`asyncio.WindowsSelectorEventLoopPolicy()` before the loop is made. Jupyter's
kernel already uses the selector loop.

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
camera frame. Messages on one topic are handled one at a time, in order,
unless the subscription says otherwise (below).

## More than one at a time

By default an agent handles one message of a topic at a time, in order, and
one task at a time, so a function that keeps state between calls is never run
against itself. That is right for a sensor stream and wrong for a function
that waits rather than computes: a model call, a training job, a graph with
three model calls in it, where a twenty-second step queues every message
behind it.

```python
@wactorz.agent(subscribes="tickets/new", publishes="tickets/triaged", concurrency=4)
async def triage(ticket: dict, me: wactorz.FunctionAgent) -> dict:
    return await GRAPH.ainvoke({"ticket": ticket})

class Trainer(Actor):
    async def on_start(self) -> None:
        self.subscribe("mlops/candidates", self.train, concurrency=2)
```

`concurrency=N` runs up to N messages of the topic, and N tasks, at once.
Order within the topic is then not kept, and the function runs against
itself, so what it shares between calls must cope with that; a model loaded
once and read by every call is fine, a counter updated with `persist` is a
race. A plain `def` under concurrency runs on that many worker threads.

## Your own files

`me.state_dir` (or `self.state_dir` in a subclass) is the agent's own
directory under the state directory: for model weights, a checkpoint store, a
local experiment log, anything too large or too un-JSON for `persist()`. It
exists from construction, survives restarts, and is removed with the agent on
a delete. What moves with a migration to another node is the persisted
state, not these files; an agent that needs a file on another machine ships
it.

```python
checkpointer = SqliteSaver.from_conn_string(str(me.state_dir / "graph.sqlite"))
```

## Spend made elsewhere

A call through `me.llm` is counted as it returns: tokens and cost on the
agent's card, and against the cost limit, with nothing to write in the
function. A model called another way, through LangChain, AG2 or an SDK
directly, is invisible until the agent says what it spent:

```python
me.record_llm_cost(0.0012, input_tokens=300, output_tokens=40, model="gpt-4o-mini")
```

That joins the agent's card and the process-wide total the cost limit is
checked against. Two helpers do it for the common cases:

- `wactorz.core.integrations.langchain.CostCallback(me, prices={...})` is a LangChain
  callback handler: pass it in a chain's or a graph's `config`, and every
  model call is reported with its tokens. LangChain reports tokens, not money,
  so `prices` maps a model name to dollars per million input and output
  tokens; an unpriced model is counted at no cost.
- `wactorz.core.integrations.ag2.record_reply(me, reply, prices={...})` reads
  AG2's usage report for a reply, tokens by model, prices them and reports
  them. `model_config(me)` in the same module gives AG2's agents the model
  the system runs on. AG2 1.x, `import ag2`, plus AG2's extra for the
  provider (`pip install 'ag2[anthropic]'`, `'ag2[openai]'`), which carries
  that provider's SDK at the version AG2 asks for. A price table is keyed by
  model family and matches the resolved name a provider reports as a prefix.

## LangGraph, LangChain and AG2

Wactorz does not compete with them. LangGraph and AG2 decide how an agent
thinks: the graph, the prompts, the conversation. Wactorz keeps that running,
wired to topics and events, restarted when it crashes, saved across restarts
and visible on a dashboard, which is what a graph you call and a chat you
start do not have on their own. An `async` function that invokes the graph
is the whole integration:

```python
@wactorz.agent(subscribes="tickets/new", publishes="tickets/triaged",
               requires={"packages": ["langgraph"]}, concurrency=4)
async def triage(ticket: dict, me: wactorz.FunctionAgent) -> dict:
    result = await GRAPH.ainvoke({"ticket": ticket}, config={"callbacks": [CostCallback(me)]})
    return result["decision"]
```

The graph's nodes can call the system's model through `me.llm`, so its spend
is counted and capped like every other call, or a LangChain model with the
callback above. `examples/langgraph_triage/` and `examples/ag2_review/` are
the two, runnable; `pip install 'wactorz[langgraph]'` and
`'wactorz[ag2]'` bring the libraries. Ray is the same shape: an always-on
agent at the edge awaits a Ray task's handle and publishes the result. Keep
Ray's actors and Wactorz's in separate roles, compute you call and the
control layer that calls it, rather than merging the two actor models.

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

## From chat

A registered agent is a building block for main and the planner, with nothing
more to declare than the decorator already says.

**Ask it directly.** In the dashboard chat, the REST interface or the CLI,
address it by name with a JSON object matching its input schema, and the
function's return value is the reply:

```text
@imu-anomaly {"ax": 9, "ay": 0, "az": 1}
→ {"score": 12.4, "reading": {"ax": 9, "ay": 0, "az": 1}}
```

Plain text after the name travels as `{"text": "..."}`, for an agent that
reads natural language.

**Let main start it.** Main's system prompt lists the registered agents that
are not running, each with the one spawn config that starts it (`"type":
"module"` and its registered target), so "start the IMU detector" is a spawn
of yours rather than freshly written code. Registered targets only: a spawn
config can be model-written, and `type: "module"` refuses anything not in the
registry.

**Let the planner use it.** When the planner designs a pipeline, its prompt
carries a *registered agents* section: name, description, the topics each
listens to and writes to, its schemas, whether it is running, and the same
spawn config. The rules say to prefer one of yours over a dynamic agent that
does the same, to subscribe to what it publishes when it runs, and to spawn it
by that exact target when it does not. So "alert me when the IMU detector
scores above 20" proposes your detector as the first step, and a notifier
listening on `anomalies/imu` after it.

To see all of this with a real agent, run the IMU example with
`python run.py --with-main` and follow *Ask it from chat* in
`examples/imu_anomaly/README.md`.

## Asking an agent

An agent that needs another agent's answer, and a program that needs one from
the running system, use the same call:

```python
# inside an agent: the actor is the second parameter
@wactorz.agent(name="relay")
async def relay(payload: dict, me: wactorz.FunctionAgent) -> dict:
    score = await me.ask("imu-anomaly", {"ax": 9, "ay": 0, "az": 1})
    return {"score": score["score"]}

# beside the system: a notebook cell, a web handler, a test
score = await wactorz.ask("imu-anomaly", {"ax": 9, "ay": 0, "az": 1}, timeout=30)
```

The request goes to the agent as a task, the way chat or a pipeline step would
send it, and the reply is what the agent answered: a function's return value,
with a plain value wrapped as `{"result": ...}`. Three things go wrong loudly
rather than quietly. No agent of that name is running: `LookupError`. The
agent answered with an error, which is what a function that raised reports:
`RuntimeError` carrying the message, a `wactorz.core.actor.ReplyError` whose
`reply` holds the agent's whole answer. No reply within `timeout` seconds, 60
by default: `asyncio.TimeoutError`. In every case nothing is left waiting.

`wactorz.ask` needs a running system, from `run()` or `serve()`; it raises
`RuntimeError` otherwise. Its reply address is a slot of the registry rather
than an actor, so nothing is registered, listed or supervised on the caller's
behalf.

## Profiles

Without Home Assistant configured (`HA_URL` and `HA_TOKEN`) its agents do not
start, and main and the planner are not told about it either: their prompts
are assembled from a core plus what each configured integration adds, so an
assistant for your agents never offers to dim the lights (see
[Prompt fragments](architecture.md#prompt-fragments)).
`WACTORZ_HA_AGENTS=on|off` decides outright. `wactorz --minimal`,
`WACTORZ_MINIMAL=1` or `minimal=True` starts the monitor, the dashboard and
your agents only. The minimal profile runs no planner, no generated code and
no runtime package install, which is the reproducible mode a pinned
environment wants.

## Examples

`examples/` in the repository holds complete programs, each with a README:

- `imu_anomaly/`: a trained model watching IMU readings, as a script, a
  pipeline, a notebook and inside a FastAPI app.
- `llm_notes/`: an agent that calls the system's language model through
  `me.llm`, with the cost kept across restarts.
- `yolo_watch/`: a YOLO model as an agent, as a function answering snapshots
  on MQTT and as an `Actor` reading a camera itself.
- `langgraph_triage/`: a LangGraph graph as an agent, several tickets in
  flight at once, with the model's spend on the dashboard.
- `ag2_review/`: an AG2 1.x writer–critic conversation as an agent, on the
  system's model.

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
| `wactorz.ask`, `Actor.ask` | send an agent a task and wait for its reply, from host code or from another agent |
| `wactorz.spec_of` | the `AgentSpec` behind a decorated function, with `build()` for tests |
| `wactorz.StartupError` | what `serve` and `run` raise for a configuration that cannot start |
| `wactorz.Actor` with `subscribe`, `window`, `publish`, `persist`, `recall`, `send`, `notify_user`, `on_start`, `on_stop`, `handle_message`, `state_dir`, `record_llm_cost` | the base class |
| `concurrency=` on `wactorz.agent` and `Actor.subscribe` | messages and tasks at once |
| `wactorz.core.integrations.langchain.CostCallback`, `wactorz.core.integrations.ag2.model_config` and `record_reply` | LangChain and AG2 beside the system |
| `wactorz.FunctionAgent` | the actor behind a decorated function: `call`, `options`, `log` |
| `wactorz.RuleConfig`, `RuleCondition`, `RuleAction`, `wactorz.RuleAgent` | typed rules |
| `wactorz.Message`, `MessageType` | what `handle_message` receives |
| `WACTORZ_AGENTS`, `WACTORZ_PIPELINES`, `WACTORZ_MINIMAL`, `WACTORZ_HA_AGENTS` | the settings above |

Other names the package exports, the built-in agents among them, are the
application's own and may change between releases.

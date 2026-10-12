# IMU anomaly detection with your own model

A sensor publishes IMU readings on MQTT. You have a model trained on normal
motion. This example wraps it in a Wactorz agent with one decorator, runs it
supervised with the dashboard on, and needs neither Home Assistant nor a
language model.

## Files

| File | Role |
| ---- | ---- |
| `model.py` | The model: fitting it to normal motion, as two named arrays, and scoring a reading with them. |
| `train.py` | Fits it on synthetic "normal" motion, writes `imu_model.npz` and keeps it on main as `imu-detector`. Stands in for your own training. |
| `agent.py` | The agent: a function declared with `@wactorz.agent`, subscribed to `sensors/imu/#`, publishing anomalies on `anomalies/imu`. |
| `run.py` | Starts Wactorz with that agent, in the minimal profile; `--with-main` starts main, the planner and the catalogue around it. |
| `pipeline.py` | The detector as one stage of a pipeline: a notifier, a report on a schedule, and a rule that alerts on a strong anomaly. |
| `notebook.ipynb` | The same, from Jupyter: the system on the notebook's loop through `wactorz.serve()`, readings published and anomalies read from cells, the running agent inspected in-process. |
| `fastapi_app.py` | The same, inside a FastAPI app: the system on the web app's loop, started and stopped by its lifespan, with routes that reach the running agent. |
| `publish_imu.py` | A fake sensor: publishes readings, a few of them abnormal. |
| `ask.py` | Starts the detector and asks it two questions with `wactorz.ask`, then stops. |

## Run it

You need a broker on `localhost:1883` (`docker compose up mosquitto` from the
repository root works) and the package installed (`pip install wactorz`).

```bash
cd examples/imu_anomaly
python train.py          # writes imu_model.npz, stores it as imu-detector
python run.py            # dashboard on http://localhost:8888/
python publish_imu.py    # in another terminal
```

`train.py` keeps the model where Wactorz keeps models, in the `state/` folder
beside these scripts, which is the state directory they all run with. Each run
stores a new version, and the agent loads the newest. Without one stored, the
agent does not start, and the error names the model it looked for.

The agent card on the dashboard shows the detector running and its message
counter climbing. The feed shows a `Listening on sensors/imu/#` line at start and
one line per anomaly it publishes; readings that score as normal are counted but
not shown, so a quiet feed with a rising counter is a working detector. The
anomalies themselves go out on `anomalies/imu`:

```bash
mosquitto_sub -t 'anomalies/imu'
```

## Ask it from chat

**Without a model.** `python run.py` alone is the minimal profile: the monitor,
the dashboard and the detector, no main and no model. The dashboard chat at
`http://localhost:8888/` still reaches the agent. Type a reading as JSON after
its name and the function's return value is the reply:

```text
@imu-anomaly {"ax": 9, "ay": -7.5, "az": 1}
→ {"score": 39.75, "reading": {"ax": 9, "ay": -7.5, "az": 1}}
```

Plain text with no `@name` is answered with the running agents and how to
address one, since there is no model to read it; `/agents`, `/topics`,
`/nodes` and `/help` work, and nothing else does.

**With main.** The same agent, with main, the planner and the catalogue around
it, so you can see how a registered agent is discovered. Nothing in `agent.py`
changes.

```bash
python run.py --with-main --llm fake        # no API key: scripted main
# or, with a model so main and the planner think:
LLM_PROVIDER=ollama python run.py --with-main
```

Open the dashboard chat at `http://localhost:8888/` and try these, in order.

**1. It is listed.** `@catalog list` names `imu-anomaly` among the agents that
can be spawned by name, with the description from the decorator, and `/agents`
shows it running. No model is involved in either.

**2. Ask it directly.** Type a reading as JSON after its name; the function's
return value is the reply:

```text
@imu-anomaly {"ax": 9, "ay": -7.5, "az": 1}
→ {"score": 39.75, "reading": {"ax": 9, "ay": -7.5, "az": 1}}

@imu-anomaly {"ax": 0.1, "ay": 0, "az": 1}
→ {"result": null}            # a normal reading: nothing to report
```

This works with the fake model too: the message goes to the agent, not to
main.

**3. Main knows it.** With a real model, ask `what agents are running?` and
main names `imu-anomaly` from its live list. Stop it with
`/agents stop imu-anomaly`, ask `start the IMU detector`, and main spawns it
with the one spawn config it was told for it (`"type": "module"` and the
agent's registered target) rather than writing new code. Main's system prompt
carries a *registered but not running* block for exactly this.

**4. The planner uses it.** Ask

```text
alert me on Discord when the IMU detector scores above 20
```

and the planner proposes a pipeline whose first step is `imu-anomaly` itself,
followed by a notifier that listens on `anomalies/imu`, instead of a freshly
written detector. Its prompt carried a *registered agents* section with the
agent's topics, schemas and spawn config; the plan is shown for approval before
anything starts.

Only a registered target can be spawned this way: a spawn config may be written
by the model, and `type: "module"` refuses any target that is not in the
registry.

## Ask it from code

The same question from a program, with `wactorz.ask`: the reading goes to the
agent as a task and the function's return value comes back, with a plain value
wrapped as `{"result": ...}`. Three things to try, each needing only the broker
and a trained model.

**1. A script.** `ask.py` starts the system on its own loop, asks twice, stops:

```bash
python ask.py
```

```text
jolt    -> {'score': 39.75, 'reading': {'ax': 9.0, 'ay': -7.5, 'az': 1.0}}
resting -> {'result': None}
```

The script asks for the selector event loop on Windows before it starts, as
`publish_imu.py` does; a program that runs Wactorz on the proactor loop there
is refused with a message naming that line.

**2. A web app.** Start `fastapi_app.py` and ask through its route, which calls
`wactorz.ask` behind it:

```bash
curl -X POST localhost:8000/detect -H 'content-type: application/json' -d '{"ax": 9, "ay": -7.5, "az": 1}'
# {"anomaly": true, "score": 39.75, "reading": {"ax": 9, "ay": -7.5, "az": 1}}
curl -X POST localhost:8000/detect -H 'content-type: application/json' -d '{"ax": 0.1, "ay": 0, "az": 1}'
# {"anomaly": false}
```

**3. A notebook.** Section 7 of `notebook.ipynb` is one line per question:

```python
await wactorz.ask("imu-anomaly", {"ax": 9.0, "ay": -7.5, "az": 1.0})
```

What goes wrong goes wrong loudly: asking an agent that is not running raises
`LookupError`, one whose function raised raises `RuntimeError` with the message,
and one that does not answer within the timeout (60 seconds by default) raises
`asyncio.TimeoutError`. Nothing is left waiting in any case. Inside an agent
the call is the same on the actor, `await me.ask("imu-anomaly", {...})`.

## How it works

`agent.py` is the whole integration:

```python
@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    description="Flags IMU readings the trained model calls abnormal.",
    requires={"ram_mb": 128},
    models={"model": "imu-detector"},
)
async def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None:
    model = await me.load_model("model")
    ...
```

The function is called once per reading. Returning `None` publishes nothing.
The second parameter is the actor, used here to load the model and to keep a
count across restarts with `persist`/`recall`.

`models=` names the model by the name it is stored under, and the agent reads
it by its own key, `"model"`. Before the agent starts, Wactorz finds the newest
`imu-detector` among the models kept on main; `me.load_model("model")` loads it
once, off the event loop, and keeps it. An `.npz` loads as a dict of its
arrays, with pickles refused, so loading the model runs nothing from the file.
`imu-detector:2` instead names one version. On a node, the node fetches the
model by name before the agent starts, so nothing is copied there by hand.

## The model

The model is two arrays, the mean of normal motion and the inverse of its
covariance, and a reading's score is its distance from that mean in units of
the spread. `/artifacts` in chat, or `wactorz-artifacts list --state-dir state`
in a terminal, lists what is kept, with the arrays each model holds:

```text
imu-detector — newest imu-detector:1 (npz, 626 B); arrays mean[3], inverse_covariance[3x3]
```

To use your own, save the arrays your scoring reads and store the file under the
name the agent gives:

```bash
wactorz-artifacts add my_model.npz imu-detector --state-dir state
```

`run.py` hands the function to `wactorz.run()`, which supervises it beside the
monitor, restarts it if it crashes, and serves the dashboard. Inside a program
that already has an event loop, `await wactorz.serve(agents=[detect], minimal=True)`
does the same without taking over the loop or the signals. With
`minimal=True` no orchestrator, catalogue or installer starts, so no model API
key is needed.

## From a notebook

`notebook.ipynb` runs the detector on Jupyter's own event loop:

```python
system_task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True, monitor_port=8890))
```

The dashboard goes on port 8890 because Jupyter's own server already has 8888,
the dashboard's default; the notebook prints the address it used.

then publishes readings from a cell, collects anomalies from another, reaches
the running actor through `wactorz.system().registry` to read its counters
and persisted state, asks it for a verdict with `wactorz.ask` the way chat
would, and stops it by cancelling the task. Start Jupyter from this folder, or the first cell adds it
to the path.

## Inside a web app

`fastapi_app.py` runs the detector on a FastAPI app's own event loop. The app
owns the loop and the signals; Wactorz is a task its lifespan starts and
cancels:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True, state_dir=STATE_DIR))
    await asyncio.sleep(0)
    if task.done():
        task.result()  # a refused start fails the app now, not at shutdown
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task  # the actors are stopped and their state written first
```

Two routes show the host reaching in: `GET /status` reads the agent's counters
through `wactorz.system().registry`, and `POST /detect` asks the agent for a
verdict with `wactorz.ask()`, the way a task from chat would; an agent that is
not running is a 503, one that fails a 502, one that does not answer a 504.

```bash
pip install fastapi uvicorn
python fastapi_app.py                      # or: uvicorn fastapi_app:app --port 8000
curl -X POST localhost:8000/detect -H 'content-type: application/json' -d '{"ax": 9, "ay": -7.5, "az": 1}'
```

On Windows, start uvicorn with `--loop asyncio:SelectorEventLoop`: its default
there is a proactor loop, which cannot watch the broker's socket, and Wactorz
refuses to start on it with a message saying so. `python fastapi_app.py` asks
for the selector loop itself. The dashboard is still on 8888; pass `web=False`
to leave it off.

## As a pipeline

`pipeline.py` keeps `detect` as it is and adds two more functions and some glue,
declared together:

```python
watch = wactorz.pipeline(
    "imu-watch",
    steps=[detect, notify, report],
    schedule={"type": "interval", "seconds": 300},
    rules=[
        {
            "triggers": ["anomalies/imu"],
            "conditions": [{"field": "score", "op": "gt", "value": 20}],
            "actions": [{"type": "publish", "topic": "alerts/imu", "payload": {"level": "high"}}],
            "cooldown_seconds": 30,
        }
    ],
)
```

Each step is still an agent with its own card. The schedule becomes a
scheduled agent ticking `pipelines/imu-watch/tick`, which `report` listens to;
the rule becomes a rule agent that publishes to `alerts/imu` at most every
thirty seconds. Wiring is checked when the pipeline is declared: a step that
listens on a topic nothing in the pipeline publishes is refused before anything
starts. Run it with `python pipeline.py` and publish readings as before; the
feed shows five agents, and `/rules` in a full installation lists `imu-watch`.

## The same agent in a full installation

Keep `run.py` out of it and name the agent in the environment of a normal
`wactorz` start instead. `agent.py` imports `model.py` from beside it, so this
folder has to be on the Python path; a `wactorz` start does not add the current
directory the way `python run.py` adds the script's:

The model has to be kept in that installation's state directory, which is
`WACTORZ_STATE_DIR` or `./state` where `wactorz` starts:

```bash
python examples/imu_anomaly/train.py --state-dir state
PYTHONPATH=examples/imu_anomaly WACTORZ_AGENTS=agent:detect wactorz
```

In PowerShell:

```powershell
python examples\imu_anomaly\train.py --state-dir state
$env:PYTHONPATH = "examples\imu_anomaly"
$env:WACTORZ_AGENTS = "agent:detect"
wactorz
```

A target that cannot be imported is reported as an error at startup, naming
the module, and the agent is simply absent from the dashboard.

or list it as a `wactorz.agents` entry point in your own package's
`pyproject.toml`:

```toml
[project.entry-points."wactorz.agents"]
imu-anomaly = "imu_anomaly.agent:detect"
```

Either way the agent is supervised at startup, listed by `@catalog list`, and
chat can ask it for a verdict on a reading: `@imu-anomaly {"ax": 9.0, "ay": 0, "az": 1}`.

## On a node

The detector runs on a [node](../../docs/remote-nodes.md) too, and moves there
and back with its model. The node builds it from this folder, so the folder
has to be on the node, on the runner's path, and named in its environment —
the same two things main needed:

```bash
PYTHONPATH=/home/pi/imu_anomaly WACTORZ_AGENTS=agent:detect wactorz-node --node rpi-kitchen --mqtt-broker 192.168.1.10
```

Under a deploy, put both lines in the node's `~/wactorz/.env`. The node then
lists `agent:detect` in its manifest, and from chat:

```text
/migrate imu-anomaly rpi-kitchen
/migrate imu-anomaly local
```

The node fetches `imu-detector` from main before it builds the agent, and
keeps it, so the model is never copied by hand; the counter goes with the agent
each way. The node needs numpy, as main does. A node that does not list the
target refuses the move and says what to install. On the node the agent has no
model API: `me.llm` is `None` there, which this detector never uses.

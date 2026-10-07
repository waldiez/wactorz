# IMU anomaly detection with your own model

A sensor publishes IMU readings on MQTT. You have a model trained on normal
motion. This example wraps it in a Wactorz agent with one decorator, runs it
supervised with the dashboard on, and needs neither Home Assistant nor a
language model.

## Files

| File | Role |
| ---- | ---- |
| `model.py` | The model class, in a module of its own so its pickle loads from any process. |
| `train.py` | Fits it on synthetic "normal" motion and writes `imu_model.pkl`. Stands in for your own training. |
| `agent.py` | The agent: a function declared with `@wactorz.agent`, subscribed to `sensors/imu/#`, publishing anomalies on `anomalies/imu`. |
| `run.py` | Starts Wactorz with that agent, in the minimal profile; `--with-main` starts main, the planner and the catalogue around it. |
| `pipeline.py` | The detector as one stage of a pipeline: a notifier, a report on a schedule, and a rule that alerts on a strong anomaly. |
| `notebook.ipynb` | The same, from Jupyter: the system on the notebook's loop through `wactorz.serve()`, readings published and anomalies read from cells, the running agent inspected in-process. |
| `fastapi_app.py` | The same, inside a FastAPI app: the system on the web app's loop, started and stopped by its lifespan, with routes that reach the running agent. |
| `publish_imu.py` | A fake sensor: publishes readings, a few of them abnormal. |

## Run it

You need a broker on `localhost:1883` (`docker compose up mosquitto` from the
repository root works) and the package installed (`pip install wactorz`).

```bash
cd examples/imu_anomaly
python train.py          # writes imu_model.pkl
python run.py            # dashboard on http://localhost:8888/
python publish_imu.py    # in another terminal
```

The agent card on the dashboard shows the detector running and its message
counter climbing. The feed shows a `Listening on sensors/imu/#` line at start and
one line per anomaly it publishes; readings that score as normal are counted but
not shown, so a quiet feed with a rising counter is a working detector. The
anomalies themselves go out on `anomalies/imu`:

```bash
mosquitto_sub -t 'anomalies/imu'
```

## Ask it from chat

The same agent, with main, the planner and the catalogue around it, so you can
see how a registered agent is discovered. Nothing in `agent.py` changes.

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

## How it works

`agent.py` is the whole integration:

```python
@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    description="Flags IMU readings the trained model calls abnormal.",
    requires={"ram_mb": 128},
)
def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None: ...
```

The function is called once per reading, on a worker thread so a slow model
never holds the event loop. Returning `None` publishes nothing. The second
parameter is the actor, used here to load the model once and keep a count
across restarts with `persist`/`recall`.

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
and persisted state, sends it a task the way chat would, and stops it by
cancelling the task. Start Jupyter from this folder, or the first cell adds it
to the path.

## Inside a web app

`fastapi_app.py` runs the detector on a FastAPI app's own event loop. The app
owns the loop and the signals; Wactorz is a task its lifespan starts and
cancels:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(wactorz.serve(agents=[detect], minimal=True, state_dir="./state"))
    await asyncio.sleep(0)
    if task.done():
        task.result()  # a refused start fails the app now, not at shutdown
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task  # the actors are stopped and their state written first
```

Two routes show the host reaching in: `GET /status` reads the agent's counters
through `wactorz.system().registry`, and `POST /detect` scores a reading on
demand with `actor.call()`, the way a task from chat would.

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

```bash
PYTHONPATH=examples/imu_anomaly WACTORZ_AGENTS=agent:detect wactorz
```

In PowerShell:

```powershell
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

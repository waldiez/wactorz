# Remote Nodes

Deploy a single file to any machine — Raspberry Pi, VM, edge device — and spawn agents on it from the main Wactorz chat. Remote agents heartbeat back to the central dashboard exactly like local ones.

## Overview

The remote node system is built around a single self-contained script: `remote_runner.py`. It requires no Wactorz installation on the edge device — only Python 3 and three pip packages. It connects to the shared MQTT broker, listens for spawn commands from the main machine, and runs DynamicAgents locally with the same supervisor semantics.

```
[Main machine]                        [Edge device — Raspberry Pi, VM, etc.]

MainActor  ──MQTT──►  nodes/{name}/spawn  ──►  remote_runner.py
                                                   │  compiles + runs agent
                                                   │  local ONE_FOR_ONE supervisor
Dashboard  ◄──MQTT──  agents/{id}/heartbeat  ◄──┘  heartbeats every 10 s
```

Remote agents appear in the central dashboard alongside local agents. The only visual difference is a `node` field in their heartbeat payload showing which machine they run on.

---

## Setup on the edge device

#### 1. Install dependencies (minimal)

```bash
pip install aiomqtt psutil aiohttp --break-system-packages
```

#### 2. Copy `remote_runner.py` to the device

```bash
scp remote_runner.py pi@raspberrypi.local:~/
```

#### 3. Start the runner

```bash
python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom
```

Replace `192.168.1.10` with the IP of the machine running the MQTT broker. The `--name` is the node identifier — it must be unique across all nodes and is used to address this device when spawning agents.

#### Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--broker` | `localhost` | MQTT broker hostname or IP. Also reads `$AGENTFLOW_BROKER`. |
| `--port` | `1883` | MQTT broker port. |
| `--name` / `--node` | random | Unique node name. Also reads `$AGENTFLOW_NODE`. |
| `--loglevel` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

#### Run as a service (systemd)

```ini
[Unit]
Description=Wactorz remote node
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/remote_runner.py --broker 192.168.1.10 --name rpi-livingroom
Restart=always
RestartSec=5
Environment=AGENTFLOW_BROKER=192.168.1.10

[Install]
WantedBy=multi-user.target
```

> **💡 Self-test** — Run `python3 remote_runner.py --test` to execute the built-in supervisor test suite without needing a broker. Useful to verify the script works on a new device before connecting it.

---

## Spawning agents on a remote node

From the main Wactorz chat, add a `"node"` field to any spawn request. The planner and main_actor both support this — or you can do it manually.

#### Natural language (via planner)

```
"deploy a temperature sensor agent to rpi-livingroom"
"spawn an agent on rpi-bedroom that reads the door sensor every 30 seconds"
```

#### Manual spawn via chat

```json
{
  "name":          "temp-sensor-agent",
  "node":          "rpi-livingroom",
  "type":          "dynamic",
  "description":   "Reads temperature from DHT22 sensor",
  "poll_interval": 30,
  "max_restarts":  5,
  "restart_delay": 3.0,
  "install":       ["adafruit-circuitpython-dht"],
  "code": "
    async def setup(agent):
        await agent.log('DHT22 sensor agent ready')

    async def process(agent):
        import random   # replace with real adafruit_dht read
        temp = round(20 + random.uniform(-2, 2), 1)
        await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C', 'node': agent.node})
        await agent.log(f'Temperature: {temp}C')
  "
}
```

The main machine publishes this config to `nodes/rpi-livingroom/spawn`. The runner picks it up, installs any declared `"install"` packages, compiles the code, and starts the agent under a local supervisor.

> **ℹ replace flag** — If an agent with the same name is already running on the node, the spawn is ignored by default. Pass `"replace": true` in the config to stop the old instance and spawn fresh.

---

## Automated deploy from chat

MainActor can deploy `remote_runner.py` to a new machine automatically via a devops agent that uses SSH. From the chat:

```
"deploy node rpi-bedroom to pi@192.168.1.52 with broker 192.168.1.10"
```

This spawns a devops agent that SSHes into the target machine, creates `~/agentflow/`, uploads `remote_runner.py`, installs the dependencies, and starts the runner as a background process. After that, the node is immediately available for agent spawning.

---

## MQTT topics

The runner subscribes to a set of control topics scoped to its node name, and publishes heartbeats for itself and all its agents.

| Topic | Direction | Description |
|-------|-----------|-------------|
| `nodes/{name}/spawn` | → runner | Spawn a new agent. Payload: full agent config dict. Published with `retain=true`; runner clears retain after processing. |
| `nodes/{name}/stop` | → runner | Stop a named agent. Payload: `{"name": "agent-name"}`. |
| `nodes/{name}/stop_all` | → runner | Stop all agents and shut down the runner. |
| `nodes/{name}/list` | → runner | Request the list of running agents. Response on `nodes/{name}/agents`. |
| `nodes/{name}/agents` | ← runner | Response to `list`. Contains agent names and actor IDs. |
| `nodes/{name}/heartbeat` | ← runner | Runner heartbeat every 10 s. Contains node name, agent count, broker address. |
| `nodes/{name}/migrate` | → runner | Migrate a running agent to another node. Payload: `{"name": "...", "target_node": "..."}`. |
| `nodes/{name}/migrate_result` | ← runner | Result of a migration request. |
| `nodes/{name}/reply/{id}` | ← runner | Reply routing for `agent.send_to()` calls originating on this node. |
| `agents/{id}/heartbeat` | ← agent | Per-agent heartbeat every 10 s. Includes `"node": "{name}"` field. |
| `agents/{id}/logs` | ← agent | Log messages from `agent.log()` and `agent.alert()`. |
| `agents/by-name/{name}/task` | → agent | Task addressed to a named agent on any node. Runner routes to local agents by name. |

---

## Supervisor behaviour

Each agent on a remote node runs under a local **ONE_FOR_ONE** supervisor — identical semantics to the main machine. If an agent crashes, the supervisor restarts it with exponential back-off:

```python
delay = min(restart_delay * (2 ** (restart_count - 1)), 60.0)
# restart_delay=3.0: 3s → 6s → 12s → 24s → 48s → 60s (cap)
```

| Scenario | Behaviour |
|----------|-----------|
| Crash in `process()` | Back-off, restart. After 5 consecutive failures in one run, escalates to supervisor for a clean restart. |
| Crash in `setup()` | Fatal — supervisor stops. Broken code won't fix itself on retry. |
| Compile error | Fatal — supervisor stops immediately. |
| Restart budget exhausted (`max_restarts`) | Agent is marked `failed`, removed from the registry, and a fatal event is published. |
| 10 consecutive successful `process()` calls | One restart token is credited back (gradual budget recovery). |
| Deliberate `stop` command | No restart — clean shutdown. |

Default values: `max_restarts=5`, `restart_delay=3.0`. Override per agent in the spawn config.

---

## Agent API on the edge

The `agent` object available inside remote agent code mirrors the local DynamicAgent API. All of the following work identically:

| Method | Description |
|--------|-------------|
| `await agent.publish(topic, data)` | Publish to any MQTT topic via the shared broker. |
| `await agent.log(message)` | Log to `agents/{id}/logs` — visible in the central dashboard. |
| `await agent.alert(message, severity)` | Publish an alert. Levels: `info`, `warning`, `error`. |
| `agent.persist(key, value)` | Write to `/tmp/agentflow_{name}_state.json` (JSON, not pickle — portable). |
| `agent.recall(key)` | Read a persisted value. |
| `agent.state` | In-memory dict, not persisted. |
| `await agent.send_to(name, payload)` | Send a task to any agent (local or remote) via MQTT request/reply. Times out after 30 s by default. |
| `agent.node` | The node name this agent is running on (e.g. `"rpi-livingroom"`). |
| `agent.agents()` | List of all agents running on this node. |

> **⚠ No `agent.subscribe()` on edge** — The remote runner does not implement `agent.subscribe()`. For MQTT subscriptions in remote agents, open an `aiomqtt.Client` directly inside `setup()` — the broker address is available as the machine's IP passed to `--broker`.

---

## Agent migration

A running agent can be moved from one node to another without stopping it manually. The runner on the source node captures the agent's config, publishes it as a spawn command to the target node, then stops the local instance.

```bash
# From the main machine, publish to MQTT:
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/migrate" \
  -m '{"name": "temp-sensor-agent", "target_node": "rpi-bedroom"}'
```

Or trigger it from agent code using `agent.send_to()` if you build a migration manager. The result is published to `nodes/{source_node}/migrate_result`.

---

## Debugging

#### Verbose diagnostics

The runner has a built-in diagnostics logger that prints startup checks, connection events, and every published message to stderr — even if logging is misconfigured. Run with `--loglevel DEBUG` to see everything:

```bash
python3 remote_runner.py --broker 192.168.1.10 --name rpi-test --loglevel DEBUG
```

On startup it prints:

- Whether `aiomqtt`, `psutil`, `aiohttp` are installed
- Whether the broker is TCP-reachable (5 s timeout)
- Every subscribe, publish, and received message with counts

#### Self-test (no broker needed)

```bash
python3 remote_runner.py --test
```

Runs 7 supervisor tests: stable agent, crash + restart, budget exhaustion, deliberate stop, health credit, compile error, setup failure. All should pass.

#### Watch node traffic from the main machine

```bash
mosquitto_sub -h localhost -t 'nodes/#' -v
mosquitto_sub -h localhost -t 'agents/+/heartbeat' -v
```

#### List agents on a node

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/list" -m '{}'
mosquitto_sub -h localhost -t "nodes/rpi-livingroom/agents" -C 1
```

#### Stop a specific remote agent

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/stop" \
  -m '{"name": "temp-sensor-agent"}'
```

#### Stop the entire runner

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/stop_all" -m '{}'
```

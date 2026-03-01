# AgentFlow

A lightweight, async **Actor Model** multi-agent framework built on MQTT.  
Agents are spawned on the fly by an LLM orchestrator — their core logic is written as Python code at runtime, with no hardcoded agent types.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AgentFlow System                      │
│                                                         │
│  ┌──────────┐   MQTT    ┌──────────┐   ┌────────────┐  │
│  │  main    │ ────────► │ monitor  │   │ any-agent  │  │
│  │ (LLM)    │ ◄──────── │ (health) │   │ (dynamic)  │  │
│  └──────────┘           └──────────┘   └────────────┘  │
│       │                                      ▲          │
│       │ spawns + writes code                 │          │
│       └──────────────────────────────────────┘          │
│                                                         │
│  ┌─────────────────────┐   ┌─────────────────────────┐ │
│  │   monitor_server.py │   │      monitor.html        │ │
│  │  (MQTT→WebSocket)   │──►│   (live dashboard)       │ │
│  └─────────────────────┘   └─────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Key Features

- **Dynamic agent spawning** — describe what you want, the LLM writes the Python code and spawns a live actor
- **Pure async** — built on `asyncio` + `aiomqtt`, no threads
- **MQTT backbone** — all agents communicate via MQTT topics
- **Live dashboard** — real-time web UI showing agent health, metrics, event log
- **Persistent state** — agents survive restarts via pickle; spawned agents auto-restore
- **Deterministic IDs** — agent UUIDs derived from name, stable across restarts
- **Protected agents** — `main` and `monitor` cannot be stopped or deleted
- **CLI direct addressing** — `@agent-name message` routes directly to any agent

## Project Structure

```
agentflow/
│
├── core/
│   ├── actor.py          # Base Actor class — mailbox, heartbeat, MQTT, persistence
│   └── registry.py       # ActorRegistry + ActorSystem orchestrator
│
├── agents/
│   ├── main_actor.py     # LLM orchestrator — spawns agents, routes user input
│   ├── dynamic_agent.py  # Generic shell — runs LLM-written Python code
│   ├── llm_agent.py      # LLM-based conversational agent
│   ├── monitor_agent.py  # Health monitor — detects unresponsive agents
│   ├── code_agent.py     # Python code execution agent
│   └── ml_agent.py       # Base for ML/DL agents (YOLO, anomaly detection, etc.)
│
├── interfaces/
│   └── chat_interfaces.py  # CLI, REST, Discord, WhatsApp
│
├── main.py               # Entry point
│
├── monitor_server.py     # MQTT → WebSocket bridge for dashboard
└── monitor.html          # Live web dashboard
```

## Quick Start

### 1. Prerequisites

```bash
# MQTT broker (Mosquitto)
# Windows: https://mosquitto.org/download/
# Linux:
sudo apt install mosquitto mosquitto-clients
sudo systemctl start mosquitto
```

### 2. Install

```bash
git clone https://github.com/waldiez/agentflow
cd agentflow
pip install -r requirements.txt
```

### 3. Configure

```bash
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# Linux/macOS
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 4. Run

```bash
# Terminal 1 — AgentFlow
python -m agentflow.main --interface cli --llm anthropic --mqtt-broker localhost

# Terminal 2 — Dashboard server
python monitor_server.py

# Open dashboard
http://localhost:8888
```

## CLI Usage

```
You: /agents                          # list all active agents
You: @main spawn a webcam yolo agent  # spawn a new agent
You: @code-agent write a bubble sort  # talk directly to an agent
```

### Spawning agents

Just describe what you want:

```
@main spawn an agent that reads from my webcam, runs YOLOv8 detection,
       and publishes detections to MQTT

@main spawn an agent that monitors CPU every 5 seconds and alerts if above 80%

@main spawn an agent that reads temperature from COM3 and publishes to sensors/temp
```

The orchestrator writes Python code and injects it into a `DynamicAgent` shell.

### Agent API (inside generated code)

```python
async def setup(agent):
    # Runs once — load models, open connections
    agent.state['model'] = load_something()

async def process(agent):
    # Runs in a loop — core logic
    await agent.publish_detection({...})
    await agent.log("detected something")

async def handle_task(agent, payload):
    # Respond to messages from other agents
    return {"result": "..."}

async def cleanup(agent):
    # Runs on stop/delete — release resources
    pass
```

| Method | Description |
|---|---|
| `agent.state` | Dict persisting across `process()` calls |
| `agent.publish(topic, data)` | Publish to any MQTT topic |
| `agent.publish_result(data)` | Publish to `agents/{id}/result` |
| `agent.publish_detection(data)` | Publish to `agents/{id}/detections` |
| `agent.log(message)` | Dashboard event log |
| `agent.alert(message, severity)` | Dashboard alert |
| `agent.persist(key, value)` | Save to disk |
| `agent.recall(key)` | Load from disk |
| `agent.send_to(agent_name, payload)` | Message another agent |

## MQTT Topics

| Topic | Content |
|---|---|
| `agents/{id}/heartbeat` | Health, CPU, memory, state |
| `agents/{id}/status` | State changes |
| `agents/{id}/metrics` | Messages processed, errors |
| `agents/{id}/logs` | Event log entries |
| `agents/{id}/alert` | Alerts |
| `agents/{id}/detections` | Vision/detection results |
| `agents/{id}/commands` | Control commands (pause/stop/resume/delete) |
| `system/health` | System-wide health summary |

## Optional Dependencies

```bash
pip install anthropic          # Claude LLM
pip install openai             # GPT-4
pip install ultralytics        # YOLO
pip install opencv-python      # Webcam / image processing
pip install discord.py         # Discord bot interface
pip install twilio             # WhatsApp interface
```

## Persistent State

```
state/
  main/state.pkl          # conversation history + spawn registry
  monitor/state.pkl
  <agent-name>/state.pkl  # per-agent state
```

Reset all state: `rm -rf state/`

## License

MIT

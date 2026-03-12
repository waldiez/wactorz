# AgentFlow

**High-Performance Actor-Model Multi-Agent Framework**  
*Python Foundation · Rust Core · 3D Immersive Dashboard*

---

## 1. What is AgentFlow?

AgentFlow is an asynchronous, actor-model multi-agent framework that allows an LLM orchestrator to spawn, coordinate, monitor, and retire live software agents at runtime. It is designed to operate on real-world IoT data streams and edge devices, providing a lightweight, offline-capable, and fully async environment for agentic workflows.

### Unified Backend Strategy
As of March 2026, AgentFlow supports two backend implementations that operate seamlessly over the same MQTT backbone:
- **Python (Foundation):** The original, flexible implementation. Ideal for rapid development, specialized ML libraries (PyTorch, Ultralytics), and deep IoT integrations like Home Assistant.
- **Rust (Performance):** A high-performance core built with Tokio and Axum. Designed for high-density agent orchestration, system-level safety, and production deployments.

---

## 2. Immersive 3D Dashboard

AgentFlow now features a **Babylon.js 3D Dashboard** (in addition to the classic HTML monitor). This immersive interface provides:
- **Real-time 3D Graph:** Visualize actor connections and message flows as glowing Bezier arcs.
- **Galaxy View:** An orbital representation of your agent system.
- **Agent HUD:** Immersive cards showing real-time token cost, heartbeats, and status.
- **Interactive Chat:** Direct @mention routing to any agent in the cluster.

---

## 3. Architecture

AgentFlow is built on the **Actor Model**. Each agent is an independent unit with its own async message loop and mailbox; agents never share memory and communicate solely via typed messages.

### Hybrid Tech Stack

| Layer | Technology |
|-------|------------|
| **Primary Backend** | Rust (Tokio, Axum, rumqttc) |
| **Specialized Workers** | Python (Asyncio, aiomqtt) |
| **Communication** | MQTT (Mosquitto) |
| **Frontend** | Babylon.js 7.x, Vite, TypeScript |
| **Knowledge Base** | Apache Jena Fuseki (RDF/SPARQL) |

---

## 4. Getting Started

### Unified Entry Point
Use the `run.sh` script to select your backend. It respects the `AGENTFLOW_BACKEND` environment variable (defaults to `rust`).

```bash
# Start with the Rust Performance Core (Default)
./run.sh

# Start with the Python Foundation
AGENTFLOW_BACKEND=python ./run.sh
```

### Quick Installation

1. **Clone and Setup Python:**
   ```bash
   git clone https://github.com/waldiez/agentflow
   cd agentflow
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Build Rust & Frontend:**
   ```bash
   make build
   ```

3. **Configure Environment:**
   Copy `.env.example` to `.env` and set your `ANTHROPIC_API_KEY` or other LLM keys.

---

## 5. Agent Types & Rosters

| Agent | Purpose | Backend |
|-------|---------|---------|
| `MainActor` | The LLM orchestrator; parses `<spawn>` blocks. | Rust/Py |
| `MonitorAgent`| Health watchdog; monitors heartbeats and alerts. | Rust/Py |
| `IOAgent` | UI Gateway; routes chat traffic to actors. | Rust |
| `NautilusAgent`| Deployment; SSH, rsync, and remote node bootstrap. | Rust |
| `ManualAgent` | PDF Specialist; finds and extracts product manuals. | Rust/Py |
| `HomeAssistant`| IoT Specialist; manages HA automations. | Python |

---

## 6. Table of Contents (Original Documentation)

The following sections provide a deep dive into the core AgentFlow philosophy and the original Python implementation details.

1. [Detailed Architecture](#detailed-architecture)
2. [Spawning Agents at Runtime](#spawning-agents-at-runtime)
3. [Agent-to-Agent Communication](#agent-to-agent-communication)
4. [Health Monitoring & Error Recovery](#health-monitoring--error-recovery)
5. [Persistence & State](#persistence--state)
6. [LLM Cost Tracking](#llm-cost-tracking)
7. [Remote Nodes & Edge Deployment](#remote-nodes--edge-deployment)
8. [Troubleshooting](#troubleshooting)

---

## Detailed Architecture

### The Actor Model
Each agent is an Actor: an independent unit with its own async message loop, mailbox (`asyncio.Queue` or `tokio::mpsc`), and lifecycle (`CREATED → RUNNING → PAUSED → STOPPED / FAILED`).

```
Message flow:

  Actor A                Registry              Actor B
  ───────               ──────────             ───────
  send(B_id, TASK, {…}) ──────────────────►  mailbox.put(msg)
                                              message_loop picks it up
                                              handle_message(msg) fires
                        ◄─────────────────── send(A_id, RESULT, {…})
```

### Core Components (Python)

| File | Layer | Role |
|------|-------|------|
| `core/actor.py` | Core | Base Actor class — mailbox, lifecycle, heartbeat, spawn, send, persist/recall |
| `core/registry.py` | Core | ActorSystem & ActorRegistry — actor registration, message routing, broadcast |
| `agents/main_actor.py` | Agent | The LLM orchestrator — processes user input, spawns agents, routes requests |
| `agents/monitor_agent.py` | Agent | Health watcher — detects crashes, fires recovery actions, notifies user |

---

## Spawning Agents at Runtime

Simply describe what you want in the chat. The LLM will write the code and wrap it in a `<spawn>` block. You never need to write code yourself.

### The Spawn Block (Python Example)

```json
<spawn>
{
  "name": "weather-agent",
  "type": "dynamic",
  "description": "Fetches live weather from Open-Meteo",
  "install": ["httpx"],
  "poll_interval": 3600,
  "code": "
    async def setup(agent):
        await agent.log('Weather agent ready')

    async def handle_task(agent, payload):
        import httpx
        city = payload.get('city', 'Athens')
        async with httpx.AsyncClient() as c:
            r = await c.get(f'https://wttr.in/{city}?format=j1')
        return r.json()['current_condition'][0]
  "
}
</spawn>
```

---

## Remote Nodes & Edge Deployment

AgentFlow can run agents on any machine on your network — Raspberry Pi, VM, or cloud server. The edge node only needs a single file (`remote_runner.py`) and the `aiomqtt` package.

### Edge Node Requirements
```bash
pip install aiomqtt --break-system-packages
python3 remote_runner.py --broker <BROKER_IP> --name rpi-kitchen
```

---

*AgentFlow — built conversation by conversation.*

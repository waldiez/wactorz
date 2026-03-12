# AgentFlow

**High-Performance Actor-Model Multi-Agent Framework**  
*Rust Core · Python Fallback · 3D Immersive Dashboard*

---

## 1. What is AgentFlow?

AgentFlow is a real-time, asynchronous multi-agent orchestration system built on the **Actor Model** with an **MQTT** communication backbone. It allows an LLM orchestrator to spawn, coordinate, monitor, and retire live software agents at runtime without code restarts.

### "Rust-First" Strategy
The system follows a **Rust-First, Python-as-needed** architecture:
- **Rust Backend:** High-performance core (Tokio/Axum) handling high-density actor orchestration, MQTT routing, and system safety.
- **Python Fallback:** Leveraged for specialized libraries (PyTorch, Ultralytics, Home Assistant) or rapid development where Python's ecosystem is essential.
- **Seamless Orchestration:** Both backends communicate over the same MQTT topics, allowing a Rust orchestrator to delegate heavy ML or IoT tasks to Python workers transparently.

---

## 2. Architecture

AgentFlow uses the **Actor Model**: every agent is an independent actor with its own isolated mailbox. No shared mutable state exists; all communication happens via typed messages over MQTT.

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser                                                           │
│  Babylon.js 3D Dashboard ←──WebSocket──►  nginx  ←─── /ws  ──►     │
│                          ←──REST───────►         ←─── /api/ ──►     │
│                          ←──MQTT/WS────►         ←─── /mqtt ──►     │
└────────────────────────────────────────────────────────────────────┘
                                     │
                              nginx  │  (single public entry point)
                                     │
          ┌──────────────────────────┼──────────────────────────────┐
          │                          │                              │
          ▼                          ▼                              ▼
  agentflow (Rust/Py)        Mosquitto (MQTT)              Fuseki (RDF)
  :8080 REST                 :1883 TCP / :9001 WS          :3030 SPARQL
  :8081 WS bridge
          │
          │  pub/sub via MQTT
          ├── MainActor        (LLM orchestrator · alpha)
          ├── MonitorAgent     (health watchdog · bravo)
          ├── IOAgent          (UI gateway · charlie)
          ├── QAAgent          (safety observer · delta)
          ├── NautilusAgent    (SSH / rsync bridge · foxtrot)
          └── DynamicAgent*    (LLM-generated scripts / Rhai / Python)
```

---

## 3. Getting Started

### Unified Entry Point
Use the `run.sh` script (or `make run`) to start the system. The backend is controlled by the `AGENTFLOW_BACKEND` environment variable.

```bash
# Start with Rust backend (default)
./run.sh

# Start with Python backend
AGENTFLOW_BACKEND=python ./run.sh
```

### Installation

1. **Rust Requirements:** Rust 1.93+ (for the high-performance core)
2. **Python Requirements:** Python 3.10+ and `pip install -r requirements.txt`
3. **Frontend Requirements:** Node.js (to build the 3D dashboard)

```bash
# Build the full stack
make build

# Start the stack (Docker for Mosquitto/nginx + Native Backend)
make up
```

---

## 4. Immersive 3D Dashboard

AgentFlow includes a **Babylon.js** single-page application that visualizes your agent network in real-time.

- **3D Graph View:** Real-time spring-force layout showing active connections.
- **Galaxy View:** Orbital representation with the MainActor at the center.
- **Agent HUD:** Real-time token usage, cost meters, and status indicators.
- **Interactive Chat:** @mention agents directly from the UI.

---

## 5. Agent Types

| Agent | Backend | Role |
|-------|---------|------|
| `MainActor` | Rust/Py | Orchestrator; parses `<spawn>` blocks to create new agents. |
| `MonitorAgent` | Rust/Py | Health watchdog; monitors heartbeats and restarts failed actors. |
| `IOAgent` | Rust | UI Gateway; routes WebSocket/MQTT chat traffic to actors. |
| `NautilusAgent` | Rust | Deployment specialist; handles SSH, rsync, and remote pings. |
| `ManualAgent` | Rust/Py | PDF specialist; searches and extracts technical manual content. |
| `HomeAssistant`| Python | IoT integration; CRUD operations for HA automations. |
| `DynamicAgent` | Rust/Py | Runtime-generated agents (Rhai scripts in Rust, Python in Py). |

---

## 6. LLM Providers

AgentFlow supports multiple LLM backends with per-agent cost and token tracking:

| Provider | Model Example | Key Required |
|----------|---------------|--------------|
| **Anthropic** | `claude-3-5-sonnet` | `ANTHROPIC_API_KEY` |
| **OpenAI** | `gpt-4o` | `OPENAI_API_KEY` |
| **Google** | `gemini-2.0-flash` | `LLM_API_KEY` |
| **NVIDIA NIM**| `meta/llama-3.3-70b` | `NIM_API_KEY` |
| **Ollama** | `llama3` | (Local) |

---

## 7. Configuration (.env)

Key variables in your `.env` file:
- `AGENTFLOW_BACKEND`: `rust` (default) or `python`.
- `LLM_PROVIDER`: Primary provider for the MainActor.
- `MQTT_HOST`: Defaults to `localhost` for native execution.

---

*AgentFlow — High-performance multi-agent orchestration.*

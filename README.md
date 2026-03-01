# AgentFlow 🤖

**Actor-Model Multi-Agent Framework in Python**

Every agent IS an actor. Actors communicate only through async message passing.
The system runs 24/7, publishes state via MQTT, and plugs into any chat platform.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ActorSystem                          │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  MainActor  │  │ MonitorActor │  │   CodeAgent   │  │
│  │  (LLM +     │  │  (watches    │  │  (LLM + exec  │  │
│  │ orchestrate)│  │   all others)│  │   sandboxed)  │  │
│  └──────┬──────┘  └──────────────┘  └───────────────┘  │
│         │                                               │
│  ┌──────▼──────────────────────────────────────────┐    │
│  │             ActorRegistry (message router)      │    │
│  └─────────────────────────────────────────────────┘    │
│                         │                               │
│  ┌──────────────┐  ┌────▼─────────┐  ┌──────────────┐  │
│  │  YOLOAgent   │  │  AnomalyDet  │  │  CustomAgent │  │
│  │  (ML/DL)     │  │  (24/7 loop) │  │  (your own)  │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
         │ MQTT
┌────────▼──────────────────────────────────────┐
│  agents/{id}/status | heartbeat | metrics     │
│  agents/{id}/logs | spawned | completed       │
│  system/health | system/alerts                │
└───────────────────────────────────────────────┘
         │ Chat Interfaces
┌────────▼──────────────────────────────────────┐
│  CLI  |  REST API  |  Discord  |  WhatsApp    │
└───────────────────────────────────────────────┘
```

---

## Quick Start

```bash
# Install core deps
pip install anthropic psutil aiomqtt aiohttp

# Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run CLI
python -m agentflow.main --interface cli --llm anthropic

# Run REST API (port 8000)
python -m agentflow.main --interface rest --port 8000

# Run Discord bot
export DISCORD_BOT_TOKEN=your_token
python -m agentflow.main --interface discord

# Run with local Ollama (no API key needed)
python -m agentflow.main --interface cli --llm ollama --ollama-model llama3
```

---

## Creating a Custom Actor

```python
from agentflow import Actor, Message, MessageType

class MyAgent(Actor):
    async def on_start(self):
        print(f"{self.name} is alive!")

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            result = await self.do_work(msg.payload)
            await self.send(msg.sender_id, MessageType.RESULT, result)

    async def do_work(self, payload):
        return {"done": True}
```

## Creating an LLM Agent

```python
from agentflow import LLMAgent, AnthropicProvider

class ResearchAgent(LLMAgent):
    pass  # All LLM logic inherited!

agent = ResearchAgent(
    name="researcher",
    llm_provider=AnthropicProvider(),
    system_prompt="You are a research specialist."
)
```

## Creating a Non-LLM ML Agent

```python
from agentflow import MLAgent

class SentimentAgent(MLAgent):
    def load_model(self):
        from transformers import pipeline
        return pipeline("sentiment-analysis")

    async def predict(self, input_data):
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._model(input_data["text"])
        )
        return {"sentiment": result[0]}
```

## Spawning Agents Dynamically

```python
# From within any actor:
child = await self.spawn(CodeAgent, name="child-coder")

# Send it a task
await self.send(child.actor_id, MessageType.TASK, {"text": "print hello world"})
```

## Lifecycle Commands

```python
# Via MainActor
await main.send_command("code-agent", MessageType.PAUSE)
await main.send_command("code-agent", MessageType.RESUME)
await main.send_command("anomaly-detector", MessageType.STOP)
```

---

## MQTT Topics

| Topic | Payload |
|-------|---------|
| `agents/{id}/status` | `"running"` \| `"stopped"` \| `"failed"` |
| `agents/{id}/heartbeat` | `{timestamp, cpu, memory_mb, state, task}` |
| `agents/{id}/metrics` | `{messages_processed, errors, uptime, tasks_completed}` |
| `agents/{id}/logs` | structured log entries |
| `agents/{id}/spawned` | `{child_id, child_name, timestamp}` |
| `agents/{id}/completed` | `{result_preview, duration, task}` |
| `agents/{id}/alert` | `{severity, last_seen_ago, ...}` |
| `system/health` | full system snapshot |
| `system/alerts` | any actor going unresponsive |

---

## 24/7 Background Deployment

Use `supervisord` or `systemd`:

```ini
# /etc/supervisor/conf.d/agentflow.conf
[program:agentflow]
command=python -m agentflow.main --interface rest --port 8000
autostart=true
autorestart=true
stderr_logfile=/var/log/agentflow.err.log
stdout_logfile=/var/log/agentflow.out.log
```

---

## Multi-Device Deployment

Each device runs its own ActorSystem. They communicate via a shared MQTT broker (e.g., Mosquitto, EMQX, or HiveMQ Cloud).

```
Device A (RPi)          Shared MQTT Broker       Device B (Server)
  YOLOAgent    ─────►  agents/yolo/heartbeat  ◄─── MonitorActor
  AnomalyDet   ─────►  agents/anomaly/result  ◄─── MainActor
```

---

## File Structure

```
agentflow/
├── core/
│   ├── actor.py          # Base Actor class
│   └── registry.py       # ActorRegistry + ActorSystem
├── agents/
│   ├── llm_agent.py      # LLM-backed agents (Anthropic/OpenAI/Ollama)
│   ├── main_actor.py     # Main orchestrator actor
│   ├── monitor_agent.py  # Health monitor
│   ├── code_agent.py     # Code generation + sandboxed execution
│   └── ml_agent.py       # ML/DL agents (YOLO, anomaly detection)
├── interfaces/
│   └── chat_interfaces.py # CLI, REST, Discord, WhatsApp
├── main.py               # Entry point
└── requirements.txt
```

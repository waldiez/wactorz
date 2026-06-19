"""
UdxAgent — User and Developer Xpert.

Zero-LLM, zero-API-key built-in knowledge base about the Wactorz system.
Responds instantly. Has access to self._registry to enumerate live agents.

Commands:
  help                    overview of all commands
  help <topic>            topic-specific help
  docs <topic>            in-depth documentation (alias for help <topic>)
  explain <concept>       explain an architecture concept
  agents                  list all registered agents (name + state)
  status                  system summary (counts by state)
  version                 Wactorz version string
"""

from __future__ import annotations

import logging
import time

from ..core.actor import Actor, ActorState, Message, MessageType

logger = logging.getLogger(__name__)

_VERSION = "Wactorz v0.1.0 — Python runtime"

_KB: dict[str, str] = {
    "architecture": (
        "**Architecture — Actor Model + MQTT pub/sub**\n\n"
        "Every agent is an isolated Actor with its own async mailbox, heartbeat loop, and\n"
        "persistence directory. Actors never share state; all communication is message-passing\n"
        "via MQTT topics or direct mailbox delivery. An `ActorRegistry` routes messages by actor ID."
    ),
    "actor-model": (
        "**Actor Model**\n\n"
        "An Actor is a concurrent entity with a private mailbox (`asyncio.Queue`).\n"
        "Actors communicate only by sending messages — no shared memory, no locks between them.\n"
        "Each actor runs independent `_message_loop`, `_heartbeat_loop`, and `_command_listener`.\n"
        "State persists to disk via pickle (`persist()` / `recall()`) across restarts."
    ),
    "mqtt": (
        "**MQTT Topics**\n\n"
        "- `agents/{id}/spawn` — agent announced itself\n"
        "- `agents/{id}/heartbeat` — liveness pulse (every 10 s)\n"
        "- `agents/{id}/status` — state change events\n"
        "- `agents/{id}/alert` — health alerts\n"
        "- `agents/{id}/chat` — chat messages to/from an agent\n"
        "- `io/chat` — inbound user messages routed by IOAgent\n"
        "- `system/health` — aggregate health from MonitorAgent\n"
        "- `system/coin` — WaldiezCoin economy events\n"
        "- `system/qa-flag` — QA safety flags"
    ),
    "api": (
        "**REST / WS / MQTT endpoints**\n\n"
        "- REST API: `http://host/api/` — send tasks, query agent status\n"
        "- WebSocket bridge: `ws://host/ws` — MQTT→browser real-time stream\n"
        "- MQTT WebSocket: `ws://host/mqtt` (port 9001 via nginx)\n"
        "- nginx is the single public entry point; wactorz binary listens on :8080 (REST) / :8081 (WS)"
    ),
    "chat": (
        "**Chat flow**\n\n"
        "User input arrives on `io/chat`. IOAgent parses an optional `@name` prefix and routes\n"
        "the message to the named actor's mailbox. If no prefix, it goes to `main-actor`.\n"
        "Replies are published on `agents/{id}/chat` and forwarded to the browser via WebSocket."
    ),
    "dashboard": (
        "**Dashboard (frontend/)**\n\n"
        "Vite + TypeScript + Babylon.js 7.x + mqtt.js. Themes: `social`, `fin`, `graph`, `galaxy`,\n"
        "`cards`, `cards-3d`, `grave`, `ops`. MQTT messages drive live agent cards, 3-D scene nodes,\n"
        "ActivityFeed, and CoinTicker. IOBar lets users chat with any agent."
    ),
    "deploy": (
        "**Deployment modes**\n\n"
        "1. Docker Compose (`compose.yaml`) — full stack (`--profile python` / `python-full`)\n"
        "2. pip install + systemd (`systemd/wactorz.service`) — Python app on the host\n"
        "3. Dev mode (`compose.dev.yaml`) — mosquitto + mock-agents only"
    ),
    "hlc-wid": (
        "**HLC-WID — Hybrid Logical Clock WID**\n\n"
        "Actor IDs use HLC-WID: time-ordered, causally consistent identifiers combining a\n"
        "physical timestamp with a logical counter and an optional node tag.\n"
        "Library: `waldiez-wid` (Rust) / `@waldiez/wid` (TypeScript) from `github:waldiez/wid`."
    ),
    "wid": (
        "**WID — Message IDs**\n\n"
        "Individual messages use plain WID (random, compact). Actor IDs use HLC-WID (time-ordered).\n"
        "Both are provided by the same `waldiez-wid` / `@waldiez/wid` package."
    ),
    "rust": (
        "**Rust port (`rust/`)**\n\n"
        "Full port using tokio (async), rumqttc (MQTT), and Rhai (dynamic scripting).\n"
        "Crates: `wactorz-core`, `wactorz-agents`, `wactorz-mqtt`.\n"
        "Binary ~12 MB. Minimum Rust toolchain: 1.93 (bookworm). All actor IDs are HLC-WID strings."
    ),
    "babylon": (
        "**Babylon.js frontend**\n\n"
        "Babylon.js 7.x + `@babylonjs/gui` for labels. 3-D themes: `graph` (spring-force spheres)\n"
        "and `galaxy` (orbiting planets). Chat messages produce Bezier comet arcs.\n"
        "Agents are glowing spheres with billboard name labels."
    ),
    "nautilus": (
        "**NautilusAgent — SSH/rsync bridge**\n\n"
        "Remote shell and file-transfer over SSH/rsync.\n"
        "Commands: `ping`, `exec`, `sync`, `push`, `help`.\n"
        "No shell injection — all args are discrete subprocess tokens."
    ),
    "io": (
        "**IOAgent — user gateway**\n\n"
        "Subscribes to `io/chat`. Parses `@name` prefix to route messages to named actors.\n"
        "No prefix → forwards to `main-actor`. agentType: `gateway`."
    ),
    "qa": (
        "**QA Agent — safety observer**\n\n"
        "Passively observes all `/chat` messages. Flags prompt-injection, error bleed,\n"
        "raw JSON exposure, PII, and no-response timeouts. Publishes to `system/qa-flag`.\n"
        "agentType: `guardian`."
    ),
    "monitor": (
        "**MonitorAgent — health watcher**\n\n"
        "Polls all actors every 15 s. Fires alerts for actors silent >60 s.\n"
        "Publishes aggregate health to `system/health`. Protected. agentType: `monitor`."
    ),
    "dynamic": (
        "**DynamicAgent — LLM-generated code executor**\n\n"
        "Receives Python source with `setup()` / `handle_task()` hooks and runs it via `exec()`.\n"
        "Rhai scripting is the Rust equivalent. agentType: `dynamic`."
    ),
    "main": (
        "**main-actor — LLM orchestrator**\n\n"
        "Primary LLM-backed actor. Calls Anthropic/OpenAI/Ollama and parses `<spawn>` blocks\n"
        "to create DynamicAgents. Protected. Default model: `claude-sonnet-4-6`.\n"
        "agentType: `orchestrator`."
    ),
    "udx": (
        "**udx-agent — User and Developer Xpert**\n\n"
        "Zero-LLM, zero-API-key knowledge base. Commands: `help`, `docs`, `explain`,\n"
        "`agents`, `status`, `version`. agentType: `assistant`."
    ),
    "weather": (
        "**weather-agent — real-time weather**\n\n"
        "Fetches current weather from wttr.in (no API key). Usage: `@weather-agent [city]`.\n"
        "Default location: `WEATHER_DEFAULT_LOCATION` env var (fallback: London).\n"
        "agentType: `data`."
    ),
    "news": (
        "**news-agent — HackerNews headlines**\n\n"
        "Fetches HN headlines on demand. Commands: `top [n]`, `new`, `best`, `ask`, `show`, `jobs`, `help`.\n"
        "No API key needed. Uses Firebase HN API. agentType: `data`."
    ),
    "wif": (
        "**wif-agent — finance expert**\n\n"
        "In-memory finance tracker: expenses, budgets, compound interest, loan, ROI, tax, tip.\n"
        "Commands: `add`, `budget`, `report`, `balance`, `compound`, `loan`, `roi`, `tax`, `tip`, `help`.\n"
        "agentType: `financier`."
    ),
    "wiz": (
        "**wiz-agent — WaldiezCoin economist**\n\n"
        "In-game token economy (+10 spawn, +2 heartbeat, +5 healthy, −5 QA flag, −3 alert).\n"
        "Commands: `balance`, `history [n]`, `earn`, `debit`, `help`.\n"
        "Publishes `system/coin`. agentType: `coin`."
    ),
}

_ALIASES: dict[str, str] = {
    "actors": "actor-model", "actor": "actor-model", "message": "chat", "messages": "chat",
    "web": "dashboard", "ui": "dashboard", "frontend": "dashboard",
    "rest": "api", "websocket": "api", "ws": "api",
    "id": "hlc-wid", "ids": "hlc-wid", "identifier": "hlc-wid", "wids": "wid",
    "ssh": "nautilus", "rsync": "nautilus", "transfer": "nautilus",
    "io-agent": "io", "ioagent": "io", "gateway": "io",
    "safety": "qa", "guardian": "qa",
    "health": "monitor", "monitoring": "monitor",
    "orchestrator": "main", "llm": "main",
    "coin": "wiz", "economy": "wiz",
    "finance": "wif", "financier": "wif",
    "coding": "dynamic", "exec": "dynamic", "script": "dynamic",
    "3d": "babylon", "graph": "babylon", "galaxy": "babylon",
    "docker": "deploy", "deployment": "deploy", "native": "deploy", "binary": "rust",
}

_TOPICS = ", ".join(sorted(_KB.keys()))

_OVERVIEW = f"""\
**udx-agent — User and Developer Xpert**

Built-in, zero-LLM knowledge base for Wactorz.

**Commands:**
- `@udx-agent help` — this message
- `@udx-agent help <topic>` — quick help on a topic
- `@udx-agent docs <topic>` — same as help <topic>
- `@udx-agent explain <concept>` — explain an architecture concept
- `@udx-agent agents` — list all registered agents
- `@udx-agent status` — system summary (agent counts by state)
- `@udx-agent version` — Wactorz version

**Available topics:** {_TOPICS}

For LLM-powered answers on anything else, try `@main-actor`."""


def _lookup(raw: str) -> str | None:
    key = raw.lower().strip()
    key = _ALIASES.get(key, key)
    return _KB.get(key)


class UdxAgent(Actor):
    """User and Developer Xpert — instant, LLM-free documentation agent."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "udx-agent")
        super().__init__(**kwargs)

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {"agentId": self.actor_id, "agentName": self.name, "agentType": "assistant", "timestamp": time.time()},
        )
        logger.info(f"[{self.name}] started — {len(_KB)} topics in KB")

    async def handle_message(self, msg: Message):
        if msg.type not in (MessageType.TASK, MessageType.RESULT):
            return
        payload = msg.payload or {}
        text = str(
            payload.get("text") or payload.get("content") or ""
            if isinstance(payload, dict) else payload
        )
        for pfx in ("@udx-agent", "@udx_agent"):
            if text.lower().startswith(pfx):
                text = text[len(pfx):].lstrip()
                break
        await self._reply(self._dispatch(text.strip()))
        self.metrics.tasks_completed += 1

    def _dispatch(self, text: str) -> str:
        if not text:
            return _OVERVIEW
        lower = text.lower()
        if lower == "help":
            return _OVERVIEW
        if lower.startswith(("help ", "docs ", "explain ")):
            topic = text.split(None, 1)[1] if " " in text else ""
            result = _lookup(topic)
            return result if result else (
                f"No docs on **{topic}** yet. Try `@main-actor`.\n\n**Available topics:** {_TOPICS}"
            )
        if lower == "agents":
            return self._list_agents()
        if lower == "status":
            return self._system_status()
        if lower == "version":
            return f"**Version:** {_VERSION}"
        # Try as a direct topic lookup
        result = _lookup(text)
        if result:
            return result
        return (
            f"I don't have docs on **{text}** yet. Try `@main-actor` for LLM-powered answers.\n\n"
            f"**Available topics:** {_TOPICS}"
        )

    def _list_agents(self) -> str:
        if not self._registry:
            return "Registry not available."
        actors = self._registry.all_actors()
        if not actors:
            return "No agents currently registered."
        lines = ["**Registered agents:**\n"]
        for a in actors:
            marker = " (you)" if a.actor_id == self.actor_id else ""
            lines.append(f"- **{a.name}** — {a.state.value}{marker}")
        return "\n".join(lines)

    def _system_status(self) -> str:
        if not self._registry:
            return "Registry not available."
        actors  = self._registry.all_actors()
        running = sum(1 for a in actors if a.state == ActorState.RUNNING)
        stopped = sum(1 for a in actors if a.state == ActorState.STOPPED)
        failed  = sum(1 for a in actors if a.state == ActorState.FAILED)
        verdict = "HEALTHY" if failed == 0 and running > 0 else ("UNHEALTHY" if failed > 0 else "DEGRADED")
        return (
            f"**System: {verdict}**\n\n"
            f"Total: {len(actors)} | Running: {running} | Stopped: {stopped} | Failed: {failed}\n\n"
            f"*{_VERSION}*"
        )

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

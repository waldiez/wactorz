/**
 * UdxAgent — User and Developer Xpert (Node.js port).
 * Zero-LLM built-in knowledge base. agentType: "assistant"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { ActorRegistry } from "../core/registry";
import { Message, MessageType } from "../core/types";

const VERSION = "AgentFlow v0.1.0 — Node.js runtime";

const KB: Record<string, string> = {
  architecture:
    "**Architecture — Actor Model + MQTT pub/sub**\n\nEvery agent is an isolated Actor with its own async mailbox, heartbeat loop, and persistence. Actors never share state; all communication is message-passing via MQTT topics or direct mailbox delivery.",
  "actor-model":
    "**Actor Model**\n\nAn Actor is a concurrent entity with a private mailbox. Actors communicate only by sending messages — no shared memory, no locks between them. Each actor runs independent message loop and heartbeat.",
  mqtt:
    "**MQTT Topics**\n\n- `agents/{id}/spawn` — agent announced itself\n- `agents/{id}/heartbeat` — liveness pulse (every 10 s)\n- `agents/{id}/status` — state change events\n- `agents/{id}/chat` — chat messages to/from an agent\n- `io/chat` — inbound user messages routed by IOAgent\n- `system/health` — aggregate health from MonitorAgent\n- `system/coin` — WaldiezCoin economy events\n- `system/qa-flag` — QA safety flags",
  api:
    "**REST / WS / MQTT endpoints**\n\n- REST API: `http://host/api/` — send tasks, query agent status\n- WebSocket bridge: `ws://host/ws` — MQTT→browser real-time stream\n- MQTT WebSocket: `ws://host/mqtt` (port 9001 via nginx)",
  chat:
    "**Chat flow**\n\nUser input arrives on `io/chat`. IOAgent parses an optional `@name` prefix and routes the message to the named actor's mailbox. If no prefix, it goes to `main-actor`. Replies are published on `agents/{id}/chat` and forwarded to the browser via WebSocket.",
  dashboard:
    "**Dashboard (frontend/)**\n\nVite + TypeScript + Babylon.js 7.x + mqtt.js. Themes: `social`, `fin`, `graph`, `galaxy`, `cards`, `cards-3d`, `grave`, `ops`. MQTT messages drive live agent cards, 3-D scene nodes, ActivityFeed, and CoinTicker.",
  deploy:
    "**Deployment modes**\n\n1. Docker Compose (`compose.yaml`) — full stack\n2. Pre-built image — `scripts/package-release.sh`\n3. Native binary (`compose.native.yaml`) — Rust binary on host\n4. Dev mode (`compose.dev.yaml`) — mosquitto + mock-agents only",
  "hlc-wid":
    "**HLC-WID — Hybrid Logical Clock WID**\n\nActor IDs use HLC-WID: time-ordered, causally consistent identifiers combining a physical timestamp with a logical counter and an optional node tag.",
  nautilus:
    "**NautilusAgent — SSH/rsync bridge**\n\nRemote shell and file-transfer over SSH/rsync. Commands: `ping`, `exec`, `sync`, `push`, `help`. No shell injection — all args are discrete subprocess tokens.",
  io: "**IOAgent — user gateway**\n\nSubscribes to `io/chat`. Parses `@name` prefix to route messages to named actors. No prefix → forwards to `main-actor`. agentType: `gateway`.",
  qa: "**QA Agent — safety observer**\n\nPassively observes all `/chat` messages. Flags prompt-injection, error bleed, raw JSON exposure, PII, and no-response timeouts. Publishes to `system/qa-flag`. agentType: `guardian`.",
  monitor:
    "**MonitorAgent — health watcher**\n\nPolls all actors every 15 s. Fires alerts for actors silent >60 s. Publishes aggregate health to `system/health`. Protected. agentType: `monitor`.",
  main: "**main-actor — LLM orchestrator**\n\nPrimary LLM-backed actor. Calls Anthropic/OpenAI/Ollama and parses `<spawn>` blocks to create DynamicAgents. Protected. Default model: `claude-sonnet-4-6`. agentType: `orchestrator`.",
  udx: "**udx-agent — User and Developer Xpert**\n\nZero-LLM, zero-API-key knowledge base. Commands: `help`, `docs`, `explain`, `agents`, `status`, `version`. agentType: `assistant`.",
  weather:
    "**weather-agent — real-time weather**\n\nFetches current weather from wttr.in (no API key). Usage: `@weather-agent [city]`. agentType: `data`.",
  news: "**news-agent — HackerNews headlines**\n\nFetches HN headlines on demand. Commands: `top [n]`, `new`, `best`, `ask`, `show`, `jobs`, `help`. No API key needed. agentType: `data`.",
  wif: "**wif-agent — finance expert**\n\nIn-memory finance tracker: expenses, budgets, compound interest, loan, ROI, tax, tip. Commands: `add`, `budget`, `report`, `balance`, `compound`, `loan`, `roi`, `tax`, `tip`, `help`. agentType: `financier`.",
  wiz: "**wiz-agent — WaldiezCoin economist**\n\nIn-game token economy (+10 spawn, +2 heartbeat, +5 healthy, −5 QA flag, −3 alert). Commands: `balance`, `history [n]`, `earn`, `debit`, `help`. agentType: `coin`.",
  fuseki:
    "**fern-agent — SPARQL knowledge graph**\n\nConnects to Apache Jena Fuseki. Commands: `query <sparql>`, `ask <sparql>`, `prefixes`, `datasets`, `help`. No API key. agentType: `librarian`.",
  tick: "**chron-agent — scheduler/timer**\n\nIn-process cron. Commands: `at <HH:MM>`, `in <n> <unit>`, `every <n> <unit>`, `list`, `cancel <id>`, `clear`, `help`. agentType: `scheduler`.",
  ha: "**ha-agent — Home Assistant discovery**\n\nQueries HA REST API for devices and entities. Commands: `status`, `devices`, `entities`, `domains`, `search <kw>`, `state <entity_id>`, `help`. agentType: `home-assistant`.",
};

const ALIASES: Record<string, string> = {
  actors: "actor-model", actor: "actor-model", message: "chat", messages: "chat",
  web: "dashboard", ui: "dashboard", frontend: "dashboard",
  rest: "api", websocket: "api", ws: "api",
  id: "hlc-wid", ids: "hlc-wid",
  ssh: "nautilus", rsync: "nautilus",
  "io-agent": "io", ioagent: "io", gateway: "io",
  safety: "qa", guardian: "qa",
  health: "monitor", monitoring: "monitor",
  orchestrator: "main", llm: "main",
  coin: "wiz", economy: "wiz",
  finance: "wif", financier: "wif",
  fern: "fuseki", "fuseki-agent": "fuseki",
  chron: "tick", "tick-agent": "tick", scheduler: "tick",
  "home-assistant": "ha", homeassistant: "ha",
};

const TOPICS = Object.keys(KB).sort().join(", ");

const OVERVIEW = `**udx-agent — User and Developer Xpert**

Built-in, zero-LLM knowledge base for AgentFlow.

**Commands:**
- \`@udx-agent help\` — this message
- \`@udx-agent help <topic>\` — quick help on a topic
- \`@udx-agent docs <topic>\` — same as help <topic>
- \`@udx-agent explain <concept>\` — explain an architecture concept
- \`@udx-agent agents\` — list all registered agents
- \`@udx-agent status\` — system summary (agent counts by state)
- \`@udx-agent version\` — AgentFlow version

**Available topics:** ${TOPICS}

For LLM-powered answers on anything else, try \`@main-actor\`.`;

function lookup(raw: string): string | undefined {
  const key = ALIASES[raw.toLowerCase()] ?? raw.toLowerCase();
  return KB[key];
}

export class UdxAgent extends Actor {
  private _registry: ActorRegistry;

  constructor(publish: MqttPublisher, registry: ActorRegistry, actorId?: string) {
    super("udx-agent", publish, actorId);
    this._registry = registry;
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("assistant");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    text = Actor.stripPrefix(text, "@udx-agent", "@udx_agent");
    this.replyChat(this._dispatch(text.trim()));
    this.metrics.tasksCompleted++;
  }

  private _dispatch(text: string): string {
    if (!text || text === "help") return OVERVIEW;
    const lower = text.toLowerCase();

    if (lower.startsWith("help ") || lower.startsWith("docs ") || lower.startsWith("explain ")) {
      const topic = text.split(/\s+/, 2)[1] ?? "";
      const result = lookup(topic);
      return result ?? `No docs on **${topic}** yet. Try \`@main-actor\`.\n\n**Available topics:** ${TOPICS}`;
    }
    if (lower === "agents") return this._listAgents();
    if (lower === "status") return this._systemStatus();
    if (lower === "version") return `**Version:** ${VERSION}`;

    const result = lookup(text);
    if (result) return result;
    return `I don't have docs on **${text}** yet. Try \`@main-actor\` for LLM-powered answers.\n\n**Available topics:** ${TOPICS}`;
  }

  private _listAgents(): string {
    const actors = this._registry.allActors();
    if (actors.length === 0) return "No agents currently registered.";
    const lines = ["**Registered agents:**\n"];
    for (const a of actors) {
      const marker = a.actorId === this.actorId ? " (you)" : "";
      lines.push(`- **${a.name}**${marker}`);
    }
    return lines.join("\n");
  }

  private _systemStatus(): string {
    const actors = this._registry.allActors();
    const total = actors.length;
    return (
      `**System Summary**\n\nTotal agents: ${total}\n\n*${VERSION}*`
    );
  }
}

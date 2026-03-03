/**
 * mock-agents.mjs
 *
 * Simulates a running AgentFlow system by publishing MQTT messages that
 * the Babylon.js frontend subscribes to.  Run via compose.dev.yaml.
 *
 * Topics published:
 *   agents/{id}/heartbeat   — periodic liveness ping per agent
 *   agents/{id}/status      — state changes
 *   agents/{id}/chat        — simulated LLM replies (+ replies to io/chat)
 *   agents/{id}/alert       — occasional warning/error
 *   agents/{id}/spawn       — new agent announcement
 *   system/health           — aggregate counts
 *
 * Topics subscribed:
 *   io/chat                 — user messages from the frontend IO bar
 */

import mqtt from "mqtt";
import { WidGen, HLCWidGen } from "./vendor/wid/index.mjs";

const BROKER = `mqtt://${process.env.MQTT_HOST ?? "localhost"}:${process.env.MQTT_PORT ?? 1883}`;

// ── ID helpers ─────────────────────────────────────────────────────────────────
const _widGen = new WidGen({ W: 4, Z: 6 });
const nextWid = () => _widGen.next();

function nextHlcWid(name) {
  const node = name.replace(/[^A-Za-z0-9_]/g, "_").slice(0, 20);
  return new HLCWidGen({ node, W: 4, Z: 0 }).next();
}

// ── Agent roster ──────────────────────────────────────────────────────────────
const AGENT_DEFS = [
  { name: "main-actor",      role: "orchestrator", color: "amber"   },
  { name: "monitor-agent",   role: "monitor",      color: "teal"    },
  { name: "io-agent",        role: "gateway",      color: "cyan"    },
  { name: "qa-agent",        role: "guardian",     color: "green"   },
  { name: "nautilus-agent",  role: "transfer",     color: "indigo"  },
  { name: "udx-agent",       role: "expert",       color: "gold"    },
  { name: "weather-agent",   role: "data",         color: "sky"     },
  { name: "news-agent",      role: "data",         color: "red"     },
  { name: "wif-agent",       role: "financier",    color: "emerald" },
  { name: "wiz-agent",       role: "coin",         color: "gold"    },
];

const agents = AGENT_DEFS.map((def) => ({
  ...def,
  id:       nextHlcWid(def.name),
  state:    "running",
  seq:      0,
  msgCount: 0,
}));

// ── MQTT client ───────────────────────────────────────────────────────────────
const client = mqtt.connect(BROKER, { clientId: "mock-agents", clean: true });

client.on("connect", () => {
  console.log(`[mock] connected to ${BROKER}`);

  // Announce all agents on startup
  for (const agent of agents) {
    publish(`agents/${agent.id}/spawn`, {
      agentId:     agent.id,
      agentName:   agent.name,
      agentType:   agent.role,
      color:       agent.color,
      state:       "running",
      timestampMs: Date.now(),
    });
    publish(`agents/${agent.id}/status`, {
      agentId:   agent.id,
      agentName: agent.name,
      state:     "running",
    });
    console.log(`[mock] spawned ${agent.name} (${agent.id})`);
  }

  // Subscribe to user input so dev mode is interactive
  client.subscribe("io/chat", { qos: 0 });

  startHeartbeats();
  startChat();
  startAlerts();
  startSystemHealth();
  startDynamicSpawns();
  startCoinEvents();
});

client.on("error", (err) => console.error("[mock] MQTT error:", err.message));

// ── Respond to user messages on io/chat ───────────────────────────────────────
const MOCK_RESPONSES = [
  "Got it — processing your request now.",
  "Understood. Running analysis on that.",
  "I'll coordinate with the other agents on this.",
  "Interesting query. Let me check the available data.",
  "That's within my capabilities. Executing now…",
  "Classification in progress — confidence is high.",
  "Fetching relevant context for your request.",
  "Task queued. Expected completion: ~2 seconds.",
  "Agent network is nominal. Request dispatched.",
  "Acknowledged. Routing to the appropriate subsystem.",
];

// UDX knowledge-base replies for the mock
const UDX_RESPONSES = [
  "**UDX** here! Type `help` for a full command list, or try `docs architecture`, `explain mqtt`, `agents`, or `status`.",
  "**AgentFlow actors** communicate via MQTT topics only — no shared state, no locks. Try `explain actor-model` for details.",
  "**Live agents** in this session: main-actor (orchestrator), monitor-agent (watchdog), io-agent (gateway), nautilus-agent (SSH/rsync), udx-agent (that's me!). Use `agents` for the full list.",
  "**Deployment tip**: run `bash scripts/package-native.sh` to produce a self-contained `agentflow-native-*.tar.gz` (~12 MB) that runs without Docker. Use `docs deploy` for the full guide.",
  "**NautilusAgent** bridges SSH/rsync — try `@nautilus-agent ping user@host`. Arguments are never shell-interpolated, so injection attacks are impossible.",
  "**MQTT topic structure**: `agents/{id}/spawn|heartbeat|status|alert|chat` + `system/health` + `io/chat`. Use `docs mqtt` for the full reference.",
  "**REST API** lives at `/api/`. Quick ref: `GET /api/actors`, `POST /api/actors/:id/pause`, `DELETE /api/actors/:id`. Use `docs api` for all endpoints.",
];

// Weather-agent mock replies
const WEATHER_RESPONSES = [
  "**Weather in London**\n\n🌡 **14°C / 57°F** (feels like 11°C)\n☁ Overcast\n💧 Humidity: 82%\n💨 Wind: 22 km/h SW\n👁 Visibility: 9 km\n☀ UV index: 1",
  "**Weather in Tokyo**\n\n🌡 **21°C / 70°F** (feels like 20°C)\n🌤 Partly cloudy\n💧 Humidity: 65%\n💨 Wind: 14 km/h NE\n👁 Visibility: 16 km\n☀ UV index: 4",
  "**Weather in New York**\n\n🌡 **8°C / 46°F** (feels like 5°C)\n🌧 Light rain\n💧 Humidity: 90%\n💨 Wind: 30 km/h NW\n👁 Visibility: 6 km\n☀ UV index: 0",
  "🌦 Fetching weather… *(in real mode this calls wttr.in — no API key needed)*",
];

// News-agent mock replies
const NEWS_RESPONSES = [
  "**Hacker News — Top Stories** (top 5)\n\n1. **[Show HN: I built a multi-agent system in Rust](https://example.com)** — ⬆ 342 · [HN](https://news.ycombinator.com)\n2. **[The unreasonable effectiveness of LLMs as orchestrators](https://example.com)** — ⬆ 289 · [HN](https://news.ycombinator.com)\n3. **[Ask HN: How do you handle secret management in containers?](https://news.ycombinator.com)** — ⬆ 201 · [HN](https://news.ycombinator.com)\n4. **[Rust 2026 roadmap announced](https://example.com)** — ⬆ 178 · [HN](https://news.ycombinator.com)\n5. **[Babylon.js 8.0 released](https://example.com)** — ⬆ 154 · [HN](https://news.ycombinator.com)",
  "**Hacker News — Newest Stories** (top 5)\n\n1. **[Actor model vs. CSP: a 2026 comparison](https://example.com)** — ⬆ 12\n2. **[MQTT vs WebSockets for real-time dashboards](https://example.com)** — ⬆ 8\n3. **[Building a zero-dependency Rust HTTP client](https://example.com)** — ⬆ 5\n4. **[Ask HN: Best free weather API?](https://news.ycombinator.com)** — ⬆ 3\n5. **[Show HN: AgentFlow dashboard in Babylon.js](https://example.com)** — ⬆ 2",
  "📰 Fetching top stories from Hacker News… *(in real mode this calls the HN Firebase API — no API key needed)*",
];

// WIZ coin-economy mock replies
const WIZ_RESPONSES = [
  "**WIZ — WaldiezCoin Economy** Ƿ\n\n```\nbalance              current coin balance\nhistory [n]          last n transactions (default 10)\nearn <n> <reason>    credit coins manually\ndebit <n> <reason>   debit coins manually\nhelp                 this message\n```",
  "💰 **Balance**: Ƿ 1,250\n📈 Net today: **+Ƿ 142** (12 events)",
  "📋 **Recent Transactions**\n\n  +10  spawn: data-fetcher\n  +2   heartbeat: main-actor\n  +5   system healthy\n  +2   heartbeat: io-agent\n  −3   stale alert: ml-classifier\n  +2   heartbeat: main-actor\n\n**Balance: Ƿ 1,254**",
  "✅ Credited **Ƿ 50** → _manual bonus_\n**New balance: Ƿ 1,304**",
  "📉 Debited **Ƿ 25** → _QA flag penalty_\n**New balance: Ƿ 1,279**",
  "💡 **Economy Rules**\n\n  +10  agent spawned\n  +2   agent heartbeat\n  +5   all agents healthy\n  −5   QA content flag\n  −3   stale agent alert\n\nEarn coins by keeping your swarm healthy!",
];

// WIF finance-agent mock replies
const WIF_RESPONSES = [
  "**WIF — Finance Expert** 💹\n\n```\nadd <amount> [category] [note]       log an expense\nbudget <category> <amount>           set budget limit\nsummary [today|week|month|all]       spending report\nbalance                              budget vs actuals\ncalc compound <p> <rate%> <years>    compound interest\ncalc loan <p> <rate%> <years>        loan / mortgage\ncalc roi <initial> <final>            return on invest\ntips [saving|investing|debt|budget]  financial advice\nhelp                                 this message\n```",
  "✅ Logged **$42.50** → `food` _lunch sushi_\n🟢 **food** budget: $127.50 / $300.00 (43%) — $172.50 left",
  "**💰 Expense Summary — This Month**\n\n  █ **food**: $127.50\n  ▆ **transport**: $84.00\n  ▄ **entertainment**: $55.00 🟡 92% of $60\n  ▂ **misc**: $22.00\n\n**Total: $288.50** (14 transactions)",
  "**📊 Budget Balance**\n\n🟢 **food**: [████████░░] $127.50 / $300.00 (43%) — $172.50 left\n🟢 **transport**: [██████░░░░] $84.00 / $140.00 (60%) — $56.00 left\n🔴 **entertainment**: [██████████] $55.00 / $60.00 (92%) — $5.00 left\n\n🟢 **TOTAL**: $266.50 / $500.00 (53%)",
  "**📈 Compound Interest (monthly)**\n\nPrincipal : $10,000.00\nRate      : 7.00% p.a.\nTerm      : 20 years\n\n→ Future Value  : **$40,387.63**\n→ Interest Earned: **$30,387.63** (304% gain)",
  "**🏠 Loan / Mortgage Calculator**\n\nPrincipal : $300,000.00\nRate      : 4.50% p.a.\nTerm      : 30 years\n\n→ Monthly Payment : **$1,520.06**\n→ Total Repaid    : **$547,220.13**\n→ Total Interest  : **$247,220.13**",
  "**💡 Saving Tips**\n\n1. **50/30/20 rule** — 50% needs · 30% wants · 20% savings\n2. **Pay yourself first** — automate a transfer on payday\n3. **Emergency fund** — target 3–6 months of expenses\n4. **Cut subscriptions** — review monthly recurring charges\n5. **Track everything** — use `add <amount> <category>` to log expenses",
  "📋 Budget set: **entertainment** → **$60.00**\n🟢 Currently at $0.00 (0%)",
  "📰 _No expenses recorded yet. Try `add 25 food coffee` to get started._",
  "📈 **Return on Investment**\n\nInitial : $5,000.00\nFinal   : $7,250.00\nGain    : $+2,250.00\n\n→ ROI: **+45.00%**",
];

// Nautilus-specific replies for the mock (sync/exec flavour)
const NAUTILUS_RESPONSES = [
  "✓ SSH connection to `remote-host` established.\n```\nLinux remote-host 6.1.0 #1 SMP x86_64 GNU/Linux\n```",
  "rsync pull complete: `src/` → `./data/` (42 files, 1.3 MB transferred)",
  "✓ `df -h` on `deploy-host`:\n```\nFilesystem  Size  Used Avail Use%\n/dev/sda1    50G   12G   36G  25%\n```",
  "✗ SSH to `unreachable-host` timed out after 10s.",
  "✓ rsync push `./dist/` → `web@cdn:/var/www/html/` — 18 files synced.",
  "Remote command `systemctl status agentflow` returned exit 0.",
  "Establishing encrypted tunnel… shell handshake complete.",
  "✓ Key fingerprint accepted. Host added to known_hosts.",
];

client.on("message", (topic, raw) => {
  if (topic !== "io/chat") return;
  let msg;
  try { msg = JSON.parse(raw.toString()); } catch { return; }

  const text = (msg.content ?? "").trim();
  if (!text) return;

  // Parse @mention to choose responding agent; default to main-actor
  let responder = agents.find((a) => a.name === "main-actor") ?? agents[0];
  const mentionMatch = text.match(/^@([\w-]+)/);
  if (mentionMatch) {
    const named = agents.find((a) => a.name === mentionMatch[1]);
    if (named) responder = named;
  }

  console.log(`[mock] io/chat → @${responder.name}: "${text.slice(0, 60)}"`);

  // Simulate a short "thinking" delay (0.8–2.5 s)
  const delay = 800 + Math.random() * 1700;
  const pool =
    responder.name === "nautilus-agent" ? NAUTILUS_RESPONSES :
    responder.name === "udx-agent"      ? UDX_RESPONSES      :
    responder.name === "weather-agent"  ? WEATHER_RESPONSES  :
    responder.name === "news-agent"     ? NEWS_RESPONSES     :
    responder.name === "wif-agent"      ? WIF_RESPONSES      :
    responder.name === "wiz-agent"      ? WIZ_RESPONSES      :
    MOCK_RESPONSES;
  setTimeout(() => {
    publish(`agents/${responder.id}/chat`, {
      id:          nextWid(),
      from:        responder.name,
      to:          "user",
      content:     pick(pool),
      timestampMs: Date.now(),
    });
  }, delay);
});

function publish(topic, payload) {
  client.publish(topic, JSON.stringify(payload), { qos: 0, retain: false });
}

// ── Heartbeats — every 5 s per agent ─────────────────────────────────────────
function startHeartbeats() {
  setInterval(() => {
    for (const agent of agents) {
      agent.seq++;
      publish(`agents/${agent.id}/heartbeat`, {
        agentId:     agent.id,
        agentName:   agent.name,
        state:       agent.state,
        sequence:    agent.seq,
        timestampMs: Date.now(),
      });
    }
  }, 5_000);
}

// ── Simulated background chat messages ───────────────────────────────────────
const PHRASES = [
  "Analysing sensor data…",
  "Classification complete: confidence 0.94",
  "Forwarding result to main-actor",
  "Fetching weather for Berlin",
  "Spawning sub-task agent",
  "LLM response: 'Task completed successfully'",
  "Detected anomaly in stream, alerting monitor",
  "Memory persisted to state store",
  "Heartbeat acknowledged",
  "Running inference on input batch",
];

function startChat() {
  setInterval(() => {
    const from = pick(agents);
    const to   = pick(agents.filter((a) => a.id !== from.id));
    from.msgCount++;
    publish(`agents/${from.id}/chat`, {
      id:          nextWid(),
      from:        from.name,
      to:          to.name,
      content:     pick(PHRASES),
      timestampMs: Date.now(),
    });
  }, 4_000);
}

// ── Occasional alerts ─────────────────────────────────────────────────────────
const SEVERITIES = ["info", "warning", "error"];
const ALERT_MSGS = [
  "High memory usage detected",
  "LLM response latency > 3s",
  "Retrying failed MQTT publish",
  "Script execution timeout",
  "Connection to ML service lost",
];

function startAlerts() {
  setInterval(() => {
    if (Math.random() > 0.3) return; // ~30% chance per tick
    const agent = pick(agents);
    publish(`agents/${agent.id}/alert`, {
      id:          nextWid(),
      agentId:     agent.id,
      agentName:   agent.name,
      severity:    pick(SEVERITIES),
      message:     pick(ALERT_MSGS),
      timestampMs: Date.now(),
    });
  }, 8_000);
}

// ── system/health ─────────────────────────────────────────────────────────────
function startSystemHealth() {
  setInterval(() => {
    publish("system/health", {
      active_agents: agents.length,
      stale_count:   0,
      timestampMs:   Date.now(),
    });
  }, 15_000);
  publish("system/health", { active_agents: agents.length, stale_count: 0, timestampMs: Date.now() });
}

// ── Occasionally spawn a new dynamic agent ────────────────────────────────────
const DYNAMIC_NAMES = [
  "sentiment-scanner", "price-tracker", "news-feed",
  "code-reviewer",     "sql-analyst",   "report-builder",
];
let dynamicIdx = 0;

function startDynamicSpawns() {
  setInterval(() => {
    if (Math.random() > 0.2) return; // ~20% chance
    const name  = DYNAMIC_NAMES[dynamicIdx++ % DYNAMIC_NAMES.length];
    const agent = {
      name,
      id:       nextHlcWid(name),
      role:     "dynamic",
      color:    "cyan",
      state:    "running",
      seq:      0,
      msgCount: 0,
    };
    agents.push(agent);
    publish(`agents/${agent.id}/spawn`, {
      agentId:     agent.id,
      agentName:   agent.name,
      agentType:   agent.role,
      color:       agent.color,
      state:       "running",
      timestampMs: Date.now(),
    });
    publish(`agents/${agent.id}/status`, {
      agentId:   agent.id,
      agentName: agent.name,
      state:     "running",
    });
    console.log(`[mock] dynamic spawn: ${name} (${agent.id})`);

    // De-spawn after 30–60 s
    const ttl = 30_000 + Math.random() * 30_000;
    setTimeout(() => {
      const idx = agents.findIndex((a) => a.id === agent.id);
      if (idx !== -1) agents.splice(idx, 1);
      publish(`agents/${agent.id}/status`, {
        agentId:   agent.id,
        agentName: agent.name,
        state:     "stopped",
      });
      console.log(`[mock] stopped: ${name}`);
    }, ttl);
  }, 20_000);
}

// ── WaldiezCoin events — published on system/coin ─────────────────────────────
let _mockBalance = 0;

const COIN_EARN_REASONS = [
  { delta: 2, reason: "heartbeat" },
  { delta: 10, reason: "agent spawned" },
  { delta: 5, reason: "all agents healthy" },
];
const COIN_DEBIT_REASONS = [
  { delta: -3, reason: "stale agent alert" },
  { delta: -5, reason: "QA content flag" },
];

function startCoinEvents() {
  // Emit a coin event every heartbeat cycle (5s) — earn or occasionally debit
  setInterval(() => {
    const isDebit = Math.random() < 0.12; // 12% chance of debit
    const entry = isDebit ? pick(COIN_DEBIT_REASONS) : pick(COIN_EARN_REASONS);
    _mockBalance += entry.delta;
    publish("system/coin", {
      balance:     _mockBalance,
      delta:       entry.delta,
      reason:      entry.reason,
      timestampMs: Date.now(),
    });
  }, 5_000);
  // Publish initial balance
  publish("system/coin", { balance: _mockBalance, delta: 0, reason: "connected", timestampMs: Date.now() });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

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
  { name: "main-actor",      role: "orchestrator", color: "amber" },
  { name: "monitor-agent",   role: "monitor",      color: "teal"  },
  { name: "data-fetcher",    role: "dynamic",      color: "cyan"  },
  { name: "weather-agent",   role: "dynamic",      color: "blue"  },
  { name: "ml-classifier",   role: "ml",           color: "violet"},
  { name: "nautilus-agent",  role: "transfer",     color: "indigo"},
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
  const pool = responder.name === "nautilus-agent" ? NAUTILUS_RESPONSES : MOCK_RESPONSES;
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

// ── Helpers ───────────────────────────────────────────────────────────────────
function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

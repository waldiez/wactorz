/**
 * mock-agents.mjs
 *
 * Simulates a running Wactorz system by publishing the MQTT traffic the
 * dashboard renders. Run via compose.dev.yaml.
 *
 * Topics published:
 *   agents/{id}/heartbeat   — periodic liveness ping per agent
 *   agents/{id}/status      — state changes
 *   agents/{id}/chat        — simulated LLM replies
 *   agents/{id}/alert       — occasional warning/error
 *   agents/{id}/spawn       — new agent announcement
 *   system/health           — aggregate counts
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

  startHeartbeats();
  startChat();
  startAlerts();
  startSystemHealth();
  startDynamicSpawns();
  startCoinEvents();
});

client.on("error", (err) => console.error("[mock] MQTT error:", err.message));

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

/**
 * AgentFlow Dashboard — entry point.
 *
 * Bootstrap order:
 * 1. Create SceneManager (Babylon.js engine + scene + camera)
 * 2. Create MQTTClient and connect to broker
 * 3. Create UI components (HUD, ThemeSwitcher, ChatPanel, IOBar, ActivityFeed)
 * 4. Create MentionPopup (needs SceneManager for agent list)
 * 5. Wire MQTT events → SceneManager + HUD + ActivityFeed
 * 6. Wire DOM events (theme-change, agent-selected) → SceneManager + ChatPanel
 */

import { SceneManager } from "./scene/SceneManager";
import { MQTTClient } from "./mqtt/MQTTClient";
import { AgentHUD } from "./ui/AgentHUD";
import { ThemeSwitcher } from "./ui/ThemeSwitcher";
import { ChatPanel } from "./ui/ChatPanel";
import { IOBar } from "./ui/IOBar";
import { ActivityFeed } from "./ui/ActivityFeed";
import { MentionPopup } from "./ui/MentionPopup";
import { VoiceInput } from "./io/VoiceInput";
import { IOManager } from "./io/IOManager";

import type { AgentInfo, ThemeChangeEvent } from "./types/agent";

// ── Scene ─────────────────────────────────────────────────────────────────────

const canvas = document.getElementById("renderCanvas") as HTMLCanvasElement;
const scene = new SceneManager(canvas);

// ── MQTT ──────────────────────────────────────────────────────────────────────

const _wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const MQTT_BROKER =
  (import.meta.env["VITE_MQTT_WS_URL"] as string | undefined) ??
  `${_wsProto}//${window.location.host}/mqtt`;
const mqtt = new MQTTClient(MQTT_BROKER);

// ── UI ────────────────────────────────────────────────────────────────────────

const hud = new AgentHUD();
const themeSwitcher = new ThemeSwitcher();
const chatPanel = new ChatPanel();
const voice = new VoiceInput();
const ioManager = new IOManager(mqtt, chatPanel);
const ioBar = new IOBar(voice, ioManager);
const feed = new ActivityFeed();

// MentionPopup needs the textarea and the agent list from SceneManager
const textInput = document.getElementById("text-input") as HTMLTextAreaElement;
new MentionPopup(textInput, () => scene.getAgents());

// ── Resize canvas when chat panel opens/closes ────────────────────────────────
// The panel is 340px wide; shrink the canvas so 3D nodes stay visible.
const PANEL_WIDTH = 340;
const chatPanelEl = document.getElementById("chat-panel")!;

new MutationObserver(() => {
  const open = chatPanelEl.classList.contains("open");
  canvas.style.width = open ? `calc(100% - ${PANEL_WIDTH}px)` : "100%";
  // Wait for the CSS slide transition (0.3 s) then tell Babylon to re-read size
  setTimeout(() => scene.engine.resize(), 320);
}).observe(chatPanelEl, { attributes: true, attributeFilter: ["class"] });

// Tooltip: follow the mouse over the canvas
canvas.addEventListener("mousemove", (e) => {
  const tooltip = document.getElementById("node-tooltip");
  if (tooltip && tooltip.style.display === "block") {
    tooltip.style.left = `${e.clientX + 14}px`;
    tooltip.style.top = `${e.clientY - 10}px`;
  }
});

// ── MQTT → Scene/HUD/Feed wiring ──────────────────────────────────────────────

mqtt.on("heartbeat", (payload) => {
  scene.onHeartbeat(payload);
  feed.push({
    type: "heartbeat",
    label: "heartbeat",
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("spawn", (payload) => {
  scene.onSpawn(payload);
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
  feed.push({
    type: "spawn",
    label: `spawned (${payload.agentType ?? "agent"})`,
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("alert", (payload) => {
  alertCount++;
  scene.onAlert(payload);
  hud.flashAlert(payload.severity);
  refreshStats();
  feed.push({
    type: payload.severity === "error" ? "alert-error" : "alert-warning",
    label: payload.message,
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("chat", (msg) => {
  ioManager.receiveAgentMessage(msg);
  scene.onChat(msg.from, msg.to);
  feed.push({
    type: "chat",
    label: `→ ${msg.to}: ${msg.content.slice(0, 40)}${msg.content.length > 40 ? "…" : ""}`,
    agentName: msg.from,
    timestamp: msg.timestampMs,
  });
});

mqtt.on("status", (payload) => {
  if (payload.state === "stopped") {
    scene.removeAgent(payload.agentId);
    feed.push({
      type: "stopped",
      label: "stopped",
      agentName: payload.agentName,
      timestamp: Date.now(),
    });
  } else {
    scene.addOrUpdateAgent({
      id: payload.agentId,
      name: payload.agentName,
      state: payload.state,
      protected: false,
    });
  }
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
  chatPanel.updateAgentStatus(payload.agentId, String(payload.state));
});

// ── Stats helpers ─────────────────────────────────────────────────────────────

let alertCount = 0;

function refreshStats(): void {
  hud.setStats(scene.getAgents(), alertCount);
}

// Seed only once — MQTT reconnects must not re-add already-known agents.
let seeded = false;

mqtt.on("connected", () => {
  console.info("[Dashboard] MQTT connected");
  hud.setSystemHealth(true);

  if (seeded) return;
  seeded = true;

  // Startup spawn events are published before the browser connects.
  // Fetch the current actor list from REST so they appear immediately.
  fetch("/api/actors")
    .then((r) => r.json())
    .then((actors: AgentInfo[]) => {
      actors.forEach((a) => scene.addOrUpdateAgent(a));
      hud.setAgentCount(scene.getAgents().length);
      refreshStats();
      console.info(`[Dashboard] seeded ${actors.length} actors from REST`);
    })
    .catch(() => {
      // Dev mode without a running server — ignore silently
    });
});

mqtt.on("qa-flag", (payload) => {
  feed.push({
    type: "qa-flag",
    label: `[${payload.category}] ${payload.excerpt}`,
    agentName: `qa-agent ← ${payload.from}`,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("metrics", (payload) => {
  // Merge cost/message metrics into the agent record so dashboards can display them.
  const existing = scene.getAgents().find((a) => a.id === payload.agentId);
  const update: AgentInfo = {
    id: payload.agentId,
    name: payload.agentName,
    state: existing?.state ?? "running",
    protected: existing?.protected ?? false,
  };
  if (payload.messagesProcessed !== undefined) update.messagesProcessed = payload.messagesProcessed;
  if (payload.costUsd !== undefined)           update.costUsd = payload.costUsd;
  if (payload.uptime !== undefined)            update.uptime = payload.uptime;
  scene.addOrUpdateAgent(update);
  refreshStats();
});

mqtt.on("logs", (payload) => {
  const msg = payload.message ?? payload.text ?? "";
  if (!msg) return;
  feed.push({
    type: "chat",
    label: msg.slice(0, 80),
    agentName: payload.agentName,
    timestamp: Date.now(),
  });
});

mqtt.on("completed", (payload) => {
  feed.push({
    type: "spawn",
    label: "task completed",
    agentName: payload.agentName,
    timestamp: Date.now(),
  });
});

mqtt.on("node-heartbeat", (payload) => {
  feed.push({
    type: "health",
    label: `node online · ${payload.agents.length} agent${payload.agents.length !== 1 ? "s" : ""}`,
    agentName: payload.node,
    timestamp: Date.now(),
  });
});

mqtt.on("system-health", () => {
  hud.setSystemHealth(true);
});

mqtt.on("coin", (payload) => {
  feed.push({
    type: "qa-flag",
    label: `balance ${payload.balance}${payload.reason ? " · " + payload.reason : ""}`,
    agentName: "wiz-agent",
    timestamp: Date.now(),
  });
});

mqtt.on("disconnected", () => {
  console.warn("[Dashboard] MQTT disconnected");
  hud.setSystemHealth(false);
});

mqtt.on("error", (err) => {
  console.error("[Dashboard] MQTT error:", err);
  hud.setSystemHealth(false);
});

// ── DOM event → Scene wiring ──────────────────────────────────────────────────

document.addEventListener("theme-change", (e) => {
  const evt = e as CustomEvent<ThemeChangeEvent>;
  scene.setTheme(evt.detail.theme);
  // Sync switcher state if theme was changed externally (e.g. CardDashboard ⬡ 3D button)
  themeSwitcher.syncState(evt.detail.theme);
});

// Camera fly-to when agent is selected (panel open)
document.addEventListener("agent-selected", (e) => {
  const evt = e as CustomEvent<{ agent: { id: string } }>;
  scene.onAgentSelected(evt.detail.agent.id);
});

// ── Set dynamic links ─────────────────────────────────────────────────────────

const haLink = document.getElementById("ha-link") as HTMLAnchorElement | null;
if (haLink) {
  haLink.href = `${window.location.protocol}//${window.location.hostname}:8123`;
}

// ── Connect ───────────────────────────────────────────────────────────────────

mqtt.connect();

// ── Cleanup on page unload ────────────────────────────────────────────────────

window.addEventListener("beforeunload", () => {
  mqtt.disconnect();
  scene.dispose();
});

/**
 * Wactorz Dashboard — entry point.
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
import { WSChatClient } from "./io/WSChatClient";
import { tts } from "./io/TTSManager";

import type { AgentInfo, AgentState, ThemeChangeEvent } from "./types/agent";

function nameFromWid(raw: string): string {
  const m = raw.match(/Z-(.+?)(?:-[0-9a-f]{6})?$/i);
  return m?.[1] ?? raw;
}

/**
 * Resolve a human-readable display name from whatever the backend sends.
 * - If name is empty or looks like a raw timestamp (all digits), extract from WID id.
 * - If name is itself a WID string (contains "Z-"), extract the embedded name.
 * - Otherwise trust the name as-is.
 */
function resolveAgentName(name: string | undefined, id: string): string {
  const n = (name ?? "").trim();
  const isTimestampOnly = !n || /^\d+$/.test(n);
  return isTimestampOnly ? nameFromWid(id) : nameFromWid(n);
}

// ── Scene ─────────────────────────────────────────────────────────────────────

const canvas = document.getElementById("renderCanvas") as HTMLCanvasElement;
canvas.style.display = "none";
const scene = new SceneManager(canvas);

// Always start with cards. Reset localStorage so ThemeSwitcher doesn't
// override this with a stale value ("graph", "social", etc.) via its setTimeout.
localStorage.setItem("wactorz-theme", "cards");
scene.setTheme("cards");

// ── MQTT ──────────────────────────────────────────────────────────────────────

const _wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const _mqttDefault = `${_wsProto}//${window.location.host}/mqtt`;
const MQTT_BROKER =
  localStorage.getItem("wactorz-mqtt-url") ||
  (import.meta.env["VITE_MQTT_WS_URL"] as string | undefined) ||
  _mqttDefault;
const mqtt = new MQTTClient(MQTT_BROKER);

// ── UI ────────────────────────────────────────────────────────────────────────

const hud = new AgentHUD();
const themeSwitcher = new ThemeSwitcher();
const chatPanel = new ChatPanel();
const voice = new VoiceInput();
const ioManager = new IOManager(mqtt, chatPanel);
const ioBar = new IOBar(voice, ioManager);

const feed = new ActivityFeed();

// ── Direct WebSocket chat (bypasses MQTT/IOAgent when server has registry) ────

const wsChat = new WSChatClient();

// Non-streaming replies (slash commands, errors, one-shot agent replies)
wsChat.onChat((content, from, timestampMs) => {
  const msg = {
    id: `ws-${timestampMs}`,
    from,
    to: "user",
    content,
    timestampMs,
  };
  ioManager.receiveAgentMessage(msg);
  scene.onChat(from, "user");
  const feedItem = {
    type: "chat" as const,
    label: content.slice(0, 60),
    agentName: from,
    timestamp: timestampMs,
  };
  feed.push(feedItem);
  document.dispatchEvent(
    new CustomEvent("af-feed-push", { detail: { item: feedItem } }),
  );
  document.dispatchEvent(
    new CustomEvent("af-chat-message", { detail: { msg } }),
  );
});

// Streaming replies — onStreamChunk / onStreamEnd are wired inside setWSClient
ioManager.setWSClient(wsChat);

// State patches broadcast by the server over the same /ws connection.
// This is how pause/stop/resume state changes reach the UI without polling.
wsChat.onStatePatch((agents, deletedId) => {
  if (deletedId) {
    scene.removeAgent(deletedId);
  }
  agents.forEach((a) => {
    if (!a.agent_id) return;
    const rawState = (a.state ?? a.status ?? "running") as string;
    const state: AgentState =
      rawState === "paused"
        ? "paused"
        : rawState === "stopped"
          ? "stopped"
          : rawState === "initializing"
            ? "initializing"
            : "running";
    const update: AgentInfo = {
      id: a.agent_id,
      name: resolveAgentName(a.name, a.agent_id),
      state,
      protected: a.protected ?? false,
    };
    if (a.messages_processed != null)
      update.messagesProcessed = a.messages_processed;
    if (a.cost_usd != null) update.costUsd = a.cost_usd;
    if (a.uptime != null) update.uptime = a.uptime;
    if (a.cpu != null) update.cpu = a.cpu;
    if (a.mem != null) update.mem = a.mem;
    if (a.task != null) update.task = a.task;
    if (a.agent_type != null) update.agentType = a.agent_type;
    scene.addOrUpdateAgent(update);
  });
  chatPanel.updateAgentList(scene.getAgents());
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
});

wsChat.connect(`${_wsProto}//${window.location.host}/ws`);

// ── Seed localStorage from backend config (only for unset keys) ───────────────
// Backend config (.env) provides defaults; a user-set localStorage value wins.
fetch("/api/config")
  .then((r) => r.ok ? r.json() : null)
  .then((cfg) => {
    if (!cfg) return;
    const setIfMissing = (key: string, value: string) => {
      if (value && !localStorage.getItem(key))
        localStorage.setItem(key, value);
    };
    setIfMissing("wactorz-ha-url",       cfg.ha?.url ?? "");
    setIfMissing("wactorz-ha-token",     cfg.ha?.token ?? "");
    setIfMissing("wactorz-fuseki-url",   cfg.fuseki?.url ?? "");
    setIfMissing("wactorz-fuseki-dataset", cfg.fuseki?.dataset ?? "");
    if (cfg.mqtt?.url) setIfMissing("wactorz-mqtt-url", cfg.mqtt.url);
  })
  .catch(() => {});

// MentionPopup needs the textarea and the agent list from SceneManager
const textInput = document.getElementById("text-input") as HTMLTextAreaElement;
new MentionPopup(textInput, () => scene.getAgents());

// ── Helpers ───────────────────────────────────────────────────────────────────

function pushFeed(item: Parameters<typeof feed.push>[0]): void {
  feed.push(item);
  document.dispatchEvent(new CustomEvent("af-feed-push", { detail: { item } }));
}

// ── MQTT → Scene/HUD/Feed wiring ──────────────────────────────────────────────

mqtt.on("heartbeat", (payload) => {
  scene.onHeartbeat(payload);
  pushFeed({
    type: "heartbeat",
    label: "heartbeat",
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("spawn", (payload) => {
  scene.onSpawn(payload);
  chatPanel.updateAgentList(scene.getAgents());
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
  pushFeed({
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
  pushFeed({
    type: payload.severity === "error" ? "alert-error" : "alert-warning",
    label: payload.message,
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("chat", (msg) => {
  ioManager.receiveAgentMessage(msg);
  scene.onChat(msg.from, msg.to);
  document.dispatchEvent(
    new CustomEvent("af-chat-message", { detail: { msg } }),
  );
  pushFeed({
    type: "chat",
    label: `→ ${msg.to}: ${msg.content.slice(0, 40)}${msg.content.length > 40 ? "…" : ""}`,
    agentName: msg.from,
    timestamp: msg.timestampMs,
  });
});

mqtt.on("status", (payload) => {
  // Keep stopped agents visible — they show with only a Delete button.
  // Removal happens only when the user explicitly deletes (delete_agent WS message).
  scene.addOrUpdateAgent({
    id: payload.agentId,
    name: payload.agentName,
    state: payload.state,
    protected: payload.protected ?? false,
    messagesProcessed: payload.messagesProcessed,
  });
  chatPanel.updateAgentList(scene.getAgents());
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
  chatPanel.updateAgentStatus(payload.agentId, String(payload.state));
  if (payload.state === "stopped") {
    pushFeed({
      type: "stopped",
      label: "stopped",
      agentName: payload.agentName,
      timestamp: Date.now(),
    });
  }
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
  document.dispatchEvent(
    new CustomEvent("af-connection-status", { detail: { status: "live" } }),
  );

  if (seeded) return;
  seeded = true;

  // Startup spawn events are published before the browser connects.
  // Fetch the current actor list from REST so they appear immediately.
  fetch("/api/actors")
    .then((r) => r.json())
    .then((actors: AgentInfo[]) => {
      actors.forEach((a) =>
        scene.addOrUpdateAgent({
          ...a,
          name: resolveAgentName(a.name, a.id),
        }),
      );
      chatPanel.updateAgentList(scene.getAgents());
      hud.setAgentCount(scene.getAgents().length);
      refreshStats();
      console.info(`[Dashboard] seeded ${actors.length} actors from REST`);
    })
    .catch(() => {
      // Dev mode without a running server — ignore silently
    });
});

mqtt.on("qa-flag", (payload) => {
  pushFeed({
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
  if (payload.messagesProcessed !== undefined)
    update.messagesProcessed = payload.messagesProcessed;
  if (payload.costUsd !== undefined) update.costUsd = payload.costUsd;
  if (payload.uptime !== undefined) update.uptime = payload.uptime;
  scene.addOrUpdateAgent(update);
  refreshStats();
});

mqtt.on("logs", (payload) => {
  const msg = payload.message ?? payload.text ?? "";
  if (!msg) return;
  pushFeed({
    type: "chat",
    label: msg.slice(0, 80),
    agentName: payload.agentName,
    timestamp: Date.now(),
  });
});

mqtt.on("completed", (payload) => {
  pushFeed({
    type: "spawn",
    label: "task completed",
    agentName: payload.agentName,
    timestamp: Date.now(),
  });
});

mqtt.on("node-heartbeat", (payload) => {
  pushFeed({
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
  pushFeed({
    type: "qa-flag",
    label: `balance ${payload.balance}${payload.reason ? " · " + payload.reason : ""}`,
    agentName: "wiz-agent",
    timestamp: Date.now(),
  });
});

mqtt.on("disconnected", () => {
  console.warn("[Dashboard] MQTT disconnected");
  hud.setSystemHealth(false);
  document.dispatchEvent(
    new CustomEvent("af-connection-status", { detail: { status: "demo" } }),
  );
});

mqtt.on("error", (err) => {
  console.error("[Dashboard] MQTT error:", err);
  hud.setSystemHealth(false);
});

// ── DOM event → Scene wiring ──────────────────────────────────────────────────

document.addEventListener("theme-change", (e) => {
  const evt = e as CustomEvent<ThemeChangeEvent>;
  scene.setTheme(evt.detail.theme);
  // Sync switcher state if theme was changed externally (e.g. CardDashboard ⊞ Social button)
  const t = evt.detail.theme;
  if (t === "cards" || t === "social") themeSwitcher.syncState(t);
});

// Camera fly-to when agent is selected (panel open)
document.addEventListener("agent-selected", (e) => {
  const evt = e as CustomEvent<{ agent: { id: string } }>;
  scene.onAgentSelected(evt.detail.agent.id);
});

// Agent commands from CardDashboard / SocialDashboard → WebSocket
document.addEventListener("af-agent-command", (e) => {
  const { command, agentId } = (
    e as CustomEvent<{ command: string; agentId: string }>
  ).detail;
  wsChat.sendRaw({ type: "command", command, agent_id: agentId });
});

// af-iobar sends: route through ioManager (same as regular io-bar)
document.addEventListener("af-send-message", (e) => {
  const { content } = (e as CustomEvent<{ content: string; target: string }>)
    .detail;
  const agent =
    scene
      .getAgents()
      .find(
        (a) => a.name === (e as CustomEvent<{ target: string }>).detail.target,
      ) ?? null;
  void ioManager.send(content, agent);
});

// ── Set dynamic links ─────────────────────────────────────────────────────────

const haLink = document.getElementById("ha-link") as HTMLAnchorElement | null;
if (haLink) {
  haLink.href = `${window.location.protocol}//${window.location.hostname}:8123`;
}

// ── Sound / TTS toggles ───────────────────────────────────────────────────────

const btnBeep = document.getElementById("btn-beep");
const btnTTS = document.getElementById("btn-tts");

function syncSoundButtons(): void {
  btnBeep?.classList.toggle("active", tts.beepEnabled);
  btnTTS?.classList.toggle("active", tts.ttsEnabled);
}
syncSoundButtons();

btnBeep?.addEventListener("click", () => {
  tts.toggleBeep();
  syncSoundButtons();
});
btnTTS?.addEventListener("click", () => {
  tts.toggleTTS();
  syncSoundButtons();
});

// ── Connect ───────────────────────────────────────────────────────────────────

mqtt.connect();

// ── Cleanup on page unload ────────────────────────────────────────────────────

window.addEventListener("beforeunload", () => {
  mqtt.disconnect();
  wsChat.disconnect();
  scene.dispose();
});

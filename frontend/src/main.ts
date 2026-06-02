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
import type { LogFeedItem } from "./io/WSChatClient";
import { tts } from "./io/TTSManager";
import { SettingsPanel } from "./ui/SettingsPanel";
import { desktopNotifyBackground, desktopNotify, clearUnreadBadge, initNotifications } from "./io/DesktopNotify";
import { toast } from "./ui/ToastManager";

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

// ── Backend base URL ──────────────────────────────────────────────────────────
// Three deployment contexts, handled in priority order:
//
//   1. Tauri desktop  — __WACTORZ_API_PORT is injected as an initialization
//                       script; the embedded Rust server owns that port.
//   2. HA addon       — __WACTORZ_INGRESS_PATH is injected by the Python
//                       server when the page is served behind HA's ingress
//                       proxy (e.g. /api/hassio_ingress/<slug>).
//   3. Direct browser — both are absent; relative URLs resolve correctly.
//
// Never use window.location.host to build absolute URLs: inside the HAOS
// webview that host is the HA instance itself, not the addon backend.

// Request notification permission early so the macOS dialog appears on first launch
initNotifications();

const _apiPort = (window as any).__WACTORZ_API_PORT as number | undefined;
const _ingressPath: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
const _isTauri = _apiPort != null;

// For fetch: absolute when Tauri, ingress-prefixed (or plain-relative) otherwise.
const _apiBase = _isTauri ? `http://localhost:${_apiPort}` : _ingressPath;

const _wsProto = _isTauri
  ? "ws:"
  : window.location.protocol === "https:"
    ? "wss:"
    : "ws:";

// For WebSocket: Tauri uses localhost; browser uses the page host + ingress prefix.
const _wsHost = _isTauri ? `localhost:${_apiPort}` : window.location.host;
const _wsBase = `${_wsProto}//${_wsHost}${_isTauri ? "" : _ingressPath}`;

// ── MQTT ──────────────────────────────────────────────────────────────────────

const _mqttDefault = `${_wsBase}/mqtt`;

// In Tauri, MQTT goes through the embedded backend proxy at /mqtt — always
// override any stale localStorage value (e.g. ws://localhost:1883 saved by
// a previous Python dev session).
if (_isTauri) localStorage.setItem("wactorz-mqtt-url", _mqttDefault);

const MQTT_BROKER =
  localStorage.getItem("wactorz-mqtt-url") ||
  (import.meta.env["VITE_MQTT_WS_URL"] as string | undefined) ||
  _mqttDefault;
const mqtt = new MQTTClient(MQTT_BROKER);

// ── UI ────────────────────────────────────────────────────────────────────────

const hud = new AgentHUD();
const themeSwitcher = new ThemeSwitcher();
const chatPanel = new ChatPanel();
chatPanel.setApiBase(_apiBase);
const voice = new VoiceInput();
const ioManager = new IOManager(mqtt, chatPanel);
const ioBar = new IOBar(voice, ioManager);

document.addEventListener("af-mic-toggle", () => {
  if (voice.isRecording) ioBar.stopMic();
  else void ioBar.startMic();
});

const feed = new ActivityFeed();

// ── Direct WebSocket chat (bypasses MQTT/IOAgent when server has registry) ────

const wsChat = new WSChatClient();
let liveSyncInFlight = false;
// Track agent IDs that were explicitly deleted so MQTT "stopped" events don't re-add them.
const deletedAgentIds = new Set<string>();

function syncAgentViews(): void {
  chatPanel.updateAgentList(scene.getAgents());
  hud.setAgentCount(scene.getAgents().length);
  refreshStats();
}

function refreshLiveActors(): void {
  if (liveSyncInFlight) return;
  liveSyncInFlight = true;
  fetch(`${_apiBase}/api/actors`)
    .then((r) =>
      r.ok ? r.json() : Promise.reject(new Error(String(r.status))),
    )
    .then((actors: AgentInfo[]) => {
      scene.reconcileAgents(
        actors
          .filter((a) => !deletedAgentIds.has(a.id))
          .map((a) => ({
            ...a,
            name: resolveAgentName(a.name, a.id),
          })),
      );
      syncAgentViews();
      console.info(
        `[Dashboard] reconciled ${actors.length} live actors from REST`,
      );
    })
    .catch(() => {
      // Dev mode without a running server — ignore silently.
    })
    .finally(() => {
      liveSyncInFlight = false;
    });
}

// Non-streaming replies (slash commands, errors, one-shot agent replies)
wsChat.onChat((content, from, timestampMs) => {
  toast.show({ type: "chat", title: from, message: content.slice(0, 120) });
  desktopNotifyBackground(from, content.slice(0, 120));
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
    label: content,
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
wsChat.onStatePatch((agents, deletedId, stats) => {
  if (deletedId) {
    deletedAgentIds.add(deletedId);
    scene.removeAgent(deletedId);
  }
  if (stats?.totalCostUsd !== undefined) scene.setTotalCostUsd(stats.totalCostUsd);
  if (stats?.totalMessages !== undefined) scene.setTotalMessages(stats.totalMessages);
  agents.forEach((a) => {
    if (!a.agent_id) return;
    if (deletedAgentIds.has(a.agent_id)) return;
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
  syncAgentViews();
});

wsChat.connect(`${_wsBase}/ws`);
refreshLiveActors();
window.setInterval(() => { refreshLiveActors(); scene.pruneStaleRemoteAgents(); }, 15000);

// ── WS log_feed → Activity Feed ───────────────────────────────────────────────
// The server embeds its in-memory log_feed (spawned/status/logs/alerts) in
// every state-patch.  We use this as a reliable secondary path so MQTT events
// appear in the feed even when the direct Mosquitto WebSocket is unavailable.

let _logFeedMaxTs = 0;
let _logFeedInitialized = false;
let _mqttLive = false;

function _mapLogFeedItem(item: LogFeedItem): Parameters<typeof pushFeed>[0] | null {
  const agentId = item.agent_id ?? "";
  const agentName = item.name ?? item.agentName ?? (nameFromWid(agentId) || agentId.slice(0, 8) || "system");
  const ts = item.timestamp ? item.timestamp * 1000 : Date.now();

  switch (item.type) {
    case "spawned":
      return {
        type: "spawn",
        label: `spawned (${item.agentType ?? item.agent_type ?? "agent"})`,
        agentName: item.agentName ?? item.name ?? agentName,
        timestamp: ts,
      };
    case "completed":
      return { type: "spawn", label: "task completed", agentName, timestamp: ts };
    case "log": {
      const msg = item.message ?? item.text ?? "";
      if (!msg) return null;
      return { type: "chat", label: msg, agentName, timestamp: ts };
    }
    case "status": {
      const st = (item.status as Record<string, unknown> | undefined)?.["state"] as string | undefined;
      if (st === "stopped") return { type: "stopped", label: "stopped", agentName, timestamp: ts };
      return null;
    }
    case "alert": {
      const isError = item.severity === "error" || item.severity === "critical";
      return {
        type: isError ? "alert-error" : "alert-warning",
        label: item.message ?? "",
        agentName: item.name ?? agentName,
        timestamp: ts,
      };
    }
    default:
      return null;
  }
}

wsChat.onLogFeed((items) => {
  if (!_logFeedInitialized) {
    _logFeedInitialized = true;
    _logFeedMaxTs = items.length ? Math.max(...items.map((i) => i.timestamp ?? 0)) : 0;
    // Push historical items (happened before browser connected — MQTT won't re-deliver them).
    [...items].reverse().forEach((item) => {
      const mapped = _mapLogFeedItem(item);
      if (mapped) pushFeed(mapped);
    });
    return;
  }

  // Always advance the high-water mark so that when MQTT reconnects and later
  // disconnects again, we don't replay the entire backlog.
  const newItems = items.filter((item) => (item.timestamp ?? 0) > _logFeedMaxTs);
  if (newItems.length) {
    _logFeedMaxTs = Math.max(...newItems.map((i) => i.timestamp ?? 0));
  }

  // Direct MQTT is live — it delivers these events already; skip to avoid duplicates.
  if (_mqttLive) return;

  // Push new items oldest-first so the feed stays chronological.
  [...newItems].reverse().forEach((item) => {
    const mapped = _mapLogFeedItem(item);
    if (mapped) pushFeed(mapped);
  });
});

// Seed the activity feed from SQLite chat_log so the feed panel isn't empty
// after a server restart. The server returns real Unix timestamps (seconds);
// convert to ms for the feed.
fetch(`${_apiBase}/api/feed`)
  .then((r) => (r.ok ? r.json() : []))
  .then((items: { type: string; label: string; agentName: string; timestamp?: number }[]) => {
    console.log("[feed] /api/feed seed:", items.length, "items");
    if (!items.length) return;
    items.forEach((item) => {
      pushFeed({
        type: "chat",
        label: item.label,
        agentName: item.agentName,
        timestamp: item.timestamp ? item.timestamp * 1000 : Date.now(),
      });
    });
  })
  .catch(() => {});

// ── Seed localStorage from backend config (only for unset keys) ───────────────
// Backend config (.env) provides defaults; a user-set localStorage value wins.
fetch(`${_apiBase}/api/config`)
  .then((r) => (r.ok ? r.json() : null))
  .then((cfg) => {
    if (!cfg) return;
    const setIfMissing = (key: string, value: string) => {
      if (value && !localStorage.getItem(key)) localStorage.setItem(key, value);
    };
    setIfMissing("wactorz-ha-url", cfg.ha?.url ?? "");
    setIfMissing("wactorz-ha-token", cfg.ha?.token ?? "");
    setIfMissing("wactorz-fuseki-url", cfg.fuseki?.url ?? "");
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
  if (deletedAgentIds.has(payload.agentId)) return;
  scene.onHeartbeat(payload);
  pushFeed({
    type: "heartbeat",
    label: "heartbeat",
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
});

mqtt.on("spawn", (payload) => {
  if (deletedAgentIds.has(payload.agentId)) return;
  scene.onSpawn(payload);
  syncAgentViews();
  pushFeed({
    type: "spawn",
    label: `spawned (${payload.agentType ?? "agent"})`,
    agentName: payload.agentName,
    timestamp: payload.timestampMs,
  });
  toast.show({ type: "spawn", title: payload.agentName, message: `${payload.agentType ?? "agent"} is online` });
  desktopNotifyBackground("Agent spawned", `${payload.agentName} is online`);
});

mqtt.on("alert", (payload) => {
  alertCount++;
  scene.onAlert(payload);
  hud.flashAlert(payload.severity);
  refreshStats();
  const alertMsg = payload.message ?? "";
  const alertAgent = payload.agentName ?? "system";
  pushFeed({
    type: payload.severity === "error" ? "alert-error" : "alert-warning",
    label: alertMsg,
    agentName: alertAgent,
    timestamp: payload.timestampMs,
  });
  const isError = payload.severity === "error" || payload.severity === "critical";
  toast.show({
    type: isError ? "alert-error" : "alert-warning",
    title: alertAgent,
    message: alertMsg.slice(0, 120),
  });
  if (isError) {
    desktopNotify(`⚠ ${alertAgent}`, alertMsg.slice(0, 100));
  } else {
    desktopNotifyBackground(alertAgent, alertMsg.slice(0, 100));
  }
});

mqtt.on("chat", (msg) => {
  if (msg.from !== "user") {
    toast.show({ type: "chat", title: msg.from, message: msg.content.slice(0, 120) });
    desktopNotifyBackground(msg.from, msg.content.slice(0, 120));
  }
  ioManager.receiveAgentMessage(msg);
  scene.onChat(msg.from, msg.to);
  document.dispatchEvent(
    new CustomEvent("af-chat-message", { detail: { msg } }),
  );
  pushFeed({
    type: "chat",
    label: `→ ${msg.to}: ${msg.content}`,
    agentName: msg.from,
    timestamp: msg.timestampMs,
  });
});

mqtt.on("status", (payload) => {
  if (!deletedAgentIds.has(payload.agentId)) {
    scene.addOrUpdateAgent({
      id: payload.agentId,
      name: payload.agentName,
      state: payload.state,
      protected: payload.protected ?? false,
      messagesProcessed: payload.messagesProcessed,
    });
    syncAgentViews();
    chatPanel.updateAgentStatus(payload.agentId, String(payload.state));
  }
  if (payload.state === "stopped") {
    window.setTimeout(() => refreshLiveActors(), 200);
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
  _mqttLive = true;
  console.info("[Dashboard] MQTT connected");
  hud.setSystemHealth(true);
  document.dispatchEvent(
    new CustomEvent("af-connection-status", { detail: { status: "live" } }),
  );

  scene.pruneStaleRemoteAgents();

  if (seeded) return;
  seeded = true;

  // Startup spawn events are published before the browser connects.
  // Fetch the current actor list from REST so they appear immediately.
  refreshLiveActors();
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
  if (!existing) return;
  const update: AgentInfo = {
    id: payload.agentId,
    name: existing.name,
    state: existing.state,
    protected: existing.protected,
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
    label: msg,
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
  scene.updateRemoteNode(payload.node, payload.agents);
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

mqtt.on("host-stats", (stats) => {
  if (stats.cpu !== undefined || stats.memUsedMb !== undefined) {
    scene.setHostStats(stats.cpu ?? 0, stats.memUsedMb ?? 0, stats.memTotalMb);
  }
});

mqtt.on("coin", (payload) => {
  pushFeed({
    type: "qa-flag",
    label: `balance ${payload.balance}${payload.reason ? " · " + payload.reason : ""}`,
    agentName: "wiz-agent",
    timestamp: Date.now(),
  });
});

// Only these domains produce meaningful on/off-style states worth showing in the feed.
const _HA_FEED_DOMAINS = new Set([
  "switch", "light", "fan", "input_boolean", "climate", "cover",
  "media_player", "vacuum", "humidifier", "lock", "alarm_control_panel",
]);

// Dedup cache: prevents the same entity+state appearing twice within 5 s when
// both the direct HAClient path and the MQTT agent path are active.
const _recentHaEvents = new Map<string, number>();
function _pushHaFeed(entityId: string, state: string, friendlyName: string): void {
  const domain = entityId.split(".")[0] ?? "";
  if (!_HA_FEED_DOMAINS.has(domain)) return;
  // Skip raw numeric states (sensors leaking through) and "unknown"/"unavailable" spam
  if (/^\d/.test(state) || state === "unknown") return;

  const key = `${entityId}:${state}`;
  const now = Date.now();
  if (now - (_recentHaEvents.get(key) ?? 0) < 5000) return;
  _recentHaEvents.set(key, now);
  pushFeed({ type: "health", label: `${friendlyName} → ${state}`, agentName: "ha", timestamp: now });
}

// Path 1: direct HA WebSocket via HAClient (always works when HA is configured in frontend)
document.addEventListener("af-ha-state-change", (e) => {
  const { entityId, state, friendlyName } = (
    e as CustomEvent<{ entityId: string; state: string; friendlyName: string }>
  ).detail;
  _pushHaFeed(entityId, state, friendlyName);
});

// Path 2: ha-state-bridge-agent → MQTT ha/state/{domain}/{entity_id}
mqtt.on("raw", ({ topic, payload }) => {
  if (!topic.startsWith("ha/")) return;
  const p = payload as Record<string, unknown>;
  const entityId = (p["entity_id"] as string | undefined) ?? topic.split("/").slice(-2).join(".");
  const newState = p["new_state"] as Record<string, unknown> | undefined;
  const state = (newState?.["state"] as string | undefined) ?? "";
  const friendlyName = (
    (newState?.["attributes"] as Record<string, unknown> | undefined)?.["friendly_name"] as string | undefined
  ) ?? entityId;
  if (!state) return;
  _pushHaFeed(entityId, state, friendlyName);
});

mqtt.on("disconnected", () => {
  _mqttLive = false;
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

// Streaming reply finished — notify
document.addEventListener("af-stream-end", (e) => {
  const { text, from } = (e as CustomEvent<{ text: string | null; from: string }>).detail;
  if (!text) return;
  toast.show({ type: "chat", title: from, message: text.slice(0, 120) });
  desktopNotifyBackground(from, text.slice(0, 120));
});

// Agent commands from CardDashboard / SocialDashboard → WebSocket
document.addEventListener("af-agent-command", (e) => {
  const { command, agentId } = (
    e as CustomEvent<{ command: string; agentId: string }>
  ).detail;
  if (command === "delete") {
    // Mark deleted immediately so MQTT "stopped" events don't re-add the card
    // before the WS state-patch reply arrives.
    deletedAgentIds.add(agentId);
    scene.removeAgent(agentId);
  }
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

// ── Settings (Tauri desktop only) ─────────────────────────────────────────────

const btnSettings = document.getElementById(
  "btn-settings",
) as HTMLButtonElement | null;
if (_isTauri && btnSettings) {
  btnSettings.style.display = "block";
  const settingsPanel = new SettingsPanel();
  btnSettings.addEventListener("click", () => settingsPanel.open());

  // Cmd+, (mac) / Ctrl+, (win/linux) → open settings
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === ",") {
      e.preventDefault();
      settingsPanel.open();
    }
  });

  // First-launch: greet + prompt for API key if not configured
  const _tauri = (window as any).__TAURI_INTERNALS__;
  if (_tauri?.invoke) {
    _tauri
      .invoke("get_config")
      .then((cfg: { llm_api_key?: string }) => {
        if (!cfg.llm_api_key) {
          setTimeout(() => {
            toast.show({
              type: "welcome",
              title: "Welcome to Wactorz",
              message: "Set your LLM API key to bring your agents to life.",
              durationMs: 12000,
              actions: [
                {
                  label: "Open Settings",
                  primary: true,
                  onClick: () => settingsPanel.open(),
                },
              ],
            });
            desktopNotify("Welcome to Wactorz", "Open Settings to add your API key.");
          }, 1200);
        } else {
          setTimeout(() => {
            toast.show({
              type: "system",
              title: "Wactorz",
              message: "Backend starting up — agents will appear shortly.",
              durationMs: 4000,
            });
            desktopNotify("Wactorz", "Backend starting up…");
          }, 800);
        }
      })
      .catch(() => {});
  }
}

// ── Sound / TTS toggles ───────────────────────────────────────────────────────

const btnBeep       = document.getElementById("btn-beep");
const btnTTS        = document.getElementById("btn-tts");
const voiceSelect   = document.getElementById("tts-voice-select") as HTMLSelectElement | null;

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

// Populate voice selector when server voices arrive
document.addEventListener("tts-voices-loaded", (e) => {
  const { voices } = (e as CustomEvent).detail;
  if (!voiceSelect || !voices?.length) return;
  voiceSelect.innerHTML = "";
  // Blank option = server default
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "— default voice —";
  voiceSelect.appendChild(blank);
  for (const v of voices) {
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = `${v.name} (${v.locale})`;
    voiceSelect.appendChild(opt);
  }
  voiceSelect.value = tts.selectedVoice;
});

voiceSelect?.addEventListener("change", () => {
  tts.setVoice(voiceSelect.value);
});

// Probe server TTS availability + load voice list
tts.init();

// ── Connect ───────────────────────────────────────────────────────────────────

mqtt.connect();

// ── Cleanup on page unload ────────────────────────────────────────────────────

window.addEventListener("focus", () => clearUnreadBadge());

window.addEventListener("beforeunload", () => {
  mqtt.disconnect();
  wsChat.disconnect();
  scene.dispose();
});

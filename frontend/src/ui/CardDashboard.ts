/**
 * CardDashboard — Wactorz.
 *
 * Full-screen overlay with af-header + af-body + af-iobar layout.
 * Views: overview (stats + cards + nodes) | feed | chat (embedded).
 *
 * Connects to the rest of the app via document-level custom events:
 *   Listens: "af-feed-push"  { item: FeedItem }
 *            "af-chat-message" { msg: ChatMessage }
 *            "af-stream-chunk" { chunk, from }
 *            "af-stream-end"
 *            "af-connection-status" { status: "live"|"connecting"|"demo" }
 *   Fires:   "af-send-message" { content, target }
 */

import type { AgentInfo, AgentState, ChatMessage } from "../types/agent";
import type { FeedItem } from "./ActivityFeed";
import { HAClient, type HAEntity } from "../io/HAClient";
import { ambient, AMBIENT_TRACKS } from "../io/AmbientManager";
import { tts } from "../io/TTSManager";
import { toast } from "./ToastManager";
import { renderMarkdown } from "./markdown";

// ── Helpers ──────────────────────────────────────────────────────────────────

const SYSTEM_AGENT_NAMES: Set<any> = new Set([
  "io-agent",
  "monitor-agent",
  "home-assistant-state-bridge",
  "home-assistant-map-agent",
]);
const ALWAYS_MESSAGEABLE = new Set([
  "main",
  "main-actor",
  "home-assistant-agent",
  "catalog",
]);
function canDirectMessage(agent: {
  name: string;
  protected?: boolean;
}): boolean {
  if (ALWAYS_MESSAGEABLE.has(agent.name)) return true;
  if (SYSTEM_AGENT_NAMES.has(agent.name)) return false;
  return !agent.protected;
}

function nameFromWid(raw: string | undefined): string {
  if (!raw) return "";
  const m = raw.match(/Z-(.+?)(?:-[0-9a-f]{6})?$/i);
  return m?.[1] ?? raw;
}

function stateColor(state: AgentState): string {
  if (typeof state === "object") return "#f87171";
  switch (state as string) {
    case "running":
      return "#34d399";
    case "paused":
      return "#fbbf24";
    case "initializing":
      return "#60a5fa";
    case "stopped":
      return "#4b5563";
    default:
      return "#34d399";
  }
}

function stateLabel(state: AgentState): string {
  if (typeof state === "object") return "failed";
  return state as string;
}

function relTime(ms: number): string {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 5) return "now";
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}

type View = "overview" | "feed" | "chat" | "ha" | "fuseki" | "settings";
type ConnState = "live" | "connecting" | "demo";

// ── CardDashboard ─────────────────────────────────────────────────────────────

export class CardDashboard {
  private root: HTMLElement;
  private agents: Map<string, AgentInfo> = new Map();
  private lastHb: Map<string, number> = new Map();
  private feedItems: FeedItem[] = [];
  private chatMessages: ChatMessage[] = [];
  private chatTarget: string = "main-actor";
  private view: View = "overview";
  private connState: ConnState = "connecting";
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private sidebarFilter: string = "";
  private hideHeartbeats: boolean = true;

  private haClient: HAClient | null = null;
  private _haEntities: import("../io/HAClient").HAEntity[] = [];
  private _haRegistries: import("../io/HAClient").HARegistries | null = null;

  private _remoteNodes = new Map<string, { agents: string[]; lastSeen: number }>();
  private _removingIds = new Set<string>();

  // Streaming
  private _streamRow: HTMLElement | null = null;
  private _streamBody: HTMLElement | null = null;
  private _streamFrom: string | null = null;
  private _streamTarget: string | null = null;
  private _streamText: string = "";
  private _lastSentTarget: string = "main-actor";
  private _historyLoaded: Set<string> = new Set();
  private _totalCostUsd: number | null = null;
  private _totalMessages: number | null = null;
  private _hostCpu: number | null = null;
  private _hostMemUsedMb: number | null = null;
  private _hostMemTotalMb: number | null = null;
  private _costLimitInfo: Record<string, any> | null = null;
  private _costPollTimer: ReturnType<typeof setInterval> | null = null;

  // Event listeners (stored for cleanup)
  private _evFeed: ((e: Event) => void) | null = null;
  private _evChat: ((e: Event) => void) | null = null;
  private _evChunk: ((e: Event) => void) | null = null;
  private _evEnd: ((e: Event) => void) | null = null;
  private _evConn: ((e: Event) => void) | null = null;
  private _evResetChat: ((e: Event) => void) | null = null;
  private _evSendMessage: ((e: Event) => void) | null = null;
  // True while _sendMessage() is dispatching — prevents the listener from
  // double-adding a message that _sendMessage() already rendered locally.
  private _selfDispatching = false;

  // Input history (up/down arrow navigation, same pattern as IOBar)
  private _inputHistory: string[] = [];
  private _inputHistIdx = -1;
  private _inputDraft = "";

  // Autosuggestion state
  private _inputSuggestion = "";
  private _mentionOpen = false;
  private _mentionIdx = -1;
  private _mentionMatches: string[] = [];

  private get haUrl(): string | null {
    return localStorage.getItem("wactorz-ha-url") || null;
  }

  private get haToken(): string | null {
    return localStorage.getItem("wactorz-ha-token") || null;
  }

  // ── Fuseki config (localStorage) ─────────────────────────────────────────

  private get fusekiUrl(): string | null {
    return localStorage.getItem("wactorz-fuseki-url") || null;
  }

  private get fusekiDataset(): string {
    return localStorage.getItem("wactorz-fuseki-dataset") || "wactorz";
  }

  private get fusekiUser(): string {
    return localStorage.getItem("wactorz-fuseki-user") || "admin";
  }

  private get fusekiPass(): string {
    return localStorage.getItem("wactorz-fuseki-pass") || "";
  }

  constructor() {
    this.root = this.buildRoot();
    document.body.appendChild(this.root);
    this._initHAClient();
    void this._loadServerConfig();
  }

  private async _loadServerConfig(): Promise<void> {
    try {
      const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
      const resp = await fetch(`${ingress}/api/config`);
      if (!resp.ok) return;
      const cfg = await resp.json() as {
        ha?:     { url?: string; token?: string };
        fuseki?: { url?: string; dataset?: string; user?: string; password?: string };
      };
      let changed = false;
      const seed = (key: string, val: string | undefined | null) => {
        if (val && !localStorage.getItem(key)) {
          localStorage.setItem(key, val);
          changed = true;
        }
      };
      seed("wactorz-fuseki-url",     cfg.fuseki?.url);
      seed("wactorz-fuseki-dataset", cfg.fuseki?.dataset);
      seed("wactorz-fuseki-user",    cfg.fuseki?.user);
      seed("wactorz-fuseki-pass",    cfg.fuseki?.password);
      seed("wactorz-ha-url",         cfg.ha?.url);
      seed("wactorz-ha-token",       cfg.ha?.token);
      if (changed) {
        if (!this.haClient) this._initHAClient();
        if (this.root.classList.contains("cd-visible")) this._renderView();
      }
    } catch {
      // best-effort — server may not be ready yet
    }
  }

  private _initHAClient(): void {
    const url = this.haUrl;
    const token = this.haToken;
    if (url && token) {
      this.haClient = new HAClient(url, token);
      this.haClient.onRegistriesUpdate = (r) => {
        this._haRegistries = r;
        if (this.view === "ha" && this._haEntities.length) {
          this._renderHADevices(this._haEntities);
        }
      };
    } else {
      this.haClient = null;
      this._haRegistries = null;
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    agents.forEach((a) => this.agents.set(a.id, a));
    this.root.classList.add("cd-visible");
    this._hideFloatingUI();
    this._wireEvents();
    this._renderView();
    this.tickTimer = setInterval(() => this._refreshTimestamps(), 5000);
    void this._fetchCostInfo();
    this._costPollTimer = setInterval(() => void this._fetchCostInfo(), 30_000);
    // Connect HA once for the session — stays connected across sub-view changes
    // so state_changed events flow to the activity feed at all times.
    if (this.haClient && !this.haClient.connected) {
      this.haClient.connect((entities) => {
        this._haEntities = entities;
        if (this.view === "ha") this._renderHADevices(entities);
      });
    }
  }

  hide(): void {
    this.root.classList.remove("cd-visible");
    this._showFloatingUI();
    this._unwireEvents();
    this.haClient?.disconnect();
    this._haEntities = [];
    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
    if (this._costPollTimer) {
      clearInterval(this._costPollTimer);
      this._costPollTimer = null;
    }
  }

  destroy(): void {
    this.hide();
    this.root.remove();
  }

  setTotalCostUsd(usd: number): void {
    this._totalCostUsd = usd;
    if (this.view === "overview") this._renderStats();
  }

  private async _fetchCostInfo(): Promise<void> {
    const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
    try {
      const res = await fetch(`${ingress}/api/cost`);
      if (res.ok) {
        this._costLimitInfo = await res.json();
        if (this.view === "overview") this._renderStats();
        else if (this.view === "settings") this._renderView();
      }
    } catch { /* ignore — server may not be ready */ }
  }

  private async _saveCostLimit(limit_usd: number, period: string): Promise<void> {
    const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
    await fetch(`${ingress}/api/cost/limit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit_usd, period }),
    });
    await this._fetchCostInfo();
  }

  private async _resetCost(): Promise<void> {
    const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
    await fetch(`${ingress}/api/cost/reset`, { method: "POST" });
    await this._fetchCostInfo();
  }

  setTotalMessages(count: number): void {
    this._totalMessages = count;
    if (this.view === "overview") this._renderStats();
  }

  setHostStats(cpu: number, memUsedMb: number, memTotalMb?: number): void {
    this._hostCpu = cpu;
    this._hostMemUsedMb = memUsedMb;
    if (memTotalMb !== undefined) this._hostMemTotalMb = memTotalMb;
    const bar = this.root.querySelector<HTMLElement>("#af-host-bar");
    if (!bar) return;
    const cpuFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-cpu");
    const cpuVal = bar.querySelector<HTMLElement>(".af-host-cpu-val");
    if (cpuFill) cpuFill.style.width = `${Math.max(0, Math.min(100, cpu)).toFixed(1)}%`;
    if (cpuVal) cpuVal.textContent = `${cpu.toFixed(1)}%`;
    const memFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-mem");
    const memVal = bar.querySelector<HTMLElement>(".af-host-mem-val");
    const total = this._hostMemTotalMb;
    const pct = total && total > 0 ? (memUsedMb / total) * 100 : 0;
    if (memFill) memFill.style.width = `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`;
    if (memVal) {
      if (total && total > 0) {
        memVal.textContent = `${(memUsedMb / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GB`;
      } else {
        memVal.textContent = `${memUsedMb.toFixed(0)} MB`;
      }
    }
  }

  // ── Agent events ──────────────────────────────────────────────────────────

  addAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    if (!this.root.classList.contains("cd-visible")) return;
    if (this.view === "overview") {
      this._renderCards();
      this._renderStats();
    }
    if (this.view === "chat") this._renderSidebar();
    this._updateTargetSelect();
  }

  updateAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    if (!this.root.classList.contains("cd-visible")) return;
    this._patchCard(agent);
    if (this.view === "overview") this._renderStats();
    if (this.view === "chat") this._renderSidebar();
  }

  removeAgent(id: string): void {
    const removed = this.agents.get(id);
    this.agents.delete(id);
    // _historyLoaded is keyed by agent NAME, not UUID — look up name before deleting
    if (removed) this._historyLoaded.delete(removed.name);
    if (!this.root.classList.contains("cd-visible")) return;
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(id)}"]`,
    );
    if (card) {
      this._removingIds.add(id);
      card.style.animation = "cd-exit 0.25s ease forwards";
      setTimeout(() => { card.remove(); this._removingIds.delete(id); }, 250);
    }
    if (this.view === "overview") this._renderStats();
    if (this.view === "chat") this._renderSidebar();
    this._updateTargetSelect();
  }

  updateRemoteNode(name: string, agents: string[]): void {
    this._remoteNodes.set(name, { agents, lastSeen: Date.now() });
    if (this.view === "overview") this._renderNodes();
  }

  onHeartbeat(agentId: string, timestampMs: number, _cpu?: number, _mem?: number): void {
    this.lastHb.set(agentId, timestampMs);
    if (!this.root.classList.contains("cd-visible")) return;
    if (this._removingIds.has(agentId)) return;
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(agentId)}"]`,
    );
    if (!card) return;
    const hbEl = card.querySelector<HTMLElement>(".af-card-hb-time");
    if (hbEl) hbEl.textContent = relTime(timestampMs);
    const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
    if (dot) {
      dot.classList.remove("af-card-pulse", "af-card-stale");
      void dot.offsetWidth;
      dot.classList.add("af-card-pulse");
    }
  }

  showAlert(agentId: string, severity: string): void {
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(agentId)}"]`,
    );
    if (!card) return;
    const cls =
      severity === "error" || severity === "critical"
        ? "af-card-alert-error"
        : "af-card-alert-warn";
    card.classList.add(cls);
    setTimeout(
      () =>
        card.classList.remove(cls, "af-card-alert-error", "af-card-alert-warn"),
      900,
    );
  }

  onChat(fromId: string, _toId: string): void {
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(fromId)}"]`,
    );
    if (!card) return;
    card.classList.add("af-card-chat-flash");
    setTimeout(() => card.classList.remove("af-card-chat-flash"), 600);
  }

  // ── Private: event wiring ─────────────────────────────────────────────────

  private _wireEvents(): void {
    this._evFeed = (e) => {
      const item = (e as CustomEvent<{ item: FeedItem }>).detail.item;
      this.feedItems.push(item);
      if (this.feedItems.length > 500) this.feedItems.shift();
      if (this.view === "feed") this._appendFeedItemToView(item);
    };

    this._evChat = (e) => {
      const msg = (e as CustomEvent<{ msg: ChatMessage }>).detail.msg;
      // Tag io-gateway / system replies with the most recent outbound target
      // so thread membership stays stable if the user switches sidebar rows
      // before the reply arrives.
      const stored: ChatMessage =
        msg.from === "io-gateway" || msg.from === "system"
          ? { ...msg, to: this._lastSentTarget }
          : msg;
      this.chatMessages.push(stored);
      if (this.chatMessages.length > 500) this.chatMessages.shift();
      if (this.view === "chat" && this._msgBelongsHere(stored)) {
        this._appendChatMsgEl(stored);
        this._scrollThread();
      }
    };

    this._evChunk = (e) => {
      const { chunk, from } = (
        e as CustomEvent<{ chunk: string; from: string }>
      ).detail;
      // Accumulate the stream regardless of the active view — chunks that
      // arrive while the user is on another tab must not be lost, otherwise
      // the message is saved truncated on stream-end (and only the page
      // refresh, which reloads from the backend, shows the full text). A new
      // session starts when there is no active stream (_streamFrom === null).
      if (this._streamFrom === null) {
        this._streamFrom = from;
        this._streamTarget = this._lastSentTarget;
        this._streamText = "";
      }
      this._streamText += chunk;

      // DOM updates only when the chat thread is on screen. The bubble is
      // (re)created lazily here and rebuilt by _renderChatThread on tab return.
      if (this.view !== "chat") return;
      if (!this._streamRow) {
        const thread = this.root.querySelector<HTMLElement>(".af-chat-thread");
        if (!thread) return;
        const row = document.createElement("div");
        row.className = "af-chat-msg af-chat-msg-agent";
        const fromEl = document.createElement("div");
        fromEl.className = "af-chat-msg-from";
        fromEl.textContent = this._streamFrom;
        const bubble = document.createElement("div");
        bubble.className = "af-chat-msg-bubble";
        row.appendChild(fromEl);
        row.appendChild(bubble);
        thread.appendChild(row);
        this._streamRow = row;
        this._streamBody = bubble;
      }
      if (this._streamBody) this._streamBody.textContent = this._streamText;
      this._scrollThread();
    };

    this._evEnd = () => {
      if (this._streamFrom && this._streamText) {
        const msg: ChatMessage = {
          id: `stream-${Date.now()}`,
          from: this._streamFrom,
          to: this._streamTarget ?? this._lastSentTarget,
          content: this._streamText,
          timestampMs: Date.now(),
        };
        this.chatMessages.push(msg);
      }
      // The live bubble showed plain text while streaming (partial Markdown
      // would flicker). Now that the reply is complete, re-render it richly.
      if (this._streamBody && this._streamText) {
        this._streamBody.textContent = "";
        this._streamBody.appendChild(renderMarkdown(this._streamText));
      }
      this._streamRow = null;
      this._streamBody = null;
      this._streamFrom = null;
      this._streamTarget = null;
      this._streamText = "";
    };

    this._evConn = (e) => {
      this.connState = (e as CustomEvent<{ status: ConnState }>).detail.status;
      this._renderConnBadge();
      this._renderHealth();
    };

    document.addEventListener("af-feed-push", this._evFeed);
    document.addEventListener("af-chat-message", this._evChat);
    document.addEventListener("af-stream-chunk", this._evChunk);
    document.addEventListener("af-stream-end", this._evEnd);
    document.addEventListener("af-connection-status", this._evConn);

    this._evResetChat = (e: Event) => {
      const agent = (e as CustomEvent).detail?.agent as string | null;
      this.chatMessages = agent
        ? this.chatMessages.filter((m) => m.from !== agent && m.from !== "user")
        : [];
      this._historyLoaded.clear();
      if (this.view === "chat") this._renderChatThread();
    };
    document.addEventListener("af-reset-chat", this._evResetChat);

    // Display the user's message in the chat UI for any send path (keyboard
    // OR voice/wake-word). Keyboard sends go through _sendMessage() which
    // already adds the message locally and sets _selfDispatching; those are
    // skipped here to avoid double-add.  Voice sends dispatch af-send-message
    // directly (from IOBar) without ever calling _sendMessage(), so they reach
    // this listener with _selfDispatching === false and are rendered here.
    this._evSendMessage = (e: Event) => {
      if (this._selfDispatching) return;
      const { content, target } = (
        e as CustomEvent<{ content: string; target: string }>
      ).detail;
      this.chatTarget = target;
      this._lastSentTarget = target;
      const msg: ChatMessage = {
        id: `user-${Date.now()}`,
        from: "user",
        to: target,
        content,
        timestampMs: Date.now(),
      };
      this.chatMessages.push(msg);
      if (this.view !== "chat") {
        this.view = "chat";
        this._renderView();
      } else {
        this._appendChatMsgEl(msg);
        this._scrollThread();
      }
    };
    document.addEventListener("af-send-message", this._evSendMessage);
  }

  private _unwireEvents(): void {
    if (this._evFeed) {
      document.removeEventListener("af-feed-push", this._evFeed);
      this._evFeed = null;
    }
    if (this._evChat) {
      document.removeEventListener("af-chat-message", this._evChat);
      this._evChat = null;
    }
    if (this._evChunk) {
      document.removeEventListener("af-stream-chunk", this._evChunk);
      this._evChunk = null;
    }
    if (this._evEnd) {
      document.removeEventListener("af-stream-end", this._evEnd);
      this._evEnd = null;
    }
    if (this._evConn) {
      document.removeEventListener("af-connection-status", this._evConn);
      this._evConn = null;
    }
    if (this._evResetChat) {
      document.removeEventListener("af-reset-chat", this._evResetChat);
      this._evResetChat = null;
    }
    if (this._evSendMessage) {
      document.removeEventListener("af-send-message", this._evSendMessage);
      this._evSendMessage = null;
    }
  }

  // ── Private: floating UI ──────────────────────────────────────────────────

  private _hideFloatingUI(): void {
    ["hud", "hud-stats", "io-bar", "chat-panel"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "none";
    });
  }

  private _showFloatingUI(): void {
    ["hud", "hud-stats", "io-bar", "feed-toggle"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });
    // Restore feed panel if it was open before the dashboard was shown
    const feedPanel = document.getElementById("activity-feed");
    if (feedPanel) feedPanel.style.display = "";
  }

  // ── Private: view rendering ───────────────────────────────────────────────

  private _renderView(): void {
    const body = this.root.querySelector<HTMLElement>(".af-body")!;
    body.innerHTML = "";
    this._streamRow = null;
    this._streamBody = null;

    if (this.view === "overview") body.appendChild(this._buildOverview());
    else if (this.view === "feed") body.appendChild(this._buildFeedView());
    else if (this.view === "ha") body.appendChild(this._buildHAView());
    else if (this.view === "fuseki") body.appendChild(this._buildFusekiView());
    else if (this.view === "settings")
      body.appendChild(this._buildSettingsView());
    else if (this.view === "chat") {
      body.appendChild(this._buildChatView());
      // _render* calls inside _buildChatView() run before the element is in
      // the DOM, so this.root.querySelector returns null. Re-run now that
      // the chat view is attached.
      this._renderSidebar();
      this._renderChatPaneHeader();
      this._renderChatThread();
      this._loadHistory(this.chatTarget);
    }

    this.root
      .querySelectorAll<HTMLElement>(".af-view-btn[data-view]")
      .forEach((btn) => {
        btn.classList.toggle("active", btn.dataset["view"] === this.view);
      });
    this._renderHealth();
    // Only show the agent-target dropdown in the chat view
    const select =
      this.root.querySelector<HTMLSelectElement>("#af-target-select");
    if (select) {
      select.style.display = this.view === "chat" ? "" : "none";
      if (this.view === "chat") select.value = this.chatTarget;
    }
  }

  /** Ensure chatTarget is a live agent, defaulting to "main" → "main-actor" → first. */
  private _syncChatTarget(): void {
    const agents = [...this.agents.values()];
    if (!agents.length) return;
    if (agents.some((a) => a.name === this.chatTarget)) return;
    const main = agents.find(
      (a) => a.name === "main" || a.name === "main-actor",
    );
    const fallback = [...agents].sort((a, b) =>
      a.name.localeCompare(b.name),
    )[0];
    this.chatTarget = main?.name ?? fallback?.name ?? this.chatTarget;
  }

  private _setView(v: View): void {
    if (v === "chat") this._syncChatTarget();
    this.view = v;
    this._renderView();

    if (this.view === "ha") {
      if (this.haClient?.connected) {
        // Already connected — just re-render with cached entities
        if (this._haEntities.length) this._renderHADevices(this._haEntities);
      } else {
        this.haClient?.connect((entities) => {
          this._haEntities = entities;
          if (this.view === "ha") this._renderHADevices(entities);
        });
      }
    }
  }

  // ── Private: overview ─────────────────────────────────────────────────────

  private _renderNodes(container?: HTMLElement): void {
    const list = container ?? this.root.querySelector<HTMLElement>("#af-node-list");
    if (!list) return;
    const agentNames = [...this.agents.values()].map((a) => a.name);
    const rows: string[] = [];
    rows.push(`
      <div class="af-node-item">
        <div>
          <div class="af-node-name">local</div>
          <div class="af-node-meta">${agentNames.length > 0 ? agentNames.join(", ") : "no agents"}</div>
        </div>
        <span class="af-node-pill online">online</span>
      </div>`);
    const staleMs = 180_000;
    const now = Date.now();
    for (const [name, info] of this._remoteNodes) {
      const online = now - info.lastSeen < staleMs;
      const meta = info.agents.length > 0 ? info.agents.join(", ") : "no agents";
      rows.push(`
        <div class="af-node-item">
          <div>
            <div class="af-node-name">${name}</div>
            <div class="af-node-meta">${meta}</div>
          </div>
          <span class="af-node-pill ${online ? "online" : "offline"}">${online ? "online" : "offline"}</span>
        </div>`);
    }
    list.innerHTML = rows.join("");
  }

  private _buildOverview(): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-overview";

    el.appendChild(this._buildHostBar());

    const statsGrid = document.createElement("div");
    statsGrid.className = "af-stats-grid";
    statsGrid.id = "af-stats-grid";
    this._buildStatCards(statsGrid);
    el.appendChild(statsGrid);

    const panels = document.createElement("div");
    panels.className = "af-overview-panels";

    // Wactorz panel
    const wp = document.createElement("section");
    wp.className = "af-panel";
    wp.innerHTML = `<div class="af-panel-head"><h3>Wactorz</h3><span>actor model · MQTT pub-sub</span></div>`;
    const grid = document.createElement("div");
    grid.className = "af-cards-grid";
    grid.id = "af-wactor-cards";
    [...this.agents.values()]
      .sort((a, b) => {
        if (a.name === "main-actor") return -1;
        if (b.name === "main-actor") return 1;
        return a.name.localeCompare(b.name);
      })
      .forEach((agent) => grid.appendChild(this._buildWactorCard(agent)));
    wp.appendChild(grid);

    // Nodes panel
    const np = document.createElement("section");
    np.className = "af-panel";
    np.innerHTML = `<div class="af-panel-head"><h3>Nodes</h3><span>from heartbeat telemetry</span></div>`;
    const nodeList = document.createElement("div");
    nodeList.className = "af-node-list";
    nodeList.id = "af-node-list";
    np.appendChild(nodeList);
    this._renderNodes(nodeList);

    panels.appendChild(wp);
    panels.appendChild(np);
    el.appendChild(panels);
    return el;
  }

  private _buildHostBar(): HTMLElement {
    const bar = document.createElement("div");
    bar.id = "af-host-bar";
    bar.className = "af-host-bar";

    const cpu = this._hostCpu;
    const memUsed = this._hostMemUsedMb;
    const memTotal = this._hostMemTotalMb;

    const cpuPct = cpu != null ? Math.max(0, Math.min(100, cpu)) : 0;
    const cpuText = cpu != null ? `${cpu.toFixed(1)}%` : "—";
    const memPct =
      memUsed != null && memTotal != null && memTotal > 0
        ? Math.max(0, Math.min(100, (memUsed / memTotal) * 100))
        : 0;
    const memText =
      memUsed != null
        ? memTotal != null && memTotal > 0
          ? `${(memUsed / 1024).toFixed(1)} / ${(memTotal / 1024).toFixed(1)} GB`
          : `${memUsed.toFixed(0)} MB`
        : "—";

    bar.innerHTML = `
      <div class="af-host-label">APP</div>
      <div class="af-host-metric">
        <div class="af-host-metric-label">CPU</div>
        <div class="af-host-bar-track">
          <div class="af-host-bar-fill af-host-bar-fill-cpu" style="width:${cpuPct.toFixed(1)}%"></div>
        </div>
        <div class="af-host-metric-val af-host-cpu-val">${cpuText}</div>
      </div>
      <div class="af-host-metric">
        <div class="af-host-metric-label">MEM</div>
        <div class="af-host-bar-track">
          <div class="af-host-bar-fill af-host-bar-fill-mem" style="width:${memPct.toFixed(1)}%"></div>
        </div>
        <div class="af-host-metric-val af-host-mem-val">${memText}</div>
      </div>
    `;
    return bar;
  }

  private _buildStatCards(container: HTMLElement): void {
    container.innerHTML = "";
    const agents = [...this.agents.values()];
    const total = agents.length;
    const healthy = agents.filter(
      (a) => stateLabel(a.state) === "running",
    ).length;
    const msgs = this._totalMessages !== null
      ? this._totalMessages
      : agents.reduce((s, a) => s + (a.messagesProcessed ?? 0), 0);
    const cost = this._totalCostUsd !== null
      ? this._totalCostUsd
      : agents.reduce((s, a) => s + (a.costUsd ?? 0), 0);
    const events = this.feedItems.length;

    const lim = this._costLimitInfo;
    const hasLimit = lim && typeof lim.limit_usd === "number" && lim.limit_usd > 0;
    const barColor = lim?.limit_reached ? "#ef4444" : lim?.warning ? "#f59e0b" : "#22d3a0";
    const pct = hasLimit ? Math.min(lim!.pct_used ?? 0, 100) : 0;
    const periodLabel =
      lim?.period === "daily"  ? "today"
    : lim?.period === "weekly" ? "this week"
    :                            "this month";
    const costDetail = hasLimit
      ? `$${lim!.spend_usd.toFixed(4)} / $${lim!.limit_usd.toFixed(2)} ${periodLabel}`
      : "reported by actors";
    const costExtra = hasLimit ? `
      <div style="margin-top:8px;background:rgba(255,255,255,0.08);border-radius:4px;height:6px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${barColor};border-radius:4px;transition:width 0.4s"></div>
      </div>` : "";

    [
      {
        label: "Wactorz",
        value: String(total),
        detail: `${healthy} running`,
        accent: "#60a5fa",
        extra: "",
      },
      {
        label: "Messages",
        value: String(msgs),
        detail: "processed across actors",
        accent: "#22d3a0",
        extra: "",
      },
      {
        label: "Cost",
        value: `$${cost.toFixed(4)}`,
        detail: costDetail,
        accent: lim?.limit_reached ? "#ef4444" : "#f59e0b",
        extra: costExtra,
      },
      {
        label: "Feed Events",
        value: String(events),
        detail: "since dashboard loaded",
        accent: "#8b5cf6",
        extra: "",
      },
    ].forEach(({ label, value, detail, accent, extra }: any) => {
      const card = document.createElement("div");
      card.className = "af-stat-card";
      card.style.borderColor = `${accent}44`;
      card.innerHTML = `
        <div class="af-stat-label">${label}</div>
        <div class="af-stat-value" style="color:${accent}">${value}</div>
        <div class="af-stat-detail">${detail}</div>
        ${extra}
      `;
      container.appendChild(card);
    });
  }

  private _renderStats(): void {
    const grid = this.root.querySelector<HTMLElement>("#af-stats-grid");
    if (grid) this._buildStatCards(grid);
  }

  private _renderCards(): void {
    const grid = this.root.querySelector<HTMLElement>("#af-wactor-cards");
    if (!grid) return;
    const sorted = [...this.agents.values()].sort((a, b) => {
      if (a.name === "main-actor") return -1;
      if (b.name === "main-actor") return 1;
      return a.name.localeCompare(b.name);
    });
    const live = new Set(sorted.map((a) => a.id));
    grid.querySelectorAll<HTMLElement>("[data-id]").forEach((el) => {
      if (!live.has(el.dataset.id!)) {
        this._removingIds.delete(el.dataset.id!);
        el.remove();
      }
    });
    sorted.forEach((agent) => {
      if (!grid.querySelector(`[data-id="${CSS.escape(agent.id)}"]`)) {
        grid.appendChild(this._buildWactorCard(agent));
      }
    });
  }

  private _patchCard(agent: AgentInfo): void {
    if (this._removingIds.has(agent.id)) return;
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(agent.id)}"]`,
    );
    if (!card) {
      if (this.view === "overview") this._renderCards();
      return;
    }
    const color = stateColor(agent.state);
    const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
    const lbl = card.querySelector<HTMLElement>(".af-card-state-label");
    const nm = card.querySelector<HTMLElement>(".af-card-name");
    if (dot) {
      dot.style.background = color;
      dot.style.boxShadow = `0 0 8px ${color}`;
    }
    if (lbl) {
      lbl.style.color = color;
      lbl.textContent = stateLabel(agent.state);
    }
    if (nm) nm.textContent = agent.name;
    this._rebuildControls(card, agent);
  }

  // ── Private: wactor card ──────────────────────────────────────────────────

  private _buildWactorCard(agent: AgentInfo): HTMLElement {
    const hbMs = this.lastHb.get(agent.id) ?? 0;
    const color = stateColor(agent.state);
    const status = stateLabel(agent.state);
    const msgs = agent.messagesProcessed ?? 0;

    const card = document.createElement("div");
    card.className = "af-card";
    card.dataset.id = agent.id;

    const dot = document.createElement("div");
    // Pre-apply af-card-pulse when we already know this agent's heartbeat —
    // prevents the infinite blink that would otherwise last until the next
    // MQTT heartbeat (~10s) on view-switch rebuilds.
    dot.className = hbMs > 0 ? "af-card-state-dot af-card-pulse" : "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}`;

    const name = document.createElement("div");
    name.className = "af-card-name";
    name.textContent = agent.name;

    const stateLbl = document.createElement("div");
    stateLbl.className = "af-card-state-label";
    stateLbl.style.color = color;
    stateLbl.textContent = status;

    const meta = document.createElement("div");
    meta.className = "af-card-meta";
    meta.innerHTML = `
      <span>♥ <span class="af-card-hb-time">${hbMs ? relTime(hbMs) : "—"}</span></span>
      <span>${msgs} msgs</span>
      ${agent.costUsd != null ? `<span>$${agent.costUsd.toFixed(4)}</span>` : ""}
    `;

    card.appendChild(dot);
    card.appendChild(name);
    card.appendChild(stateLbl);
    card.appendChild(meta);

    if (agent.task) {
      const task = document.createElement("div");
      task.className = "af-card-task";
      task.textContent = agent.task;
      task.title = agent.task;
      card.appendChild(task);
    }

    const controls = document.createElement("div");
    controls.className = "af-card-controls";

    const canMessage = canDirectMessage(agent);
    if (canMessage) {
      const chatBtn = document.createElement("button");
      chatBtn.className = "af-mini-btn af-chat-btn";
      chatBtn.textContent = "Chat";
      chatBtn.hidden = stateLabel(agent.state) === "stopped";
      chatBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.chatTarget = agent.name;
        this._setView("chat");
      });
      controls.appendChild(chatBtn);
    }
    this._appendActionBtns(controls, agent);
    controls.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>(
        "[data-action]",
      );
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this._sendCommand(
        agent.id,
        btn.dataset.action as "pause" | "resume" | "stop" | "delete",
        btn,
      );
    });
    card.appendChild(controls);

    if (agent.protected) {
      const shield = document.createElement("div");
      shield.className = "af-card-protected";
      shield.title = "Protected wactor";
      shield.textContent = "🔒";
      card.appendChild(shield);
    }

    return card;
  }

  private _appendActionBtns(controls: HTMLElement, agent: AgentInfo): void {
    if (!canDirectMessage(agent)) return;
    const status = stateLabel(agent.state);
    if (status === "running") {
      const b = document.createElement("button");
      b.className = "af-mini-btn";
      b.textContent = "Pause";
      b.dataset.action = "pause";
      controls.appendChild(b);
    }
    if (status === "paused") {
      const b = document.createElement("button");
      b.className = "af-mini-btn";
      b.textContent = "Resume";
      b.dataset.action = "resume";
      controls.appendChild(b);
    }
    if (!agent.protected && status !== "stopped") {
      const b = document.createElement("button");
      b.className = "af-mini-btn danger";
      b.textContent = "Stop";
      b.dataset.action = "stop";
      controls.appendChild(b);
    }
    if (!agent.protected) {
      const b = document.createElement("button");
      b.className = "af-mini-btn danger";
      b.textContent = "Delete";
      b.dataset.action = "delete";
      controls.appendChild(b);
    }
  }

  private _rebuildControls(card: HTMLElement, agent: AgentInfo): void {
    const controls = card.querySelector<HTMLElement>(".af-card-controls");
    if (!controls) return;
    // Toggle Chat button visibility based on state
    const chatBtn = controls.querySelector<HTMLButtonElement>(".af-chat-btn");
    if (chatBtn) chatBtn.hidden = stateLabel(agent.state) === "stopped";
    // Only replace the action buttons — the click listener from _buildWactorCard
    // is already on the controls element via event delegation, do not re-add it.
    controls.querySelectorAll("[data-action]").forEach((b) => b.remove());
    this._appendActionBtns(controls, agent);
  }

  // ── Private: feed view ────────────────────────────────────────────────────

  private _buildFeedView(): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "af-feed-wrap";

    const toolbar = document.createElement("div");
    toolbar.className = "af-feed-toolbar";

    const hbBtn = document.createElement("button");
    hbBtn.className = `af-mini-btn${this.hideHeartbeats ? "" : " active"}`;
    hbBtn.style.cssText = "font-size:11px;padding:3px 10px;";
    hbBtn.title = "Toggle heartbeat events";
    hbBtn.textContent = this.hideHeartbeats
      ? "♥ heartbeats: off"
      : "♥ heartbeats: on";
    hbBtn.addEventListener("click", () => {
      this.hideHeartbeats = !this.hideHeartbeats;
      hbBtn.textContent = this.hideHeartbeats
        ? "♥ heartbeats: off"
        : "♥ heartbeats: on";
      hbBtn.className = `af-mini-btn${this.hideHeartbeats ? "" : " active"}`;
      const feed = wrap.querySelector<HTMLElement>("#af-feed-view")!;
      feed.querySelectorAll<HTMLElement>(".af-feed-heartbeat").forEach((el) => {
        el.hidden = this.hideHeartbeats;
      });
    });
    toolbar.appendChild(hbBtn);
    wrap.appendChild(toolbar);

    const feed = document.createElement("div");
    feed.className = "af-feed";
    feed.id = "af-feed-view";

    const visible = this.feedItems.filter(
      (i) =>
        !(this.hideHeartbeats && i.type === "heartbeat") &&
        !SYSTEM_AGENT_NAMES.has(nameFromWid(i.agentName)),
    );
    if (visible.length === 0) {
      const empty = document.createElement("div");
      empty.className = "af-feed-empty";
      empty.textContent = "No events yet.";
      feed.appendChild(empty);
    } else {
      visible.forEach((item) => this._feedItemEl(feed, item));
    }
    wrap.appendChild(feed);
    setTimeout(() => {
      feed.scrollTop = feed.scrollHeight;
    }, 0);
    return wrap;
  }

  private _appendFeedItemToView(item: FeedItem): void {
    const feed = this.root.querySelector<HTMLElement>("#af-feed-view");
    if (!feed) return;
    if (this.hideHeartbeats && item.type === "heartbeat") return;
    if (SYSTEM_AGENT_NAMES.has(nameFromWid(item.agentName))) return;
    feed.querySelector(".af-feed-empty")?.remove();
    this._feedItemEl(feed, item);
    feed.scrollTop = feed.scrollHeight;
  }

  private _feedItemEl(container: HTMLElement, item: FeedItem): void {
    const TYPE_CLASS: Record<string, string> = {
      spawn: "af-feed-spawn",
      heartbeat: "af-feed-heartbeat",
      chat: "af-feed-chat",
      "alert-error": "af-feed-alert",
      "alert-warning": "af-feed-alert",
      health: "af-feed-heartbeat",
      "qa-flag": "af-feed-chat",
    };
    const TYPE_ICON: Record<string, string> = {
      spawn: "⚡",
      heartbeat: "♥",
      chat: "💬",
      "alert-error": "🔴",
      "alert-warning": "🟡",
      stopped: "◻",
      health: "◉",
      "qa-flag": "⚑",
    };

    const row = document.createElement("div");
    row.className = `af-feed-item ${TYPE_CLASS[item.type] ?? ""}`.trim();

    const icon = document.createElement("span");
    icon.className = "af-feed-icon";
    icon.textContent = TYPE_ICON[item.type] ?? "·";

    const time = document.createElement("span");
    time.className = "af-feed-time";
    time.textContent = new Date(item.timestamp).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });

    const agent = document.createElement("span");
    agent.className = "af-feed-agent";
    agent.textContent = nameFromWid(item.agentName);

    const text = document.createElement("span");
    text.className = "af-feed-text";
    const label = item.label ?? "";
    const trimmed = label.length > 120 ? label.slice(0, 120) + "…" : label;
    text.textContent = trimmed;
    if (label.length > 120) text.title = label;

    row.append(icon, time, agent, text);
    container.appendChild(row);
  }

  // ── Private: chat view ────────────────────────────────────────────────────

  private _buildChatView(): HTMLElement {
    const chat = document.createElement("div");
    chat.className = "af-chat";

    // Sidebar
    const sidebar = document.createElement("div");
    sidebar.className = "af-chat-sidebar";

    const searchWrap = document.createElement("div");
    searchWrap.className = "af-chat-sidebar-search";
    const searchInput = document.createElement("input");
    searchInput.placeholder = "Filter agents…";
    searchInput.value = this.sidebarFilter;
    searchInput.addEventListener("input", () => {
      this.sidebarFilter = searchInput.value.toLowerCase();
      this._renderSidebar();
    });
    searchWrap.appendChild(searchInput);
    sidebar.appendChild(searchWrap);

    const agentList = document.createElement("div");
    agentList.className = "af-chat-agent-list";
    agentList.id = "af-chat-agent-list";
    sidebar.appendChild(agentList);
    chat.appendChild(sidebar);

    // Pane
    const pane = document.createElement("div");
    pane.className = "af-chat-pane";

    const paneHdr = document.createElement("div");
    paneHdr.className = "af-chat-pane-header";
    paneHdr.id = "af-chat-pane-header";
    pane.appendChild(paneHdr);

    const thread = document.createElement("div");
    thread.className = "af-chat-thread";
    thread.id = "af-chat-thread";
    pane.appendChild(thread);

    chat.appendChild(pane);

    this._renderSidebar();
    this._renderChatPaneHeader();
    this._renderChatThread();

    return chat;
  }

  private _renderSidebar(): void {
    const list = this.root.querySelector<HTMLElement>("#af-chat-agent-list");
    if (!list) return;

    const sorted = [...this.agents.values()]
      .filter(
        (a) =>
          !this.sidebarFilter ||
          a.name.toLowerCase().includes(this.sidebarFilter),
      )
      .sort((a, b) => {
        if (a.name === "main-actor") return -1;
        if (b.name === "main-actor") return 1;
        return a.name.localeCompare(b.name);
      });

    // Collect existing rows for diffing
    const existing = new Map<string, HTMLElement>();
    list.querySelectorAll<HTMLElement>(".af-chat-agent-row").forEach((r) => {
      if (r.dataset["name"]) existing.set(r.dataset["name"], r);
    });

    const keep = new Set(sorted.map((a) => a.name));
    existing.forEach((row, name) => {
      if (!keep.has(name)) row.remove();
    });

    sorted.forEach((agent, idx) => {
      const color = stateColor(agent.state);
      const isActive = agent.name === this.chatTarget;
      const isDisabled = !canDirectMessage(agent);

      let row = existing.get(agent.name);
      if (!row) {
        row = document.createElement("button");
        row.dataset["name"] = agent.name;
        const dot = document.createElement("span");
        dot.className = "af-chat-agent-dot";
        const nm = document.createElement("span");
        nm.className = "af-chat-agent-name";
        nm.textContent = agent.name;
        const lock = document.createElement("span");
        lock.className = "af-chat-agent-lock";
        lock.setAttribute("aria-hidden", "true");
        row.append(dot, nm, lock);
        row.addEventListener("click", () => {
          const latest = [...this.agents.values()].find(
            (a) => a.name === agent.name,
          );
          if (!latest || !canDirectMessage(latest)) return;
          this.chatTarget = agent.name;
          this._renderSidebar();
          this._renderChatPaneHeader();
          this._renderChatThread();
          this._loadHistory(agent.name);
          this._updateTargetSelect();
          // Mobile: switch to pane view
          this.root.querySelector(".af-chat")?.classList.add("agent-selected");
        });
      }

      // Patch only what may have changed
      const cls = ["af-chat-agent-row"];
      if (isActive) cls.push("active");
      if (isDisabled) cls.push("protected-agent");
      row.className = cls.join(" ");
      (row as HTMLButtonElement).disabled = isDisabled;
      row.title = isDisabled
        ? `${agent.name} — infrastructure agent, not directly reachable`
        : agent.name;
      const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
      if (dot && dot.style.background !== color) dot.style.background = color;
      const lock = row.querySelector<HTMLElement>(".af-chat-agent-lock");
      if (lock) lock.textContent = isDisabled ? "🔒" : "";

      const sibling = list.children[idx];
      if (sibling !== row) list.insertBefore(row, sibling ?? null);
    });
  }

  private _renderChatPaneHeader(): void {
    const hdr = this.root.querySelector<HTMLElement>("#af-chat-pane-header");
    if (!hdr) return;
    hdr.innerHTML = "";

    // Back button (mobile-only via CSS)
    const backBtn = document.createElement("button");
    backBtn.className = "af-chat-back-btn";
    backBtn.textContent = "‹ Back";
    backBtn.addEventListener("click", () => {
      this.root.querySelector(".af-chat")?.classList.remove("agent-selected");
    });
    hdr.appendChild(backBtn);

    const agent = [...this.agents.values()].find(
      (a) => a.name === this.chatTarget,
    );
    if (agent) {
      const dot = document.createElement("span");
      dot.className = "af-chat-agent-dot";
      dot.style.background = stateColor(agent.state);
      hdr.appendChild(dot);
    }
    const title = document.createElement("span");
    title.className = "af-chat-pane-title";
    title.textContent = `@${this.chatTarget}`;
    hdr.appendChild(title);
    if (agent) {
      const st = document.createElement("span");
      st.className = "af-chat-pane-state";
      st.textContent = stateLabel(agent.state);
      hdr.appendChild(st);
    }
  }

  /** True when `msg` belongs to the currently open agent thread. */
  private _msgBelongsHere(msg: ChatMessage): boolean {
    // User-sent messages: keyed by who they were sent to
    if (msg.from === "user") return msg.to === this.chatTarget;
    // io-gateway / system are tagged with chatTarget in _evChat; match on .to
    if (msg.from === "io-gateway" || msg.from === "system")
      return msg.to === this.chatTarget;
    // Regular agent messages: keyed by sender
    return msg.from === this.chatTarget;
  }

  private async _loadHistory(agentId: string): Promise<void> {
    if (this._historyLoaded.has(agentId)) return;
    this._historyLoaded.add(agentId);
    const ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
    const liveIds = () => new Set(this.chatMessages.map((m) => m.id));
    const prepend = (msgs: ChatMessage[]) => {
      const ids = liveIds();
      this.chatMessages.unshift(...msgs.filter((m) => !ids.has(m.id)));
      this._renderChatThread();
    };
    try {
      // Primary: chat_log table — real persisted timestamps
      const chatRes = await fetch(
        `${ingress}/api/chats?agent=${encodeURIComponent(agentId)}&limit=200`,
      );
      if (chatRes.ok) {
        const rows: { id: number; ts: number; role: string; content: string }[] =
          await chatRes.json();
        if (rows.length) {
          prepend(
            rows.reverse().map((r) => ({
              id: `hist-${agentId}-${r.id}`,
              from: r.role === "user" ? "user" : agentId,
              to:   r.role === "user" ? agentId : "user",
              content: r.content,
              timestampMs: r.ts < 1e10 ? r.ts * 1000 : r.ts,
            })),
          );
          return;
        }
      }
      // Fallback: actor kv_store history — no timestamps, synthesise
      const res = await fetch(
        `${ingress}/api/actors/${encodeURIComponent(agentId)}/history`,
      );
      if (!res.ok) return;
      const raw: { role: string; content: string }[] = await res.json();
      if (!raw.length) return;
      const base = Date.now() - raw.length * 2000 - 5000;
      prepend(
        raw.map((m, i) => ({
          id: `hist-${agentId}-${i}`,
          from: m.role === "user" ? "user" : agentId,
          to:   m.role === "user" ? agentId : "user",
          content: m.content,
          timestampMs: base + i * 2000,
        })),
      );
    } catch {
      // history unavailable — silent
    }
  }

  private _renderChatThread(): void {
    const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
    if (!thread) return;
    thread.innerHTML = "";
    // The wipe above detached any live stream bubble; drop the stale DOM refs
    // so they don't point at orphaned nodes (rebuilt below if still streaming).
    this._streamRow = null;
    this._streamBody = null;

    // Is a stream in progress that belongs to the thread currently shown?
    const streamHere =
      !!this._streamFrom &&
      this._streamText.length > 0 &&
      this._msgBelongsHere({
        id: "",
        from: this._streamFrom,
        to: this._streamTarget ?? this._lastSentTarget,
        content: "",
        timestampMs: 0,
      });

    const msgs = this.chatMessages.filter((m) => this._msgBelongsHere(m));
    if (msgs.length === 0 && !streamHere) {
      const empty = document.createElement("div");
      empty.className = "af-chat-empty";
      empty.innerHTML =
        this.chatTarget === "main-actor"
          ? `<p>Say hello to <strong>@main-actor</strong> — the system orchestrator.</p>`
          : `<p>No messages with <strong>@${this.chatTarget}</strong> yet.</p>
           <p style="font-size:11px;opacity:0.5">New messages will be sent directly to this agent.</p>`;
      thread.appendChild(empty);
    } else {
      msgs.forEach((m) => this._appendChatMsgEl(m, thread));
    }

    // Re-attach the in-progress streaming bubble so returning to the chat tab
    // mid-reply shows the text accumulated so far and keeps updating it.
    if (streamHere) {
      const row = document.createElement("div");
      row.className = "af-chat-msg af-chat-msg-agent";
      const fromEl = document.createElement("div");
      fromEl.className = "af-chat-msg-from";
      fromEl.textContent = this._streamFrom!;
      const bubble = document.createElement("div");
      bubble.className = "af-chat-msg-bubble";
      bubble.textContent = this._streamText;
      row.append(fromEl, bubble);
      thread.appendChild(row);
      this._streamRow = row;
      this._streamBody = bubble;
    }

    this._scrollThread();
  }

  private _appendChatMsgEl(msg: ChatMessage, container?: HTMLElement): void {
    const thread =
      container ?? this.root.querySelector<HTMLElement>("#af-chat-thread");
    if (!thread) return;
    thread.querySelector(".af-chat-empty")?.remove();
    const isUser = msg.from === "user";
    const row = document.createElement("div");
    row.className = `af-chat-msg af-chat-msg-${isUser ? "user" : "agent"}`;
    const from = document.createElement("div");
    from.className = "af-chat-msg-from";
    from.textContent = isUser
      ? `you · ${new Date(msg.timestampMs).toLocaleTimeString()}`
      : msg.from;
    const bubble = document.createElement("div");
    bubble.className = "af-chat-msg-bubble";
    // Agent replies are rendered as a small Markdown subset; user messages stay
    // plain so stray *asterisks* / backticks they type are never reformatted.
    if (isUser) bubble.textContent = msg.content;
    else bubble.appendChild(renderMarkdown(msg.content));
    row.append(from, bubble);
    if (!isUser) {
      const time = document.createElement("div");
      time.className = "af-chat-msg-time";
      time.textContent = new Date(msg.timestampMs).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      row.appendChild(time);
    }
    thread.appendChild(row);
  }

  private _scrollThread(): void {
    const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  // ── Private: conn badge & health ──────────────────────────────────────────

  private _renderConnBadge(): void {
    const badge = this.root.querySelector<HTMLElement>(".af-conn-badge");
    if (!badge) return;
    badge.className = `af-conn-badge af-conn-${this.connState}`;
    badge.textContent =
      this.connState === "live"
        ? "● live"
        : this.connState === "connecting"
          ? "○ Connecting…"
          : "◎ Demo fallback";
  }

  private _renderHealth(): void {
    const el = this.root.querySelector<HTMLElement>(".af-health");
    if (!el) return;
    const agents = [...this.agents.values()];
    const healthy = agents.filter(
      (a) => stateLabel(a.state) === "running",
    ).length;
    el.textContent = `${healthy}/${agents.length} wactorz healthy`;
  }

  // ── Private: iobar ────────────────────────────────────────────────────────

  private _buildIobar(): HTMLElement {
    const bar = document.createElement("div");
    bar.className = "af-iobar";

    const select = document.createElement("select");
    select.className = "af-target-select";
    select.id = "af-target-select";
    this._populateSelect(select);

    // ── Input wrapper (ghost text + mention panel + textarea + hint) ──────────
    const inputWrap = document.createElement("div");
    inputWrap.className = "af-input-wrap";

    // @mention panel — floats above the wrap when @ is typed
    const mentionPanel = document.createElement("div");
    mentionPanel.className = "af-mention-panel";

    // Ghost text layer — positioned behind the textarea
    const ghost = document.createElement("div");
    ghost.className = "af-input-ghost";
    ghost.setAttribute("aria-hidden", "true");

    const input = document.createElement("textarea");
    input.className = "af-iobar-input";
    input.id = "af-iobar-input";
    input.rows = 1;
    input.placeholder = `Message @${this.chatTarget}…`;

    // Hint row — keyboard shortcuts, visible on focus
    const hint = document.createElement("div");
    hint.className = "af-input-hint";
    hint.textContent = "↑↓ history · Tab accept · @agent";

    const autoGrow = () => {
      input.style.height = "1px";
      const h = Math.min(input.scrollHeight, 140);
      input.style.height = h + "px";
      input.style.overflowY = h >= 140 ? "auto" : "hidden";
    };

    input.addEventListener("input", () => {
      autoGrow();
      this._onInputChange(input, select, ghost, mentionPanel);
    });

    input.addEventListener("keydown", (e) => {
      // @mention panel navigation
      if (this._mentionOpen) {
        if (e.key === "Escape") {
          e.preventDefault();
          this._closeMentionPanel(mentionPanel);
          return;
        }
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          const dir = e.key === "ArrowRight" ? 1 : -1;
          this._mentionIdx = Math.max(-1, Math.min(this._mentionMatches.length - 1, this._mentionIdx + dir));
          this._renderMentionChips(mentionPanel, select, input);
          return;
        }
        if (e.key === "Tab" || e.key === "Enter") {
          e.preventDefault();
          const idx = this._mentionIdx >= 0 ? this._mentionIdx : 0;
          this._acceptMention(this._mentionMatches[idx] ?? "", input, select, mentionPanel, ghost);
          return;
        }
      }

      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._closeMentionPanel(mentionPanel);
        this._sendMessage(input, select);
        input.style.height = "auto";
        this._clearGhost(input, ghost);
        return;
      }
      // Tab or → at end-of-line accepts suggestion
      if ((e.key === "Tab" || (e.key === "ArrowRight" && input.selectionStart === input.value.length))
          && this._inputSuggestion) {
        e.preventDefault();
        this._acceptSuggestion(input, ghost);
        return;
      }
      if (e.key === "ArrowUp" && !e.shiftKey) {
        e.preventDefault();
        this._historyUp(input);
        this._onInputChange(input, select, ghost, mentionPanel);
        return;
      }
      if (e.key === "ArrowDown" && !e.shiftKey) {
        e.preventDefault();
        this._historyDown(input);
        this._onInputChange(input, select, ghost, mentionPanel);
        return;
      }
      if (e.key === "Escape") {
        this._clearGhost(input, ghost);
        return;
      }
      if (!["ArrowUp", "ArrowDown", "Tab"].includes(e.key)) this._inputHistIdx = -1;
    });

    input.addEventListener("blur", () => {
      setTimeout(() => this._closeMentionPanel(mentionPanel), 150);
    });

    select.addEventListener("change", () => {
      this.chatTarget = select.value;
      input.placeholder = `Message @${select.value}…`;
    });

    inputWrap.append(mentionPanel, ghost, input, hint);

    const sendBtn = document.createElement("button");
    sendBtn.className = "af-send-btn";
    sendBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 13L13 7 1 1v4.5l8.5 1.5-8.5 1.5V13z" fill="currentColor"/></svg>`;
    sendBtn.addEventListener("click", () => {
      this._closeMentionPanel(mentionPanel);
      this._sendMessage(input, select);
      this._clearGhost(input, ghost);
    });

    // Wake button hidden for 0.5 — create with hidden id so IOBar refs don't throw
    const wakeBtn = document.createElement("button");
    wakeBtn.id = "af-wake-btn-cd";
    wakeBtn.style.display = "none";

    const micBtn = document.createElement("button");
    micBtn.className = "af-voice-btn";
    micBtn.id = "af-mic-btn-cd";
    micBtn.title = "Tap to speak";
    micBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true"><path d="M7.5 1.5a2.5 2.5 0 0 0-2.5 2.5v4a2.5 2.5 0 0 0 5 0V4a2.5 2.5 0 0 0-2.5-2.5Z" fill="currentColor"/><path d="M3 7.5a4.5 4.5 0 0 0 9 0" stroke="currentColor" stroke-width="1.25" stroke-linecap="round"/><line x1="7.5" y1="12" x2="7.5" y2="13.5" stroke="currentColor" stroke-width="1.25" stroke-linecap="round"/><line x1="5" y1="13.5" x2="10" y2="13.5" stroke="currentColor" stroke-width="1.25" stroke-linecap="round"/></svg>`;
    micBtn.addEventListener("click", () =>
      document.dispatchEvent(new CustomEvent("af-mic-toggle")),
    );

    if ((document.body as any).__voiceUnavailable) {
      micBtn.style.display = "none";
    }

    bar.append(wakeBtn, micBtn, select, inputWrap, sendBtn);
    return bar;
  }

  private _populateSelect(select: HTMLSelectElement): void {
    const PRIORITY = ["main", "main-actor", "home-assistant-agent", "catalog"];
    select.innerHTML = "";
    [...this.agents.values()]
      .filter(canDirectMessage)
      .sort((a, b) => {
        const ai = PRIORITY.indexOf(a.name);
        const bi = PRIORITY.indexOf(b.name);
        if (ai !== -1 && bi !== -1) return ai - bi;
        if (ai !== -1) return -1;
        if (bi !== -1) return 1;
        return a.name.localeCompare(b.name);
      })
      .forEach((agent) => {
        const opt = document.createElement("option");
        opt.value = agent.name;
        opt.textContent = `@${agent.name}`;
        select.appendChild(opt);
      });
    select.value = this.chatTarget;
  }

  private _updateTargetSelect(): void {
    const select =
      this.root.querySelector<HTMLSelectElement>("#af-target-select");
    if (select) this._populateSelect(select);
    const input = this.root.querySelector<HTMLTextAreaElement>("#af-iobar-input");
    if (input) input.placeholder = `Message @${this.chatTarget}…`;
  }

  private _sendMessage(
    input: HTMLTextAreaElement,
    select: HTMLSelectElement,
  ): void {
    const content = input.value.trim();
    if (!content) return;
    this._inputHistory.unshift(content);
    if (this._inputHistory.length > 50) this._inputHistory.pop();
    this._inputHistIdx = -1;
    this._inputDraft = "";
    this._inputSuggestion = "";
    input.classList.remove("has-suggestion");
    const ghost = input.previousElementSibling as HTMLElement | null;
    if (ghost?.classList.contains("af-input-ghost")) ghost.textContent = "";
    const target = select.value || "main-actor";
    this.chatTarget = target;
    this._lastSentTarget = target;
    const msg: ChatMessage = {
      id: `user-${Date.now()}`,
      from: "user",
      to: target,
      content,
      timestampMs: Date.now(),
    };
    this.chatMessages.push(msg);
    if (this.view !== "chat") {
      this.view = "chat";
      this._renderView();
    } else {
      this._appendChatMsgEl(msg);
      this._scrollThread();
    }
    input.value = "";
    input.style.height = "auto";
    this._selfDispatching = true;
    document.dispatchEvent(
      new CustomEvent("af-send-message", { detail: { content, target } }),
    );
    this._selfDispatching = false;
  }

  private _historyUp(input: HTMLTextAreaElement): void {
    if (this._inputHistory.length === 0) return;
    if (this._inputHistIdx === -1) this._inputDraft = input.value;
    this._inputHistIdx = Math.min(this._inputHistIdx + 1, this._inputHistory.length - 1);
    input.value = this._inputHistory[this._inputHistIdx] ?? "";
    input.setSelectionRange(input.value.length, input.value.length);
  }

  private _historyDown(input: HTMLTextAreaElement): void {
    if (this._inputHistIdx === -1) return;
    this._inputHistIdx--;
    input.value = this._inputHistIdx === -1
      ? this._inputDraft
      : (this._inputHistory[this._inputHistIdx] ?? "");
    input.setSelectionRange(input.value.length, input.value.length);
  }

  // ── Private: autosuggestion + @mention ───────────────────────────────────

  private _onInputChange(
    input: HTMLTextAreaElement,
    select: HTMLSelectElement,
    ghost: HTMLElement,
    mentionPanel: HTMLElement,
  ): void {
    const val = input.value;
    // @mention detection: `@` anywhere at the end of the current word
    const mentionMatch = /@(\w*)$/.exec(val);
    if (mentionMatch) {
      this._openMentionPanel(mentionMatch[1] ?? "", mentionPanel, select, input);
      this._clearGhost(input, ghost);
      return;
    }
    this._closeMentionPanel(mentionPanel);
    this._updateGhost(input, ghost);
  }

  private _updateGhost(input: HTMLTextAreaElement, ghost: HTMLElement): void {
    const val = input.value;
    if (!val.trim()) { this._clearGhost(input, ghost); return; }
    const lower = val.toLowerCase();
    const match = this._inputHistory.find(
      (h) => h.toLowerCase().startsWith(lower) && h !== val,
    );
    if (match) {
      this._inputSuggestion = match;
      const typed = document.createElement("span");
      typed.style.color = "transparent";
      typed.textContent = val;
      const tail = document.createElement("span");
      tail.textContent = match.slice(val.length);
      ghost.textContent = "";
      ghost.append(typed, tail);
      input.classList.add("has-suggestion");
    } else {
      this._clearGhost(input, ghost);
    }
  }

  private _clearGhost(input: HTMLTextAreaElement, ghost: HTMLElement): void {
    this._inputSuggestion = "";
    ghost.textContent = "";
    input.classList.remove("has-suggestion");
  }

  private _acceptSuggestion(input: HTMLTextAreaElement, ghost: HTMLElement): void {
    if (!this._inputSuggestion) return;
    input.value = this._inputSuggestion;
    input.setSelectionRange(input.value.length, input.value.length);
    this._clearGhost(input, ghost);
    // trigger autoGrow after filling
    input.dispatchEvent(new Event("input"));
  }

  private _openMentionPanel(
    query: string,
    panel: HTMLElement,
    select: HTMLSelectElement,
    input: HTMLTextAreaElement,
  ): void {
    const all = [...this.agents.values()].map((a) => a.name).filter(Boolean);
    this._mentionMatches = query
      ? all.filter((n) => n.toLowerCase().startsWith(query.toLowerCase()))
      : all;
    if (this._mentionMatches.length === 0) { this._closeMentionPanel(panel); return; }
    this._mentionIdx = 0;
    this._mentionOpen = true;
    this._renderMentionChips(panel, select, input);
    panel.classList.add("open");
  }

  private _renderMentionChips(
    panel: HTMLElement,
    select: HTMLSelectElement,
    input: HTMLTextAreaElement,
  ): void {
    panel.textContent = "";
    this._mentionMatches.forEach((name, i) => {
      const chip = document.createElement("button");
      chip.className = "af-mention-chip" + (i === this._mentionIdx ? " active" : "");
      chip.textContent = name;
      chip.addEventListener("mousedown", (e) => {
        e.preventDefault();
        this._acceptMention(name, input, select, panel,
          panel.previousElementSibling as HTMLElement);
      });
      panel.appendChild(chip);
    });
  }

  private _acceptMention(
    name: string,
    input: HTMLTextAreaElement,
    select: HTMLSelectElement,
    panel: HTMLElement,
    ghost: HTMLElement,
  ): void {
    if (!name) return;
    // Replace the trailing @query with nothing (target is now set via select)
    input.value = input.value.replace(/@\w*$/, "").trimEnd();
    // Update the target select to point to this agent
    const opt = [...select.options].find((o) => o.value === name || o.text === name);
    if (opt) { select.value = opt.value; this.chatTarget = opt.value; }
    input.placeholder = `Message @${name}…`;
    this._closeMentionPanel(panel);
    if (ghost) this._clearGhost(input, ghost);
    input.focus();
    input.dispatchEvent(new Event("input"));
  }

  private _closeMentionPanel(panel: HTMLElement): void {
    this._mentionOpen = false;
    this._mentionIdx = -1;
    this._mentionMatches = [];
    panel.classList.remove("open");
  }

  // ── Private: API calls ────────────────────────────────────────────────────

  private _sendCommand(
    id: string,
    action: "pause" | "resume" | "stop" | "delete",
    btn?: HTMLButtonElement,
  ): void {
    if (btn) {
      btn.disabled = true;
      btn.classList.add("sending");
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("sending");
      }, 600);
    }
    document.dispatchEvent(
      new CustomEvent("af-agent-command", {
        detail: { command: action, agentId: id },
      }),
    );
  }

  // ── Private: Home Assistant view ─────────────────────────────────────────

  private _buildHAView(): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-overview";

    if (!this.haUrl || !this.haToken) {
      el.appendChild(this._buildHAConfigForm());
      return el;
    }

    el.innerHTML = `
      <div class="af-panel" style="height:100%;display:flex;flex-direction:column;overflow:hidden;">
        <div class="af-panel-head" style="display:flex;justify-content:space-between;align-items:center;flex-shrink:0;">
          <h3>Home Assistant Devices</h3>
          <div style="display:flex;align-items:center;gap:8px;">
            <a id="ha-open-link" href="${this.haUrl}" target="_blank" rel="noopener"
               style="font-size:11px;opacity:0.6;color:inherit;text-decoration:none;display:flex;align-items:center;gap:4px;">
              ${this.haUrl} ↗
            </a>
            <button id="ha-reconfigure-btn" class="af-mini-btn" style="font-size:10px;">⚙ Configure</button>
          </div>
        </div>
        <div id="ha-devices-container" style="flex:1;overflow-y:auto;overflow-x:hidden;">
          <div style="color:rgba(255,255,255,0.4);text-align:center;grid-column:1/-1;margin-top:40px;">
            Connecting to Home Assistant...
          </div>
        </div>
      </div>
    `;

    el.querySelector("#ha-reconfigure-btn")?.addEventListener("click", () => {
      const panel = el.querySelector<HTMLElement>(".af-panel");
      if (panel) {
        panel.innerHTML = "";
        panel.appendChild(this._buildHAConfigForm());
      }
    });

    return el;
  }

  private _buildHAConfigForm(): HTMLElement {
    // Strip protocol from stored URL so we show just the host in the input
    const storedUrl = this.haUrl ?? "";
    const storedHost = storedUrl.replace(/^https?:\/\//, "");
    const storedTls = storedUrl.startsWith("https://");

    const form = document.createElement("div");
    form.className = "af-panel";
    form.style.cssText =
      "max-width:420px;margin:40px auto;display:flex;flex-direction:column;gap:16px;";
    form.innerHTML = `
      <div class="af-panel-head"><h3>Home Assistant</h3></div>
      <p style="font-size:12px;opacity:0.6;margin:0;">Enter your Home Assistant host and a long-lived access token.<br>These are stored locally in your browser only.</p>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Host / IP
        <input id="ha-cfg-url" type="text" placeholder="192.168.1.2:8123 or ha.example.com/ha"
          value="${storedHost}"
          style="background:#1a2230;border:1px solid #2a3a50;border-radius:4px;padding:8px 10px;color:#e2e8f0;font-size:13px;outline:none;">
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;">
        <input id="ha-cfg-tls" type="checkbox" ${storedTls ? "checked" : ""}
          style="width:14px;height:14px;accent-color:#38bdf8;">
        Use HTTPS (TLS)
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Long-lived access token
        <input id="ha-cfg-token" type="password" placeholder="eyJ..."
          value="${this.haToken ?? ""}"
          style="background:#1a2230;border:1px solid #2a3a50;border-radius:4px;padding:8px 10px;color:#e2e8f0;font-size:13px;outline:none;">
      </label>
      <div style="display:flex;gap:8px;">
        <button id="ha-cfg-save" class="af-mini-btn" style="flex:1;padding:8px;">Save</button>
        ${storedHost ? `<button id="ha-cfg-clear" class="af-mini-btn danger" style="padding:8px 12px;" title="Remove saved credentials">Reset</button>` : ""}
      </div>
      <div id="ha-cfg-msg" style="font-size:12px;min-height:16px;"></div>
    `;

    form.querySelector("#ha-cfg-save")?.addEventListener("click", () => {
      let raw = (
        form.querySelector<HTMLInputElement>("#ha-cfg-url")?.value ?? ""
      ).trim();
      // Detect TLS from explicit protocol prefix (ws/wss/http/https)
      let detectedTls: boolean | null = null;
      if (/^(https|wss):\/\//i.test(raw)) detectedTls = true;
      else if (/^(http|ws):\/\//i.test(raw)) detectedTls = false;
      // Strip any protocol prefix — we re-add http[s] for storage
      raw = raw.replace(/^(https?|wss?):\/\//i, "").replace(/\/$/, "");
      const tlsCheckbox =
        form.querySelector<HTMLInputElement>("#ha-cfg-tls")?.checked ?? false;
      const tls = detectedTls ?? tlsCheckbox;
      const url = raw ? `${tls ? "https" : "http"}://${raw}` : "";
      const token = (
        form.querySelector<HTMLInputElement>("#ha-cfg-token")?.value ?? ""
      ).trim();
      const msg = form.querySelector<HTMLElement>("#ha-cfg-msg")!;
      if (!url || !token) {
        msg.style.color = "#f87171";
        msg.textContent = "Both fields required.";
        return;
      }
      localStorage.setItem("wactorz-ha-url", url);
      localStorage.setItem("wactorz-ha-token", token);
      msg.style.color = "#34d399";
      msg.textContent = "Saved — reloading…";
      this._initHAClient();
      setTimeout(() => this._setView("ha"), 600);
    });

    form.querySelector("#ha-cfg-clear")?.addEventListener("click", () => {
      localStorage.removeItem("wactorz-ha-url");
      localStorage.removeItem("wactorz-ha-token");
      this._initHAClient();
      this._setView("ha");
    });

    return form;
  }

  private _renderHADevices(entities: HAEntity[]): void {
    const container = this.root.querySelector<HTMLElement>("#ha-devices-container");
    if (!container) return;
    container.innerHTML = "";
    container.style.display = "flex";
    container.style.flexDirection = "column";
    container.style.gap = "0";

    // ── Domain classification ────────────────────────────────────────────────
    const DEVICE_DOMAINS = new Set([
      "light", "switch", "fan", "climate", "cover", "lock",
      "media_player", "vacuum", "camera", "alarm_control_panel",
      "remote", "humidifier", "siren", "water_heater", "valve",
      "lawn_mower", "button", "number", "select",
    ]);
    const CAP_DOMAINS = new Set(["sensor", "binary_sensor"]);
    const domainOf = (e: HAEntity) => e.entity_id.split(".")[0] || "";

    // ── Build registry maps ──────────────────────────────────────────────────
    const reg = this._haRegistries;
    const areaById = new Map<string, import("../io/HAClient").HAArea>();
    const entityDeviceId = new Map<string, string>();  // entity_id → device_id
    const entityAreaId   = new Map<string, string>();  // entity_id → area_id (direct)
    const deviceAreaId   = new Map<string, string>();  // device_id → area_id

    if (reg) {
      reg.areas.forEach((a) => areaById.set(a.area_id, a));
      reg.deviceEntries.forEach((d) => { if (d.area_id) deviceAreaId.set(d.id, d.area_id); });
      reg.entityEntries.forEach((entry) => {
        if (entry.device_id) entityDeviceId.set(entry.entity_id, entry.device_id);
        if (entry.area_id)   entityAreaId.set(entry.entity_id, entry.area_id);
      });
    }

    const getAreaId = (entityId: string): string => {
      if (entityAreaId.has(entityId)) return entityAreaId.get(entityId)!;
      const devId = entityDeviceId.get(entityId);
      if (devId && deviceAreaId.has(devId)) return deviceAreaId.get(devId)!;
      return "";
    };

    // ── Group capability entities by device_id ───────────────────────────────
    const capsByDevice = new Map<string, HAEntity[]>();
    entities
      .filter((e) => CAP_DOMAINS.has(domainOf(e)))
      .forEach((e) => {
        const devId = entityDeviceId.get(e.entity_id);
        if (!devId) return;
        if (!capsByDevice.has(devId)) capsByDevice.set(devId, []);
        capsByDevice.get(devId)!.push(e);
      });

    // ── Filter to device-domain entities and group by area ──────────────────
    const deviceEntities = entities.filter((e) => DEVICE_DOMAINS.has(domainOf(e)));

    if (deviceEntities.length === 0) {
      container.innerHTML = `<div style="color:rgba(255,255,255,0.4);text-align:center;padding:40px;">No devices found.</div>`;
      return;
    }

    const byArea = new Map<string, HAEntity[]>();
    deviceEntities.forEach((e) => {
      const aId = getAreaId(e.entity_id);
      if (!byArea.has(aId)) byArea.set(aId, []);
      byArea.get(aId)!.push(e);
    });

    // Named areas first (sorted by name), no-room section last
    const sortedAreaIds = [...byArea.keys()].sort((a, b) => {
      if (!a && !b) return 0;
      if (!a) return 1;
      if (!b) return -1;
      return (areaById.get(a)?.name ?? a).localeCompare(areaById.get(b)?.name ?? b);
    });

    // ── Render sections ──────────────────────────────────────────────────────
    sortedAreaIds.forEach((areaId) => {
      const area = areaById.get(areaId);
      const sectionEntities = (byArea.get(areaId) ?? []).sort((a, b) =>
        (a.attributes.friendly_name ?? a.entity_id).localeCompare(
          b.attributes.friendly_name ?? b.entity_id,
        ),
      );

      const section = document.createElement("div");
      section.style.marginBottom = "4px";

      // Room header
      const header = document.createElement("div");
      header.style.cssText =
        "padding:8px 16px 6px;font-size:10px;font-weight:700;letter-spacing:0.08em;" +
        "color:rgba(255,255,255,0.35);text-transform:uppercase;display:flex;align-items:center;gap:6px;" +
        "border-bottom:1px solid rgba(255,255,255,0.06);";
      const roomIcon = area?.icon ? String.fromCodePoint(parseInt(area.icon.replace(/^mdi:/, ""), 16)) : "🏠";
      header.innerHTML = `<span>${area ? roomIcon : "📦"}</span><span>${area?.name ?? "Other"}</span>` +
        `<span style="opacity:0.4;font-weight:400">${sectionEntities.length}</span>`;
      section.appendChild(header);

      // Device rows
      sectionEntities.forEach((e) => {
        const devId = entityDeviceId.get(e.entity_id);
        const caps = devId ? (capsByDevice.get(devId) ?? []) : [];
        section.appendChild(this._buildHADeviceRow(e, caps));
      });

      container.appendChild(section);
    });
  }

  private _buildHADeviceRow(e: HAEntity, capabilities: HAEntity[]): HTMLElement {
    const domain = e.entity_id.split(".")[0] || "";
    const isActive = ["on","playing","cool","heat","open","active","detected","home","locked"].includes(e.state);
    const isAlert  = ["problem","error","critical","warning","emergency"].includes(e.state);
    const stateColor = isAlert ? "#f87171" : isActive ? "#34d399" : "rgba(255,255,255,0.35)";

    const wrapper = document.createElement("div");

    // ── Row ──────────────────────────────────────────────────────────────────
    const row = document.createElement("div");
    row.style.cssText =
      "display:flex;align-items:center;gap:10px;padding:9px 16px;" +
      "border-bottom:1px solid rgba(255,255,255,0.04);transition:background 0.12s;";
    row.addEventListener("mouseenter", () => { row.style.background = "rgba(255,255,255,0.04)"; });
    row.addEventListener("mouseleave", () => { row.style.background = ""; });

    // Icon
    const iconEl = document.createElement("span");
    iconEl.textContent = this._getDomainIcon(domain);
    iconEl.style.cssText = "font-size:16px;width:22px;text-align:center;flex-shrink:0;";

    // Name
    const nameEl = document.createElement("div");
    nameEl.style.cssText = "flex:1;min-width:0;font-size:13px;font-weight:500;" +
      "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
    nameEl.textContent = e.attributes.friendly_name || e.entity_id;

    // State
    const stateEl = document.createElement("span");
    stateEl.style.cssText = `font-size:11px;font-weight:600;white-space:nowrap;color:${stateColor};flex-shrink:0;`;
    stateEl.textContent = e.state + (e.attributes.unit_of_measurement ? " " + e.attributes.unit_of_measurement : "");

    row.append(iconEl, nameEl, stateEl);

    // Quick toggle (for toggleable domains)
    const TOGGLEABLE = new Set(["light","switch","fan","humidifier","vacuum","input_boolean"]);
    if (TOGGLEABLE.has(domain)) {
      const toggle = document.createElement("button");
      toggle.title = isActive ? "Turn off" : "Turn on";
      toggle.style.cssText =
        `width:28px;height:16px;border-radius:8px;border:none;cursor:pointer;flex-shrink:0;position:relative;` +
        `background:${isActive ? "#34d399" : "rgba(255,255,255,0.15)"};transition:background 0.2s;`;
      const thumb = document.createElement("div");
      thumb.style.cssText =
        `position:absolute;top:2px;width:12px;height:12px;border-radius:50%;background:#fff;transition:left 0.2s;` +
        `left:${isActive ? "14px" : "2px"};`;
      toggle.appendChild(thumb);
      toggle.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this.haClient?.toggleEntity(e.entity_id);
      });
      row.appendChild(toggle);
    }

    // Expand chevron (shown when there's a detail panel)
    const hasControls = this._entityHasControls(e, domain);
    const hasDetail = hasControls || capabilities.length > 0;

    if (hasDetail) {
      const chevron = document.createElement("span");
      chevron.textContent = "›";
      chevron.style.cssText = "color:rgba(255,255,255,0.25);font-size:18px;flex-shrink:0;transition:transform 0.18s;line-height:1;";
      row.appendChild(chevron);
      row.style.cursor = "pointer";

      // ── Detail panel (lazy-rendered on first expand) ─────────────────────
      const detail = document.createElement("div");
      detail.style.cssText =
        "display:none;padding:10px 16px 14px 48px;" +
        "background:rgba(255,255,255,0.02);border-bottom:1px solid rgba(255,255,255,0.05);";
      let detailRendered = false;

      row.addEventListener("click", () => {
        const open = detail.style.display !== "none";
        detail.style.display = open ? "none" : "block";
        chevron.style.transform = open ? "rotate(0deg)" : "rotate(90deg)";

        if (!open && !detailRendered) {
          detailRendered = true;

          // Controls
          if (hasControls) {
            const ctrlDiv = document.createElement("div");
            ctrlDiv.style.cssText = "display:flex;flex-direction:column;gap:8px;margin-bottom:capabilities.length > 0 ? '10px' : '0';";
            this._appendEntityControls(ctrlDiv, e, isActive);
            if (ctrlDiv.children.length > 0) detail.appendChild(ctrlDiv);
          }

          // Capability badges
          if (capabilities.length > 0) {
            const capWrap = document.createElement("div");
            capWrap.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;";
            capabilities.forEach((cap) => {
              const badge = document.createElement("span");
              const capName = cap.attributes.friendly_name || cap.entity_id.split(".").pop() || cap.entity_id;
              const capState = cap.state + (cap.attributes.unit_of_measurement ? " " + cap.attributes.unit_of_measurement : "");
              badge.style.cssText =
                "font-size:10px;padding:2px 8px;border-radius:4px;" +
                "background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.45);white-space:nowrap;";
              badge.textContent = `${capName}: ${capState}`;
              capWrap.appendChild(badge);
            });
            detail.appendChild(capWrap);
          }
        }
      });

      wrapper.appendChild(detail);
    }

    wrapper.insertBefore(row, wrapper.firstChild);
    return wrapper;
  }

  private _entityHasControls(e: HAEntity, domain: string): boolean {
    if (["light","switch","fan","humidifier","vacuum","input_boolean","climate","cover","media_player"].includes(domain)) return true;
    return false;
  }

  private _getDomainIcon(domain: string): string {
    const icons: Record<string, string> = {
      light: "💡",
      switch: "🔌",
      sensor: "🌡",
      binary_sensor: "🔔",
      media_player: "📺",
      climate: "❄",
      camera: "📷",
      fan: "🌀",
      vacuum: "🧹",
      cover: "🚪",
      lock: "🔒",
      drone: "🚁",
      person: "👤",
      device_tracker: "📍",
      sun: "☀️",
    };
    return icons[domain] || "📦";
  }

  private _appendEntityControls(
    container: HTMLElement,
    e: HAEntity,
    isActive: boolean,
  ): void {
    const domain = e.entity_id.split(".")[0] || "";

    // Toggleable items
    if (
      [
        "light",
        "switch",
        "fan",
        "input_boolean",
        "humidifier",
        "vacuum",
      ].includes(domain)
    ) {
      const btn = document.createElement("button");
      btn.className = "af-mini-btn";
      btn.textContent = isActive ? "Turn Off" : "Turn On";
      btn.style.width = "100%";
      btn.addEventListener("click", () =>
        this.haClient?.toggleEntity(e.entity_id),
      );
      container.appendChild(btn);
    }

    // Dimmable Light
    if (
      domain === "light" &&
      e.attributes.supported_color_modes?.some((m: string) => m !== "onoff")
    ) {
      this._addSlider(
        container,
        "Brightness",
        0,
        255,
        e.attributes.brightness || 0,
        (val) => {
          this.haClient?.callService("light", "turn_on", {
            entity_id: e.entity_id,
            brightness: val,
          });
        },
        (v) => Math.round((v / 255) * 100) + "%",
      );
    }

    // Color Light
    if (
      domain === "light" &&
      e.attributes.supported_color_modes?.includes("rgb")
    ) {
      this._addColorPicker(container, e);
    }

    // Climate (Thermostat)
    if (domain === "climate") {
      const target =
        e.attributes.temperature || e.attributes.target_temp_low || 20;
      this._addSlider(
        container,
        "Target Temp",
        15,
        30,
        target,
        (val) => {
          this.haClient?.callService("climate", "set_temperature", {
            entity_id: e.entity_id,
            temperature: val,
          });
        },
        (v) => v + "°",
      );
    }

    // Covers (Blinds/Doors)
    if (domain === "cover") {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.gap = "4px";
      ["open_cover", "stop_cover", "close_cover"].forEach((svc) => {
        const btn = document.createElement("button");
        btn.className = "af-mini-btn";
        btn.textContent = (svc.split("_")[0] || "ACTION").toUpperCase();
        btn.style.flex = "1";
        btn.addEventListener("click", () =>
          this.haClient?.callService("cover", svc, { entity_id: e.entity_id }),
        );
        row.appendChild(btn);
      });
      container.appendChild(row);
    }

    // Media Player
    if (domain === "media_player") {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.gap = "4px";
      const playPause = document.createElement("button");
      playPause.className = "af-mini-btn";
      playPause.textContent = e.state === "playing" ? "⏸" : "▶";
      playPause.style.flex = "1";
      playPause.addEventListener("click", () => {
        const svc = e.state === "playing" ? "media_pause" : "media_play";
        this.haClient?.callService("media_player", svc, {
          entity_id: e.entity_id,
        });
      });
      row.appendChild(playPause);
      container.appendChild(row);

      if (e.attributes.volume_level != null) {
        this._addSlider(
          container,
          "Volume",
          0,
          100,
          Math.round(e.attributes.volume_level * 100),
          (val) => {
            this.haClient?.callService("media_player", "volume_set", {
              entity_id: e.entity_id,
              volume_level: val / 100,
            });
          },
          (v) => v + "%",
        );
      }
    }
  }

  private _addSlider(
    container: HTMLElement,
    labelText: string,
    min: number,
    max: number,
    current: number,
    onChange: (val: number) => void,
    format?: (v: number) => string,
  ): void {
    const wrap = document.createElement("div");
    wrap.style.display = "flex";
    wrap.style.flexDirection = "column";
    wrap.style.gap = "2px";

    const lbl = document.createElement("div");
    lbl.style.fontSize = "9px";
    lbl.style.color = "rgba(255,255,255,0.4)";
    lbl.textContent = `${labelText}: ${format ? format(current) : current}`;

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = String(min);
    slider.max = String(max);
    slider.value = String(current);
    slider.style.width = "100%";
    slider.style.accentColor = "#34d399";
    slider.addEventListener("change", () => {
      const val = parseInt(slider.value, 10);
      if (format) lbl.textContent = `${labelText}: ${format(val)}`;
      onChange(val);
    });

    wrap.append(lbl, slider);
    container.appendChild(wrap);
  }

  private _addColorPicker(container: HTMLElement, e: HAEntity): void {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.gap = "8px";
    const lbl = document.createElement("div");
    lbl.style.fontSize = "9px";
    lbl.style.color = "rgba(255,255,255,0.4)";
    lbl.textContent = "Color:";

    const picker = document.createElement("input");
    picker.type = "color";
    picker.style.border = "none";
    picker.style.width = "20px";
    picker.style.height = "20px";
    picker.style.background = "none";
    picker.style.cursor = "pointer";

    if (e.attributes.rgb_color) {
      const [r, g, b] = e.attributes.rgb_color;
      picker.value = `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
    }

    picker.addEventListener("change", () => {
      const hex = picker.value;
      const r = parseInt(hex.slice(1, 3), 16),
        g = parseInt(hex.slice(3, 5), 16),
        b = parseInt(hex.slice(5, 7), 16);
      this.haClient?.callService("light", "turn_on", {
        entity_id: e.entity_id,
        rgb_color: [r, g, b],
      });
    });
    row.append(lbl, picker);
    container.appendChild(row);
  }

  // ── Private: timestamp refresh ────────────────────────────────────────────

  private _refreshTimestamps(): void {
    const now = Date.now();
    const STALE_MS = 180_000; // matches nodes panel threshold
    this.lastHb.forEach((ms, id) => {
      const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
      if (!card) return;
      const el = card.querySelector<HTMLElement>(".af-card-hb-time");
      if (el) el.textContent = relTime(ms);
      const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
      if (dot) dot.classList.toggle("af-card-stale", now - ms > STALE_MS);
    });
  }

  // ── Private: DOM skeleton ─────────────────────────────────────────────────

  private buildRoot(): HTMLElement {
    const root = document.createElement("div");
    root.id = "card-dashboard";
    root.className = "cd-root";

    // Header
    const header = document.createElement("div");
    header.className = "af-header";

    const left = document.createElement("div");
    left.className = "af-header-left";

    const icon = document.createElement("img");
    icon.src = "./favicon.svg";
    icon.width = 22;
    icon.height = 22;
    icon.alt = "Wactorz";
    icon.style.opacity = "0.9";
    left.appendChild(icon);

    const title = document.createElement("span");
    title.className = "af-title";
    title.textContent = "Wactorz";
    left.appendChild(title);

    const connBadge = document.createElement("span");
    connBadge.className = `af-conn-badge af-conn-${this.connState}`;
    connBadge.textContent = "○ Connecting…";
    left.appendChild(connBadge);

    const center = document.createElement("div");
    center.className = "af-header-center";
    const health = document.createElement("span");
    health.className = "af-health";
    health.textContent = "0/0 wa healthy";
    center.appendChild(health);

    const right = document.createElement("div");
    right.className = "af-header-right";

    const views: { key: View; label: string }[] = [
      { key: "overview", label: "◫ Overview" },
      { key: "feed", label: "≡ Feed" },
      { key: "chat", label: "💬 Chat" },
    ];

    views.push({ key: "ha", label: "🏠 Devices" });
    views.push({ key: "fuseki", label: "⬡ Graph" });
    views.push({ key: "settings", label: "⚙ Settings" });

    views.forEach(({ key, label }) => {
      const btn = document.createElement("button");
      btn.className = `af-view-btn${key === this.view ? " active" : ""}`;
      btn.dataset["view"] = key;
      btn.textContent = label;
      btn.addEventListener("click", () => this._setView(key));
      right.appendChild(btn);
    });

    // 🔊 Audio button → glass popover with all sound controls
    const audioBtn = document.createElement("button");
    audioBtn.className = "af-view-btn";
    audioBtn.title = "Audio settings";
    audioBtn.textContent = "🔊";
    right.appendChild(audioBtn);

    const popover = this._buildAudioPopover();
    document.body.appendChild(popover);

    audioBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = popover.classList.toggle("open");
      if (open) {
        const r = audioBtn.getBoundingClientRect();
        popover.style.top  = `${r.bottom + 6}px`;
        popover.style.right = `${window.innerWidth - r.right}px`;
      }
    });
    document.addEventListener("click", (e) => {
      if (!popover.contains(e.target as Node)) popover.classList.remove("open");
    });

    // ↺ Reset button → popover with scope choices
    const resetBtn = document.createElement("button");
    resetBtn.className = "af-view-btn";
    resetBtn.title = "Clear stored state";
    resetBtn.textContent = "↺";
    right.appendChild(resetBtn);

    const resetPop = this._buildResetPopover();
    document.body.appendChild(resetPop);
    resetBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = resetPop.classList.toggle("open");
      if (open) {
        const r = resetBtn.getBoundingClientRect();
        resetPop.style.top   = `${r.bottom + 6}px`;
        resetPop.style.right = `${window.innerWidth - r.right}px`;
      }
    });
    document.addEventListener("click", (e) => {
      if (!resetPop.contains(e.target as Node)) resetPop.classList.remove("open");
    });

    header.append(left, center, right);

    const body = document.createElement("div");
    body.className = "af-body";

    const iobar = this._buildIobar();
    const bottomNav = this._buildBottomNav();

    root.append(header, body, bottomNav, iobar);
    return root;
  }

  // ── Private: Fuseki view ───────────────────────────────────────────────────

  private _buildFusekiView(): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-overview";

    if (!this.fusekiUrl) {
      el.appendChild(this._buildFusekiConfigForm());
      return el;
    }

    const base = this.fusekiUrl;
    const ds = this.fusekiDataset;

    // ── Preset queries ─────────────────────────────────────────────────────
    const PRESETS: { label: string; icon: string; sparql: string }[] = [
      {
        label: "All graphs",
        icon: "◌",
        sparql: `SELECT ?g (COUNT(*) AS ?triples) WHERE {
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?g ORDER BY ?g`,
      },
      {
        label: "Top predicates",
        icon: "≡",
        sparql: `SELECT ?p (COUNT(*) AS ?count) WHERE {
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?p ORDER BY DESC(?count) LIMIT 50`,
      },
      {
        label: "Sample triples",
        icon: "⋯",
        sparql: `SELECT ?g ?s ?p ?o WHERE {
  GRAPH ?g { ?s ?p ?o }
} LIMIT 50`,
      },
      {
        label: "Current states",
        icon: "◉",
        sparql: `SELECT ?g ?entity ?state ?unit WHERE {
  VALUES ?g { <urn:ha:current> <urn:wactorz:current> }
  GRAPH ?g {
    ?entity ?statePred ?state .
    FILTER(?statePred IN (syn:state, saref:hasState))
    OPTIONAL {
      ?entity ?unitPred ?unit .
      FILTER(?unitPred IN (syn:unit, saref:hasUnitOfMeasure))
    }
  }
} ORDER BY ?entity LIMIT 200`,
      },
      {
        label: "Recent observations",
        icon: "⏱",
        sparql: `SELECT ?g ?obs ?entity ?result ?ts WHERE {
  VALUES ?g { <urn:ha:history> <urn:wactorz:history> }
  GRAPH ?g {
    ?obs a sosa:Observation .
    OPTIONAL { ?obs sosa:madeBySensor ?entity . }
    OPTIONAL { ?obs sosa:hasSimpleResult ?result . }
    OPTIONAL { ?obs sosa:resultTime ?ts . }
  }
} ORDER BY DESC(?ts) LIMIT 100`,
      },
      {
        label: "Device catalog",
        icon: "⊡",
        sparql: `SELECT ?entity ?label ?state ?area WHERE {
  GRAPH <urn:ha:devices> {
    ?entity rdfs:label ?label ;
            syn:state   ?state .
    OPTIONAL { ?entity syn:areaName ?area . }
    FILTER(!STRSTARTS(STR(?entity), "urn:ha:bridge:"))
  }
} ORDER BY ?area ?label LIMIT 500`,
      },
      {
        label: "Agents",
        icon: "⚙",
        sparql: `SELECT ?entity ?label ?state ?protected ?actorId WHERE {
  GRAPH <urn:wactorz:agents> {
    ?entity rdfs:label ?label .
    OPTIONAL { ?entity syn:state ?state . }
    OPTIONAL { ?entity syn:protected ?protected . }
    OPTIONAL { ?entity syn:actorId ?actorId . }
  }
} ORDER BY ?label LIMIT 200`,
      },
      {
        label: "Sensors with units",
        icon: "📡",
        sparql: `SELECT ?g ?entity ?state ?unit WHERE {
  VALUES ?g { <urn:ha:current> <urn:wactorz:current> }
  GRAPH ?g {
    ?entity a sosa:Sensor ;
            ?statePred ?state .
    FILTER(?statePred IN (syn:state, saref:hasState))
    OPTIONAL {
      ?entity ?unitPred ?unit .
      FILTER(?unitPred IN (syn:unit, saref:hasUnitOfMeasure))
    }
  }
} ORDER BY ?entity LIMIT 200`,
      },
      {
        label: "Graph sizes",
        icon: "∑",
        sparql: `SELECT ?g (COUNT(*) AS ?triples) WHERE {
  VALUES ?g { <urn:ha:current> <urn:ha:history> <urn:ha:devices> <urn:wactorz:agents> }
  GRAPH ?g { ?s ?p ?o }
} GROUP BY ?g ORDER BY ?g`,
      },
    ];

    const wrapper = document.createElement("div");
    wrapper.style.cssText =
      "display:flex;flex-direction:column;gap:14px;height:100%;min-height:0;";

    // ── Header bar ─────────────────────────────────────────────────────────
    const hdr = document.createElement("div");
    hdr.style.cssText =
      "display:flex;align-items:center;gap:10px;flex-shrink:0;";
    hdr.innerHTML = `
      <span style="font-size:20px;line-height:1;">⬡</span>
      <span style="font-weight:700;font-size:14px;color:rgba(255,255,255,0.92);">Knowledge Graph</span>
      <span class="af-fuseki-ds-badge">${ds}</span>
      <a href="${base}" target="_blank" rel="noopener"
         style="font-size:11px;opacity:0.4;color:inherit;text-decoration:none;margin-left:2px;">${base} ↗</a>
      <div style="flex:1;"></div>
      <button id="fsk-reconfigure" class="af-mini-btn" style="font-size:10px;">⚙ Configure</button>
    `;
    hdr.querySelector("#fsk-reconfigure")?.addEventListener("click", () => {
      wrapper.innerHTML = "";
      wrapper.appendChild(this._buildFusekiConfigForm());
    });
    wrapper.appendChild(hdr);

    const hint = document.createElement("div");
    hint.style.cssText =
      "font-size:12px;line-height:1.45;color:rgba(255,255,255,0.6);padding:10px 12px;border:1px solid rgba(255,255,255,0.08);border-radius:10px;background:rgba(255,255,255,0.03);";
    hint.innerHTML =
      "This panel only shows data already stored in Fuseki. If the dataset is empty, all presets return 0 rows even when the endpoint is reachable.";
    wrapper.appendChild(hint);

    // ── Presets + editor row ───────────────────────────────────────────────
    const mainRow = document.createElement("div");
    mainRow.style.cssText =
      "display:flex;gap:14px;flex:1;min-height:0;overflow:hidden;";

    // Left: preset buttons
    const sidebar = document.createElement("div");
    sidebar.className = "af-fuseki-sidebar";
    PRESETS.forEach((p) => {
      const btn = document.createElement("button");
      btn.className = "af-fuseki-preset-btn";
      btn.innerHTML = `<span class="af-fuseki-preset-icon">${p.icon}</span><span>${p.label}</span>`;
      btn.addEventListener("click", () => {
        editor.value = p.sparql;
        void runQuery(p.sparql);
      });
      sidebar.appendChild(btn);
    });
    mainRow.appendChild(sidebar);

    // Right: editor + results
    const editorPanel = document.createElement("div");
    editorPanel.style.cssText =
      "flex:1;display:flex;flex-direction:column;gap:10px;min-width:0;overflow:hidden;";

    const editorRow = document.createElement("div");
    editorRow.style.cssText =
      "display:flex;gap:8px;align-items:flex-start;flex-shrink:0;";

    const editor = document.createElement("textarea");
    editor.className = "af-fuseki-editor";
    editor.spellcheck = false;
    editor.placeholder = "SELECT * WHERE { ?s ?p ?o } LIMIT 10";
    editor.rows = 6;
    editor.value = PRESETS[0]?.sparql ?? "";

    const runBtn = document.createElement("button");
    runBtn.className = "af-mini-btn af-fuseki-run-btn";
    runBtn.innerHTML = "▶ Run";
    editorRow.append(editor, runBtn);
    editorPanel.appendChild(editorRow);

    // Status line
    const status = document.createElement("div");
    status.className = "af-fuseki-status";
    status.textContent = "Ready.";
    editorPanel.appendChild(status);

    // Results
    const results = document.createElement("div");
    results.className = "af-fuseki-results";
    editorPanel.appendChild(results);

    mainRow.appendChild(editorPanel);
    wrapper.appendChild(mainRow);
    el.appendChild(wrapper);

    // ── SPARQL runner ──────────────────────────────────────────────────────
    const datasetPath = encodeURIComponent(ds);
    const _ingress: string = (window as any).__WACTORZ_INGRESS_PATH ?? "";
    const sparqlUrl = `${_ingress}/api/fuseki/${datasetPath}/sparql`;
    const updateUrl = `${_ingress}/api/fuseki/${datasetPath}/update`;

    const SPARQL_PREFIXES = `PREFIX syn:    <https://synapse.waldiez.io/ns#>
PREFIX sosa:   <http://www.w3.org/ns/sosa/>
PREFIX ssn:    <http://www.w3.org/ns/ssn/>
PREFIX saref:  <https://saref.etsi.org/core/>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
PREFIX prov:   <http://www.w3.org/ns/prov#>
`;

    const withPrefixes = (q: string): string => {
      // Only inject prefixes that aren't already declared in the query
      const declared = new Set(
        [...q.matchAll(/^\s*PREFIX\s+(\w*:)/gim)].map((m) => m[1]),
      );
      const needed = SPARQL_PREFIXES.split("\n")
        .filter((line) => {
          const m = line.match(/^PREFIX\s+(\w*:)/);
          return m && !declared.has(m[1]);
        })
        .join("\n");
      return needed ? needed + "\n" + q : q;
    };

    const runQuery = async (q: string): Promise<void> => {
      const trimmed = q.trim();
      if (!trimmed) return;

      status.textContent = "Running…";
      status.style.color = "rgba(255,255,255,0.4)";
      results.innerHTML = "";

      const isUpdate =
        /^\s*(INSERT|DELETE|DROP|CREATE|LOAD|CLEAR|ADD|MOVE|COPY)/i.test(
          trimmed,
        );
      const full = withPrefixes(trimmed);
      const headers: Record<string, string> = {};

      try {
        let resp: Response;
        if (isUpdate) {
          headers["Content-Type"] = "application/x-www-form-urlencoded";
          resp = await fetch(updateUrl, {
            method: "POST",
            headers,
            body: `update=${encodeURIComponent(full)}`,
          });
        } else {
          headers["Accept"] = "application/sparql-results+json";
          headers["Content-Type"] = "application/x-www-form-urlencoded";
          resp = await fetch(sparqlUrl, {
            method: "POST",
            headers,
            body: `query=${encodeURIComponent(full)}`,
          });
        }

        if (!resp.ok) {
          const text = await resp.text();
          status.textContent = `Error ${resp.status}`;
          status.style.color = "#f87171";
          results.innerHTML = `<pre class="af-fuseki-error">${text.slice(0, 600)}</pre>`;
          return;
        }

        if (isUpdate) {
          status.textContent = "Update OK";
          status.style.color = "#34d399";
          return;
        }

        const json = (await resp.json()) as {
          head: { vars: string[] };
          results?: {
            bindings: Record<string, { value: string; type: string }>[];
          };
          boolean?: boolean;
        };

        // ASK query
        if (typeof json.boolean === "boolean") {
          status.textContent = `Result: ${json.boolean}`;
          status.style.color = json.boolean ? "#34d399" : "#fbbf24";
          return;
        }

        const vars = json.head?.vars ?? [];
        const bindings = json.results?.bindings ?? [];
        status.textContent = `${bindings.length} row${bindings.length !== 1 ? "s" : ""}`;
        status.style.color = "rgba(255,255,255,0.45)";

        if (bindings.length === 0) {
          const looksLikeHaGraphQuery =
            /urn:ha:(current|history|devices)/i.test(trimmed);
          results.innerHTML = looksLikeHaGraphQuery
            ? `<div class="af-fuseki-empty">No results.<br><span style="opacity:0.65">Fuseki is reachable, but the expected HA graphs appear empty.</span></div>`
            : `<div class="af-fuseki-empty">No results.</div>`;
          return;
        }

        const table = document.createElement("table");
        table.className = "af-fuseki-table";

        const thead = table.createTHead();
        const hrow = thead.insertRow();
        vars.forEach((v) => {
          const th = document.createElement("th");
          th.textContent = v;
          hrow.appendChild(th);
        });

        const tbody = table.createTBody();
        bindings.forEach((row) => {
          const tr = tbody.insertRow();
          vars.forEach((v) => {
            const td = tr.insertCell();
            const cell = row[v];
            if (!cell) {
              td.textContent = "";
              return;
            }
            const val = cell.value;
            // shorten long URIs
            const display =
              val.length > 60
                ? `<span title="${val}">${val.slice(0, 58)}…</span>`
                : val;
            const isUri = cell.type === "uri";
            td.innerHTML = isUri
              ? `<span class="af-fuseki-uri">${display}</span>`
              : display;
          });
          tbody.appendChild(tr);
        });

        results.appendChild(table);
      } catch (err) {
        status.textContent = "Network error";
        status.style.color = "#f87171";
        results.innerHTML = `<pre class="af-fuseki-error">${String(err)}</pre>`;
      }
    };

    runBtn.addEventListener("click", () => void runQuery(editor.value));
    editor.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        void runQuery(editor.value);
      }
    });

    // Auto-run first preset
    void runQuery(PRESETS[0]?.sparql ?? "");

    return el;
  }

  private _buildFusekiConfigForm(): HTMLElement {
    const form = document.createElement("div");
    form.className = "af-panel";
    form.style.cssText =
      "max-width:440px;margin:40px auto;display:flex;flex-direction:column;gap:16px;";

    const stored = {
      url: this.fusekiUrl?.replace(/^https?:\/\//, "") ?? "",
      tls: (this.fusekiUrl ?? "").startsWith("https://"),
      ds: this.fusekiDataset,
      user: this.fusekiUser,
      pass: this.fusekiPass,
    };

    form.innerHTML = `
      <div class="af-panel-head">
        <h3>⬡ Knowledge Graph (Fuseki)</h3>
      </div>
      <p style="font-size:12px;opacity:0.6;margin:0;">
        Connect to an Apache Jena Fuseki instance.<br>
        Credentials are stored locally in your browser.
      </p>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Host / IP
        <input id="fsk-cfg-url" type="text" placeholder="localhost:3030"
          value="${stored.url}"
          class="af-cfg-input">
      </label>
      <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;">
        <input id="fsk-cfg-tls" type="checkbox" ${stored.tls ? "checked" : ""}
          style="width:14px;height:14px;accent-color:#38bdf8;">
        Use HTTPS (TLS)
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Dataset
        <input id="fsk-cfg-ds" type="text" placeholder="wactorz"
          value="${stored.ds}"
          class="af-cfg-input">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Username <span style="opacity:0.5;">(optional)</span>
        <input id="fsk-cfg-user" type="text" placeholder="admin"
          value="${stored.user}"
          class="af-cfg-input">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">
        Password <span style="opacity:0.5;">(optional)</span>
        <input id="fsk-cfg-pass" type="password" placeholder=""
          value="${stored.pass}"
          class="af-cfg-input">
      </label>
      <div style="display:flex;gap:8px;">
        <button id="fsk-cfg-save" class="af-mini-btn" style="flex:1;padding:8px;">Save &amp; Connect</button>
        ${stored.url ? `<button id="fsk-cfg-clear" class="af-mini-btn danger" style="padding:8px 12px;">Reset</button>` : ""}
      </div>
      <div id="fsk-cfg-msg" style="font-size:12px;min-height:16px;"></div>
    `;

    form.querySelector("#fsk-cfg-save")?.addEventListener("click", () => {
      const raw = (
        form.querySelector<HTMLInputElement>("#fsk-cfg-url")?.value ?? ""
      ).trim();
      if (!raw) {
        const msg = form.querySelector<HTMLElement>("#fsk-cfg-msg")!;
        msg.style.color = "#f87171";
        msg.textContent = "Host is required.";
        return;
      }
      const tls = form.querySelector<HTMLInputElement>("#fsk-cfg-tls")?.checked;
      const proto = tls ? "https" : "http";
      const hasProto = /^https?:\/\//i.test(raw);
      const url = hasProto ? raw : `${proto}://${raw}`;
      const ds =
        (
          form.querySelector<HTMLInputElement>("#fsk-cfg-ds")?.value ??
          "wactorz"
        ).trim() || "wactorz";
      const user = (
        form.querySelector<HTMLInputElement>("#fsk-cfg-user")?.value ?? ""
      ).trim();
      const pass =
        form.querySelector<HTMLInputElement>("#fsk-cfg-pass")?.value ?? "";

      localStorage.setItem("wactorz-fuseki-url", url);
      localStorage.setItem("wactorz-fuseki-dataset", ds);
      localStorage.setItem("wactorz-fuseki-user", user);
      localStorage.setItem("wactorz-fuseki-pass", pass);

      this._renderView();
    });

    form.querySelector("#fsk-cfg-clear")?.addEventListener("click", () => {
      [
        "wactorz-fuseki-url",
        "wactorz-fuseki-dataset",
        "wactorz-fuseki-user",
        "wactorz-fuseki-pass",
      ].forEach((k) => localStorage.removeItem(k));
      this._renderView();
    });

    return form;
  }

  // ── Private: settings view ────────────────────────────────────────────────

  private _buildSettingsView(): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-settings";

    const title = document.createElement("h2");
    title.className = "af-settings-title";
    title.textContent = "Settings";
    el.appendChild(title);

    el.appendChild(this._buildCostLimitSection());

    el.appendChild(
      this._buildSettingsSection("🏠 Home Assistant", [
        {
          key: "wactorz-ha-url",
          label: "URL",
          placeholder: "http://homeassistant.local:8123",
          type: "text",
        },
        {
          key: "wactorz-ha-token",
          label: "Token",
          placeholder: "Long-lived access token",
          type: "password",
        },
      ]),
    );

    el.appendChild(
      this._buildSettingsSection("⬡ Knowledge Graph (Fuseki)", [
        {
          key: "wactorz-fuseki-url",
          label: "URL",
          placeholder: "http://localhost:3030",
          type: "text",
        },
        {
          key: "wactorz-fuseki-dataset",
          label: "Dataset",
          placeholder: "wactorz",
          type: "text",
        },
        {
          key: "wactorz-fuseki-user",
          label: "Username",
          placeholder: "admin",
          type: "text",
        },
        {
          key: "wactorz-fuseki-pass",
          label: "Password",
          placeholder: "",
          type: "password",
        },
      ]),
    );

    // No MQTT URL field: the dashboard always connects to the same-origin
    // /mqtt proxy (derived from window.location), so there is nothing to
    // configure here and a stored value could only ever go stale.

    return el;
  }

  private _buildCostLimitSection(): HTMLElement {
    const section = document.createElement("div");
    section.className = "af-settings-section";

    const h = document.createElement("h3");
    h.className = "af-settings-section-heading";
    h.textContent = "🪙 LLM Spend Limit";
    section.appendChild(h);

    const grid = document.createElement("div");
    grid.className = "af-settings-grid";

    const lim = this._costLimitInfo;
    const currentLimit = lim?.limit_usd ?? 0;
    const currentPeriod = lim?.period ?? "monthly";

    // Limit input
    const limitLbl = document.createElement("label");
    limitLbl.className = "af-settings-field";
    const limitSpan = document.createElement("span");
    limitSpan.className = "af-settings-label";
    limitSpan.textContent = "Limit (USD, 0 to disable)";
    const limitInput = document.createElement("input");
    limitInput.type = "number";
    limitInput.min = "0";
    limitInput.step = "0.01";
    limitInput.className = "af-cfg-input";
    limitInput.placeholder = "0.00";
    limitInput.value = currentLimit ? String(currentLimit) : "";
    limitLbl.append(limitSpan, limitInput);
    grid.appendChild(limitLbl);

    // Period select
    const periodLbl = document.createElement("label");
    periodLbl.className = "af-settings-field";
    const periodSpan = document.createElement("span");
    periodSpan.className = "af-settings-label";
    periodSpan.textContent = "Period";
    const periodSelect = document.createElement("select");
    periodSelect.className = "af-cfg-input";
    ["daily", "weekly", "monthly"].forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p.charAt(0).toUpperCase() + p.slice(1);
      if (p === currentPeriod) opt.selected = true;
      periodSelect.appendChild(opt);
    });
    periodLbl.append(periodSpan, periodSelect);
    grid.appendChild(periodLbl);

    section.appendChild(grid);

    // Status line
    const status = document.createElement("p");
    status.className = "af-settings-note";
    const spend = lim?.spend_usd ?? 0;
    const periodLabel =
      currentPeriod === "daily"  ? "today"
    : currentPeriod === "weekly" ? "this week"
    :                              "this month";
    status.textContent = currentLimit > 0
      ? `Current spend: $${spend.toFixed(4)} / $${Number(currentLimit).toFixed(2)} ${periodLabel}`
      : `Current spend: $${spend.toFixed(4)} ${periodLabel} (no limit set)`;
    section.appendChild(status);

    // Actions
    const actions = document.createElement("div");
    actions.className = "af-settings-actions";

    const saveBtn = document.createElement("button");
    saveBtn.className = "af-mini-btn";
    saveBtn.textContent = "Save limit";
    saveBtn.addEventListener("click", async () => {
      const v = parseFloat(limitInput.value || "0");
      if (isNaN(v) || v < 0) return;
      saveBtn.disabled = true;
      try {
        await this._saveCostLimit(v, periodSelect.value);
        if (this.view === "settings") this._renderView();
      } finally {
        saveBtn.disabled = false;
      }
    });
    actions.appendChild(saveBtn);

    const resetBtn = document.createElement("button");
    resetBtn.className = "af-mini-btn danger";
    resetBtn.textContent = "Reset spend";
    resetBtn.title = "Clears the period budget counter only. The lifetime " +
      "“Cost” total is separate and is not affected (use wactorz-reset --metrics for that).";
    resetBtn.addEventListener("click", async () => {
      if (!window.confirm(
        "Reset the period budget counter?\n\n" +
        "This only zeroes spend for the current period. The lifetime " +
        "“Cost” total stays unchanged.")) return;
      resetBtn.disabled = true;
      try {
        await this._resetCost();
        if (this.view === "settings") this._renderView();
      } finally {
        resetBtn.disabled = false;
      }
    });
    actions.appendChild(resetBtn);

    section.appendChild(actions);
    return section;
  }

  private _buildSettingsSection(
    heading: string,
    fields: { key: string; label: string; placeholder: string; type: string }[],
    note?: string,
  ): HTMLElement {
    const section = document.createElement("div");
    section.className = "af-settings-section";

    const h = document.createElement("h3");
    h.className = "af-settings-section-heading";
    h.textContent = heading;
    section.appendChild(h);

    const grid = document.createElement("div");
    grid.className = "af-settings-grid";

    const inputs = new Map<string, HTMLInputElement>();

    fields.forEach(({ key, label, placeholder, type }) => {
      const lbl = document.createElement("label");
      lbl.className = "af-settings-field";

      const span = document.createElement("span");
      span.className = "af-settings-label";
      span.textContent = label;

      const input = document.createElement("input");
      input.type = type;
      input.className = "af-cfg-input";
      input.placeholder = placeholder;
      input.value = localStorage.getItem(key) ?? "";

      // Show origin badge
      const badge = document.createElement("span");
      badge.className = `af-settings-origin${input.value ? " set" : ""}`;
      badge.title = input.value ? "Value is set" : "Not configured";
      badge.textContent = input.value ? "●" : "○";

      input.addEventListener("input", () => {
        badge.className = `af-settings-origin${input.value ? " set" : ""}`;
        badge.title = input.value ? "Value is set" : "Not configured";
        badge.textContent = input.value ? "●" : "○";
      });

      if (type === "password") {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "af-settings-eye";
        toggle.title = "Show / hide";
        toggle.textContent = "👁";
        toggle.addEventListener("click", () => {
          input.type = input.type === "password" ? "text" : "password";
          toggle.textContent = input.type === "password" ? "👁" : "🙈";
        });
        lbl.append(span, input, toggle, badge);
      } else {
        lbl.append(span, input, badge);
      }
      grid.appendChild(lbl);
      inputs.set(key, input);
    });

    section.appendChild(grid);

    if (note) {
      const noteEl = document.createElement("p");
      noteEl.className = "af-settings-note";
      noteEl.textContent = note;
      section.appendChild(noteEl);
    }

    // Action row
    const actions = document.createElement("div");
    actions.className = "af-settings-actions";

    const saveBtn = document.createElement("button");
    saveBtn.className = "af-mini-btn";
    saveBtn.style.cssText = "padding:6px 18px;font-size:12px;";
    saveBtn.textContent = "Save";

    const msg = document.createElement("span");
    msg.className = "af-settings-msg";

    saveBtn.addEventListener("click", () => {
      inputs.forEach((input, key) => {
        if (input.value.trim()) localStorage.setItem(key, input.value.trim());
        else localStorage.removeItem(key);
      });
      msg.textContent = "Saved.";
      msg.style.color = "#34d399";
      setTimeout(() => (msg.textContent = ""), 2000);
    });

    actions.append(saveBtn, msg);
    section.appendChild(actions);

    return section;
  }

  // ── Private: Bottom nav (mobile) ─────────────────────────────────────────

  private _buildBottomNav(): HTMLElement {
    const nav = document.createElement("nav");
    nav.className = "af-bottom-nav";

    const primary: { key: View; icon: string; label: string }[] = [
      { key: "overview", icon: "◫", label: "Overview" },
      { key: "feed",     icon: "≡", label: "Feed"     },
      { key: "chat",     icon: "💬", label: "Chat"     },
      { key: "ha",       icon: "🏠", label: "Devices"  },
    ];

    primary.forEach(({ key, icon, label }) => {
      const btn = document.createElement("button");
      btn.className = `af-view-btn af-bottom-tab${key === this.view ? " active" : ""}`;
      btn.dataset["view"] = key;
      btn.innerHTML = `<span class="af-bottom-tab-icon">${icon}</span><span class="af-bottom-tab-label">${label}</span>`;
      btn.addEventListener("click", () => {
        sheet.classList.remove("open");
        this._setView(key);
      });
      nav.appendChild(btn);
    });

    // ⋯ More → slide-up sheet for Graph + Settings
    const moreBtn = document.createElement("button");
    moreBtn.className = "af-bottom-tab af-bottom-more-btn";
    moreBtn.innerHTML = `<span class="af-bottom-tab-icon">⋯</span><span class="af-bottom-tab-label">More</span>`;

    const sheet = document.createElement("div");
    sheet.className = "af-bottom-sheet";

    const secondary: { key: View; icon: string; label: string }[] = [
      { key: "fuseki",   icon: "⬡", label: "Graph"    },
      { key: "settings", icon: "⚙", label: "Settings" },
    ];
    secondary.forEach(({ key, icon, label }) => {
      const btn = document.createElement("button");
      btn.className = `af-view-btn af-bottom-tab af-bottom-sheet-btn${key === this.view ? " active" : ""}`;
      btn.dataset["view"] = key;
      btn.innerHTML = `<span class="af-bottom-tab-icon">${icon}</span><span class="af-bottom-tab-label">${label}</span>`;
      btn.addEventListener("click", () => {
        sheet.classList.remove("open");
        moreBtn.classList.remove("active");
        this._setView(key);
      });
      sheet.appendChild(btn);
    });

    moreBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      sheet.classList.toggle("open");
      moreBtn.classList.toggle("active", sheet.classList.contains("open"));
    });
    document.addEventListener("click", () => {
      sheet.classList.remove("open");
      moreBtn.classList.remove("active");
    });

    nav.appendChild(moreBtn);
    nav.appendChild(sheet);

    return nav;
  }

  // ── Private: Audio popover ────────────────────────────────────────────────

  private _buildAudioPopover(): HTMLElement {
    const pop = document.createElement("div");
    pop.className = "af-audio-popover glass";

    // ── Row: Beep + TTS toggles ───────────────────────────────────────────
    const toggleRow = document.createElement("div");
    toggleRow.className = "af-audio-row";

    const beepBtn = document.createElement("button");
    beepBtn.className = `af-audio-toggle${tts.beepEnabled ? " on" : ""}`;
    beepBtn.textContent = `🔔 Beep`;
    beepBtn.title = "Notification beep";
    beepBtn.addEventListener("click", () => {
      const on = tts.toggleBeep();
      beepBtn.classList.toggle("on", on);
    });

    const ttsBtn = document.createElement("button");
    ttsBtn.className = `af-audio-toggle${tts.ttsEnabled ? " on" : ""}`;
    ttsBtn.textContent = `🗣 TTS`;
    ttsBtn.title = "Read replies aloud";
    ttsBtn.addEventListener("click", () => {
      const on = tts.toggleTTS();
      ttsBtn.classList.toggle("on", on);
      voiceRow.style.display = on ? "" : "none";
    });

    toggleRow.append(beepBtn, ttsBtn);
    pop.appendChild(toggleRow);

    // ── Row: Voice select ─────────────────────────────────────────────────
    const voiceRow = document.createElement("div");
    voiceRow.className = "af-audio-row";
    voiceRow.style.display = tts.ttsEnabled ? "" : "none";

    const voiceSel = document.createElement("select");
    voiceSel.className = "af-audio-select";
    voiceSel.title = "TTS voice";

    const placeholderOpt = document.createElement("option");
    placeholderOpt.value = "";
    placeholderOpt.textContent = "— loading voices… —";
    voiceSel.appendChild(placeholderOpt);

    const populateVoices = (): void => {
      const voices = tts.voices;
      if (!voices.length) return;
      while (voiceSel.options.length > 1) voiceSel.remove(1);
      voices.forEach(v => {
        const o = document.createElement("option");
        o.value = v.name;
        o.textContent = v.name.replace(/^Microsoft\s+/, "").replace(/\s+Online.*$/i, "");
        voiceSel.appendChild(o);
      });
      const saved = tts.selectedVoice;
      if (saved) voiceSel.value = saved;
    };

    populateVoices();
    document.addEventListener("tts-voices-loaded", () => populateVoices());
    voiceSel.addEventListener("change", () => tts.setVoice(voiceSel.value));

    voiceRow.appendChild(voiceSel);
    pop.appendChild(voiceRow);

    // ── Divider ───────────────────────────────────────────────────────────
    const divider = document.createElement("div");
    divider.className = "af-audio-divider";
    pop.appendChild(divider);

    // ── Row: Ambient track ────────────────────────────────────────────────
    const trackLabel = document.createElement("div");
    trackLabel.className = "af-audio-label";
    trackLabel.textContent = "Ambient";
    pop.appendChild(trackLabel);

    const trackRow = document.createElement("div");
    trackRow.className = "af-audio-tracks";

    AMBIENT_TRACKS.forEach(({ id, label }) => {
      const btn = document.createElement("button");
      btn.className = `af-audio-track-btn${ambient.track === id ? " on" : ""}`;
      btn.textContent = label;
      btn.addEventListener("click", () => {
        trackRow.querySelectorAll(".af-audio-track-btn").forEach(b => b.classList.remove("on"));
        btn.classList.add("on");
        ambient.setTrack(id);
        volRow.style.display = id === "none" ? "none" : "";
      });
      trackRow.appendChild(btn);
    });

    pop.appendChild(trackRow);

    // ── Row: Volume slider ────────────────────────────────────────────────
    const volRow = document.createElement("div");
    volRow.className = "af-audio-row af-audio-vol-row";
    volRow.style.display = ambient.track === "none" ? "none" : "";

    const volIcon = document.createElement("span");
    volIcon.textContent = "🔉";
    volIcon.style.fontSize = "14px";

    const volSlider = document.createElement("input");
    volSlider.type = "range";
    volSlider.className = "af-audio-slider";
    volSlider.min = "0"; volSlider.max = "1"; volSlider.step = "0.05";
    volSlider.value = String(ambient.volume);
    volSlider.addEventListener("input", () => ambient.setVolume(parseFloat(volSlider.value)));

    volRow.append(volIcon, volSlider);
    pop.appendChild(volRow);

    return pop;
  }

  private _buildResetPopover(): HTMLElement {
    const pop = document.createElement("div");
    pop.className = "af-audio-popover glass";
    pop.style.cssText = "min-width:210px;padding:12px 14px;";

    const title = document.createElement("div");
    title.textContent = "Clear stored state";
    title.style.cssText = "font-size:10px;font-weight:600;opacity:.45;margin-bottom:10px;text-transform:uppercase;letter-spacing:.08em;";
    pop.appendChild(title);

    const ICON = {
      chat:    `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9.5a5 5 0 0 1-5 5H3l-2 2V5a5 5 0 0 1 5-5h3"/><circle cx="12" cy="4" r="3"/></svg>`,
      metrics: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="9" width="3" height="6" rx="1"/><rect x="6" y="5" width="3" height="10" rx="1"/><rect x="11" y="2" width="3" height="13" rx="1"/></svg>`,
      spawns:  `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="2" r="1.5"/><circle cx="2" cy="13" r="1.5"/><circle cx="14" cy="13" r="1.5"/><path d="M8 3.5v4m0 4-5 3.5m5-3.5 5 3.5m-5-7.5-5 3.5m5-3.5 5 3.5"/></svg>`,
      state:   `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="12" height="3" rx="1"/><rect x="2" y="8" width="12" height="3" rx="1"/><rect x="2" y="13" width="8" height="2" rx="1"/></svg>`,
      logs:    `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"/><path d="M5 6h6M5 9h4"/></svg>`,
      all:     `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4h12M5 4V2h6v2M6 7v5M10 7v5M3 4l1 9a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l1-9"/></svg>`,
    } as Record<string, string>;

    const scopes: { scope: string; label: string; danger?: boolean }[] = [
      { scope: "chat",    label: "Chat history" },
      { scope: "metrics", label: "Metrics & costs" },
      { scope: "spawns",  label: "Spawn registry" },
      { scope: "state",   label: "Agent state files" },
      { scope: "logs",    label: "Log files" },
      { scope: "all",     label: "Wipe everything", danger: true },
    ];

    scopes.forEach(({ scope, label, danger }, i) => {
      if (danger) {
        const hr = document.createElement("div");
        hr.style.cssText = "height:1px;background:rgba(255,255,255,.08);margin:6px 0 8px;";
        pop.appendChild(hr);
      }

      const btn = document.createElement("button");
      btn.className = "af-mini-btn";
      btn.style.cssText = [
        "display:flex;align-items:center;gap:8px;width:100%;",
        "padding:6px 8px;margin-bottom:3px;border-radius:6px;",
        "font-size:12px;text-align:left;transition:background .15s;",
        danger ? "color:#f87171;" : "",
      ].join("");
      btn.innerHTML = `${ICON[scope] ?? ""}<span>${label}</span>`;

      // Two-step confirm: first click arms, second fires
      let armed = false;
      let armTimer: ReturnType<typeof setTimeout> | null = null;

      btn.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          const span = btn.querySelector("span")!;
          const orig = span.textContent!;
          span.textContent = `Confirm ${label.toLowerCase()}?`;
          btn.style.background = danger ? "rgba(248,113,113,.15)" : "rgba(255,255,255,.1)";
          armTimer = setTimeout(() => {
            armed = false;
            span.textContent = orig;
            btn.style.background = "";
          }, 3000);
          return;
        }

        if (armTimer) clearTimeout(armTimer);
        armed = false;
        pop.classList.remove("open");

        try {
          const res = await fetch("/api/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scope }),
          });
          if (res.ok) {
            toast.show({ type: "system", title: "Reset", message: `${label} cleared` });
          } else {
            const err = await res.json().catch(() => ({}));
            toast.show({ type: "alert-error", title: "Reset failed", message: (err as any).error ?? String(res.status) });
          }
        } catch (e) {
          toast.show({ type: "alert-error", title: "Reset failed", message: String(e) });
        }
      });

      pop.appendChild(btn);
    });

    return pop;
  }
}

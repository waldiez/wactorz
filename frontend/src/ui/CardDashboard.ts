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

// ── Helpers ──────────────────────────────────────────────────────────────────

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

function agentTypeColor(agentType?: string): string {
  switch (agentType) {
    case "orchestrator":
      return "#f59e0b";
    case "monitor":
      return "#34d399";
    case "synapse":
      return "#8b5cf6";
    default:
      return "#93c5fd";
  }
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

  private haClient: HAClient | null = null;

  // Streaming
  private _streamRow: HTMLElement | null = null;
  private _streamBody: HTMLElement | null = null;
  private _streamFrom: string | null = null;
  private _streamText: string = "";

  // Event listeners (stored for cleanup)
  private _evFeed: ((e: Event) => void) | null = null;
  private _evChat: ((e: Event) => void) | null = null;
  private _evChunk: ((e: Event) => void) | null = null;
  private _evEnd: ((e: Event) => void) | null = null;
  private _evConn: ((e: Event) => void) | null = null;

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
  }

  private _initHAClient(): void {
    const url = this.haUrl;
    const token = this.haToken;
    if (url && token) {
      this.haClient = new HAClient(url, token);
    } else {
      this.haClient = null;
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
  }

  hide(): void {
    this.root.classList.remove("cd-visible");
    this._showFloatingUI();
    this._unwireEvents();
    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }
  }

  destroy(): void {
    this.hide();
    this.root.remove();
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
    this.agents.delete(id);
    if (!this.root.classList.contains("cd-visible")) return;
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(id)}"]`,
    );
    if (card) {
      card.style.animation = "cd-exit 0.25s ease forwards";
      setTimeout(() => card.remove(), 250);
    }
    if (this.view === "overview") this._renderStats();
    if (this.view === "chat") this._renderSidebar();
    this._updateTargetSelect();
  }

  onHeartbeat(agentId: string, timestampMs: number): void {
    this.lastHb.set(agentId, timestampMs);
    if (!this.root.classList.contains("cd-visible")) return;
    const card = this.root.querySelector<HTMLElement>(
      `[data-id="${CSS.escape(agentId)}"]`,
    );
    if (!card) return;
    const hbEl = card.querySelector<HTMLElement>(".af-card-hb-time");
    if (hbEl) hbEl.textContent = relTime(timestampMs);
    const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
    if (dot) {
      dot.classList.remove("af-card-pulse");
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
      if (this.feedItems.length > 200) this.feedItems.shift();
      if (this.view === "feed") this._appendFeedItemToView(item);
    };

    this._evChat = (e) => {
      const msg = (e as CustomEvent<{ msg: ChatMessage }>).detail.msg;
      // Tag io-gateway / system replies with the active chatTarget so they
      // only appear in the thread where the user sent the triggering message.
      const stored: ChatMessage =
        msg.from === "io-gateway" || msg.from === "system"
          ? { ...msg, to: this.chatTarget }
          : msg;
      this.chatMessages.push(stored);
      if (this.chatMessages.length > 200) this.chatMessages.shift();
      if (this.view === "chat" && this._msgBelongsHere(stored)) {
        this._appendChatMsgEl(stored);
        this._scrollThread();
      }
    };

    this._evChunk = (e) => {
      if (this.view !== "chat") return;
      const { chunk, from } = (
        e as CustomEvent<{ chunk: string; from: string }>
      ).detail;
      if (!this._streamRow) {
        this._streamFrom = from;
        this._streamText = "";
        const thread = this.root.querySelector<HTMLElement>(".af-chat-thread");
        if (!thread) return;
        const row = document.createElement("div");
        row.className = "af-chat-msg af-chat-msg-agent";
        const fromEl = document.createElement("div");
        fromEl.className = "af-chat-msg-from";
        fromEl.textContent = from;
        const bubble = document.createElement("div");
        bubble.className = "af-chat-msg-bubble";
        row.appendChild(fromEl);
        row.appendChild(bubble);
        thread.appendChild(row);
        this._streamRow = row;
        this._streamBody = bubble;
      }
      this._streamText += chunk;
      if (this._streamBody) this._streamBody.textContent = this._streamText;
      this._scrollThread();
    };

    this._evEnd = () => {
      if (this._streamFrom && this._streamText) {
        const msg: ChatMessage = {
          id: `stream-${Date.now()}`,
          from: this._streamFrom,
          to: this.chatTarget, // tag with active context for thread filtering
          content: this._streamText,
          timestampMs: Date.now(),
        };
        this.chatMessages.push(msg);
      }
      this._streamRow = null;
      this._streamBody = null;
      this._streamFrom = null;
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
  }

  // ── Private: floating UI ──────────────────────────────────────────────────

  private _hideFloatingUI(): void {
    [
      "hud",
      "hud-stats",
      "io-bar",
      "chat-panel",
      "activity-feed",
      "feed-toggle",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "none";
    });
  }

  private _showFloatingUI(): void {
    ["hud", "hud-stats", "io-bar", "feed-toggle"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.style.display = "";
    });
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
    else if (this.view === "settings") body.appendChild(this._buildSettingsView());
    else if (this.view === "chat") {
      body.appendChild(this._buildChatView());
      // _render* calls inside _buildChatView() run before the element is in
      // the DOM, so this.root.querySelector returns null. Re-run now that
      // the chat view is attached.
      this._renderSidebar();
      this._renderChatPaneHeader();
      this._renderChatThread();
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
    if (this.view === "ha" && v !== "ha") {
      this.haClient?.disconnect();
    }
    if (v === "chat") this._syncChatTarget();
    this.view = v;
    this._renderView();

    if (this.view === "ha") {
      this.haClient?.connect((entities) => this._renderHADevices(entities));
    }
  }

  // ── Private: overview ─────────────────────────────────────────────────────

  private _buildOverview(): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-overview";

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
    const agentNames = [...this.agents.values()].map((a) => a.name);
    nodeList.innerHTML = `
      <div class="af-node-item">
        <div>
          <div class="af-node-name">local</div>
          <div class="af-node-meta">${agentNames.length > 0 ? agentNames.join(", ") : "no agents"}</div>
        </div>
        <span class="af-node-pill online">online</span>
      </div>
    `;
    np.appendChild(nodeList);

    panels.appendChild(wp);
    panels.appendChild(np);
    el.appendChild(panels);
    return el;
  }

  private _buildStatCards(container: HTMLElement): void {
    container.innerHTML = "";
    const agents = [...this.agents.values()];
    const total = agents.length;
    const healthy = agents.filter(
      (a) => stateLabel(a.state) === "running",
    ).length;
    const msgs = agents.reduce((s, a) => s + (a.messagesProcessed ?? 0), 0);
    const cost = agents.reduce((s, a) => s + (a.costUsd ?? 0), 0);
    const events = this.feedItems.length;

    [
      {
        label: "Wactorz",
        value: String(total),
        detail: `${healthy} running`,
        accent: "#60a5fa",
      },
      {
        label: "Messages",
        value: String(msgs),
        detail: "processed across actors",
        accent: "#22d3a0",
      },
      {
        label: "Cost",
        value: `$${cost.toFixed(4)}`,
        detail: "reported by actors",
        accent: "#f59e0b",
      },
      {
        label: "Feed Events",
        value: String(events),
        detail: "since dashboard loaded",
        accent: "#8b5cf6",
      },
    ].forEach(({ label, value, detail, accent }) => {
      const card = document.createElement("div");
      card.className = "af-stat-card";
      card.style.borderColor = `${accent}44`;
      card.innerHTML = `
        <div class="af-stat-label">${label}</div>
        <div class="af-stat-value" style="color:${accent}">${value}</div>
        <div class="af-stat-detail">${detail}</div>
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
      if (!live.has(el.dataset.id!)) el.remove();
    });
    sorted.forEach((agent) => {
      if (!grid.querySelector(`[data-id="${CSS.escape(agent.id)}"]`)) {
        grid.appendChild(this._buildWactorCard(agent));
      }
    });
  }

  private _patchCard(agent: AgentInfo): void {
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
    const typeColor = agentTypeColor(agent.agentType);
    const msgs = agent.messagesProcessed ?? 0;

    const card = document.createElement("div");
    card.className = "af-card";
    card.dataset.id = agent.id;

    const dot = document.createElement("div");
    dot.className = "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow = `0 0 8px ${color}`;

    const badge = document.createElement("div");
    badge.className = "af-card-type-badge";
    badge.style.color = typeColor;
    badge.style.borderColor = `${typeColor}55`;
    badge.textContent = agent.agentType ?? "wactor";

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
    card.appendChild(badge);
    card.appendChild(name);
    card.appendChild(stateLbl);
    card.appendChild(meta);

    if (agent.task) {
      const task = document.createElement("div");
      task.className = "af-card-task";
      task.textContent = agent.task;
      card.appendChild(task);
    }

    const controls = document.createElement("div");
    controls.className = "af-card-controls";

    const canMessage = ["main", "main-actor", "home-assistant-agent", "catalog"].includes(agent.name);
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
    const isSystem = !["main", "main-actor", "home-assistant-agent", "catalog"].includes(agent.name);
    if (isSystem) return;
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
    const feed = document.createElement("div");
    feed.className = "af-feed";
    feed.id = "af-feed-view";

    if (this.feedItems.length === 0) {
      const empty = document.createElement("div");
      empty.className = "af-feed-empty";
      empty.textContent = "No events yet.";
      feed.appendChild(empty);
    } else {
      this.feedItems.forEach((item) => this._feedItemEl(feed, item));
    }
    setTimeout(() => {
      feed.scrollTop = feed.scrollHeight;
    }, 0);
    return feed;
  }

  private _appendFeedItemToView(item: FeedItem): void {
    const feed = this.root.querySelector<HTMLElement>("#af-feed-view");
    if (!feed) return;
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
    agent.textContent = item.agentName;

    const text = document.createElement("span");
    text.className = "af-feed-text";
    text.textContent = item.label;

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
      // Only main-actor, home-assistant-agent, and catalog are directly messageable.
      const isDisabled = !["main", "main-actor", "home-assistant-agent", "catalog"].includes(agent.name);

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
          if (latest?.protected && latest.name !== "main-actor") return;
          this.chatTarget = agent.name;
          this._renderSidebar();
          this._renderChatPaneHeader();
          this._renderChatThread();
          this._updateTargetSelect();
        });
      }

      // Patch only what may have changed
      const cls = ["af-chat-agent-row"];
      if (isActive) cls.push("active");
      if (isDisabled) cls.push("protected-agent");
      row.className = cls.join(" ");
      (row as HTMLButtonElement).disabled = isDisabled;
      row.title = isDisabled
        ? `${agent.name} — system agent, not directly reachable`
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
    if (this.chatTarget !== "main-actor") {
      const via = document.createElement("span");
      via.className = "af-chat-pane-via";
      via.title = "Context filter — all messages go to @main-actor.";
      via.textContent = "context · all msgs → @main-actor";
      hdr.appendChild(via);
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

  private _renderChatThread(): void {
    const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
    if (!thread) return;
    thread.innerHTML = "";
    const msgs = this.chatMessages.filter((m) => this._msgBelongsHere(m));
    if (msgs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "af-chat-empty";
      empty.innerHTML =
        this.chatTarget === "main-actor"
          ? `<p>Say hello to <strong>@main-actor</strong> — the system orchestrator.</p>`
          : `<p>No messages in <strong>@${this.chatTarget}</strong> context yet.</p>
           <p style="font-size:11px;opacity:0.5">Messages go to @main-actor.</p>`;
      thread.appendChild(empty);
    } else {
      msgs.forEach((m) => this._appendChatMsgEl(m, thread));
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
    bubble.textContent = msg.content;
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

    const input = document.createElement("input");
    input.className = "af-iobar-input";
    input.id = "af-iobar-input";
    input.placeholder = `Message @${this.chatTarget}…`;
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._sendMessage(input, select);
      }
    });
    select.addEventListener("change", () => {
      this.chatTarget = select.value;
      input.placeholder = `Message @${select.value}…`;
    });

    const sendBtn = document.createElement("button");
    sendBtn.className = "af-send-btn";
    sendBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M1 13L13 7 1 1v4.5l8.5 1.5-8.5 1.5V13z" fill="currentColor"/></svg>`;
    sendBtn.addEventListener("click", () => this._sendMessage(input, select));

    bar.append(select, input, sendBtn);
    return bar;
  }

  private _populateSelect(select: HTMLSelectElement): void {
    select.innerHTML = "";
    [...this.agents.values()]
      .filter((a) => !(a.protected && a.name !== "main-actor"))
      .sort((a, b) => {
        if (a.name === "main-actor") return -1;
        if (b.name === "main-actor") return 1;
        return a.name.localeCompare(b.name);
      })
      .forEach((agent) => {
        const opt = document.createElement("option");
        opt.value = agent.name;
        opt.textContent = `@${agent.name}`;
        select.appendChild(opt);
      });
    // Keep dropdown in sync with chatTarget
    select.value = this.chatTarget;
  }

  private _updateTargetSelect(): void {
    const select =
      this.root.querySelector<HTMLSelectElement>("#af-target-select");
    if (select) this._populateSelect(select);
    const input = this.root.querySelector<HTMLInputElement>("#af-iobar-input");
    if (input) input.placeholder = `Message @${this.chatTarget}…`;
  }

  private _sendMessage(
    input: HTMLInputElement,
    select: HTMLSelectElement,
  ): void {
    const content = input.value.trim();
    if (!content) return;
    const target = select.value || "main-actor";
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
    document.dispatchEvent(
      new CustomEvent("af-send-message", { detail: { content, target } }),
    );
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
        <div id="ha-devices-container" style="flex:1;overflow-y:auto;overflow-x:hidden;margin-top:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;">
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
    const container = this.root.querySelector<HTMLElement>(
      "#ha-devices-container",
    );
    if (!container) return;

    container.innerHTML = "";

    // Sort by domain then friendly name
    const sorted = [...entities].sort((a, b) => {
      const domA = a.entity_id.split(".")[0] || "";
      const domB = b.entity_id.split(".")[0] || "";
      if (domA !== domB) return domA.localeCompare(domB);
      return (a.attributes.friendly_name || a.entity_id).localeCompare(
        b.attributes.friendly_name || b.entity_id,
      );
    });

    if (sorted.length === 0) {
      container.innerHTML = `<div style="color: rgba(255,255,255,0.4); text-align: center; grid-column: 1/-1; margin-top: 40px;">No entities found.</div>`;
      return;
    }

    sorted.forEach((e) => {
      const domain = e.entity_id.split(".")[0] || "";
      const card = document.createElement("div");
      card.className = "af-card";
      card.style.cursor = "default";
      card.style.minHeight = "130px";
      card.style.display = "flex";
      card.style.flexDirection = "column";

      // ── Header: Avatar + Name + ID ──
      const headerRow = document.createElement("div");
      headerRow.style.display = "flex";
      headerRow.style.alignItems = "center";
      headerRow.style.gap = "8px";
      headerRow.style.marginBottom = "8px";

      if (e.attributes.entity_picture) {
        const img = document.createElement("img");
        img.src = (this.haUrl ?? "") + e.attributes.entity_picture;
        img.style.width = "28px";
        img.style.height = "28px";
        img.style.borderRadius = "4px";
        img.style.objectFit = "cover";
        headerRow.appendChild(img);
      } else {
        const iconPlaceholder = document.createElement("div");
        iconPlaceholder.style.width = "28px";
        iconPlaceholder.style.height = "28px";
        iconPlaceholder.style.borderRadius = "4px";
        iconPlaceholder.style.background = "rgba(255,255,255,0.05)";
        iconPlaceholder.style.display = "flex";
        iconPlaceholder.style.alignItems = "center";
        iconPlaceholder.style.justifyContent = "center";
        iconPlaceholder.style.fontSize = "14px";
        iconPlaceholder.textContent = this._getDomainIcon(domain);
        headerRow.appendChild(iconPlaceholder);
      }

      const nameCol = document.createElement("div");
      nameCol.style.flex = "1";
      nameCol.style.minWidth = "0";

      const name = document.createElement("div");
      name.className = "af-card-name";
      name.textContent = e.attributes.friendly_name || e.entity_id;
      name.style.fontSize = "12px";

      const idMeta = document.createElement("div");
      idMeta.className = "af-card-meta";
      idMeta.style.fontSize = "9px";
      idMeta.style.opacity = "0.6";
      idMeta.textContent = e.entity_id;

      nameCol.append(name, idMeta);
      headerRow.appendChild(nameCol);
      card.appendChild(headerRow);

      // ── State Display ──
      const stateRow = document.createElement("div");
      stateRow.style.display = "flex";
      stateRow.style.alignItems = "baseline";
      stateRow.style.gap = "4px";
      stateRow.style.marginBottom = "10px";

      const stateVal = document.createElement("div");
      stateVal.className = "af-card-state-label";
      stateVal.textContent = e.state;
      stateVal.style.fontSize = "16px";
      stateVal.style.fontWeight = "700";

      const isActive = [
        "on",
        "playing",
        "cool",
        "heat",
        "open",
        "active",
        "detected",
        "home",
      ].includes(e.state);
      const isAlert = [
        "problem",
        "error",
        "critical",
        "warning",
        "emergency",
      ].includes(e.state);
      stateVal.style.color = isAlert
        ? "#f87171"
        : isActive
          ? "#34d399"
          : "rgba(255,255,255,0.4)";

      stateRow.appendChild(stateVal);

      if (e.attributes.unit_of_measurement) {
        const unit = document.createElement("span");
        unit.style.fontSize = "11px";
        unit.style.color = "rgba(255,255,255,0.3)";
        unit.textContent = e.attributes.unit_of_measurement;
        stateRow.appendChild(unit);
      }
      card.appendChild(stateRow);

      // ── Controls Section ──
      const controls = document.createElement("div");
      controls.className = "af-card-controls";
      controls.style.marginTop = "auto";
      controls.style.display = "flex";
      controls.style.flexDirection = "column";
      controls.style.gap = "8px";

      this._appendEntityControls(controls, e, isActive);

      if (controls.children.length > 0) {
        card.appendChild(controls);
      }

      container.appendChild(card);
    });
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
    this.lastHb.forEach((ms, id) => {
      const el = this.root.querySelector<HTMLElement>(
        `[data-id="${CSS.escape(id)}"] .af-card-hb-time`,
      );
      if (el) el.textContent = relTime(ms);
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
    icon.src = "/favicon.svg";
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

    // const btn3d = document.createElement("button");
    // btn3d.className = "af-view-btn";
    // btn3d.style.marginLeft = "8px";
    // btn3d.textContent = "⊞ Social";
    // btn3d.addEventListener("click", () => {
    //   document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: "social" } }));
    // });
    // right.appendChild(btn3d);

    header.append(left, center, right);

    const body = document.createElement("div");
    body.className = "af-body";

    const iobar = this._buildIobar();

    root.append(header, body, iobar);
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
    const auth = this.fusekiUser
      ? `Basic ${btoa(`${this.fusekiUser}:${this.fusekiPass}`)}`
      : "";

    // ── Preset queries ─────────────────────────────────────────────────────
    const PRESETS: { label: string; icon: string; sparql: string }[] = [
      {
        label: "Current states",
        icon: "◉",
        sparql: `SELECT ?entity ?state ?unit WHERE {
  GRAPH <urn:ha:current> {
    ?entity syn:state ?state .
    OPTIONAL { ?entity syn:unit ?unit . }
  }
} ORDER BY ?entity LIMIT 200`,
      },
      {
        label: "Recent observations",
        icon: "⏱",
        sparql: `SELECT ?obs ?entity ?result ?ts WHERE {
  GRAPH <urn:ha:history> {
    ?obs a sosa:Observation ;
         sosa:madeBySensor ?entity ;
         sosa:hasSimpleResult ?result ;
         sosa:resultTime ?ts .
  }
} ORDER BY DESC(?ts) LIMIT 100`,
      },
      {
        label: "Device catalog",
        icon: "⊡",
        sparql: `SELECT ?entity ?label WHERE {
  GRAPH <urn:ha:devices> {
    ?entity rdfs:label ?label .
    FILTER(?entity != <urn:ha:bridge:wactorz>)
  }
} ORDER BY ?label LIMIT 200`,
      },
      {
        label: "Sensors with units",
        icon: "📡",
        sparql: `SELECT ?entity ?state ?unit WHERE {
  GRAPH <urn:ha:current> {
    ?entity a sosa:Sensor ;
            syn:state ?state .
    OPTIONAL { ?entity syn:unit ?unit . }
  }
} ORDER BY ?entity LIMIT 200`,
      },
      {
        label: "Graph sizes",
        icon: "∑",
        sparql: `SELECT ?g (COUNT(*) AS ?triples) WHERE {
  VALUES ?g { <urn:ha:current> <urn:ha:history> <urn:ha:devices> }
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
    hdr
      .querySelector("#fsk-reconfigure")
      ?.addEventListener("click", () => {
        wrapper.innerHTML = "";
        wrapper.appendChild(this._buildFusekiConfigForm());
      });
    wrapper.appendChild(hdr);

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
    editorRow.style.cssText = "display:flex;gap:8px;align-items:flex-start;flex-shrink:0;";

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
    const sparqlUrl = `${base}/${ds}/sparql`;
    const updateUrl = `${base}/${ds}/update`;

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
        [...q.matchAll(/^\s*PREFIX\s+(\w*:)/gim)].map((m) => m[1])
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

      const isUpdate = /^\s*(INSERT|DELETE|DROP|CREATE|LOAD|CLEAR|ADD|MOVE|COPY)/i.test(trimmed);
      const full = withPrefixes(trimmed);
      const headers: Record<string, string> = {};
      if (auth) headers["Authorization"] = auth;

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
          results?: { bindings: Record<string, { value: string; type: string }>[] };
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
          results.innerHTML = `<div class="af-fuseki-empty">No results.</div>`;
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
            if (!cell) { td.textContent = ""; return; }
            const val = cell.value;
            // shorten long URIs
            const display = val.length > 60
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
      url:  this.fusekiUrl?.replace(/^https?:\/\//, "") ?? "",
      tls:  (this.fusekiUrl ?? "").startsWith("https://"),
      ds:   this.fusekiDataset,
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
      const raw = (form.querySelector<HTMLInputElement>("#fsk-cfg-url")?.value ?? "").trim();
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
      const ds = (form.querySelector<HTMLInputElement>("#fsk-cfg-ds")?.value ?? "wactorz").trim() || "wactorz";
      const user = (form.querySelector<HTMLInputElement>("#fsk-cfg-user")?.value ?? "").trim();
      const pass = form.querySelector<HTMLInputElement>("#fsk-cfg-pass")?.value ?? "";

      localStorage.setItem("wactorz-fuseki-url", url);
      localStorage.setItem("wactorz-fuseki-dataset", ds);
      localStorage.setItem("wactorz-fuseki-user", user);
      localStorage.setItem("wactorz-fuseki-pass", pass);

      this._renderView();
    });

    form.querySelector("#fsk-cfg-clear")?.addEventListener("click", () => {
      ["wactorz-fuseki-url", "wactorz-fuseki-dataset",
       "wactorz-fuseki-user", "wactorz-fuseki-pass"].forEach((k) =>
        localStorage.removeItem(k),
      );
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

    el.appendChild(this._buildSettingsSection("🏠 Home Assistant", [
      { key: "wactorz-ha-url",   label: "URL",   placeholder: "http://homeassistant.local:8123", type: "text" },
      { key: "wactorz-ha-token", label: "Token", placeholder: "Long-lived access token",          type: "password" },
    ]));

    el.appendChild(this._buildSettingsSection("⬡ Knowledge Graph (Fuseki)", [
      { key: "wactorz-fuseki-url",     label: "URL",      placeholder: "http://localhost:3030", type: "text" },
      { key: "wactorz-fuseki-dataset", label: "Dataset",  placeholder: "wactorz",              type: "text" },
      { key: "wactorz-fuseki-user",    label: "Username", placeholder: "admin",                 type: "text" },
      { key: "wactorz-fuseki-pass",    label: "Password", placeholder: "",                      type: "password" },
    ]));

    el.appendChild(this._buildSettingsSection("📡 MQTT Broker", [
      { key: "wactorz-mqtt-url", label: "WebSocket URL", placeholder: "ws://localhost:9001", type: "text" },
    ], "⚠ Changes require a page reload"));

    return el;
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
}

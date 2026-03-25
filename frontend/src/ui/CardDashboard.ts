/**
 * CardDashboard — Workers / AgentFlow UI (exact port from synapse-os).
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

// ── Helpers ──────────────────────────────────────────────────────────────────

function stateColor(state: AgentState): string {
  if (typeof state === "object") return "#f87171";
  switch (state as string) {
    case "running":      return "#34d399";
    case "paused":       return "#fbbf24";
    case "initializing": return "#60a5fa";
    case "stopped":      return "#4b5563";
    default:             return "#34d399";
  }
}

function stateLabel(state: AgentState): string {
  if (typeof state === "object") return "failed";
  return state as string;
}

function agentTypeColor(agentType?: string): string {
  switch (agentType) {
    case "orchestrator": return "#f59e0b";
    case "monitor":      return "#34d399";
    case "synapse":      return "#8b5cf6";
    default:             return "#93c5fd";
  }
}

function relTime(ms: number): string {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 5)  return "now";
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}

type View = "overview" | "feed" | "chat";
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

  constructor() {
    this.root = this.buildRoot();
    document.body.appendChild(this.root);
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
    if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null; }
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
    const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
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
    const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
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
    const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;
    const cls = severity === "error" || severity === "critical" ? "af-card-alert-error" : "af-card-alert-warn";
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls, "af-card-alert-error", "af-card-alert-warn"), 900);
  }

  onChat(fromId: string, _toId: string): void {
    const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
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
      this.chatMessages.push(msg);
      if (this.chatMessages.length > 200) this.chatMessages.shift();
      if (this.view === "chat") {
        this._appendChatMsgEl(msg);
        this._scrollThread();
      }
    };

    this._evChunk = (e) => {
      if (this.view !== "chat") return;
      const { chunk, from } = (e as CustomEvent<{ chunk: string; from: string }>).detail;
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
          to: "user",
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
    if (this._evFeed)  { document.removeEventListener("af-feed-push", this._evFeed);  this._evFeed  = null; }
    if (this._evChat)  { document.removeEventListener("af-chat-message", this._evChat); this._evChat = null; }
    if (this._evChunk) { document.removeEventListener("af-stream-chunk", this._evChunk); this._evChunk = null; }
    if (this._evEnd)   { document.removeEventListener("af-stream-end", this._evEnd);  this._evEnd   = null; }
    if (this._evConn)  { document.removeEventListener("af-connection-status", this._evConn); this._evConn = null; }
  }

  // ── Private: floating UI ──────────────────────────────────────────────────

  private _hideFloatingUI(): void {
    ["hud", "hud-stats", "io-bar", "chat-panel", "activity-feed", "feed-toggle"].forEach((id) => {
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
    else if (this.view === "chat") {
      body.appendChild(this._buildChatView());
      // _renderSidebar() inside _buildChatView() runs before the element is in
      // the DOM, so this.root.querySelector returns null. Re-run it now that
      // the chat view is attached.
      this._renderSidebar();
    }

    this.root.querySelectorAll<HTMLElement>(".af-view-btn[data-view]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset["view"] === this.view);
    });
    this._renderHealth();
  }

  private _setView(v: View): void {
    this.view = v;
    this._renderView();
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

    // Workers panel
    const wp = document.createElement("section");
    wp.className = "af-panel";
    wp.innerHTML = `<div class="af-panel-head"><h3>Wactorz</h3><span>actor model · MQTT pub-sub</span></div>`;
    const grid = document.createElement("div");
    grid.className = "af-cards-grid";
    grid.id = "af-worker-cards";
    [...this.agents.values()]
      .sort((a, b) => {
        if (a.name === "main-actor") return -1;
        if (b.name === "main-actor") return 1;
        return a.name.localeCompare(b.name);
      })
      .forEach((agent) => grid.appendChild(this._buildWorkerCard(agent)));
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
    const total    = agents.length;
    const healthy  = agents.filter((a) => stateLabel(a.state) === "running").length;
    const msgs     = agents.reduce((s, a) => s + (a.messagesProcessed ?? 0), 0);
    const cost     = agents.reduce((s, a) => s + (a.costUsd ?? 0), 0);
    const events   = this.feedItems.length;

    [
      { label: "Wactorz",      value: String(total),           detail: `${healthy} running`,               accent: "#60a5fa" },
      { label: "Messages",     value: String(msgs),            detail: "processed across actors",          accent: "#22d3a0" },
      { label: "Cost",         value: `$${cost.toFixed(4)}`,   detail: "reported by actors",               accent: "#f59e0b" },
      { label: "Feed Events",  value: String(events),          detail: "since dashboard loaded",           accent: "#8b5cf6" },
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
    const grid = this.root.querySelector<HTMLElement>("#af-worker-cards");
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
        grid.appendChild(this._buildWorkerCard(agent));
      }
    });
  }

  private _patchCard(agent: AgentInfo): void {
    const card = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agent.id)}"]`);
    if (!card) {
      if (this.view === "overview") this._renderCards();
      return;
    }
    const color = stateColor(agent.state);
    const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
    const lbl = card.querySelector<HTMLElement>(".af-card-state-label");
    const nm  = card.querySelector<HTMLElement>(".af-card-name");
    if (dot) { dot.style.background = color; dot.style.boxShadow = `0 0 8px ${color}`; }
    if (lbl) { lbl.style.color = color; lbl.textContent = stateLabel(agent.state); }
    if (nm)  nm.textContent = agent.name;
    this._rebuildControls(card, agent);
  }

  // ── Private: worker card ──────────────────────────────────────────────────

  private _buildWorkerCard(agent: AgentInfo): HTMLElement {
    const hbMs     = this.lastHb.get(agent.id) ?? 0;
    const color    = stateColor(agent.state);
    const status   = stateLabel(agent.state);
    const typeColor = agentTypeColor(agent.agentType);
    const msgs     = agent.messagesProcessed ?? 0;

    const card = document.createElement("div");
    card.className = "af-card";
    card.dataset.id = agent.id;

    const dot = document.createElement("div");
    dot.className = "af-card-state-dot";
    dot.style.background = color;
    dot.style.boxShadow  = `0 0 8px ${color}`;

    const badge = document.createElement("div");
    badge.className = "af-card-type-badge";
    badge.style.color       = typeColor;
    badge.style.borderColor = `${typeColor}55`;
    badge.textContent = agent.agentType ?? "worker";

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
    this._appendActionBtns(controls, agent);
    controls.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this._sendCommand(agent.id, btn.dataset.action as "pause" | "resume" | "stop" | "delete", btn);
    });
    card.appendChild(controls);

    if (agent.protected) {
      const shield = document.createElement("div");
      shield.className = "af-card-protected";
      shield.title = "Protected worker";
      shield.textContent = "🔒";
      card.appendChild(shield);
    }

    return card;
  }

  private _appendActionBtns(controls: HTMLElement, agent: AgentInfo): void {
    const status = stateLabel(agent.state);
    if (status === "running") {
      const b = document.createElement("button");
      b.className = "af-mini-btn"; b.textContent = "Pause"; b.dataset.action = "pause";
      controls.appendChild(b);
    }
    if (status === "paused") {
      const b = document.createElement("button");
      b.className = "af-mini-btn"; b.textContent = "Resume"; b.dataset.action = "resume";
      controls.appendChild(b);
    }
    if (!agent.protected && status !== "stopped") {
      const b = document.createElement("button");
      b.className = "af-mini-btn danger"; b.textContent = "Stop"; b.dataset.action = "stop";
      controls.appendChild(b);
    }
    if (!agent.protected) {
      const b = document.createElement("button");
      b.className = "af-mini-btn danger"; b.textContent = "Delete"; b.dataset.action = "delete";
      controls.appendChild(b);
    }
  }

  private _rebuildControls(card: HTMLElement, agent: AgentInfo): void {
    const controls = card.querySelector<HTMLElement>(".af-card-controls");
    if (!controls) return;
    // Toggle Chat button visibility based on state
    const chatBtn = controls.querySelector<HTMLButtonElement>(".af-chat-btn");
    if (chatBtn) chatBtn.hidden = stateLabel(agent.state) === "stopped";
    // Only replace the action buttons — the click listener from _buildWorkerCard
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
    setTimeout(() => { feed.scrollTop = feed.scrollHeight; }, 0);
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
      spawn: "af-feed-spawn", heartbeat: "af-feed-heartbeat", chat: "af-feed-chat",
      "alert-error": "af-feed-alert", "alert-warning": "af-feed-alert",
      health: "af-feed-heartbeat", "qa-flag": "af-feed-chat",
    };
    const TYPE_ICON: Record<string, string> = {
      spawn: "⚡", heartbeat: "♥", chat: "💬",
      "alert-error": "🔴", "alert-warning": "🟡",
      stopped: "◻", health: "◉", "qa-flag": "⚑",
    };

    const row = document.createElement("div");
    row.className = `af-feed-item ${TYPE_CLASS[item.type] ?? ""}`.trim();

    const icon = document.createElement("span");
    icon.className = "af-feed-icon";
    icon.textContent = TYPE_ICON[item.type] ?? "·";

    const time = document.createElement("span");
    time.className = "af-feed-time";
    time.textContent = new Date(item.timestamp).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
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
    list.innerHTML = "";
    [...this.agents.values()]
      .filter((a) => !this.sidebarFilter || a.name.toLowerCase().includes(this.sidebarFilter))
      .sort((a, b) => {
        if (a.name === "main-actor") return -1;
        if (b.name === "main-actor") return 1;
        return a.name.localeCompare(b.name);
      })
      .forEach((agent) => {
        const row = document.createElement("button");
        row.className = `af-chat-agent-row${agent.name === this.chatTarget ? " active" : ""}`;
        const dot = document.createElement("span");
        dot.className = "af-chat-agent-dot";
        dot.style.background = stateColor(agent.state);
        const nm = document.createElement("span");
        nm.className = "af-chat-agent-name";
        nm.textContent = agent.name;
        row.append(dot, nm);
        row.addEventListener("click", () => {
          this.chatTarget = agent.name;
          this._renderSidebar();
          this._renderChatPaneHeader();
          this._renderChatThread();
        });
        list.appendChild(row);
      });
  }

  private _renderChatPaneHeader(): void {
    const hdr = this.root.querySelector<HTMLElement>("#af-chat-pane-header");
    if (!hdr) return;
    hdr.innerHTML = "";
    const agent = [...this.agents.values()].find((a) => a.name === this.chatTarget);
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

  private _renderChatThread(): void {
    const thread = this.root.querySelector<HTMLElement>("#af-chat-thread");
    if (!thread) return;
    thread.innerHTML = "";
    const msgs = this.chatMessages.filter((m) => {
      if (m.from === "user") return true;
      if (m.from === "io-gateway" || m.from === "system") return true;
      return m.from === this.chatTarget;
    });
    if (msgs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "af-chat-empty";
      empty.innerHTML = this.chatTarget === "main-actor"
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
    const thread = container ?? this.root.querySelector<HTMLElement>("#af-chat-thread");
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
        hour: "2-digit", minute: "2-digit",
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
      this.connState === "live" ? "● live" :
      this.connState === "connecting" ? "○ Connecting…" :
      "◎ Demo fallback";
  }

  private _renderHealth(): void {
    const el = this.root.querySelector<HTMLElement>(".af-health");
    if (!el) return;
    const agents = [...this.agents.values()];
    const healthy = agents.filter((a) => stateLabel(a.state) === "running").length;
    el.textContent = `${healthy}/${agents.length} workers healthy`;
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
    input.placeholder = "Message @main-actor…";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._sendMessage(input, select);
      }
    });
    select.addEventListener("change", () => {
      const t = select.value;
      input.placeholder = t === "main-actor" ? "Message @main-actor…" : `Context: @${t} — asking @main-actor…`;
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
  }

  private _updateTargetSelect(): void {
    const select = this.root.querySelector<HTMLSelectElement>("#af-target-select");
    if (select) this._populateSelect(select);
  }

  private _sendMessage(input: HTMLInputElement, select: HTMLSelectElement): void {
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
    if (this.view === "chat") {
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
      new CustomEvent("af-agent-command", { detail: { command: action, agentId: id } }),
    );
  }

  // ── Private: timestamp refresh ────────────────────────────────────────────

  private _refreshTimestamps(): void {
    this.lastHb.forEach((ms, id) => {
      const el = this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"] .af-card-hb-time`);
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
    health.textContent = "0/0 workers healthy";
    center.appendChild(health);

    const right = document.createElement("div");
    right.className = "af-header-right";

    (["overview", "feed", "chat"] as View[]).forEach((key) => {
      const label = key === "overview" ? "◫ Overview" : key === "feed" ? "≡ Feed" : "💬 Chat";
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
}

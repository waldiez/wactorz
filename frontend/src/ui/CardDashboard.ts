/**
 * CardDashboard — pure HTML/CSS agent overview, agentflow-style.
 *
 * Shown when the "Cards" theme is active.  Renders agent state as live DOM
 * cards using the af-card design from the synapse agentflow UI.
 */

import type { AgentInfo, AgentState } from "../types/agent";

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

function stateClass(state: AgentState): string {
  if (typeof state === "object") return "error";
  return state as string;
}

function stateLabel(state: AgentState): string {
  if (typeof state === "object") return "failed";
  return state as string;
}

function relTime(ms: number): string {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 5)  return "now";
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}

const LAYOUT_KEY = "wactorz-card-layout";
type Layout = "af" | "classic";

export class CardDashboard {
  private container: HTMLElement;
  private grid: HTMLElement;
  private agents: Map<string, AgentInfo> = new Map();
  private lastHb: Map<string, number> = new Map();
  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private unreadListener: ((e: Event) => void) | null = null;
  private unreadClearListener: ((e: Event) => void) | null = null;
  private layout: Layout = (localStorage.getItem(LAYOUT_KEY) as Layout) ?? "af";

  constructor() {
    this.container = this.buildContainer();
    this.grid = this.container.querySelector(".cd-card-grid")!;
    document.body.appendChild(this.container);
    this.applyLayout();

    this.unreadListener = (e) => {
      const { name, count } = (e as CustomEvent<{ name: string; count: number }>).detail;
      const btn = this.grid.querySelector<HTMLElement>(
        `[data-chat-name="${CSS.escape(name)}"]`,
      );
      if (!btn) return;
      let badge = btn.querySelector<HTMLElement>(".chat-unread-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "chat-unread-badge";
        btn.appendChild(badge);
      }
      badge.textContent = String(count);
    };
    document.addEventListener("agent-unread", this.unreadListener);

    this.unreadClearListener = (e) => {
      const { name } = (e as CustomEvent<{ name: string }>).detail;
      this.grid
        .querySelector(`[data-chat-name="${CSS.escape(name)}"] .chat-unread-badge`)
        ?.remove();
    };
    document.addEventListener("agent-unread-cleared", this.unreadClearListener);
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    this.container.classList.add("cd-visible");
    agents.forEach((a) => this.agents.set(a.id, a));
    this.renderAll();
    this.tickTimer = setInterval(() => this.refreshTimestamps(), 5000);
  }

  hide(): void {
    this.container.classList.remove("cd-visible");
    if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null; }
  }

  destroy(): void {
    this.hide();
    if (this.unreadListener) {
      document.removeEventListener("agent-unread", this.unreadListener);
      this.unreadListener = null;
    }
    if (this.unreadClearListener) {
      document.removeEventListener("agent-unread-cleared", this.unreadClearListener);
      this.unreadClearListener = null;
    }
    this.container.remove();
  }

  // ── Agent events ───────────────────────────────────────────────────────────

  addAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    this.renderAll();
  }

  updateAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    this.renderCard(agent);
  }

  removeAgent(id: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
    if (card) {
      card.style.animation = "cd-exit 0.25s ease forwards";
      setTimeout(() => { card.remove(); this.agents.delete(id); }, 250);
    } else {
      this.agents.delete(id);
    }
  }

  onHeartbeat(agentId: string, timestampMs: number): void {
    this.lastHb.set(agentId, timestampMs);
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;

    const hbEl = card.querySelector<HTMLElement>(".af-card-hb-time, .cd-hb-time");
    if (hbEl) hbEl.textContent = relTime(timestampMs);

    const agent = this.agents.get(agentId);
    if (agent) {
      const sc = stateClass(agent.state);
      const dot  = card.querySelector<HTMLElement>(".af-card-state-dot");
      const lbl  = card.querySelector<HTMLElement>(".af-card-state-label");
      if (dot) { dot.className = `af-card-state-dot af-dot-${sc}`; }
      if (lbl) { lbl.className = `af-card-state-label af-state-${sc}`; lbl.textContent = stateLabel(agent.state); }
      this.updateControls(card, agent);
    }

    // Pulse the dot
    const dotSel = this.layout === "af" ? ".af-card-state-dot" : ".cd-status-dot";
    const pulseClass = this.layout === "af" ? "af-card-pulse" : "cd-pulse-once";
    const dot = card.querySelector<HTMLElement>(dotSel);
    if (dot) {
      dot.classList.remove(pulseClass);
      void dot.offsetWidth;
      dot.classList.add(pulseClass);
    }
  }

  showAlert(agentId: string, severity: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;
    const cls = severity === "error" || severity === "critical" ? "af-card-alert-error" : "af-card-alert-warn";
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls, "af-card-alert-error", "af-card-alert-warn"), 900);
  }

  onChat(fromId: string, _toId: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
    if (!card) return;
    card.classList.add("af-card-chat-flash");
    setTimeout(() => card.classList.remove("af-card-chat-flash"), 600);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  private renderAll(): void {
    const sorted = [...this.agents.values()].sort((a, b) => {
      if (a.name === "main-actor") return -1;
      if (b.name === "main-actor") return 1;
      return a.name.localeCompare(b.name);
    });
    const live = new Set(sorted.map((a) => a.id));
    this.grid.querySelectorAll<HTMLElement>("[data-id]").forEach((el) => {
      if (!live.has(el.dataset.id!)) el.remove();
    });
    sorted.forEach((a) => {
      if (!this.grid.querySelector(`[data-id="${CSS.escape(a.id)}"]`)) {
        this.grid.appendChild(this.buildCard(a));
      } else {
        this.renderCard(a);
      }
    });
  }

  private renderCard(agent: AgentInfo): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agent.id)}"]`);
    if (!card) { this.grid.appendChild(this.buildCard(agent)); return; }

    const sc = stateClass(agent.state);
    if (this.layout === "af") {
      const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
      const lbl = card.querySelector<HTMLElement>(".af-card-state-label");
      const nm  = card.querySelector<HTMLElement>(".af-card-name");
      if (dot) dot.className = `af-card-state-dot af-dot-${sc}`;
      if (lbl) { lbl.className = `af-card-state-label af-state-${sc}`; lbl.textContent = stateLabel(agent.state); }
      if (nm)  nm.textContent = agent.name;
    } else {
      const dot   = card.querySelector<HTMLElement>(".cd-status-dot");
      const badge = card.querySelector<HTMLElement>(".cd-badge");
      const nm    = card.querySelector<HTMLElement>(".cd-name");
      if (dot)   dot.className   = `cd-status-dot cd-dot-${sc}`;
      if (badge) { badge.className = `cd-badge cd-badge-${sc}`; badge.textContent = stateLabel(agent.state).toUpperCase(); }
      if (nm)    nm.textContent  = agent.name;
    }
    this.updateControls(card, agent);
  }

  private updateControls(card: HTMLElement, agent: AgentInfo): void {
    const st   = stateClass(agent.state);
    const prot = agent.protected ?? false;

    const pauseBtn  = card.querySelector<HTMLButtonElement>('[data-action="pause"]');
    const resumeBtn = card.querySelector<HTMLButtonElement>('[data-action="resume"]');
    const stopBtn   = card.querySelector<HTMLButtonElement>('[data-action="stop"]');
    const deleteBtn = card.querySelector<HTMLButtonElement>('[data-action="delete"]');

    if (pauseBtn)  pauseBtn.style.display  = st === "running" ? "" : "none";
    if (resumeBtn) resumeBtn.style.display = st === "paused"  ? "" : "none";
    if (stopBtn)   { stopBtn.style.display = st !== "stopped" ? "" : "none"; stopBtn.disabled = prot; }
    if (deleteBtn) deleteBtn.disabled = prot;
  }

  private buildCard(agent: AgentInfo): HTMLElement {
    return this.layout === "classic"
      ? this.buildCardClassic(agent)
      : this.buildCardAF(agent);
  }

  private buildCardAF(agent: AgentInfo): HTMLElement {
    const hbMs = this.lastHb.get(agent.id) ?? 0;
    const sc   = stateClass(agent.state);
    const msgs = agent.messagesProcessed ?? 0;
    const cost = agent.costUsd != null ? `$${agent.costUsd.toFixed(4)}` : "";

    const card = document.createElement("div");
    card.className = "af-card";
    card.dataset.id = agent.id;
    card.title = agent.id;

    card.innerHTML = `
      <div class="af-card-header-row">
        <span class="af-card-state-dot af-dot-${sc}"></span>
        <span class="af-card-type-badge">${agent.agentType ?? "WORKER"}</span>
        ${agent.protected ? '<span class="af-card-protected" title="protected">🛡</span>' : ""}
      </div>
      <div class="af-card-name">${agent.name}</div>
      <div class="af-card-state-label af-state-${sc}">${stateLabel(agent.state)}</div>
      <div class="af-card-meta">
        <span>♥ <span class="af-card-hb-time">${hbMs ? relTime(hbMs) : "—"}</span></span>
        <span><span class="af-card-msg-count">${msgs}</span> msgs</span>
        ${cost ? `<span>${cost}</span>` : ""}
      </div>
      <div class="af-card-footer">
        <button class="af-card-btn af-card-btn-primary" data-chat-name="${agent.name}" data-id="${agent.id}">Chat</button>
        <button class="af-card-btn" data-action="pause">Pause</button>
        <button class="af-card-btn" data-action="resume" style="display:none">Resume</button>
        <button class="af-card-btn af-card-btn-danger" data-action="stop">Stop</button>
        <button class="af-card-btn af-card-btn-danger" data-action="delete" title="Delete">✕</button>
      </div>
    `;

    card.querySelector("[data-chat-name]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      document.dispatchEvent(new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent } }));
    });
    card.querySelector(".af-card-footer")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this.sendCommand(agent.id, agent.name, btn.dataset.action as "pause" | "resume" | "stop" | "delete");
    });

    this.updateControls(card, agent);
    return card;
  }

  private buildCardClassic(agent: AgentInfo): HTMLElement {
    const hbMs  = this.lastHb.get(agent.id) ?? 0;
    const sc    = stateClass(agent.state);
    const isMain = agent.name === "main-actor" || agent.agentType === "orchestrator";
    const accent = sc === "running" ? "#3dd68c" : sc === "paused" ? "#fb923c" :
                   sc === "initializing" ? "#60a5fa" : sc === "error" ? "#f43f5e" : "#475569";

    const card = document.createElement("div");
    card.className = `cd-card${isMain ? " cd-card-main" : ""}`;
    card.dataset.id = agent.id;
    card.title = agent.id;
    card.style.setProperty("--accent", accent);

    card.innerHTML = `
      <div class="cd-accent-bar"></div>
      <div class="cd-body">
        <div class="cd-header">
          <div class="cd-avatar">
            <span style="font-size:16px;font-weight:700;color:var(--accent)">${(agent.name[0] ?? "?").toUpperCase()}</span>
          </div>
          <div class="cd-info">
            <div class="cd-name-row">
              <span class="cd-name">${agent.name}</span>
              ${agent.protected ? '<span class="cd-shield" title="protected">🛡</span>' : ""}
            </div>
            <span class="cd-type">${agent.agentType ?? ""}</span>
          </div>
          <span class="cd-badge cd-badge-${sc}">${stateLabel(agent.state).toUpperCase()}</span>
        </div>
        <div class="cd-divider"></div>
        <div class="cd-meta">
          <div class="cd-meta-row">
            <div class="cd-status-dot cd-dot-${sc}"></div>
            <span class="cd-meta-key">heartbeat</span>
            <span class="cd-meta-val cd-hb-time">${hbMs ? relTime(hbMs) : "—"}</span>
          </div>
        </div>
        <div class="cd-footer">
          <button class="cd-chat-btn" data-chat-name="${agent.name}" data-id="${agent.id}">💬 Chat</button>
          <div class="cd-controls">
            <button class="cd-ctrl" data-action="pause"  title="Pause">⏸</button>
            <button class="cd-ctrl" data-action="resume" title="Resume">▶</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="stop"   title="Stop">⏹</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="delete" title="Delete">🗑</button>
          </div>
        </div>
      </div>
    `;

    card.querySelector("[data-chat-name]")?.addEventListener("click", (e) => {
      e.stopPropagation();
      document.dispatchEvent(new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent } }));
    });
    card.querySelector(".cd-controls")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this.sendCommand(agent.id, agent.name, btn.dataset.action as "pause" | "resume" | "stop" | "delete");
    });

    this.updateControls(card, agent);
    return card;
  }

  // ── Agent control API ──────────────────────────────────────────────────────

  private sendCommand(id: string, name: string, action: "pause" | "resume" | "stop" | "delete"): void {
    const base = `/api/actors/${encodeURIComponent(id)}`;
    const [url, method] =
      action === "pause"   ? [`${base}/pause`, "POST"] :
      action === "resume"  ? [`${base}/resume`, "POST"] :
                             [base, "DELETE"];

    fetch(url, { method })
      .then((r) => {
        if (!r.ok && r.status !== 404) { this.flashError(id); return; }
        if (action === "delete") this.removeAgent(id);
      })
      .catch(() => this.flashError(id));
  }

  private flashError(id: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
    if (!card) return;
    card.classList.add("af-card-alert-error");
    setTimeout(() => card.classList.remove("af-card-alert-error"), 900);
  }

  private applyLayout(): void {
    const isAF = this.layout === "af";
    const gridClass = isAF ? "af-cards-grid" : "cd-grid";
    this.grid.className = gridClass;
    const btn = this.container.querySelector<HTMLButtonElement>(".cd-layout-btn");
    if (btn) btn.textContent = isAF ? "⊞ Classic" : "⊟ Compact";
  }

  private refreshTimestamps(): void {
    const timeClass = this.layout === "af" ? ".af-card-hb-time" : ".cd-hb-time";
    this.lastHb.forEach((ms, id) => {
      const el = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"] ${timeClass}`);
      if (el) el.textContent = relTime(ms);
    });
  }

  // ── DOM skeleton ───────────────────────────────────────────────────────────

  private buildContainer(): HTMLElement {
    const el = document.createElement("div");
    el.id = "card-dashboard";
    el.className = "cd-root";
    el.innerHTML = `
      <div class="cd-topbar">
        <span class="cd-title">Wactorz</span>
        <span class="cd-subtitle">Live Agents</span>
        <button class="cd-layout-btn" title="Switch card layout">⊞ Classic</button>
        <button class="cd-3d-btn" title="Switch to 3D view">⬡ 3D</button>
      </div>
      <div class="cd-card-grid"></div>
    `;

    el.querySelector(".cd-layout-btn")?.addEventListener("click", () => {
      this.layout = this.layout === "af" ? "classic" : "af";
      localStorage.setItem(LAYOUT_KEY, this.layout);
      this.applyLayout();
      this.grid.innerHTML = "";
      this.renderAll();
    });

    el.querySelector(".cd-3d-btn")?.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: "cards-3d" } }));
    });

    return el;
  }
}

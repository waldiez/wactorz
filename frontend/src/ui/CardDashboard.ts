/**
 * CardDashboard — pure HTML/CSS agent overview.
 *
 * Shown when the "Cards" theme is active.  Renders agent state as live DOM
 * cards; no Babylon.js involved.  All state changes arrive through the same
 * event methods used by the 3D themes (onHeartbeat, showAlert, etc.).
 *
 * Waldiez palette:
 *   teal   #3dd68c  running / accent
 *   amber  #f59e0b  main-actor / paused
 *   rose   #f43f5e  error / failed
 *   slate  #475569  stopped / muted
 *   sky    #60a5fa  initializing
 */

import type { AgentInfo, AgentState } from "../types/agent";
import { agentImageGen } from "../io/AgentImageGen";

// ── Palette helpers ──────────────────────────────────────────────────────────

function accentFor(info: AgentInfo): string {
  if (info.name === "main-actor" || info.agentType === "orchestrator") return "#f59e0b";
  if (typeof info.state === "object") return "#f43f5e";
  switch (info.state as string) {
    case "running":      return "#3dd68c";
    case "paused":       return "#fb923c";
    case "initializing": return "#60a5fa";
    case "stopped":      return "#475569";
    default:             return "#3dd68c";
  }
}

function stateLabel(state: AgentState): string {
  if (typeof state === "object") return "FAILED";
  return (state as string).toUpperCase();
}

function relTime(ms: number): string {
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}

// ── CardDashboard ────────────────────────────────────────────────────────────

export class CardDashboard {
  private container:     HTMLElement;
  private grid:          HTMLElement;
  private agents:        Map<string, AgentInfo> = new Map();
  private lastHb:        Map<string, number>    = new Map();
  private tickTimer:     ReturnType<typeof setInterval> | null = null;
  private unreadListener:       ((e: Event) => void) | null = null;
  private unreadClearListener:  ((e: Event) => void) | null = null;

  constructor() {
    this.container = this.buildContainer();
    this.grid      = this.container.querySelector(".cd-grid")!;
    document.body.appendChild(this.container);

    // Unread badge: show count when a background thread gets a message
    this.unreadListener = (e) => {
      const { name, count } = (e as CustomEvent<{ name: string; count: number }>).detail;
      const btn = this.grid.querySelector<HTMLElement>(
        `.cd-chat-btn[data-name="${CSS.escape(name)}"]`,
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

    // Unread clear: remove badge when user opens that thread
    this.unreadClearListener = (e) => {
      const { name } = (e as CustomEvent<{ name: string }>).detail;
      this.grid
        .querySelector(`.cd-chat-btn[data-name="${CSS.escape(name)}"] .chat-unread-badge`)
        ?.remove();
    };
    document.addEventListener("agent-unread-cleared", this.unreadClearListener);
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    this.container.classList.add("cd-visible");
    agents.forEach((a) => this.agents.set(a.id, a));
    this.renderAll();
    // Tick every 5 s to refresh "Xs ago" timestamps
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
    const card = this.grid.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (card) {
      (card as HTMLElement).style.animation = "cd-exit 0.25s ease forwards";
      setTimeout(() => { card.remove(); this.agents.delete(id); }, 250);
    } else {
      this.agents.delete(id);
    }
  }

  onHeartbeat(agentId: string, timestampMs: number): void {
    this.lastHb.set(agentId, timestampMs);
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;

    // Update heartbeat row
    const hbEl = card.querySelector<HTMLElement>(".cd-hb-time");
    if (hbEl) hbEl.textContent = relTime(timestampMs);

    // Sync state badge — agent.state is already mutated in-place by SceneManager
    const agent = this.agents.get(agentId);
    if (agent) {
      const stType  = typeof agent.state === "object" ? "error" : (agent.state as string);
      const badgeEl = card.querySelector<HTMLElement>(".cd-badge");
      const dotEl   = card.querySelector<HTMLElement>(".cd-status-dot");
      if (badgeEl) { badgeEl.textContent = stateLabel(agent.state); badgeEl.className = `cd-badge cd-badge-${stType}`; }
      if (dotEl)   dotEl.className       = `cd-status-dot cd-dot-${stType}`;
      card.style.setProperty("--accent", accentFor(agent));
      this.updateControls(card, agent);
    }

    // Pulse the status dot
    const dot = card.querySelector<HTMLElement>(".cd-status-dot");
    if (dot) {
      dot.classList.remove("cd-pulse-once");
      void dot.offsetWidth;
      dot.classList.add("cd-pulse-once");
    }
  }

  showAlert(agentId: string, severity: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;
    const cls = severity === "error" || severity === "critical" ? "cd-alert-error" : "cd-alert-warn";
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls, "cd-alert-error", "cd-alert-warn"), 900);
  }

  onChat(fromId: string, _toId: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
    if (!card) return;
    card.classList.add("cd-chat-flash");
    setTimeout(() => card.classList.remove("cd-chat-flash"), 600);
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  private renderAll(): void {
    const sorted = [...this.agents.values()].sort((a, b) => {
      if (a.name === "main-actor") return -1;
      if (b.name === "main-actor") return 1;
      return a.name.localeCompare(b.name);
    });

    // Sync: add missing, update existing, remove gone
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

    const accent = accentFor(agent);
    card.style.setProperty("--accent", accent);

    const stType = typeof agent.state === "object" ? "error" : (agent.state as string);
    const nameEl  = card.querySelector<HTMLElement>(".cd-name");
    const badgeEl = card.querySelector<HTMLElement>(".cd-badge");
    const typeEl  = card.querySelector<HTMLElement>(".cd-type");
    const dotEl   = card.querySelector<HTMLElement>(".cd-status-dot");
    if (nameEl)  nameEl.textContent  = agent.name;
    if (badgeEl) { badgeEl.textContent = stateLabel(agent.state); badgeEl.className = `cd-badge cd-badge-${stType}`; }
    if (typeEl)  typeEl.textContent  = agent.agentType ?? "";
    if (dotEl)   dotEl.className     = `cd-status-dot cd-dot-${stType}`;

    this.updateControls(card, agent);
  }

  /** Show/hide and enable/disable control buttons based on current agent state. */
  private updateControls(card: HTMLElement, agent: AgentInfo): void {
    const st   = typeof agent.state === "object" ? "error" : (agent.state as string);
    const prot = agent.protected ?? false;

    const pauseBtn  = card.querySelector<HTMLButtonElement>('[data-action="pause"]');
    const resumeBtn = card.querySelector<HTMLButtonElement>('[data-action="resume"]');
    const stopBtn   = card.querySelector<HTMLButtonElement>('[data-action="stop"]');
    const deleteBtn = card.querySelector<HTMLButtonElement>('[data-action="delete"]');

    if (pauseBtn)  pauseBtn.style.display  = st === "running" ? "" : "none";
    if (resumeBtn) resumeBtn.style.display = st === "paused"  ? "" : "none";
    if (stopBtn) {
      stopBtn.style.display = st !== "stopped" ? "" : "none";
      stopBtn.disabled      = prot;
      stopBtn.title         = prot ? "Protected — cannot stop" : "Stop";
    }
    if (deleteBtn) {
      deleteBtn.disabled = prot;
      deleteBtn.title    = prot ? "Protected — cannot delete" : "Delete";
    }
  }

  private buildCard(agent: AgentInfo): HTMLElement {
    const accent = accentFor(agent);
    const hbMs   = this.lastHb.get(agent.id) ?? 0;
    const isMain = agent.name === "main-actor" || agent.agentType === "orchestrator";
    const stType = typeof agent.state === "object" ? "error" : (agent.state as string);

    const card = document.createElement("div");
    card.className = `cd-card${isMain ? " cd-card-main" : ""}`;
    card.dataset.id = agent.id;
    card.style.setProperty("--accent", accent);
    card.title = agent.id;

    card.innerHTML = `
      <div class="cd-accent-bar"></div>
      <div class="cd-body">
        <div class="cd-header">
          <div class="cd-avatar">
            <img src="${agentImageGen.get(agent)}" alt="${agent.name}" loading="lazy"
                 onerror="this.style.display='none'">
          </div>
          <div class="cd-info">
            <div class="cd-name-row">
              <span class="cd-name">${agent.name}</span>
              ${agent.protected ? '<span class="cd-shield" title="protected">🛡</span>' : ""}
            </div>
            <span class="cd-type">${agent.agentType ?? ""}</span>
          </div>
          <span class="cd-badge cd-badge-${stType}">${stateLabel(agent.state)}</span>
        </div>

        <div class="cd-divider"></div>

        <div class="cd-meta">
          <div class="cd-meta-row">
            <span class="cd-meta-key">ID</span>
            <span class="cd-meta-val cd-mono">…${agent.id.slice(-20)}</span>
          </div>
          ${agent.agentType ? `<div class="cd-meta-row">
            <span class="cd-meta-key">type</span>
            <span class="cd-meta-val">${agent.agentType}</span>
          </div>` : ""}
          <div class="cd-meta-row">
            <div class="cd-status-dot cd-dot-${stType}"></div>
            <span class="cd-meta-key">heartbeat</span>
            <span class="cd-meta-val cd-hb-time">${hbMs ? relTime(hbMs) : "—"}</span>
          </div>
        </div>

        <div class="cd-footer">
          <button class="cd-chat-btn" data-id="${agent.id}" data-name="${agent.name}">
            💬 Chat
          </button>
          <div class="cd-controls">
            <button class="cd-ctrl" data-action="pause"  title="Pause">⏸</button>
            <button class="cd-ctrl" data-action="resume" title="Resume">▶</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="stop"   title="Stop">⏹</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="delete" title="Delete">🗑</button>
          </div>
        </div>
      </div>
    `;

    card.querySelector(".cd-chat-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      document.dispatchEvent(
        new CustomEvent<{ agent: AgentInfo }>("agent-selected", { detail: { agent } }),
      );
    });

    card.querySelector(".cd-controls")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      const action = btn.dataset.action as "pause" | "resume" | "stop" | "delete";
      this.sendCommand(agent.id, agent.name, action);
    });

    this.updateControls(card, agent);
    return card;
  }

  // ── Agent control API ──────────────────────────────────────────────────────

  private sendCommand(
    id: string,
    name: string,
    action: "pause" | "resume" | "stop" | "delete",
  ): void {
    const base = `/api/actors/${encodeURIComponent(id)}`;
    const [url, method] =
      action === "pause"  ? [`${base}/pause`,  "POST"] :
      action === "resume" ? [`${base}/resume`, "POST"] :
                            [base,             "DELETE"]; // stop + delete both send Stop

    fetch(url, { method })
      .then((r) => {
        if (!r.ok && r.status !== 404) {
          console.warn(`[CardDashboard] ${action} ${name}: HTTP ${r.status}`);
          this.flashError(id);
          return;
        }
        // "delete" removes the card immediately; "stop" waits for MQTT status event
        if (action === "delete") {
          this.removeAgent(id);
        }
      })
      .catch(() => this.flashError(id));
  }

  /** Briefly flash the card border red on a failed command. */
  private flashError(id: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
    if (!card) return;
    card.classList.add("cd-alert-error");
    setTimeout(() => card.classList.remove("cd-alert-error"), 900);
  }

  private refreshTimestamps(): void {
    this.lastHb.forEach((ms, id) => {
      const el = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"] .cd-hb-time`);
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
        <span class="cd-title">Wactorz — Live Agents</span>
        <span class="cd-subtitle">Cards view</span>
        <button class="cd-3d-btn" title="Switch to Babylon.js 3D card grid">⬡ 3D</button>
      </div>
      <div class="cd-grid"></div>
    `;

    el.querySelector(".cd-3d-btn")?.addEventListener("click", () => {
      document.dispatchEvent(
        new CustomEvent("theme-change", { detail: { theme: "cards-3d" } }),
      );
    });

    return el;
  }
}

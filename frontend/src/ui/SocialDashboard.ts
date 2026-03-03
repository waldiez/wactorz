/**
 * SocialDashboard — social-network style HTML overlay.
 *
 * Agents appear as profile cards (Instagram × Twitter hybrid).
 * Profile photos are served by {@link AgentImageGen}: DiceBear robots
 * instantly, then swapped for a Gemini-generated portrait when ready.
 *
 * Controls per card:  💬 Chat  ⏸ Pause  ▶ Resume  ⏹ Stop  🗑 Delete
 * Same REST API endpoints as CardDashboard.
 */

import type { AgentInfo, AgentState } from "../types/agent";
import { agentImageGen } from "../io/AgentImageGen";

// ── Helpers ───────────────────────────────────────────────────────────────────

function stateStr(state: AgentState): string {
  return typeof state === "object" ? "error" : (state as string);
}

function coverGradient(info: AgentInfo): string {
  const t = (info.agentType ?? "").toLowerCase();
  const n = info.name.toLowerCase();
  if (n === "main-actor"  || t.includes("orchestrator")) return "linear-gradient(135deg,#78350f,#b45309,#f59e0b)";
  if (t.includes("monitor") || n.includes("monitor"))    return "linear-gradient(135deg,#172554,#1e3a8a,#3b82f6)";
  if (t.includes("guardian") || n.includes("qa"))        return "linear-gradient(135deg,#14532d,#15803d,#4ade80)";
  if (t.includes("gateway")  || n.includes("io"))        return "linear-gradient(135deg,#0c4a6e,#0284c7,#38bdf8)";
  if (t.includes("expert")   || n.includes("udx"))       return "linear-gradient(135deg,#713f12,#b45309,#fcd34d)";
  if (t.includes("dynamic")  || t.includes("script"))    return "linear-gradient(135deg,#581c87,#7c3aed,#a78bfa)";
  if (n.includes("math"))                                 return "linear-gradient(135deg,#1e1b4b,#3730a3,#818cf8)";
  if (n.includes("weather"))                              return "linear-gradient(135deg,#0c4a6e,#0ea5e9,#bae6fd)";
  if (n.includes("ml") || n.includes("classifier"))      return "linear-gradient(135deg,#4c0519,#be123c,#fb7185)";
  return "linear-gradient(135deg,#0f2027,#203a43,#2c5364)";
}

function bioline(info: AgentInfo): string {
  const t = (info.agentType ?? "").toLowerCase();
  const n = info.name.toLowerCase();
  if (t.includes("orchestrator"))  return "Central AI orchestrator · spawns + routes agents";
  if (t.includes("monitor"))       return "System health monitor · tracks all actors";
  if (t.includes("guardian"))      return "QA guardian · passive safety observer";
  if (t.includes("gateway"))       return "User I/O gateway · routes messages";
  if (t.includes("expert") || n.includes("udx")) return "User & Developer Xpert · built-in knowledge base";
  if (t.includes("dynamic"))       return "Runtime script agent · LLM-generated";
  if (n.includes("math"))          return "Math evaluator · runs Rhai expressions";
  if (n.includes("weather"))       return "Weather data fetcher";
  if (n.includes("ml") || n.includes("classifier")) return "ML inference agent";
  return info.name.replace(/-/g, " ");
}

// ── SocialDashboard ───────────────────────────────────────────────────────────

export class SocialDashboard {
  private container: HTMLElement;
  private grid:      HTMLElement;
  private agents     = new Map<string, AgentInfo>();
  private heartbeats = new Map<string, number>();
  private messages   = new Map<string, number>();
  private imageListener:       ((e: Event) => void) | null = null;
  private unreadListener:      ((e: Event) => void) | null = null;
  private unreadClearListener: ((e: Event) => void) | null = null;
  private tickTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    this.container = this.buildContainer();
    this.grid = this.container.querySelector(".sd-grid")!;
    document.body.appendChild(this.container);

    // Swap in AI-generated images when they arrive
    this.imageListener = (e) => {
      const { id, url } = (e as CustomEvent<{ id: string; url: string }>).detail;
      const img = this.grid.querySelector<HTMLImageElement>(
        `[data-id="${CSS.escape(id)}"] .sd-avatar img`,
      );
      if (img) img.src = url;
    };
    document.addEventListener("agent-image-ready", this.imageListener);

    // Unread badge on the chat button
    this.unreadListener = (e) => {
      const { name, count } = (e as CustomEvent<{ name: string; count: number }>).detail;
      const btn = this.grid.querySelector<HTMLElement>(
        `.sd-chat-btn[data-name="${CSS.escape(name)}"]`,
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
        .querySelector(`.sd-chat-btn[data-name="${CSS.escape(name)}"] .chat-unread-badge`)
        ?.remove();
    };
    document.addEventListener("agent-unread-cleared", this.unreadClearListener);
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    this.container.classList.add("sd-visible");
    agents.forEach((a) => this.agents.set(a.id, a));
    this.renderAll();
    this.tickTimer = setInterval(() => {}, 60_000); // placeholder for future ticks
  }

  hide(): void {
    this.container.classList.remove("sd-visible");
    if (this.tickTimer) { clearInterval(this.tickTimer); this.tickTimer = null; }
  }

  destroy(): void {
    this.hide();
    if (this.imageListener) {
      document.removeEventListener("agent-image-ready", this.imageListener);
      this.imageListener = null;
    }
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
      card.style.animation = "sd-exit 0.3s ease forwards";
      setTimeout(() => { card.remove(); this.agents.delete(id); }, 300);
    } else {
      this.agents.delete(id);
    }
  }

  onHeartbeat(agentId: string, _ts: number): void {
    this.heartbeats.set(agentId, (this.heartbeats.get(agentId) ?? 0) + 1);
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;

    // Update beat counter
    const el = card.querySelector<HTMLElement>(".sd-hb-count");
    if (el) el.textContent = String(this.heartbeats.get(agentId));

    // Sync state badge — agent.state is mutated in-place by SceneManager before this call.
    // A dynamically-spawned agent arrives with state "initializing" in its spawn payload;
    // subsequent heartbeats carry "running", so we must re-render the badge each beat.
    const agent = this.agents.get(agentId);
    if (agent) {
      const st = stateStr(agent.state);
      const dot   = card.querySelector<HTMLElement>(".sd-state-dot");
      const badge = card.querySelector<HTMLElement>(".sd-type-badge");
      if (dot)   dot.className = `sd-state-dot sd-dot-${st}`;
      if (badge) { badge.textContent = st.toUpperCase(); badge.className = `sd-type-badge sd-badge-${st}`; }
      this.updateControls(card, agent);
    }

    // Pulse avatar ring
    const ring = card.querySelector<HTMLElement>(".sd-avatar");
    if (ring) {
      ring.classList.remove("sd-avatar-pulse");
      void ring.offsetWidth;
      ring.classList.add("sd-avatar-pulse");
    }
  }

  onChat(fromId: string, _toId: string): void {
    this.messages.set(fromId, (this.messages.get(fromId) ?? 0) + 1);
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
    if (!card) return;
    const el = card.querySelector<HTMLElement>(".sd-msg-count");
    if (el) el.textContent = String(this.messages.get(fromId));
    card.classList.add("sd-chat-flash");
    setTimeout(() => card.classList.remove("sd-chat-flash"), 600);
  }

  showAlert(agentId: string, severity: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;
    const cls = severity === "error" || severity === "critical" ? "sd-alert-error" : "sd-alert-warn";
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls), 900);
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

    const st = stateStr(agent.state);
    const dot   = card.querySelector<HTMLElement>(".sd-state-dot");
    const badge = card.querySelector<HTMLElement>(".sd-type-badge");
    if (dot)   dot.className   = `sd-state-dot sd-dot-${st}`;
    if (badge) { badge.textContent = st.toUpperCase(); badge.className = `sd-type-badge sd-badge-${st}`; }
    this.updateControls(card, agent);
  }

  private updateControls(card: HTMLElement, agent: AgentInfo): void {
    const st   = stateStr(agent.state);
    const prot = agent.protected ?? false;
    const pauseBtn  = card.querySelector<HTMLButtonElement>('[data-action="pause"]');
    const resumeBtn = card.querySelector<HTMLButtonElement>('[data-action="resume"]');
    const stopBtn   = card.querySelector<HTMLButtonElement>('[data-action="stop"]');
    const deleteBtn = card.querySelector<HTMLButtonElement>('[data-action="delete"]');
    if (pauseBtn)  pauseBtn.style.display  = st === "running" ? "" : "none";
    if (resumeBtn) resumeBtn.style.display = st === "paused"  ? "" : "none";
    if (stopBtn)   { stopBtn.style.display = st !== "stopped" ? "" : "none"; stopBtn.disabled = prot; }
    if (deleteBtn) { deleteBtn.disabled = prot; deleteBtn.title = prot ? "Protected" : "Delete"; }
  }

  private buildCard(agent: AgentInfo): HTMLElement {
    const imgUrl = agentImageGen.get(agent);
    const st     = stateStr(agent.state);
    const hb     = this.heartbeats.get(agent.id) ?? 0;
    const msgs   = this.messages.get(agent.id)   ?? 0;
    const handle = `@${(agent.agentType ?? agent.name).replace(/[^a-z0-9]/gi, "").toLowerCase()}`;

    const card = document.createElement("div");
    card.className = "sd-card";
    card.dataset.id = agent.id;
    card.title = agent.id;

    card.innerHTML = `
      <div class="sd-cover" style="background:${coverGradient(agent)}">
        ${agent.protected ? '<span class="sd-star" title="Core protected agent">⭐</span>' : ""}
      </div>
      <div class="sd-body">
        <div class="sd-avatar-wrap">
          <div class="sd-avatar">
            <img src="${imgUrl}" alt="${agent.name}" loading="lazy"
                 onerror="this.style.display='none'">
          </div>
          <span class="sd-state-dot sd-dot-${st}"></span>
        </div>

        <div class="sd-info">
          <span class="sd-name">${agent.name}</span>
          <span class="sd-handle">${handle}</span>
          <p class="sd-bio">${bioline(agent)}</p>
          <span class="sd-type-badge sd-badge-${st}">${st.toUpperCase()}</span>
        </div>

        <div class="sd-stats">
          <div class="sd-stat">
            <strong class="sd-hb-count">${hb}</strong>
            <span>♡ beats</span>
          </div>
          <div class="sd-stat">
            <strong class="sd-msg-count">${msgs}</strong>
            <span>💬 msgs</span>
          </div>
        </div>

        <div class="sd-footer">
          <button class="sd-chat-btn" data-name="${agent.name}">💬 Chat</button>
          <div class="cd-controls">
            <button class="cd-ctrl" data-action="pause"  title="Pause">⏸</button>
            <button class="cd-ctrl" data-action="resume" title="Resume">▶</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="stop"   title="Stop">⏹</button>
            <button class="cd-ctrl cd-ctrl-danger" data-action="delete" title="Delete">🗑</button>
          </div>
        </div>
      </div>
    `;

    card.querySelector(".sd-chat-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      document.dispatchEvent(new CustomEvent("agent-selected", { detail: { agent } }));
    });

    card.querySelector(".cd-controls")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this.sendCommand(agent.id, btn.dataset.action as "pause" | "resume" | "stop" | "delete");
    });

    this.updateControls(card, agent);
    return card;
  }

  private sendCommand(id: string, action: "pause" | "resume" | "stop" | "delete"): void {
    const base = `/api/actors/${encodeURIComponent(id)}`;
    const [url, method] =
      action === "pause"  ? [`${base}/pause`,  "POST"] :
      action === "resume" ? [`${base}/resume`, "POST"] :
                            [base,             "DELETE"];
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
    card.classList.add("sd-alert-error");
    setTimeout(() => card.classList.remove("sd-alert-error"), 900);
  }

  // ── DOM skeleton ───────────────────────────────────────────────────────────

  private buildContainer(): HTMLElement {
    const el = document.createElement("div");
    el.id = "social-dashboard";
    el.className = "sd-root";
    el.innerHTML = `
      <div class="sd-topbar">
        <span class="sd-logo">🌐 AgentFlow Social</span>
        <span class="sd-tagline">Live multi-agent network</span>
        <button class="sd-3d-btn" title="Switch to 3D view">⬡ 3D</button>
      </div>
      <div class="sd-grid"></div>
    `;
    el.querySelector(".sd-3d-btn")?.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: "graph" } }));
    });
    return el;
  }
}

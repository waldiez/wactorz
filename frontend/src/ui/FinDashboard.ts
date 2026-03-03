/**
 * FinDashboard — Bloomberg-terminal-style HTML overlay.
 *
 * Agents are displayed as "position cards" in a finance terminal aesthetic:
 * dark background, monospace fonts, LIVE/HALTED/CLOSED/INIT status labels,
 * activity sparklines (last 8 heartbeat timestamps → block-char bars).
 *
 * Accessibility: role=article on cards, aria-label on all interactive elements,
 * tabindex=0 for keyboard nav, :focus-visible rings, prefers-reduced-motion,
 * prefers-contrast:more support, aria-live on status indicators.
 */

import type { AgentInfo, AgentState } from "../types/agent";
import { agentImageGen } from "../io/AgentImageGen";

// ── Helpers ────────────────────────────────────────────────────────────────────

function stateStr(state: AgentState): string {
  return typeof state === "object" ? "error" : (state as string);
}

/** Finance-terminal status labels. */
function stateLabel(st: string): string {
  switch (st) {
    case "running":      return "LIVE";
    case "paused":       return "HALTED";
    case "stopped":      return "CLOSED";
    case "initializing": return "INIT";
    case "error":        return "ERROR";
    default:             return st.toUpperCase().slice(0, 6);
  }
}

/** Short ticker-style agent name (max 16 chars). */
function agentTicker(info: AgentInfo): string {
  return info.name.replace(/-/g, " ").toUpperCase().slice(0, 20);
}

/** 4-letter type code like Bloomberg tickers. */
function agentTypeShort(info: AgentInfo): string {
  const t = (info.agentType ?? "").toLowerCase();
  const n = info.name.toLowerCase();
  if (t.includes("orchestrator"))                        return "ORCH";
  if (t.includes("monitor"))                             return "MON";
  if (t.includes("guardian"))                            return "QA";
  if (t.includes("gateway"))                             return "GWY";
  if (t.includes("expert"))                              return "UDX";
  if (t.includes("financier") || n.includes("wif"))      return "FIN";
  if (t.includes("transfer")  || n.includes("nautilus")) return "SCP";
  if (t.includes("dynamic")   || t.includes("script"))  return "DYN";
  if (n.includes("weather"))                             return "WX";
  if (n.includes("news"))                                return "NEWS";
  if (n.includes("ml") || n.includes("classifier"))      return "ML";
  return "AGT";
}

/** Per-type accent colour (CSS hex). */
function agentAccent(info: AgentInfo): string {
  const t = (info.agentType ?? "").toLowerCase();
  const n = info.name.toLowerCase();
  if (n === "main-actor"  || t.includes("orchestrator")) return "#ffd700";
  if (t.includes("monitor")  || n.includes("monitor"))  return "#38bdf8";
  if (t.includes("guardian") || n.includes("qa"))       return "#a78bfa";
  if (t.includes("gateway")  || n.includes("io"))       return "#22d3ee";
  if (t.includes("expert")   || n.includes("udx"))      return "#f59e0b";
  if (t.includes("financier")|| n.includes("wif"))      return "#00d97e";
  if (t.includes("transfer") || n.includes("nautilus")) return "#818cf8";
  if (n.includes("weather"))                            return "#7dd3fc";
  if (n.includes("news"))                               return "#f87171";
  if (t.includes("dynamic")  || t.includes("script"))  return "#c084fc";
  return "#64748b";
}

/** 8-bar sparkline from heartbeat timestamps. */
function buildSparkline(tsList: number[]): string {
  const BARS = 8;
  const now  = Date.now();
  const chars: string[] = [];

  for (let i = 0; i < BARS; i++) {
    const ts = tsList[i];
    if (ts === undefined) { chars.push("░"); continue; }
    const ageSec = (now - ts) / 1000;
    if      (ageSec <   5) chars.push("█");
    else if (ageSec <  20) chars.push("▇");
    else if (ageSec <  40) chars.push("▆");
    else if (ageSec <  60) chars.push("▄");
    else if (ageSec <  90) chars.push("▃");
    else if (ageSec < 120) chars.push("▂");
    else                   chars.push("▁");
  }
  return chars.join("");
}

// ── FinDashboard ──────────────────────────────────────────────────────────────

export class FinDashboard {
  private container: HTMLElement;
  private grid:      HTMLElement;
  private countEl:   HTMLElement | null = null;
  private clockEl:   HTMLElement | null = null;

  private agents      = new Map<string, AgentInfo>();
  private heartbeats  = new Map<string, number>();
  private heartbeatTs = new Map<string, number[]>();   // last 8 timestamps
  private messages    = new Map<string, number>();

  private clockTimer:         ReturnType<typeof setInterval> | null = null;
  private imageListener:      ((e: Event) => void) | null = null;
  private unreadListener:     ((e: Event) => void) | null = null;
  private unreadClearLis:     ((e: Event) => void) | null = null;

  constructor() {
    this.container = this.buildContainer();
    this.grid      = this.container.querySelector(".fin-grid")!;
    this.countEl   = this.container.querySelector(".fin-agent-count span");
    this.clockEl   = this.container.querySelector(".fin-clock");
    document.body.appendChild(this.container);

    // Swap in AI-generated portrait when ready
    this.imageListener = (e) => {
      const { id, url } = (e as CustomEvent<{ id: string; url: string }>).detail;
      const img = this.grid.querySelector<HTMLImageElement>(
        `[data-id="${CSS.escape(id)}"] .fin-avatar img`,
      );
      if (img) img.src = url;
    };
    document.addEventListener("agent-image-ready", this.imageListener);

    // Unread badge on Chat button
    this.unreadListener = (e) => {
      const { name, count } = (e as CustomEvent<{ name: string; count: number }>).detail;
      const btn = this.grid.querySelector<HTMLElement>(
        `.fin-chat-btn[data-name="${CSS.escape(name)}"]`,
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

    this.unreadClearLis = (e) => {
      const { name } = (e as CustomEvent<{ name: string }>).detail;
      this.grid
        .querySelector(`.fin-chat-btn[data-name="${CSS.escape(name)}"] .chat-unread-badge`)
        ?.remove();
    };
    document.addEventListener("agent-unread-cleared", this.unreadClearLis);
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    this.container.classList.add("fin-visible");
    agents.forEach((a) => this.agents.set(a.id, a));
    this.renderAll();
    this.startClock();
  }

  hide(): void {
    this.container.classList.remove("fin-visible");
    this.stopClock();
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
    if (this.unreadClearLis) {
      document.removeEventListener("agent-unread-cleared", this.unreadClearLis);
      this.unreadClearLis = null;
    }
    this.container.remove();
  }

  // ── Agent events ──────────────────────────────────────────────────────────

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
      card.style.animation = "fin-exit 0.3s ease forwards";
      setTimeout(() => { card.remove(); this.agents.delete(id); this.syncCount(); }, 300);
    } else {
      this.agents.delete(id);
      this.syncCount();
    }
  }

  onHeartbeat(agentId: string, ts: number): void {
    const count = (this.heartbeats.get(agentId) ?? 0) + 1;
    this.heartbeats.set(agentId, count);

    const tsList = this.heartbeatTs.get(agentId) ?? [];
    tsList.push(ts);
    if (tsList.length > 8) tsList.shift();
    this.heartbeatTs.set(agentId, tsList);

    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;

    // Update tick count
    const tickEl = card.querySelector<HTMLElement>(".fin-ticks");
    if (tickEl) tickEl.textContent = String(count);

    // Update sparkline
    const sparkEl = card.querySelector<HTMLElement>(".fin-spark");
    if (sparkEl) sparkEl.textContent = buildSparkline(tsList);

    // Sync state badge
    const agent = this.agents.get(agentId);
    if (agent) {
      const st = stateStr(agent.state);
      const statusEl = card.querySelector<HTMLElement>(".fin-status");
      if (statusEl) {
        statusEl.className = `fin-status fin-status-${st}`;
        statusEl.textContent = stateLabel(st);
        statusEl.setAttribute("aria-label", `Status: ${st}`);
      }
      const dotEl = card.querySelector<HTMLElement>(".fin-dot");
      if (dotEl) { dotEl.className = `fin-dot fin-dot-${st}`; }
      const stateValEl = card.querySelector<HTMLElement>(".fin-state-val");
      if (stateValEl) stateValEl.textContent = st.toUpperCase();
      this.updateControls(card, agent);
    }

    // Dot pulse animation
    const dot = card.querySelector<HTMLElement>(".fin-dot");
    if (dot) {
      dot.classList.remove("fin-pulse");
      void dot.offsetWidth; // force reflow
      dot.classList.add("fin-pulse");
    }
  }

  onChat(fromId: string, _toId: string): void {
    const count = (this.messages.get(fromId) ?? 0) + 1;
    this.messages.set(fromId, count);

    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(fromId)}"]`);
    if (!card) return;

    const msgEl = card.querySelector<HTMLElement>(".fin-msgs");
    if (msgEl) msgEl.textContent = String(count);

    card.classList.add("fin-chat-flash");
    setTimeout(() => card.classList.remove("fin-chat-flash"), 600);
  }

  showAlert(agentId: string, severity: string): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    if (!card) return;
    const cls = severity === "error" || severity === "critical" ? "fin-alert-error" : "fin-alert-warn";
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls), 900);
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  private renderAll(): void {
    const sorted = [...this.agents.values()].sort((a, b) => {
      if (a.name === "main-actor") return -1;
      if (b.name === "main-actor") return 1;
      return a.name.localeCompare(b.name);
    });

    // Remove cards no longer in live set
    const live = new Set(sorted.map((a) => a.id));
    this.grid.querySelectorAll<HTMLElement>("[data-id]").forEach((el) => {
      if (!live.has(el.dataset.id!)) el.remove();
    });

    // Add or refresh
    sorted.forEach((a) => {
      if (!this.grid.querySelector(`[data-id="${CSS.escape(a.id)}"]`)) {
        this.grid.appendChild(this.buildCard(a));
      } else {
        this.renderCard(a);
      }
    });

    this.syncCount();
  }

  private renderCard(agent: AgentInfo): void {
    const card = this.grid.querySelector<HTMLElement>(`[data-id="${CSS.escape(agent.id)}"]`);
    if (!card) { this.grid.appendChild(this.buildCard(agent)); return; }

    const st = stateStr(agent.state);
    const statusEl = card.querySelector<HTMLElement>(".fin-status");
    if (statusEl) {
      statusEl.className = `fin-status fin-status-${st}`;
      statusEl.textContent = stateLabel(st);
      statusEl.setAttribute("aria-label", `Status: ${st}`);
    }
    const dotEl = card.querySelector<HTMLElement>(".fin-dot");
    if (dotEl) dotEl.className = `fin-dot fin-dot-${st}`;
    const stateValEl = card.querySelector<HTMLElement>(".fin-state-val");
    if (stateValEl) stateValEl.textContent = st.toUpperCase();
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
    if (deleteBtn) { deleteBtn.disabled = prot; deleteBtn.title = prot ? "Protected agent" : "Delete agent"; }
  }

  private syncCount(): void {
    if (this.countEl) this.countEl.textContent = String(this.agents.size);
  }

  private buildCard(agent: AgentInfo): HTMLElement {
    const imgUrl  = agentImageGen.get(agent);
    const st      = stateStr(agent.state);
    const hb      = this.heartbeats.get(agent.id) ?? 0;
    const msgs    = this.messages.get(agent.id) ?? 0;
    const tsList  = this.heartbeatTs.get(agent.id) ?? [];
    const spark   = buildSparkline(tsList);
    const ticker  = agentTicker(agent);
    const tyShort = agentTypeShort(agent);
    const accent  = agentAccent(agent);
    const isMain  = agent.name === "main-actor" || (agent.agentType ?? "").includes("orchestrator");

    const card = document.createElement("article");
    card.className   = `fin-card${isMain ? " fin-card-main" : ""}`;
    card.dataset.id  = agent.id;
    card.setAttribute("role", "article");
    card.setAttribute("aria-label", `${agent.name} agent — ${stateLabel(st)}`);
    card.setAttribute("tabindex", "0");
    card.style.setProperty("--card-accent", accent);

    card.innerHTML = `
      <div class="fin-card-header">
        <div class="fin-status-row">
          <span class="fin-dot fin-dot-${st}" aria-hidden="true"></span>
          <span class="fin-status fin-status-${st}"
                role="status" aria-live="polite"
                aria-label="Agent status: ${stateLabel(st)}">${stateLabel(st)}</span>
          ${agent.protected
            ? '<span class="fin-protected" aria-label="Core protected agent" title="Protected">⊛</span>'
            : ""}
        </div>
        <div class="fin-card-id">
          <div class="fin-avatar-wrap">
            <div class="fin-avatar">
              <img src="${imgUrl}" alt="${agent.name}" loading="lazy"
                   onerror="this.style.display='none'">
            </div>
          </div>
          <div class="fin-id-text">
            <span class="fin-ticker">${ticker}</span>
            <span class="fin-type-badge">${tyShort}</span>
          </div>
        </div>
      </div>

      <div class="fin-divider" role="separator"></div>

      <div class="fin-metrics" role="group" aria-label="Agent metrics">
        <div class="fin-metric">
          <span class="fin-metric-val fin-ticks" aria-label="Heartbeat ticks: ${hb}">${hb}</span>
          <span class="fin-metric-key" aria-hidden="true">TICKS</span>
        </div>
        <div class="fin-metric">
          <span class="fin-metric-val fin-msgs" aria-label="Messages: ${msgs}">${msgs}</span>
          <span class="fin-metric-key" aria-hidden="true">MSGS</span>
        </div>
        <div class="fin-metric">
          <span class="fin-metric-val fin-state-val" aria-label="State: ${st}">${st.toUpperCase()}</span>
          <span class="fin-metric-key" aria-hidden="true">STATE</span>
        </div>
      </div>

      <div class="fin-spark-row" aria-label="Activity history (${spark.replace(/░/g, "empty").replace(/[▁▂▃▄▅▆▇█]/g, "beat")})">
        <span class="fin-spark" aria-hidden="true">${spark}</span>
        <span class="fin-spark-label" aria-hidden="true">ACTIVITY</span>
      </div>

      <div class="fin-footer">
        <button class="fin-chat-btn" data-name="${agent.name}"
                aria-label="Open chat with ${agent.name}">
          💬 Chat
        </button>
        <div class="fin-controls" role="group" aria-label="Agent lifecycle controls">
          <button class="fin-ctrl" data-action="pause"
                  aria-label="Pause ${agent.name}" title="Pause">⏸</button>
          <button class="fin-ctrl" data-action="resume"
                  aria-label="Resume ${agent.name}" title="Resume">▶</button>
          <button class="fin-ctrl fin-ctrl-danger" data-action="stop"
                  aria-label="Stop ${agent.name}" title="Stop">⏹</button>
          <button class="fin-ctrl fin-ctrl-danger" data-action="delete"
                  aria-label="Delete ${agent.name}" title="Delete">🗑</button>
        </div>
      </div>
    `;

    // Chat button
    card.querySelector(".fin-chat-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      document.dispatchEvent(new CustomEvent("agent-selected", { detail: { agent } }));
    });

    // Control buttons
    card.querySelector(".fin-controls")?.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-action]");
      if (!btn || btn.disabled) return;
      e.stopPropagation();
      this.sendCommand(agent.id, btn.dataset.action as "pause" | "resume" | "stop" | "delete");
    });

    // Keyboard: Enter/Space on card → open chat (like clicking)
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        document.dispatchEvent(new CustomEvent("agent-selected", { detail: { agent } }));
      }
    });

    this.updateControls(card, agent);
    return card;
  }

  // ── REST commands ─────────────────────────────────────────────────────────

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
    card.classList.add("fin-alert-error");
    setTimeout(() => card.classList.remove("fin-alert-error"), 900);
  }

  // ── Clock ─────────────────────────────────────────────────────────────────

  private startClock(): void {
    const tick = () => {
      if (!this.clockEl) return;
      const now = new Date();
      const hh  = String(now.getUTCHours()).padStart(2, "0");
      const mm  = String(now.getUTCMinutes()).padStart(2, "0");
      const ss  = String(now.getUTCSeconds()).padStart(2, "0");
      this.clockEl.textContent = `${hh}:${mm}:${ss} UTC`;
    };
    tick();
    this.clockTimer = setInterval(tick, 1000);
  }

  private stopClock(): void {
    if (this.clockTimer) { clearInterval(this.clockTimer); this.clockTimer = null; }
  }

  // ── DOM skeleton ─────────────────────────────────────────────────────────

  private buildContainer(): HTMLElement {
    const el = document.createElement("div");
    el.id        = "fin-dashboard";
    el.className = "fin-root";
    el.setAttribute("role", "main");
    el.setAttribute("aria-label", "WIF Finance Terminal — Agent Dashboard");

    el.innerHTML = `
      <header class="fin-topbar" role="banner">
        <div class="fin-topbar-left">
          <span class="fin-logo" aria-label="WIF Finance Terminal">💹 WIF</span>
          <span class="fin-tagline" aria-hidden="true">FINANCE TERMINAL</span>
        </div>
        <div class="fin-topbar-center">
          <time class="fin-clock" aria-label="Current UTC time"></time>
        </div>
        <div class="fin-topbar-right">
          <span class="fin-agent-count" aria-live="polite">
            <span>0</span> AGENTS
          </span>
          <button class="fin-3d-btn" title="Switch to 3D graph view"
                  aria-label="Switch to 3D graph view">⬡ 3D</button>
        </div>
      </header>
      <div class="fin-grid" role="region" aria-label="Agent position cards"></div>
    `;

    el.querySelector(".fin-3d-btn")?.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: "graph" } }));
    });

    return el;
  }
}

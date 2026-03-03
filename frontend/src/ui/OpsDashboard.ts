/**
 * OpsDashboard — WAB live-ops HTML overlay (7th theme: "ops").
 *
 * Displays swarm verdict (HEALTHY / DEGRADED / UNHEALTHY), integrity %,
 * live status chips, token counts, and an event log.
 *
 * Verdict → mood mapping:
 *   HEALTHY    → body[data-mood="trust"]
 *   DEGRADED   → body[data-mood="velocity"]
 *   UNHEALTHY  → body[data-mood="heritage"]
 *
 * Mode-aware:
 *   demo → ROI footnote in hero card ("Ƿ/req efficiency")
 *   edu  → human-friendly status labels + badge triggers
 */

import type { AgentInfo, AgentState } from "../types/agent";
import type { AppMode } from "./CoinTicker";

// ── Types ─────────────────────────────────────────────────────────────────────

type Verdict = "HEALTHY" | "DEGRADED" | "UNHEALTHY";

function stateStr(state: AgentState): string {
  return typeof state === "object" ? "error" : (state as string);
}

// ── OpsDashboard ──────────────────────────────────────────────────────────────

export class OpsDashboard {
  private container:    HTMLElement;
  private verdictEl:    HTMLElement | null = null;
  private progressEl:   HTMLElement | null = null;
  private integrityEl:  HTMLElement | null = null;
  private totalEl:      HTMLElement | null = null;
  private okEl:         HTMLElement | null = null;
  private staleEl:      HTMLElement | null = null;
  private eventList:    HTMLElement | null = null;

  private agents      = new Map<string, AgentInfo>();
  private heartbeats  = new Map<string, number>();   // agentId → last heartbeat timestamp
  private messages    = new Map<string, number>();   // agentId → msg count
  private alerts      = new Map<string, string>();   // agentId → severity

  private eventLog:   string[] = [];
  private verdict:    Verdict = "HEALTHY";
  private mode:       AppMode;

  private imageListener:      ((e: Event) => void) | null = null;
  private unreadListener:     ((e: Event) => void) | null = null;
  private unreadClearLis:     ((e: Event) => void) | null = null;
  private verdictTimer:       ReturnType<typeof setInterval> | null = null;

  constructor(mode: AppMode = "demo") {
    this.mode = mode;
    this.container = this.buildContainer();
    this.verdictEl   = this.container.querySelector("#ops-verdict");
    this.progressEl  = this.container.querySelector("#ops-progress");
    this.integrityEl = this.container.querySelector("#ops-integrity");
    this.totalEl     = this.container.querySelector("#ops-total");
    this.okEl        = this.container.querySelector("#ops-ok");
    this.staleEl     = this.container.querySelector("#ops-stale");
    this.eventList   = this.container.querySelector("#ops-event-log");
    document.body.appendChild(this.container);

    // Swap AI-generated avatars (not used in ops but consistent API)
    this.imageListener = () => {};
    document.addEventListener("agent-image-ready", this.imageListener);

    this.unreadListener = () => {};
    document.addEventListener("agent-unread", this.unreadListener);

    this.unreadClearLis = () => {};
    document.addEventListener("agent-unread-cleared", this.unreadClearLis);
  }

  setMode(mode: AppMode): void {
    this.mode = mode;
    this.syncVerdict();
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  show(agents: AgentInfo[]): void {
    this.container.classList.add("ops-visible");
    agents.forEach((a) => this.agents.set(a.id, a));
    this.syncAll();

    // Re-evaluate verdict every 15 s to catch stale agents
    this.verdictTimer = setInterval(() => this.syncVerdict(), 15_000);
  }

  hide(): void {
    this.container.classList.remove("ops-visible");
    if (this.verdictTimer) { clearInterval(this.verdictTimer); this.verdictTimer = null; }
  }

  destroy(): void {
    this.hide();
    if (this.imageListener)  { document.removeEventListener("agent-image-ready",    this.imageListener);  this.imageListener  = null; }
    if (this.unreadListener) { document.removeEventListener("agent-unread",         this.unreadListener); this.unreadListener = null; }
    if (this.unreadClearLis) { document.removeEventListener("agent-unread-cleared", this.unreadClearLis); this.unreadClearLis = null; }
    this.container.remove();
  }

  // ── Agent events ──────────────────────────────────────────────────────────

  addAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    this.pushEvent(`Agent spawned: ${agent.name} (${agent.agentType ?? "agent"})`);
    this.syncAll();
  }

  updateAgent(agent: AgentInfo): void {
    this.agents.set(agent.id, agent);
    this.syncTokens();
    this.syncVerdict();
  }

  removeAgent(id: string): void {
    const agent = this.agents.get(id);
    if (agent) this.pushEvent(`Agent stopped: ${agent.name}`);
    this.agents.delete(id);
    this.heartbeats.delete(id);
    this.syncAll();
  }

  onHeartbeat(agentId: string, ts: number): void {
    this.heartbeats.set(agentId, ts);
    const agent = this.agents.get(agentId);
    if (agent) {
      agent.lastHeartbeatAt = new Date(ts).toISOString();
    }
    this.syncVerdict();
  }

  onChat(fromId: string, _toId: string): void {
    this.messages.set(fromId, (this.messages.get(fromId) ?? 0) + 1);
  }

  showAlert(agentId: string, severity: string): void {
    this.alerts.set(agentId, severity);
    const agent = this.agents.get(agentId);
    const name = agent?.name ?? agentId;
    this.pushEvent(`Alert [${severity}]: ${name}`);
    this.syncVerdict();
  }

  onSpawn(payload: { agentName: string; agentType: string }): void {
    this.pushEvent(`Spawn: ${payload.agentName} (${payload.agentType})`);
  }

  // ── Verdict ────────────────────────────────────────────────────────────────

  private computeVerdict(): Verdict {
    const now       = Date.now();
    const agents    = [...this.agents.values()];
    const total     = agents.length;
    if (total === 0) return "HEALTHY";

    const staleCount = agents.filter((a) => {
      const ts = this.heartbeats.get(a.id);
      return ts === undefined || (now - ts) > 60_000;
    }).length;

    const hasError   = [...this.alerts.values()].some((s) => s === "error" || s === "critical");
    const staleFrac  = staleCount / total;

    if (hasError || staleFrac >= 0.5) return "UNHEALTHY";
    if (staleCount >= 1)              return "DEGRADED";
    return "HEALTHY";
  }

  private syncVerdict(): void {
    const next = this.computeVerdict();
    if (next !== this.verdict) {
      this.verdict = next;
      this.pushEvent(`Verdict changed → ${next}`);
    }

    // Apply mood to body
    const moodMap: Record<Verdict, string> = {
      HEALTHY:   "trust",
      DEGRADED:  "velocity",
      UNHEALTHY: "heritage",
    };
    document.body.setAttribute("data-mood", moodMap[this.verdict]);

    if (!this.verdictEl || !this.progressEl || !this.integrityEl) return;

    const labelEdu: Record<Verdict, string> = {
      HEALTHY:   "All Good!",
      DEGRADED:  "Some Issues",
      UNHEALTHY: "Needs Attention",
    };

    this.verdictEl.textContent = this.mode === "edu" ? labelEdu[this.verdict] : this.verdict;
    this.verdictEl.className   = `ops-verdict-text ops-verdict-${this.verdict.toLowerCase()}`;
    this.verdictEl.setAttribute("aria-label", `Swarm verdict: ${this.verdict}`);

    const canProceed = this.verdict === "HEALTHY";
    this.progressEl.textContent = this.mode === "edu"
      ? (canProceed ? "Ready to go!" : "Hold on…")
      : (canProceed ? "CAN PROCEED" : "HOLD");
    this.progressEl.className = `ops-progress-text ${canProceed ? "ops-proceed" : "ops-hold"}`;

    // Integrity % — percent of non-stale agents
    const total    = this.agents.size;
    const now      = Date.now();
    const okCount  = [...this.agents.keys()].filter((id) => {
      const ts = this.heartbeats.get(id);
      return ts !== undefined && (now - ts) <= 60_000;
    }).length;
    const pct = total > 0 ? Math.round((okCount / total) * 100) : 100;
    this.integrityEl.textContent = `${pct}%`;
    this.integrityEl.setAttribute("aria-label", `Swarm integrity: ${pct}%`);

    // Demo mode: ROI footnote
    if (this.mode === "demo") {
      const footnote = this.container.querySelector<HTMLElement>("#ops-roi-footnote");
      if (footnote) {
        const totalMsgs = [...this.messages.values()].reduce((a, b) => a + b, 0);
        footnote.textContent = totalMsgs > 0
          ? `Efficiency: ${(okCount / totalMsgs).toFixed(2)} agents/msg`
          : "";
      }
    }

    this.syncTokens();
  }

  private syncTokens(): void {
    if (!this.totalEl || !this.okEl || !this.staleEl) return;
    const total = this.agents.size;
    const now   = Date.now();
    const ok    = [...this.agents.keys()].filter((id) => {
      const ts = this.heartbeats.get(id);
      return ts !== undefined && (now - ts) <= 60_000;
    }).length;
    this.totalEl.textContent = String(total);
    this.okEl.textContent    = String(ok);
    this.staleEl.textContent = String(total - ok);
  }

  private syncAll(): void {
    this.syncVerdict();
    this.syncTokens();
  }

  private pushEvent(msg: string): void {
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    this.eventLog.unshift(`${time}  ${msg}`);
    if (this.eventLog.length > 50) this.eventLog.pop();

    if (!this.eventList) return;
    const li = document.createElement("li");
    li.className = "ops-event-item";
    li.setAttribute("role", "listitem");
    li.textContent = `${time} — ${msg}`;
    this.eventList.insertBefore(li, this.eventList.firstChild);
    // Cap DOM items
    while (this.eventList.childElementCount > 50) {
      this.eventList.lastElementChild?.remove();
    }
  }

  // ── DOM skeleton ──────────────────────────────────────────────────────────

  private buildContainer(): HTMLElement {
    const el = document.createElement("div");
    el.id        = "ops-dashboard";
    el.className = "ops-root";
    el.setAttribute("role", "main");
    el.setAttribute("aria-label", "Ops Dashboard — Swarm Live Operations");

    const roiFootnote = this.mode === "demo"
      ? `<p id="ops-roi-footnote" class="ops-roi-footnote" aria-live="polite"></p>`
      : "";

    el.innerHTML = `
      <header class="ops-topbar" role="banner">
        <span class="ops-brand" aria-label="Ops Dashboard">⚙ Ops</span>
        <span class="ops-title">SWARM LIVE OPS</span>
        <span class="ops-source-chip" aria-live="polite">MQTT LIVE</span>
        <button class="ops-3d-btn" aria-label="Switch to 3D graph view" title="Switch to 3D">⬡ 3D</button>
      </header>

      <div class="ops-grid" role="region" aria-label="Operations dashboard">

        <!-- Hero: swarm verdict -->
        <section class="ops-hero" aria-label="Swarm verdict">
          <div class="ops-kpi-row">
            <div class="ops-kpi">
              <span class="ops-kpi-label" aria-hidden="true">VERDICT</span>
              <span id="ops-verdict" class="ops-verdict-text ops-verdict-healthy"
                    role="status" aria-live="polite" aria-label="Swarm verdict: HEALTHY">HEALTHY</span>
            </div>
            <div class="ops-kpi">
              <span class="ops-kpi-label" aria-hidden="true">STATUS</span>
              <span id="ops-progress" class="ops-progress-text ops-proceed"
                    role="status" aria-live="polite">CAN PROCEED</span>
            </div>
            <div class="ops-kpi">
              <span class="ops-kpi-label" aria-hidden="true">INTEGRITY</span>
              <span id="ops-integrity" class="ops-integrity-val"
                    role="status" aria-live="polite" aria-label="Swarm integrity: 100%">100%</span>
            </div>
          </div>
          ${roiFootnote}
        </section>

        <!-- Sidebar: live status chips -->
        <aside class="ops-sidebar" aria-label="System status">
          <div class="ops-chip ops-chip-ok"   aria-label="API status: OK">API <span>OK</span></div>
          <div class="ops-chip ops-chip-ok"   aria-label="Feed status: LIVE">Feed <span>LIVE</span></div>
          <div class="ops-chip ops-chip-ok"   aria-label="Activity status: ACTIVE">Activity <span>ACTIVE</span></div>
        </aside>

        <!-- Token counts -->
        <section class="ops-tokens" aria-label="Agent counts">
          <div class="ops-token">
            <span class="ops-token-val" id="ops-total" aria-label="Total agents: 0">0</span>
            <span class="ops-token-key" aria-hidden="true">TOTAL</span>
          </div>
          <div class="ops-token">
            <span class="ops-token-val ops-token-ok" id="ops-ok" aria-label="OK agents: 0">0</span>
            <span class="ops-token-key" aria-hidden="true">OK</span>
          </div>
          <div class="ops-token">
            <span class="ops-token-val ops-token-stale" id="ops-stale" aria-label="Stale agents: 0">0</span>
            <span class="ops-token-key" aria-hidden="true">STALE</span>
          </div>
        </section>

        <!-- Event log -->
        <section class="ops-events" aria-label="Live event log">
          <div class="ops-events-header" aria-hidden="true">EVENT LOG</div>
          <ul id="ops-event-log" class="ops-event-list" role="log" aria-live="polite" aria-atomic="false"
              aria-label="Live agent events"></ul>
        </section>

      </div>
    `;

    el.querySelector(".ops-3d-btn")?.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("theme-change", { detail: { theme: "graph" } }));
    });

    return el;
  }
}

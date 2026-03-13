/**
 * Top-left HUD: shows live agent count, system health badge, and state breakdown.
 */

import type { AgentInfo } from "../types/agent";

export class AgentHUD {
  private countEl: HTMLElement;
  private healthEl: HTMLElement;
  private statsEl: HTMLElement;

  private count = 0;
  private healthy = true;

  constructor() {
    this.countEl  = document.getElementById("hud-count")!;
    this.healthEl = document.getElementById("hud-health")!;
    this.statsEl  = document.getElementById("hud-stats")!;
    this.render();
  }

  /** Update the agent count display. */
  setAgentCount(n: number): void {
    this.count = n;
    this.countEl.textContent = String(n);
  }

  /** Increment agent count by 1. */
  incrementCount(): void {
    this.setAgentCount(this.count + 1);
  }

  /** Decrement agent count (floor 0). */
  decrementCount(): void {
    this.setAgentCount(Math.max(0, this.count - 1));
  }

  /** Set system health badge. */
  setSystemHealth(healthy: boolean): void {
    this.healthy = healthy;
    this.render();
  }

  /** Flash a warning (temporarily marks unhealthy). */
  flashAlert(severity: string): void {
    const wasHealthy = this.healthy;
    if (severity === "error" || severity === "critical") {
      this.setSystemHealth(false);
      setTimeout(() => this.setSystemHealth(wasHealthy), 5000);
    }
  }

  /**
   * Update the breakdown stats panel from the current agent list.
   * Call after any event that can change agent state or metrics.
   */
  setStats(agents: AgentInfo[], alertCount = 0): void {
    const stateStr = (a: AgentInfo) => (typeof a.state === "object" ? "failed" : a.state);
    const running  = agents.filter((a) => stateStr(a) === "running").length;
    const paused   = agents.filter((a) => stateStr(a) === "paused").length;
    const stopped  = agents.filter((a) => stateStr(a) === "stopped").length;
    const failed   = agents.filter((a) => stateStr(a) === "failed").length;
    const msgs     = agents.reduce((s, a) => s + (a.messagesProcessed ?? 0), 0);
    const cost     = agents.reduce((s, a) => s + (a.costUsd ?? 0), 0);
    const costStr  = cost < 0.001 ? "$0.000"
                   : cost < 0.01  ? (cost * 100).toFixed(2) + "¢"
                   : "$" + cost.toFixed(3);

    this.statsEl.innerHTML =
      `<span class="hud-stat hud-stat-run">${running} run</span>` +
      (paused  ? `<span class="hud-stat hud-stat-pause">${paused} paused</span>` : "") +
      (stopped ? `<span class="hud-stat hud-stat-stop">${stopped} stopped</span>` : "") +
      (failed  ? `<span class="hud-stat hud-stat-fail">${failed} failed</span>` : "") +
      (alertCount ? `<span class="hud-stat hud-stat-alert">⚠ ${alertCount}</span>` : "") +
      (msgs    ? `<span class="hud-stat hud-stat-msgs">${msgs} msgs</span>` : "") +
      (cost > 0.0001 ? `<span class="hud-stat hud-stat-cost">${costStr}</span>` : "");
  }

  private render(): void {
    if (this.healthy) {
      this.healthEl.textContent = "System Healthy";
      this.healthEl.className = "hud-badge healthy";
    } else {
      this.healthEl.textContent = "Alert Active";
      this.healthEl.className = "hud-badge alert";
    }
  }
}

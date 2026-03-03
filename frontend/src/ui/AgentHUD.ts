/**
 * Top-left HUD: shows live agent count and system health badge.
 *
 * Subscribes to MQTT alert events via the callback API; call
 * `onAgentCountChange(n)` and `onSystemHealth(healthy)` to update.
 */

export class AgentHUD {
  private countEl: HTMLElement;
  private healthEl: HTMLElement;

  private count = 0;
  private healthy = true;

  constructor() {
    this.countEl = document.getElementById("hud-count")!;
    this.healthEl = document.getElementById("hud-health")!;
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

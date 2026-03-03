/**
 * CoinTicker — HUD display for the WaldiezCoin (Ƿ) balance.
 *
 * Reads `#coin-display` from the DOM and updates it whenever the balance
 * changes.  Flashes green on earn, red on debit.
 *
 * Mode-aware:
 *   edu  → tooltip explains how coins are earned; badge after 5/10/20 earns
 *   demo → shows "Ƿ/req efficiency" footnote
 */

import type { WaldiezCoin } from "./WaldiezCoin";

export type AppMode = "edu" | "demo";

const STREAK_KEY = "waldiez_streaks";
const BADGE_THRESHOLDS = [5, 10, 20] as const;

function loadStreaks(): number {
  return parseInt(localStorage.getItem(STREAK_KEY) ?? "0", 10);
}

function saveStreaks(n: number): void {
  localStorage.setItem(STREAK_KEY, String(n));
}

export class CoinTicker {
  private el: HTMLElement | null;
  private earnStreak = 0;
  private mode: AppMode;
  private coin: WaldiezCoin;

  constructor(coin: WaldiezCoin, mode: AppMode = "demo") {
    this.coin = coin;
    this.mode = mode;
    this.el   = document.getElementById("coin-display");
    this.earnStreak = loadStreaks();
    this.render();
  }

  setMode(mode: AppMode): void {
    this.mode = mode;
    this.render();
  }

  /**
   * Called when a `system/coin` event arrives.
   * @param newBalance — authoritative server balance
   * @param delta      — positive = earn, negative = debit
   * @param reason     — human-readable reason string
   */
  update(newBalance: number, delta: number, reason: string): void {
    if (!this.el) return;

    this.render(newBalance);

    // Flash animation
    this.el.classList.remove("coin-earn", "coin-debit");
    void this.el.offsetWidth; // force reflow
    this.el.classList.add(delta >= 0 ? "coin-earn" : "coin-debit");

    // Streak tracking (edu mode)
    if (delta > 0) {
      this.earnStreak++;
      saveStreaks(this.earnStreak);
      if (this.mode === "edu") {
        this.checkBadge(this.earnStreak, reason);
      }
    }

    // Edu: low balance warning
    if (this.mode === "edu" && newBalance < 100) {
      this.showTooltip("Budget alert: balance below Ƿ 100!");
    }

    // Demo: ROI footnote
    if (this.mode === "demo" && this.el) {
      const footnote = this.el.querySelector<HTMLElement>(".coin-roi");
      if (footnote) {
        const efficiency = newBalance > 0 ? (newBalance / Math.max(this.earnStreak, 1)).toFixed(1) : "0";
        footnote.textContent = `Ƿ/req: ${efficiency}`;
      }
    }
  }

  private render(balance?: number): void {
    if (!this.el) return;
    const bal = balance ?? this.coin.getBalance();
    const formatted = bal < 0
      ? `−Ƿ ${Math.abs(bal).toLocaleString()}`
      : `Ƿ ${bal.toLocaleString()}`;

    let inner = `<span class="coin-value">${formatted}</span>`;

    if (this.mode === "edu") {
      inner += `<span class="coin-hint sr-only">WaldiezCoin — earned by heartbeats and spawns</span>`;
    } else if (this.mode === "demo") {
      inner += `<span class="coin-roi" aria-live="polite" title="Efficiency: coins per request"></span>`;
    }

    this.el.innerHTML = inner;
    this.el.setAttribute("aria-label", `WaldiezCoin balance: ${formatted}`);
    this.el.setAttribute("title", this.mode === "edu"
      ? "WaldiezCoin — earned by agent activity"
      : "WaldiezCoin balance");
  }

  private checkBadge(streak: number, _reason: string): void {
    for (const threshold of BADGE_THRESHOLDS) {
      if (streak === threshold) {
        const medals: Record<number, string> = { 5: "🥉", 10: "🥈", 20: "🥇" };
        this.showTooltip(`${medals[threshold]} ${threshold} earn streak! You're building momentum.`);
        break;
      }
    }
  }

  private showTooltip(msg: string): void {
    const toast = document.createElement("div");
    toast.className = "coin-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
  }
}

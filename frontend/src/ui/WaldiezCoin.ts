/**
 * WaldiezCoin — in-game economy cache (frontend).
 *
 * Mirrors the server-side WizAgent balance via MQTT `system/coin` events.
 * Persists balance + recent history to localStorage so the HUD survives
 * a page refresh between server restarts.
 *
 * Singleton exported as `coin`.
 */

const COIN_KEY = "waldiez_coin";
const HISTORY_CAP = 200;

export interface CoinEntry {
  delta: number;
  reason: string;
  timestampMs: number;
}

interface CoinState {
  balance: number;
  history: CoinEntry[];
  lastSync: number; // server timestamp of last reconciliation
}

function load(): CoinState {
  try {
    const raw = localStorage.getItem(COIN_KEY);
    if (raw) return JSON.parse(raw) as CoinState;
  } catch {
    // ignore parse errors
  }
  return { balance: 0, history: [], lastSync: 0 };
}

function save(state: CoinState): void {
  try {
    localStorage.setItem(COIN_KEY, JSON.stringify(state));
  } catch {
    // storage full or unavailable — degrade gracefully
  }
}

export class WaldiezCoin {
  private state: CoinState = load();

  /** Apply an earning (positive delta). */
  earn(delta: number, reason: string): void {
    this.state.balance += Math.abs(delta);
    this.push({ delta: Math.abs(delta), reason, timestampMs: Date.now() });
  }

  /** Apply a debit (positive amount → negative delta stored). */
  debit(delta: number, reason: string): void {
    this.state.balance -= Math.abs(delta);
    this.push({ delta: -Math.abs(delta), reason, timestampMs: Date.now() });
  }

  getBalance(): number {
    return this.state.balance;
  }

  getHistory(n = 50): CoinEntry[] {
    return this.state.history.slice(-n).reverse();
  }

  /** Formatted balance string: "Ƿ 1,250" */
  format(): string {
    const bal = this.state.balance;
    const abs = Math.abs(bal).toLocaleString();
    return bal < 0 ? `−Ƿ ${abs}` : `Ƿ ${abs}`;
  }

  /**
   * Reconcile local balance with the server value.
   * Called when a `system/coin` MQTT event arrives.
   * Always accepts the server balance; also records the entry.
   */
  sync(payload: { balance: number; delta: number; reason: string; timestampMs: number }): void {
    // Accept server balance authoritatively
    this.state.balance = payload.balance;
    this.state.lastSync = payload.timestampMs;
    this.push({
      delta:       payload.delta,
      reason:      payload.reason,
      timestampMs: payload.timestampMs,
    });
  }

  private push(entry: CoinEntry): void {
    this.state.history.push(entry);
    if (this.state.history.length > HISTORY_CAP) {
      this.state.history.shift();
    }
    save(this.state);
  }
}

/** Singleton instance — import `coin` and call methods directly. */
export const coin = new WaldiezCoin();

/**
 * WizAgent — WaldiezCoin economist (Node.js port).
 * Tracks Ƿ balance. agentType: "coin"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

interface CoinEntry {
  delta:     number;
  reason:    string;
  balance:   number;
  timestamp: number;
}

const MAX_HISTORY = 200;

const HELP = `**WIZ — WaldiezCoin Economist** Ƿ
_In-game economy for the Wactorz swarm_

\`\`\`
balance              current Ƿ balance
history [n]          last n transactions (default 10)
earn <n> [reason]    credit n coins manually
debit <n> [reason]   debit n coins manually
help                 this message
\`\`\`

**Auto-economy:**
+10 Ƿ agent spawn  ·  +2 Ƿ heartbeat  ·  +5 Ƿ healthy system
 −5 Ƿ QA flag  ·  −3 Ƿ stale alert`;

export class WizAgent extends Actor {
  private _balance = 0;
  private _history: CoinEntry[] = [];

  constructor(publish: MqttPublisher, actorId?: string) {
    super("wiz-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("coin");
  }

  /** Called by the MQTT router with economy events (spawn/heartbeat/health/qa-flag/alert). */
  handleEconomyEvent(topic: string, payload: unknown): void {
    const p = payload as Record<string, unknown>;
    const event = String(p["__event"] ?? "");
    if (topic.endsWith("/spawn") || event === "spawn") {
      if (String(p["agentId"] ?? "") === this.actorId) return;
      const name = String(p["agentName"] ?? "agent");
      this._apply(10, `Agent spawned: ${name}`);
    } else if (topic.endsWith("/heartbeat") || event === "heartbeat") {
      if (String(p["agentId"] ?? "") === this.actorId) return;
      this._apply(2, "Heartbeat received");
    } else if (topic === "system/health" || event === "health") {
      if (Number(p["failed"] ?? 1) === 0 && Number(p["stopped"] ?? 1) === 0) {
        this._apply(5, "System health OK");
      }
    } else if (topic === "system/qa-flag" || event === "qa-flag") {
      this._apply(-5, "QA flag raised");
    } else if (topic.endsWith("/alert") || event === "alert") {
      this._apply(-3, "Alert received");
    }
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    if (!text) return;
    text = Actor.stripPrefix(text, "@wiz-agent", "@wiz_agent");
    const parts = text.trim().split(/\s+/);
    const cmd  = (parts[0] ?? "").toLowerCase();
    const args = parts.slice(1);
    const reply = this._dispatch(cmd, args);
    if (reply) this.replyChat(reply);
  }

  private _dispatch(cmd: string, args: string[]): string | null {
    switch (cmd) {
      case "balance":  return this._cmdBalance();
      case "history":  return this._cmdHistory(args[0] ? parseInt(args[0]) : 10);
      case "earn":     return this._cmdEarn(args);
      case "debit":    return this._cmdDebit(args);
      case "help":
      case "":         return HELP;
      default:         return `Unknown command: \`${cmd}\`. Type \`help\`.`;
    }
  }

  private _apply(delta: number, reason: string): void {
    this._balance += delta;
    this._history.push({ delta, reason, balance: this._balance, timestamp: Date.now() });
    if (this._history.length > MAX_HISTORY) {
      this._history = this._history.slice(-MAX_HISTORY);
    }
    this._publishCoin(delta, reason);
  }

  private _publishCoin(delta: number, reason: string): void {
    this.mqttPublish("system/coin", {
      balance:   this._balance,
      event:     delta >= 0 ? "earn" : "debit",
      amount:    Math.abs(delta),
      reason,
      timestampMs: Date.now(),
    });
  }

  protected override async onHeartbeat(): Promise<void> {
    await super.onHeartbeat();
    this._publishCoin(0, "heartbeat");
  }

  private _cmdBalance(): string {
    const sign = this._balance >= 0 ? "+" : "";
    return (
      `**Ƿ WaldiezCoin Balance**\n\nCurrent: **${sign}Ƿ ${this._balance}**\n\n` +
      `_Earn: spawn +10 · heartbeat +2 · healthy +5_\n` +
      `_Lose: QA flag −5 · alert −3_`
    );
  }

  private _cmdHistory(n: number): string {
    if (this._history.length === 0) return "📭 No coin history yet.";
    const take = Math.max(1, Math.min(n, 50, this._history.length));
    const rows = this._history.slice(-take).reverse().map((e) => {
      const sign = e.delta >= 0 ? "+" : "";
      const d = new Date(e.timestamp);
      const t = `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`;
      return `  \`${t}\` ${sign}${e.delta} Ƿ — ${e.reason} (bal: ${e.balance})`;
    });
    return `**Ƿ Coin History** (last ${take})\n\n${rows.join("\n")}\n\n**Balance: Ƿ ${this._balance}**`;
  }

  private _cmdEarn(args: string[]): string {
    if (!args[0]) return "Usage: `earn <amount> [reason]`";
    const amount = parseInt(args[0]);
    if (isNaN(amount) || amount <= 0) return "Amount must be a positive integer.";
    const reason = args.slice(1).join(" ") || "manual earn";
    this._apply(amount, reason);
    return `✅ Earned **Ƿ ${amount}** — ${reason}\n\n**New balance: Ƿ ${this._balance}**`;
  }

  private _cmdDebit(args: string[]): string {
    if (!args[0]) return "Usage: `debit <amount> [reason]`";
    const amount = parseInt(args[0]);
    if (isNaN(amount) || amount <= 0) return "Amount must be a positive integer.";
    const reason = args.slice(1).join(" ") || "manual debit";
    this._apply(-amount, reason);
    return `📉 Debited **Ƿ ${amount}** — ${reason}\n\n**New balance: Ƿ ${this._balance}**`;
  }
}

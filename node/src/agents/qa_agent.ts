/**
 * QAAgent — safety observer (Node.js port).
 * Passively inspects all /chat messages. agentType: "guardian"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const INJECTION_PATTERNS = [
  /ignore (all |previous |prior )?instructions/i,
  /you are now/i,
  /act as (a |an )?/i,
  /pretend (to be|you are)/i,
  /system prompt/i,
  /jailbreak/i,
  /\[INST\]/i,
  /###\s*(system|instruction)/i,
];

const ERROR_PATTERNS = [
  /error:/i,
  /traceback/i,
  /exception:/i,
  /stack trace/i,
  /\bat line\b/i,
];

const PII_PATTERNS = [
  /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i,
  /\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b/, // SSN-like
  /\b(?:\d[ -]?){13,16}\b/,             // card-like
];

export class QAAgent extends Actor {
  private _pending: Map<string, { content: string; time: number }> = new Map();
  private _checkInterval?: ReturnType<typeof setInterval>;

  constructor(publish: MqttPublisher, actorId?: string) {
    super("qa-agent", publish, actorId);
    this.protected_ = true;
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("guardian");
    this._checkInterval = setInterval(() => this._checkPending(), 10_000);
  }

  protected override async onStop(): Promise<void> {
    if (this._checkInterval) clearInterval(this._checkInterval);
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Text && msg.type !== MessageType.Task) return;
    const payload = msg.payload as Record<string, unknown>;
    const content = String(payload["content"] ?? payload["text"] ?? "");
    const from    = String(payload["from"] ?? "");
    const to      = String(payload["to"] ?? "");
    if (!content) return;

    // Track pending responses (user messages awaiting agent reply)
    if (from === "user" || from === "") {
      this._pending.set(`${to}:${Date.now()}`, { content, time: Date.now() });
    } else {
      // Agent replied — clear pending for this agent
      for (const key of this._pending.keys()) {
        if (key.startsWith(`${from}:`)) this._pending.delete(key);
      }
    }

    this._inspect(content, from, to);
  }

  private _inspect(content: string, from: string, to: string): void {
    const ctx = `from=${from} to=${to}`;

    for (const pat of INJECTION_PATTERNS) {
      if (pat.test(content)) {
        this._flag("prompt-injection", `Potential prompt injection detected (${ctx}): ${pat}`);
        return;
      }
    }

    for (const pat of ERROR_PATTERNS) {
      if (pat.test(content) && from !== "user") {
        this._flag("error-bleed", `Agent error leaked to chat (${ctx})`);
        return;
      }
    }

    // Raw JSON bleed
    if (content.trim().startsWith("{") && content.includes('"')) {
      try {
        JSON.parse(content);
        this._flag("raw-json", `Raw JSON exposed in chat (${ctx})`);
        return;
      } catch { /* not pure JSON */ }
    }

    for (const pat of PII_PATTERNS) {
      if (pat.test(content)) {
        this._flag("pii", `Potential PII detected in chat (${ctx})`);
        return;
      }
    }
  }

  private _checkPending(): void {
    const now = Date.now();
    for (const [key, entry] of this._pending.entries()) {
      if (now - entry.time > 30_000) {
        this._flag("no-response", `No response within 30s for: ${entry.content.slice(0, 80)}`);
        this._pending.delete(key);
      }
    }
  }

  private _flag(type: string, reason: string): void {
    this.mqttPublish("system/qa-flag", {
      type,
      reason,
      agentId:     this.actorId,
      timestampMs: Date.now(),
    });
  }
}

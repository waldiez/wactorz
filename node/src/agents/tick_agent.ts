/**
 * TickAgent — in-process scheduler/timer (Node.js port).
 * NATO: CHRON / Tango. agentType: "scheduler"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

interface Timer {
  id:         string;
  message:    string;
  fireAt:     number; // ms
  intervalMs: number; // 0 = one-shot
  handle?:    ReturnType<typeof setTimeout>;
}

const HELP = `**CHRON — TickAgent** ⏱
_In-process scheduler — no external cron needed_

| Command | Description |
|---------|-------------|
| \`at <HH:MM> <message>\` | Fire at clock time today/tomorrow |
| \`in <n> <unit> <msg>\` | Fire after delay (s/m/h/d) |
| \`every <n> <unit> <msg>\` | Recurring timer |
| \`list\` | Show pending timers |
| \`cancel <id>\` | Cancel timer by ID prefix |
| \`clear\` | Cancel all timers |
| \`help\` | This message |

**Examples:** \`in 5 m check the oven\` · \`at 09:00 Good morning!\``;

function parseMs(n: string, unit: string): number | null {
  const v = parseFloat(n);
  if (isNaN(v) || v <= 0) return null;
  const factors: Record<string, number> = {
    s: 1000, sec: 1000, second: 1000,
    m: 60000, min: 60000, minute: 60000,
    h: 3600000, hr: 3600000, hour: 3600000,
    d: 86400000, day: 86400000,
  };
  const key = unit.toLowerCase().replace(/s$/, "");
  const f = factors[key];
  return f ? Math.round(v * f) : null;
}

function parseHHMM(s: string): number | null {
  const m = s.trim().match(/^(\d{1,2}):(\d{2})$/);
  if (!m) return null;
  const h = parseInt(m[1]), mn = parseInt(m[2]);
  if (h > 23 || mn > 59) return null;
  const now = new Date();
  const target = new Date(now);
  target.setHours(h, mn, 0, 0);
  if (target.getTime() <= now.getTime()) target.setDate(target.getDate() + 1);
  return target.getTime();
}

let _idCounter = 0;
function shortId(): string {
  return `${Date.now().toString(16)}${(++_idCounter).toString(16).padStart(4, "0")}`;
}

export class TickAgent extends Actor {
  private _timers: Map<string, Timer> = new Map();

  constructor(publish: MqttPublisher, actorId?: string) {
    super("chron-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("scheduler");
  }

  protected override async onStop(): Promise<void> {
    for (const t of this._timers.values()) {
      if (t.handle) clearTimeout(t.handle);
    }
    this._timers.clear();
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    if (!text) return;
    text = Actor.stripPrefix(text, "@chron-agent", "@chron_agent", "@tick-agent", "@tick_agent");
    const parts = text.trim().split(/\s+/);
    const cmd  = (parts[0] ?? "").toLowerCase();
    const args = parts.slice(1);
    const reply = await this._dispatch(cmd, args, parts);
    if (reply) this.replyChat(reply);
  }

  private async _dispatch(cmd: string, args: string[], all: string[]): Promise<string> {
    switch (cmd) {
      case "": case "help":   return HELP;
      case "list":            return this._cmdList();
      case "clear":           return this._cmdClear();
      case "cancel": case "rm": case "del": return this._cmdCancel(args[0] ?? "");
      case "at":              return args.length < 2 ? "Usage: `at <HH:MM> <message>`"
                                : this._cmdAt(args[0], args.slice(1).join(" "));
      case "in":              return args.length < 3 ? "Usage: `in <n> <unit> <message>`"
                                : this._cmdIn(args[0], args[1], args.slice(2).join(" "));
      case "every":           return args.length < 3 ? "Usage: `every <n> <unit> <message>`"
                                : this._cmdEvery(args[0], args[1], args.slice(2).join(" "));
      default:                return `Unknown command: \`${cmd}\`. Type \`help\`.`;
    }
  }

  private _cmdAt(timeStr: string, message: string): string {
    const fireAt = parseHHMM(timeStr);
    if (!fireAt) return `Invalid time \`${timeStr}\`. Use HH:MM (24-h), e.g. \`14:30\`.`;
    const id = shortId();
    const delayMs = fireAt - Date.now();
    const h = Math.floor(delayMs / 3600000), m = Math.floor((delayMs % 3600000) / 60000);
    this._schedule({ id, message, fireAt, intervalMs: 0 });
    return `✓ Timer \`${id}\` set for **${timeStr}** (in ${h > 0 ? `${h}h ` : ""}${m}m).\n\nMessage: _${message}_`;
  }

  private _cmdIn(n: string, unit: string, message: string): string {
    const ms = parseMs(n, unit);
    if (!ms) return `Invalid delay \`${n} ${unit}\`. Use e.g. \`5 m\`, \`2 h\`, \`30 s\`.`;
    const id = shortId();
    this._schedule({ id, message, fireAt: Date.now() + ms, intervalMs: 0 });
    return `✓ Timer \`${id}\` — fires in **${n} ${unit}**.\n\nMessage: _${message}_`;
  }

  private _cmdEvery(n: string, unit: string, message: string): string {
    const ms = parseMs(n, unit);
    if (!ms || ms < 60000) return `Invalid interval \`${n} ${unit}\`. Minimum is 1 minute.`;
    const id = shortId();
    this._schedule({ id, message, fireAt: Date.now() + ms, intervalMs: ms });
    return `✓ Recurring timer \`${id}\` — every **${n} ${unit}**.\n\nMessage: _${message}_`;
  }

  private _cmdList(): string {
    if (this._timers.size === 0) return "No active timers. Use `in`, `at`, or `every` to schedule one.";
    const sorted = [...this._timers.values()].sort((a, b) => a.fireAt - b.fireAt);
    const lines = [`**Active Timers (${sorted.length}):**\n`];
    for (const t of sorted) {
      const remaining = Math.max(0, t.fireAt - Date.now());
      const kind = t.intervalMs > 0 ? "every" : "once";
      const label = remaining < 60000 ? `in ${Math.ceil(remaining / 1000)}s`
        : remaining < 3600000 ? `in ${Math.ceil(remaining / 60000)}m`
        : `in ${(remaining / 3600000).toFixed(1)}h`;
      lines.push(`- \`${t.id}\` [${kind}] ${label} — _${t.message.slice(0, 60)}_`);
    }
    return lines.join("\n");
  }

  private _cmdCancel(prefix: string): string {
    if (!prefix) return "Usage: `cancel <id-prefix>`  (use `list` to see IDs)";
    const matches = [...this._timers.keys()].filter(id => id.startsWith(prefix));
    if (!matches.length) return `No timer found matching \`${prefix}\`.`;
    if (matches.length > 1) return `Ambiguous prefix \`${prefix}\` — ${matches.length} matches. Be more specific.`;
    const t = this._timers.get(matches[0])!;
    if (t.handle) clearTimeout(t.handle);
    this._timers.delete(matches[0]);
    return `✓ Timer \`${matches[0]}\` cancelled.`;
  }

  private _cmdClear(): string {
    const count = this._timers.size;
    for (const t of this._timers.values()) if (t.handle) clearTimeout(t.handle);
    this._timers.clear();
    return `✓ Cleared ${count} timer(s).`;
  }

  private _schedule(timer: Timer): void {
    const fire = () => {
      const short = timer.id.slice(0, 12);
      this.replyChat(`⏰ **Timer \`${short}…\`** fired!\n\n${timer.message}`);
      if (timer.intervalMs > 0) {
        timer.fireAt = Date.now() + timer.intervalMs;
        timer.handle = setTimeout(fire, timer.intervalMs);
        this._timers.set(timer.id, timer);
      } else {
        this._timers.delete(timer.id);
      }
    };
    const delayMs = Math.max(0, timer.fireAt - Date.now());
    timer.handle = setTimeout(fire, delayMs);
    this._timers.set(timer.id, timer);
  }
}

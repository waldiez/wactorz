/**
 * Base Actor class for Wactorz Node.js runtime.
 *
 * Mirrors Python core/actor.py and Rust wactorz-core Actor trait.
 * Each actor has:
 *   - Async message queue (via EventEmitter + Promise queue)
 *   - MQTT publish via shared MqttBroker reference
 *   - Heartbeat loop
 *   - Lifecycle hooks: onStart(), onStop(), handleMessage()
 */

import { EventEmitter } from "events";
import { v4 as uuidv4 } from "uuid";
import {
  ActorMetrics,
  ActorState,
  Message,
  MessageType,
} from "./types";

export type MqttPublisher = (topic: string, payload: unknown) => void;

export abstract class Actor extends EventEmitter {
  readonly actorId: string;
  readonly name: string;

  protected state: ActorState = ActorState.Initializing;
  protected metrics: ActorMetrics = {
    tasksReceived:   0,
    tasksCompleted:  0,
    tasksFailed:     0,
    heartbeatsSent:  0,
    lastHeartbeatMs: 0,
  };
  protected protected_: boolean = false;

  private _mailbox: Message[] = [];
  private _mailboxResolvers: Array<(msg: Message) => void> = [];
  private _running = false;
  private _heartbeatTimer?: ReturnType<typeof setInterval>;
  private _publish: MqttPublisher;

  /** Heartbeat interval in ms (default: 10s) */
  protected heartbeatIntervalMs = 10_000;

  constructor(
    name: string,
    publish: MqttPublisher,
    actorId?: string,
  ) {
    super();
    this.name = name;
    this.actorId = actorId ?? uuidv4();
    this._publish = publish;
  }

  // ── Mailbox ────────────────────────────────────────────────────────────────

  /** Enqueue a message into this actor's mailbox. */
  send(msg: Message): void {
    if (this._mailboxResolvers.length > 0) {
      const resolve = this._mailboxResolvers.shift()!;
      resolve(msg);
    } else {
      this._mailbox.push(msg);
    }
  }

  private _recv(): Promise<Message> {
    if (this._mailbox.length > 0) {
      return Promise.resolve(this._mailbox.shift()!);
    }
    return new Promise((resolve) => {
      this._mailboxResolvers.push(resolve);
    });
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  /** Start the actor: run onStart(), heartbeat loop, then message loop. */
  async run(): Promise<void> {
    this.state = ActorState.Running;
    this._running = true;

    try {
      await this.onStart();
    } catch (e) {
      this.state = ActorState.Failed;
      return;
    }

    this._heartbeatTimer = setInterval(() => {
      void this._doHeartbeat();
    }, this.heartbeatIntervalMs);

    while (this._running) {
      const msg = await this._recv();
      if (!this._running) break;

      if (
        msg.type === MessageType.Command &&
        (msg.payload as { command?: string })?.command === "stop"
      ) {
        break;
      }

      this.metrics.tasksReceived++;
      try {
        await this.handleMessage(msg);
      } catch (e) {
        this.metrics.tasksFailed++;
      }
    }

    this._running = false;
    if (this._heartbeatTimer) clearInterval(this._heartbeatTimer);
    this.state = ActorState.Stopped;
    await this.onStop().catch(() => {});
  }

  stop(): void {
    this._running = false;
    // Wake up the message loop
    this.send({
      id: uuidv4(),
      type: MessageType.Command,
      payload: { command: "stop" },
      timestamp: Date.now(),
    });
  }

  private async _doHeartbeat(): Promise<void> {
    this.metrics.heartbeatsSent++;
    this.metrics.lastHeartbeatMs = Date.now();
    await this.onHeartbeat().catch(() => {});
  }

  // ── Hooks (override these) ─────────────────────────────────────────────────

  protected async onStart(): Promise<void> {}
  protected async onStop(): Promise<void> {}
  protected async onHeartbeat(): Promise<void> {
    this.mqttPublish(`agents/${this.actorId}/heartbeat`, {
      agentId:     this.actorId,
      agentName:   this.name,
      state:       this.state,
      timestampMs: Date.now(),
    });
  }

  /** Override this to handle incoming messages. */
  abstract handleMessage(msg: Message): Promise<void>;

  // ── MQTT helpers ───────────────────────────────────────────────────────────

  protected mqttPublish(topic: string, payload: unknown): void {
    this._publish(topic, payload);
  }

  protected replyChat(content: string): void {
    this.mqttPublish(`agents/${this.actorId}/chat`, {
      from:        this.name,
      to:          "user",
      content,
      timestampMs: Date.now(),
    });
  }

  protected publishSpawn(agentType: string): void {
    this.mqttPublish(`agents/${this.actorId}/spawn`, {
      agentId:     this.actorId,
      agentName:   this.name,
      agentType,
      timestampMs: Date.now(),
    });
  }

  // ── Text extraction helper ─────────────────────────────────────────────────

  protected static extractText(payload: unknown): string {
    if (typeof payload === "string") return payload;
    if (payload && typeof payload === "object") {
      const p = payload as Record<string, unknown>;
      return String(p["text"] ?? p["content"] ?? p["task"] ?? "");
    }
    return "";
  }

  protected static stripPrefix(text: string, ...prefixes: string[]): string {
    const lower = text.toLowerCase();
    for (const pfx of prefixes) {
      if (lower.startsWith(pfx)) {
        return text.slice(pfx.length).trimStart();
      }
    }
    return text;
  }
}

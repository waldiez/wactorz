/**
 * MonitorAgent — health watcher (Node.js port).
 * Polls all actors every 15s, alerts on silence >60s. agentType: "monitor"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { ActorRegistry } from "../core/registry";
import { Message } from "../core/types";

export class MonitorAgent extends Actor {
  private _registry: ActorRegistry;
  private _lastSeen: Map<string, number> = new Map();
  private _pollInterval?: ReturnType<typeof setInterval>;

  constructor(publish: MqttPublisher, registry: ActorRegistry, actorId?: string) {
    super("monitor-agent", publish, actorId);
    this._registry = registry;
    this.protected_ = true;
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("monitor");
    this._pollInterval = setInterval(() => this._poll(), 15_000);
  }

  protected override async onStop(): Promise<void> {
    if (this._pollInterval) clearInterval(this._pollInterval);
  }

  override async handleMessage(_msg: Message): Promise<void> {
    // Monitor doesn't handle user messages
  }

  /** Record a heartbeat for an actor. */
  recordHeartbeat(actorId: string): void {
    this._lastSeen.set(actorId, Date.now());
  }

  private _poll(): void {
    const now = Date.now();
    const actors = this._registry.allActors();
    let running = 0, stopped = 0, failed = 0;

    for (const actor of actors) {
      if (actor.actorId === this.actorId) continue;
      const last = this._lastSeen.get(actor.actorId) ?? 0;
      const silentMs = now - last;
      running++;
      if (last > 0 && silentMs > 60_000) {
        this.mqttPublish(`agents/${actor.actorId}/alert`, {
          agentId:     actor.actorId,
          agentName:   actor.name,
          reason:      `No heartbeat for ${Math.round(silentMs / 1000)}s`,
          timestampMs: now,
        });
      }
    }

    this.mqttPublish("system/health", {
      total:       actors.length,
      running,
      stopped,
      failed,
      verdict:     failed > 0 ? "UNHEALTHY" : running > 0 ? "HEALTHY" : "DEGRADED",
      timestampMs: now,
    });
  }
}

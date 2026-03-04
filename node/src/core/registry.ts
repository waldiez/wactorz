/**
 * ActorRegistry + ActorSystem for the AgentFlow Node.js runtime.
 * Mirrors Python core/registry.py and Rust agentflow-core.
 */

import mqtt from "mqtt";
import { v4 as uuidv4 } from "uuid";
import { Actor, MqttPublisher } from "./actor";
import { Message, MessageType, RegistryEntry, ActorState } from "./types";

export interface SystemConfig {
  mqttUrl:  string; // e.g. "mqtt://localhost:1883"
  clientId?: string;
}

export class ActorRegistry {
  private _actors: Map<string, Actor> = new Map();
  private _byName: Map<string, string> = new Map(); // name → id

  register(actor: Actor): void {
    this._actors.set(actor.actorId, actor);
    this._byName.set(actor.name, actor.actorId);
  }

  unregister(actorId: string): void {
    const actor = this._actors.get(actorId);
    if (actor) {
      this._byName.delete(actor.name);
      this._actors.delete(actorId);
    }
  }

  getById(id: string): Actor | undefined {
    return this._actors.get(id);
  }

  getByName(name: string): Actor | undefined {
    const id = this._byName.get(name);
    return id ? this._actors.get(id) : undefined;
  }

  allActors(): Actor[] {
    return Array.from(this._actors.values());
  }

  send(targetId: string, msg: Message): boolean {
    const actor = this._actors.get(targetId);
    if (!actor) return false;
    actor.send(msg);
    return true;
  }

  entries(): RegistryEntry[] {
    return this.allActors().map((a) => ({
      id:        a.actorId,
      name:      a.name,
      agentType: "unknown",
      state:     ActorState.Running,
    }));
  }
}

export class ActorSystem {
  readonly registry = new ActorRegistry();
  private _mqttClient?: mqtt.MqttClient;
  private _publish: MqttPublisher;
  private _config: SystemConfig;

  constructor(config: SystemConfig) {
    this._config = config;
    this._publish = (_topic, _payload) => {
      // Will be replaced once MQTT connects
    };
  }

  /** Connect to MQTT and wire up the pub/sub bridge. */
  async connect(): Promise<void> {
    const client = mqtt.connect(this._config.mqttUrl, {
      clientId: this._config.clientId ?? `agentflow-node-${uuidv4().slice(0, 8)}`,
      clean: true,
    });

    this._mqttClient = client;

    this._publish = (topic: string, payload: unknown) => {
      const json = JSON.stringify(payload);
      client.publish(topic, json, { qos: 0 });
    };

    await new Promise<void>((resolve, reject) => {
      client.once("connect", resolve);
      client.once("error", reject);
    });

    client.subscribe(["agents/#", "system/#", "io/chat"]);

    client.on("message", (topic, buf) => {
      try {
        const payload = JSON.parse(buf.toString());
        this._routeInbound(topic, payload);
      } catch {
        // ignore non-JSON
      }
    });
  }

  private _routeInbound(topic: string, payload: unknown): void {
    // io/chat → io-agent
    if (topic === "io/chat") {
      const actor = this.registry.getByName("io-agent");
      if (actor) {
        actor.send({
          id: uuidv4(),
          type: MessageType.Text,
          senderId: "user",
          payload,
          timestamp: Date.now(),
        });
      }
      return;
    }

    // agents/{id}/chat from user → direct mailbox delivery
    const chatMatch = topic.match(/^agents\/([^/]+)\/chat$/);
    if (chatMatch) {
      const p = payload as Record<string, unknown>;
      const from = p?.["from"] as string | undefined;
      if (!from || from === "user") {
        const actorId = chatMatch[1];
        const actor = this.registry.getById(actorId);
        if (actor) {
          actor.send({
            id: uuidv4(),
            type: MessageType.Task,
            senderId: "user",
            payload,
            timestamp: Date.now(),
          });
        }
      }
    }
  }

  /** Spawn (register + run) an actor. */
  spawnActor(actor: Actor): void {
    // Re-bind actor's publish to this system's MQTT client
    (actor as unknown as { _publish: MqttPublisher })["_publish"] = this._publish;
    this.registry.register(actor);
    void actor.run().catch((e) => {
      console.error(`[actor:${actor.name}] crashed:`, e);
    });
  }

  async shutdown(): Promise<void> {
    for (const actor of this.registry.allActors()) {
      actor.stop();
    }
    if (this._mqttClient) {
      await new Promise<void>((r) => this._mqttClient!.end(false, {}, r));
    }
  }

  /** Get a publisher bound to this system (for passing to actors pre-connect). */
  getPublisher(): MqttPublisher {
    return (topic: string, payload: unknown) => this._publish(topic, payload);
  }
}

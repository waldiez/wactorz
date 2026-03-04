/**
 * IOAgent — user gateway (Node.js port).
 * Subscribes to io/chat, parses @name prefix, routes to named actor.
 * agentType: "gateway"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { ActorRegistry } from "../core/registry";
import { Message, MessageType } from "../core/types";

export class IOAgent extends Actor {
  private _registry: ActorRegistry;

  constructor(publish: MqttPublisher, registry: ActorRegistry, actorId?: string) {
    super("io-agent", publish, actorId);
    this._registry = registry;
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("gateway");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Text && msg.type !== MessageType.Task) return;
    const raw = Actor.extractText(msg.payload).trim();
    if (!raw) return;

    // Parse @name prefix
    const match = raw.match(/^@([\w-]+)\s*([\s\S]*)$/);
    if (match) {
      const targetName = match[1];
      const rest = match[2].trim();
      const target = this._registry.getByName(targetName);
      if (target) {
        target.send({ ...msg, payload: { ...(msg.payload as object), text: rest, content: rest } });
        return;
      }
      this.replyChat(`⚠ No agent named \`@${targetName}\` found. Use \`@udx-agent agents\` to list agents.`);
      return;
    }

    // No prefix → main-actor
    const main = this._registry.getByName("main-actor");
    if (main) {
      main.send(msg);
    } else {
      this.replyChat("⚠ No main-actor available.");
    }
    this.metrics.tasksCompleted++;
  }
}

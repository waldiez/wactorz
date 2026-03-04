/**
 * HomeAssistantAgent — HA device discovery (Node.js port).
 * agentType: "home-assistant"
 */

import axios from "axios";
import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const HA_URL   = (process.env["HA_URL"] ?? process.env["HOME_ASSISTANT_URL"] ?? "").replace(/\/$/, "");
const HA_TOKEN = process.env["HA_TOKEN"] ?? process.env["HOME_ASSISTANT_TOKEN"] ?? "";

interface Entity { entity_id: string; original_name?: string; name?: string }
interface Device  { name?: string; manufacturer?: string; model?: string; area_id?: string }

export class HomeAssistantAgent extends Actor {
  private _entities: Entity[] = [];
  private _devices:  Device[] = [];
  private _cachedAt = 0;

  constructor(publish: MqttPublisher, actorId?: string) {
    super("ha-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("home-assistant");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    text = Actor.stripPrefix(text, "@ha-agent", "@ha_agent");
    await this._dispatch(text.trim());
    this.metrics.tasksCompleted++;
  }

  private get _configured(): boolean { return !!HA_URL && !!HA_TOKEN; }

  private async _haGet(path: string): Promise<unknown> {
    const resp = await axios.get(`${HA_URL}${path}`, {
      headers: { Authorization: `Bearer ${HA_TOKEN}`, "Content-Type": "application/json" },
      timeout: 10_000,
    });
    return resp.data;
  }

  private async _ensureCache(): Promise<void> {
    if (Date.now() - this._cachedAt < 30_000) return;
    const [devices, entities] = await Promise.all([
      this._haGet("/api/config/device_registry/list"),
      this._haGet("/api/config/entity_registry/list"),
    ]);
    this._devices  = (devices  as Device[])  ?? [];
    this._entities = (entities as Entity[]) ?? [];
    this._cachedAt = Date.now();
  }

  private async _dispatch(text: string): Promise<void> {
    const tokens = text.split(/\s+/);
    const cmd = (tokens[0] ?? "").toLowerCase();
    switch (cmd) {
      case "": case "help":
        this.replyChat(
          "**HomeAssistantAgent** — HA device discovery\n\n" +
          "| Command | Description |\n|---------|-------------|\n" +
          "| `status` | HA connection status |\n" +
          "| `devices` | List all devices |\n" +
          "| `entities` | List all entities |\n" +
          "| `domains` | List entity domains |\n" +
          "| `search <keyword>` | Search entities/devices |\n" +
          "| `state <entity_id>` | Get entity state |\n" +
          "| `help` | This message |\n\n" +
          "_For hardware recommendations, use `@main-actor`._"
        ); break;
      case "status":   await this._cmdStatus(); break;
      case "devices":  await this._cmdDevices(); break;
      case "entities": await this._cmdEntities(); break;
      case "domains":  await this._cmdDomains(); break;
      case "search":   await this._cmdSearch(tokens.slice(1).join(" ")); break;
      case "state":    await this._cmdState(tokens[1] ?? ""); break;
      default: this.replyChat(`Unknown command: \`${cmd}\`. Type \`help\`.`);
    }
  }

  private async _cmdStatus(): Promise<void> {
    if (!this._configured) {
      this.replyChat("**Home Assistant Agent**\n\n⚠ Not configured.\n\nSet `HA_URL` and `HA_TOKEN` env vars.");
      return;
    }
    try {
      const info = await this._haGet("/api/") as Record<string, unknown>;
      this.replyChat(
        `**Home Assistant Status**\n\n✓ Connected to \`${HA_URL}\`\n` +
        `📍 Location: ${info["location_name"] ?? "unknown"}\n🔖 Version: ${info["version"] ?? "unknown"}`
      );
    } catch (e) {
      this.replyChat(`✗ Cannot connect to \`${HA_URL}\`: ${e}`);
    }
  }

  private async _cmdDevices(): Promise<void> {
    if (!this._configured) { this.replyChat("HA not configured."); return; }
    try {
      await this._ensureCache();
      if (!this._devices.length) { this.replyChat("No devices found."); return; }
      const lines = [`**Devices (${this._devices.length}):**\n`];
      for (const d of this._devices.slice(0, 25)) {
        let line = `- **${d.name ?? "?"}**`;
        if (d.manufacturer || d.model) line += ` (${[d.manufacturer, d.model].filter(Boolean).join(" ")})`;
        if (d.area_id) line += ` — area: ${d.area_id}`;
        lines.push(line);
      }
      if (this._devices.length > 25) lines.push(`… and ${this._devices.length - 25} more`);
      this.replyChat(lines.join("\n"));
    } catch (e) { this.replyChat(`✗ ${e}`); }
  }

  private async _cmdEntities(): Promise<void> {
    if (!this._configured) { this.replyChat("HA not configured."); return; }
    try {
      await this._ensureCache();
      if (!this._entities.length) { this.replyChat("No entities found."); return; }
      const lines = [`**Entities (${this._entities.length}):**\n`];
      for (const e of this._entities.slice(0, 30)) {
        const name = e.original_name ?? e.name ?? "";
        lines.push(name ? `- \`${e.entity_id}\` — ${name}` : `- \`${e.entity_id}\``);
      }
      if (this._entities.length > 30) lines.push(`… and ${this._entities.length - 30} more. Use \`search <keyword>\` to filter.`);
      this.replyChat(lines.join("\n"));
    } catch (e) { this.replyChat(`✗ ${e}`); }
  }

  private async _cmdDomains(): Promise<void> {
    if (!this._configured) { this.replyChat("HA not configured."); return; }
    try {
      await this._ensureCache();
      const counts: Record<string, number> = {};
      for (const e of this._entities) {
        const domain = e.entity_id.split(".")[0] ?? "";
        if (domain) counts[domain] = (counts[domain] ?? 0) + 1;
      }
      const sorted = Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
      const lines = [`**Entity Domains (${sorted.length}):**\n`];
      for (const [domain, count] of sorted) lines.push(`- \`${domain}\` — ${count} entities`);
      this.replyChat(lines.join("\n"));
    } catch (e) { this.replyChat(`✗ ${e}`); }
  }

  private async _cmdSearch(keyword: string): Promise<void> {
    if (!keyword) { this.replyChat("Usage: `search <keyword>`"); return; }
    if (!this._configured) { this.replyChat("HA not configured."); return; }
    try {
      await this._ensureCache();
      const kw = keyword.toLowerCase();
      const matches = this._entities.filter(e =>
        e.entity_id.toLowerCase().includes(kw) ||
        (e.original_name ?? e.name ?? "").toLowerCase().includes(kw)
      );
      if (!matches.length) { this.replyChat(`No entities matching \`${keyword}\`.`); return; }
      const lines = [`**Search: \`${keyword}\`** — ${matches.length} match(es):\n`];
      for (const e of matches.slice(0, 20)) {
        const name = e.original_name ?? e.name ?? "";
        lines.push(name ? `- \`${e.entity_id}\` — ${name}` : `- \`${e.entity_id}\``);
      }
      if (matches.length > 20) lines.push(`… and ${matches.length - 20} more`);
      this.replyChat(lines.join("\n"));
    } catch (e) { this.replyChat(`✗ ${e}`); }
  }

  private async _cmdState(entityId: string): Promise<void> {
    if (!entityId) { this.replyChat("Usage: `state <entity_id>`  e.g. `state light.living_room`"); return; }
    try {
      const s = await this._haGet(`/api/states/${entityId}`) as Record<string, unknown>;
      const attrs = s["attributes"] as Record<string, unknown> ?? {};
      const friendly = String(attrs["friendly_name"] ?? entityId);
      this.replyChat(
        `**${friendly}** (\`${entityId}\`)\n\nState: **${s["state"]}**\n\nLast updated: ${s["last_updated"]}`
      );
    } catch (e) { this.replyChat(`✗ Could not get state for \`${entityId}\`: ${e}`); }
  }
}

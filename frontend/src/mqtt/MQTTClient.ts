/**
 * MQTT WebSocket client.
 *
 * Connects to the Mosquitto broker's WebSocket listener (default: ws://localhost:9001)
 * and emits typed events for each topic pattern AgentFlow uses.
 *
 * Usage:
 * ```ts
 * const client = new MQTTClient("ws://localhost:9001");
 * client.on("heartbeat", (payload) => { ... });
 * ```
 */

import mqtt, { type MqttClient } from "mqtt";
import type {
  AlertPayload,
  ChatMessage,
  CoinPayload,
  HeartbeatPayload,
  LogPayload,
  MetricsPayload,
  NodeHeartbeatPayload,
  SpawnPayload,
  StatusPayload,
} from "../types/agent";

// ── Event map ─────────────────────────────────────────────────────────────────

/** QA safety flag raised by the QAAgent. */
export interface QaFlagPayload {
  agentId: string;
  agentName: string;
  from: string;
  category: string;
  severity: string;
  excerpt: string;
  message: string;
  timestampMs: number;
}

export interface MQTTEvents {
  connected: void;
  disconnected: void;
  error: Error;
  heartbeat: HeartbeatPayload;
  status: StatusPayload;
  alert: AlertPayload;
  spawn: SpawnPayload;
  chat: ChatMessage;
  "qa-flag": QaFlagPayload;
  /** LLM cost + token + message count metrics from an agent. */
  metrics: MetricsPayload;
  /** Log/text output from an agent. */
  logs: LogPayload;
  /** Agent completed a task. */
  completed: { agentId: string; agentName: string };
  /** Remote AgentFlow node phoned home. */
  "node-heartbeat": NodeHeartbeatPayload;
  /** system/health snapshot from MonitorAgent. */
  "system-health": unknown;
  /** WizAgent coin economy event. */
  coin: CoinPayload;
  /** Catch-all for raw messages not matching a known pattern. */
  raw: { topic: string; payload: unknown };
}

type Listener<T> = (data: T) => void;
type Listeners = { [K in keyof MQTTEvents]: Array<Listener<MQTTEvents[K]>> };

// ── Client ────────────────────────────────────────────────────────────────────

export class MQTTClient {
  private client: MqttClient | null = null;
  private listeners: Partial<Listeners> = {};

  // Default: MQTT WebSocket via nginx path (/mqtt) rather than direct port 9001.
  // Override with VITE_MQTT_WS_URL env var or constructor argument.
  constructor(private readonly brokerUrl: string = "ws://localhost/mqtt") {}

  /** Connect and subscribe to all agent topics. */
  connect(): void {
    this.client = mqtt.connect(this.brokerUrl, {
      clientId: `agentflow-dashboard-${Math.random().toString(16).slice(2, 8)}`,
      keepalive: 30,
      reconnectPeriod: 2000,
    });

    this.client.on("connect", () => {
      console.info("[MQTT] Connected to", this.brokerUrl);
      this.client?.subscribe(["agents/#", "nodes/#", "system/#"], { qos: 1 });
      this.emit("connected", undefined);
    });

    this.client.on("disconnect", () => {
      this.emit("disconnected", undefined);
    });

    this.client.on("error", (err) => {
      console.error("[MQTT] Error:", err);
      this.emit("error", err);
    });

    this.client.on("message", (topic: string, raw: Buffer) => {
      this.handleMessage(topic, raw);
    });
  }

  /** Disconnect cleanly. */
  disconnect(): void {
    this.client?.end(true);
    this.client = null;
  }

  /** Publish a raw JSON payload to a topic. Returns false if not connected. */
  publish(topic: string, payload: unknown): boolean {
    if (!this.client?.connected) return false;
    this.client.publish(topic, JSON.stringify(payload), { qos: 1 });
    return true;
  }

  // ── Event emitter ───────────────────────────────────────────────────────────

  on<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
    if (!this.listeners[event]) {
      (this.listeners as Listeners)[event] = [];
    }
    (this.listeners[event] as Array<Listener<MQTTEvents[K]>>).push(listener);
    return this;
  }

  off<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
    const arr = this.listeners[event] as Array<Listener<MQTTEvents[K]>> | undefined;
    if (arr) {
      const idx = arr.indexOf(listener);
      if (idx !== -1) arr.splice(idx, 1);
    }
    return this;
  }

  private emit<K extends keyof MQTTEvents>(event: K, data: MQTTEvents[K]): void {
    const arr = this.listeners[event] as Array<Listener<MQTTEvents[K]>> | undefined;
    arr?.forEach((fn) => fn(data));
  }

  // ── Message routing ─────────────────────────────────────────────────────────

  private handleMessage(topic: string, raw: Buffer): void {
    let payload: unknown;
    try {
      payload = JSON.parse(raw.toString());
    } catch {
      return;
    }

    // agents/{id}/heartbeat
    if (/^agents\/[^/]+\/heartbeat$/.test(topic)) {
      this.emit("heartbeat", payload as HeartbeatPayload);
      return;
    }
    // agents/{id}/status
    if (/^agents\/[^/]+\/status$/.test(topic)) {
      this.emit("status", payload as StatusPayload);
      return;
    }
    // agents/{id}/alert
    if (/^agents\/[^/]+\/alert$/.test(topic)) {
      this.emit("alert", payload as AlertPayload);
      return;
    }
    // agents/{id}/chat
    if (/^agents\/[^/]+\/chat$/.test(topic)) {
      this.emit("chat", payload as ChatMessage);
      return;
    }
    // agents/{id}/spawn  (real backend + mock both use this topic)
    if (/^agents\/[^/]+\/spawn$/.test(topic)) {
      this.emit("spawn", payload as SpawnPayload);
      return;
    }

    // system/qa-flag
    if (topic === "system/qa-flag") {
      this.emit("qa-flag", payload as QaFlagPayload);
      return;
    }

    // system/spawn  (legacy / alternate)
    if (topic === "system/spawn") {
      this.emit("spawn", payload as SpawnPayload);
      return;
    }

    // system/health
    if (topic === "system/health") {
      this.emit("system-health", payload);
      return;
    }

    // system/coin
    if (topic === "system/coin") {
      this.emit("coin", payload as CoinPayload);
      return;
    }

    // agents/{id}/metrics  (LLM cost, token counts, message counts)
    const metricsMatch = topic.match(/^agents\/([^/]+)\/metrics$/);
    if (metricsMatch?.[1]) {
      const agentId = metricsMatch[1];
      const p = payload as Record<string, unknown>;
      const costUsd = (p["costUsd"] ?? p["cost_usd"]) as number | undefined;
      const inputTokens = (p["inputTokens"] ?? p["input_tokens"]) as number | undefined;
      const outputTokens = (p["outputTokens"] ?? p["output_tokens"]) as number | undefined;
      const messagesProcessed = (p["messagesProcessed"] ?? p["messages_processed"]) as number | undefined;
      const uptime = p["uptime"] as number | undefined;
      this.emit("metrics", {
        agentId,
        agentName: (p["agentName"] as string) ?? (p["name"] as string) ?? agentId.slice(0, 8),
        ...(costUsd !== undefined          && { costUsd }),
        ...(inputTokens !== undefined      && { inputTokens }),
        ...(outputTokens !== undefined     && { outputTokens }),
        ...(messagesProcessed !== undefined && { messagesProcessed }),
        ...(uptime !== undefined           && { uptime }),
      });
      return;
    }

    // agents/{id}/logs
    const logsMatch = topic.match(/^agents\/([^/]+)\/logs$/);
    if (logsMatch?.[1]) {
      const agentId = logsMatch[1];
      const p = payload as Record<string, unknown>;
      const message = (p["message"] ?? p["text"]) as string | undefined;
      this.emit("logs", {
        agentId,
        agentName: (p["agentName"] as string) ?? (p["name"] as string) ?? agentId.slice(0, 8),
        ...(message !== undefined && { message }),
      });
      return;
    }

    // agents/{id}/completed
    const completedMatch = topic.match(/^agents\/([^/]+)\/completed$/);
    if (completedMatch?.[1]) {
      const agentId = completedMatch[1];
      const p = payload as Record<string, unknown>;
      this.emit("completed", {
        agentId,
        agentName: (p["agentName"] as string) ?? (p["name"] as string) ?? agentId.slice(0, 8),
      });
      return;
    }

    // nodes/{name}/heartbeat
    const nodeMatch = topic.match(/^nodes\/([^/]+)\/heartbeat$/);
    if (nodeMatch?.[1]) {
      const node = nodeMatch[1];
      const p = payload as Record<string, unknown>;
      const nodeId = p["node_id"] as string | undefined;
      this.emit("node-heartbeat", {
        node,
        agents: (p["agents"] as string[]) ?? [],
        ...(nodeId !== undefined && { nodeId }),
      });
      return;
    }

    this.emit("raw", { topic, payload });
  }
}

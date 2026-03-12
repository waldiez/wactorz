/** All shared type definitions for agents, messages, and MQTT events. */

// ── Agent info ────────────────────────────────────────────────────────────────

/** Lifecycle state of an agent (mirrors Rust ActorState). */
export type AgentState =
  | "initializing"
  | "running"
  | "paused"
  | "stopped"
  | { failed: string };

/** Static info about a registered agent. */
export interface AgentInfo {
  id: string;
  name: string;
  state: AgentState;
  protected: boolean;
  /** Agent role / type hint (e.g. "main", "dynamic", "monitor", "ml"). */
  agentType?: string;
  /** ISO timestamp of last heartbeat. */
  lastHeartbeatAt?: string;
}

// ── MQTT payloads ─────────────────────────────────────────────────────────────

/** Heartbeat payload published by each actor. */
export interface HeartbeatPayload {
  agentId: string;
  agentName: string;
  state: AgentState;
  sequence: number;
  timestampMs: number;
}

/** Status update payload. */
export interface StatusPayload {
  agentId: string;
  agentName: string;
  state: AgentState;
  messagesReceived: number;
  messagesProcessed: number;
  messagesFailed: number;
}

/** Alert payload broadcast by MonitorAgent or any actor. */
export interface AlertPayload {
  agentId: string;
  agentName: string;
  severity: "info" | "warning" | "error" | "critical";
  message: string;
  timestampMs: number;
}

/** Spawn notification: a new agent was created. */
export interface SpawnPayload {
  agentId: string;
  agentName: string;
  agentType: string;
  timestampMs: number;
}

/** Chat message (user → agent or agent → user). */
export interface ChatMessage {
  id: string;
  from: "user" | string; // "user" or agent name
  to: string;            // agent name or "user"
  content: string;
  timestampMs: number;
}

// ── Scene events ──────────────────────────────────────────────────────────────

/** Custom DOM event payload for agent selection. */
export interface AgentSelectedEvent {
  agent: AgentInfo;
}

/** Custom DOM event payload for theme switching. */
// 
  // theme: "graph" | "galaxy" | "cards" | "cards-3d" | "grave" | "social" | "fin";
export interface ThemeChangeEvent {
  theme: "graph" | "galaxy" | "cards" | "grave" | "social" | "fin";
}

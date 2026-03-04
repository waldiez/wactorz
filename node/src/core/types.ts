/**
 * Core type definitions for the AgentFlow Node.js runtime.
 * Mirrors Python core/actor.py + Rust agentflow-core.
 */

export enum ActorState {
  Initializing = "initializing",
  Running      = "running",
  Paused       = "paused",
  Stopped      = "stopped",
  Failed       = "failed",
}

export enum MessageType {
  Task    = "task",
  Result  = "result",
  Status  = "status",
  Error   = "error",
  Command = "command",
  Text    = "text",
}

export interface Message {
  id:        string;
  type:      MessageType;
  senderId?: string;
  targetId?: string;
  payload:   unknown;
  timestamp: number; // ms
}

export interface ActorMetrics {
  tasksReceived:   number;
  tasksCompleted:  number;
  tasksFailed:     number;
  heartbeatsSent:  number;
  lastHeartbeatMs: number;
}

export interface RegistryEntry {
  id:        string;
  name:      string;
  agentType: string;
  state:     ActorState;
}

/** MQTT spawn/heartbeat camelCase wire format */
export interface SpawnPayload {
  agentId:     string;
  agentName:   string;
  agentType:   string;
  timestampMs: number;
}

export interface HeartbeatPayload {
  agentId:     string;
  agentName:   string;
  state:       ActorState;
  timestampMs: number;
}

export interface ChatPayload {
  from:        string;
  to:          string;
  content:     string;
  timestampMs: number;
}

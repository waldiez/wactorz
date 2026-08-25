/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** All shared type definitions for agents, messages, and MQTT events. */

/** Lifecycle state of an agent. */
export type AgentState = "initializing" | "running" | "paused" | "stopped" | { failed: string };

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
    /** Runtime metrics — populated from heartbeat or metrics topic. */
    cpu?: number;
    mem?: number;
    task?: string;
    messagesProcessed?: number;
    costUsd?: number;
    /** Cumulative LLM tokens (absent for non-LLM agents). */
    inputTokens?: number;
    outputTokens?: number;
    uptime?: number;
    /** Set for remote-runner agents — the node name (e.g. "rpi"). */
    node?: string;
}

/** Heartbeat payload published by each actor. */
export interface HeartbeatPayload {
    agentId: string;
    agentName: string;
    state: AgentState;
    sequence: number;
    timestampMs: number;
    /** Optional runtime metrics (Python backend includes these). */
    cpu?: number;
    memory_mb?: number;
    task?: string;
    /** Set for remote-runner agents — matches node_name from remote_runner.py. */
    node?: string;
}

/** Metrics payload — LLM cost, token counts, message counts. */
export interface MetricsPayload {
    agentId: string;
    agentName: string;
    costUsd?: number;
    inputTokens?: number;
    outputTokens?: number;
    messagesProcessed?: number;
    uptime?: number;
}

/** Log entry from an agent. */
export interface LogPayload {
    agentId: string;
    agentName: string;
    message?: string;
    text?: string;
}

/** Node heartbeat — a remote Wactorz node phoning home. */
export interface NodeHeartbeatPayload {
    node: string;
    agents: string[];
    nodeId?: string;
}

/** Status update payload. */
export interface StatusPayload {
    agentId: string;
    agentName: string;
    state: AgentState;
    protected?: boolean;
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
    protected?: boolean;
}

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

/** Chat message (user → agent or agent → user). */
/** A file the user attached to a chat turn (image / document / …). */
export interface Attachment {
    /** Stable id — local while pending, server-assigned once uploaded. */
    id: string;
    name: string;
    /** MIME type, e.g. "image/png", "application/pdf". */
    mime: string;
    size: number;
    /** Resolvable source for previews — an object URL while local, a server
     *  URL once uploaded. */
    url?: string;
}

export interface ChatMessage {
    id: string;
    from: string; // "user" or agent name
    to: string; // agent name or "user"
    content: string;
    timestampMs: number;
    /** Origin channel for special routing, such as an embodied voice turn. */
    source?: string;
    /** Agent surface carrying the turn while another agent may own reasoning. */
    surface?: string;
    /** Human-friendly name for the active interface surface. */
    surfaceLabel?: string;
    /** Internal reasoning agent, retained only as diagnostic metadata. */
    brain?: string;
    /** Files attached to this turn, if any. */
    attachments?: Attachment[];
}

/** Custom DOM event payload for agent selection. */
export interface AgentSelectedEvent {
    agent: AgentInfo;
}

/** Host-level system stats from the backend. */
export interface HostStats {
    cpu?: number;
    memUsedMb?: number;
    memTotalMb?: number;
}

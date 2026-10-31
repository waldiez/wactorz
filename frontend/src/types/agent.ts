/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** All shared type definitions for agents, messages, and MQTT events. */

/** Lifecycle state of an agent. */
export type AgentState = "initializing" | "running" | "stopped" | { failed: string };

/** Static info about a registered agent. */
export interface AgentInfo {
    id: string;
    name: string;
    state: AgentState;
    protected: boolean;
    essential?: boolean;
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
    /** Set for agents on a node — matches the node’s own name. */
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
    /** The chat turn the agent was working on when it logged this, if any. */
    turn?: string;
}

/** Node heartbeat — a remote Wactorz node phoning home. */
export interface NodeHeartbeatPayload {
    node: string;
    agents: string[];
    nodeId?: string;
    /** How close the node is to running out, as its heartbeat said. */
    readings?: NodeReadings;
}

/** A node's readings, under the names its heartbeat uses. Absent where the node did not say. */
export interface NodeReadings {
    /** CPU in use, in percent. */
    cpu_pct?: number;
    /** Memory in use, in MiB. */
    mem_used_mb?: number;
    /** Memory available, in MiB, within any limit on the node. */
    mem_free_mb?: number;
    /** Swap in use, in MiB. */
    swap_used_mb?: number;
    /** One-minute load average. */
    load_1m?: number;
    /** Five-minute load average. */
    load_5m?: number;
    /** Free disk where the node keeps agent state, in MiB. */
    disk_free_mb?: number;
    /** The CPU's temperature in °C. */
    temp_c?: number;
    /** What holds the board back now; empty for nothing, absent where it cannot tell. */
    throttled?: string[];
}

/** What the dashboard knows of one remote node. */
export interface RemoteNode {
    /** The agents its last heartbeat listed. */
    agents: string[];
    /** When it was last heard from, in milliseconds. */
    lastSeen: number;
    /** Its latest readings. */
    readings?: NodeReadings;
    /** What its machine is, from its manifest; null from a node that has not said. */
    manifest?: Record<string, unknown> | null;
}

/** What a node's listing entry carries that the dashboard reads. */
export interface NodeListing {
    /** The node's name. */
    node: string;
    /** Whether main has heard from it lately. */
    online: boolean;
    /** Seconds since the epoch it was last heard from. */
    last_seen?: number;
    /** What its machine is, or null from a node that has not said. */
    manifest?: Record<string, unknown> | null;
    /** Its latest readings, under the names its heartbeat uses. */
    [reading: string]: unknown;
}

/** Status update payload. */
export interface StatusPayload {
    agentId: string;
    agentName: string;
    state: AgentState;
    protected?: boolean;
    essential?: boolean;
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
    /** The id of the agent whose thread the message is in, as the server names it:
     *  the agent's own for its replies, the one spoken to for the person's words. */
    threadId: string;
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
    /** The id of the agent that sent it, when the message came off its own topic. */
    agentId?: string;
    /** A choice the agent offers; the person answers by picking one. */
    options?: string[];
    /** The option picked, once the question is answered, on any channel. */
    answer?: string;
    /** A chat channel the message also went to ("telegram", "discord", "whatsapp"). */
    channel?: string;
    /** A name the person used fits more than one agent: the addresses to pick from. */
    addressChoice?: AddressChoice;
    /** The person's message waits to go out: for a turn under way to end, or for the
     *  connection. Unset once it is sent. */
    queued?: boolean;
}

/** The addresses a name fits, offered to pick from, and the person's message a pick re-sends. */
export interface AddressChoice {
    /** Each agent the name fits, as `name@node`. */
    addresses: string[];
    /** The id of the person's message that used the name. */
    retryOf: string;
}

/** A question as the ledger and the agents' books list it: the question's record, and who asked it. */
export interface LedgerQuestion {
    /** The question's id, the one its answer names. */
    id: string;
    /** The id of the agent that asked. */
    agent_id: string;
    /** The name of the agent that asked. */
    agent: string;
    text: string;
    options: string[];
    /** Where else it went ("telegram"…), or "dashboard". */
    channel?: string;
    /** When it was asked, in Unix seconds. */
    asked_at?: number;
    /** The option picked, once there is one. */
    answer?: string | null;
    /** Where it was picked. */
    answer_channel?: string;
    /** What the person added to the pick, if anything. */
    answer_note?: string;
    /** When it was picked, in Unix seconds. */
    answered_at?: number;
    /** Why the agent says it asks, if it said. */
    context?: string;
}

/** One agent's question book: every question it still keeps, open or answered. */
export interface QuestionBook {
    /** The id of the agent it is. */
    agentId: string;
    /** Its questions; none when the book was cleared, which is how an agent's goes. */
    questions: LedgerQuestion[];
}

/** A person's pick among the options an agent offered, from whichever channel showed them. */
export interface AnswerPayload {
    /** The id of the message answered. */
    id: string;
    agentId: string;
    option: string;
    /** The option's place among those offered, from zero. */
    index: number;
    /** Where it was picked: "dashboard", "telegram", "discord" or "whatsapp". */
    channel: string;
    timestampMs: number;
}

/** Host-level system stats from the backend. */
export interface HostStats {
    cpu?: number;
    memUsedMb?: number;
    memTotalMb?: number;
}

/** A stored sample as the history endpoints serve it: seconds since the epoch, then a value. */
export type Sample = [number, number | null];

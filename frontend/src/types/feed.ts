/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** Shared activity-feed event types, consumed by the cards feed view. */
export type FeedEventType =
    "spawn" | "heartbeat" | "chat" | "alert-error" | "alert-warning" | "stopped" | "health" | "qa-flag";

export interface FeedItem {
    type: FeedEventType;
    label: string;
    agentName: string;
    timestamp: number;
    /** Speaker role for chat rows ("user" | "assistant"); absent for other types. */
    role?: string | undefined;
}

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * How long an agent or a node may go unheard, at each step from quiet to gone.
 *
 * Kept with the agent model rather than the views: the store lets an agent go
 * by them, the feed decides by them that a node came online, and the cards
 * colour a dot by them. All of them must agree on what "stale" means.
 */

/** Heartbeat age (ms) after which an agent's dot turns yellow: a few missed
 *  heartbeats, not yet gone. Heartbeats come every ten seconds. */
export const QUIET_MS = 30_000;

/** Heartbeat age (ms) after which a remote node / agent is treated as stale:
 *  a node is drawn offline, and an agent's dot turns red. */
export const STALE_MS = 180_000;

/**
 * How long a remote node, or an agent on one, may go unheard before it is
 * forgotten entirely. Deliberately far beyond `STALE_MS`, which only decides
 * that it is drawn offline or missing -- worth showing for a good while.
 */
export const NODE_EVICT_MS = STALE_MS * 10;

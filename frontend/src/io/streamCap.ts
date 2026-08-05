/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Ceiling on a single streamed reply's accumulated text, guarding against a
 * runaway or looping stream growing it without bound.
 *
 * Two layers accumulate the same stream independently — the transport, to emit
 * the full text at stream-end, and the chat view, to render it — so both need
 * the guard. They shared a copy of the literal until one of them was changed
 * without the other; the point of this module is that there is nothing to keep
 * in sync.
 */
export const MAX_STREAM_CHARS = 200_000;

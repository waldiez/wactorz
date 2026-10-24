/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Asking the machine's own microphone to listen, on the `host` branch.
 *
 * The server owns that microphone and decides when the speaker has finished,
 * so the page only asks and then hears back what happened. One request shape
 * for the chat box's button and for Settings' microphone check, which differ
 * only in whether what is heard is acted on.
 */

/** How long to wait on one turn: the recording, and then reading it back. */
export const LISTEN_TIMEOUT_MS = 120_000;

/** What came of asking the room to listen. */
export type RoomAnswer =
    | { kind: "heard"; text: string }
    | { kind: "silence" }
    | { kind: "speaking" }
    | { kind: "failed"; reason: string };

/**
 * Ask the room's microphone for one turn.
 *
 * With `act`, what is heard is routed as though typed, and the turn reaches the
 * chat by the usual way; without it, it is only heard, for checking the
 * microphone without spending a turn on it.
 */
export async function listenInTheRoom(apiBase: string, act: boolean): Promise<RoomAnswer> {
    try {
        // Generous, because listening legitimately takes many seconds -- but
        // bounded, because a connection that never answers would otherwise
        // leave the asker waiting for the life of the page.
        const res = await fetch(`${apiBase}/api/stt/listen`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ act }),
            signal: AbortSignal.timeout(LISTEN_TIMEOUT_MS),
        });
        const body = (await res.json()) as {
            text?: string;
            heard?: boolean;
            speaking?: boolean;
            error?: string;
        };
        if (!res.ok) {
            return { kind: "failed", reason: body.error ?? "" };
        }
        if (body.speaking) {
            return { kind: "speaking" };
        }
        return body.heard ? { kind: "heard", text: body.text ?? "" } : { kind: "silence" };
    } catch {
        return { kind: "failed", reason: "The server did not answer." };
    }
}

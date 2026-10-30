/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Asking the server for JSON.
 *
 * Every request has a timeout: a reader waiting on a server that has stopped
 * answering would hold its view's "loading" forever. A refusal comes back with
 * the server's own words (`{"error": ...}`), which say why better than a status
 * code does.
 */

/** How long a request may take before it is given up on. */
export const REQUEST_TIMEOUT_MS = 15_000;

/** What a request came back with: its body, or why there is none. */
export type Answer<T> = { ok: true; value: T } | { ok: false; error: string };

/** A POST of `body` as JSON. */
export function posting(body: object): RequestInit {
    return {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    };
}

/** The JSON at `url`, or why not: the server's words for a refusal, else what went wrong. */
export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<Answer<T>> {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
    try {
        const res = await fetch(url, { ...init, signal: abort.signal });
        const body = (await res.json().catch(() => null)) as unknown;
        if (!res.ok) {
            const said = (body as { error?: unknown } | null)?.error;
            return { ok: false, error: typeof said === "string" ? said : `HTTP ${res.status}` };
        }
        return { ok: true, value: body as T };
    } catch (err) {
        return { ok: false, error: abort.signal.aborted ? "the server did not answer in time" : String(err) };
    } finally {
        clearTimeout(timer);
    }
}

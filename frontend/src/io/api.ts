/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Asking the server.
 *
 * The one place the app calls `fetch`, which the lint rules hold it to. Every
 * request has a timeout: a reader waiting on a server that has stopped answering
 * would hold its view's "loading" forever. The default suits a read or a small
 * change; a route that is slow when healthy passes a longer one, since cutting
 * off work that was going to finish only swaps one failure for another. A refusal
 * comes back with the server's own words (`{"error": ...}`), which say why better
 * than a status code does.
 */

/** How long a request may take, unless it says otherwise, before it is given up on. */
export const REQUEST_TIMEOUT_MS = 15_000;

/** A request that brought back no body, and why. */
export interface Refusal {
    /** Marks the failure side of an {@link Answer}. */
    ok: false;
    /** Why, in words fit to show a person. */
    error: string;
    /** The HTTP status, when the server answered at all. */
    status?: number;
    /** Set when the timeout ran out, which leaves unknown what the server did. */
    timedOut?: true;
}

/** What a request came back with: its body, or why there is none. */
export type Answer<T> = { ok: true; value: T } | Refusal;

/** What every address starts with: empty, or the prefix Home Assistant's ingress serves the page under. */
export function ingressBase(): string {
    return window.__WACTORZ_INGRESS_PATH ?? "";
}

/** The address of `path` (`/api/...`) under any ingress prefix. */
export function apiUrl(path: string): string {
    return `${ingressBase()}${path}`;
}

/** Who is told of every response, for what no single caller can act on. */
let responseWatcher: ((response: Response) => void) | null = null;

/** Be told of every response; returns the way to stop. */
export function watchResponses(watcher: (response: Response) => void): () => void {
    responseWatcher = watcher;
    return () => {
        if (responseWatcher === watcher) {
            responseWatcher = null;
        }
    };
}

/** A POST of `body` as JSON. */
export function posting(body: object): RequestInit {
    return {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    };
}

/**
 * The body as JSON, or null when it is not: a success need not have one. A body
 * cut off by the timeout is not that case, so the abort is let through.
 */
async function jsonBody(res: Response, signal: AbortSignal): Promise<unknown> {
    try {
        return (await res.json()) as unknown;
    } catch (err) {
        if (signal.aborted) {
            throw err;
        }
        return null;
    }
}

/** What a response that is not a success says, in the server's words if it gave any. */
function refused(res: Response, body: unknown): Refusal {
    const said = (body as { error?: unknown } | null)?.error;
    return {
        ok: false,
        error: typeof said === "string" ? said : `HTTP ${res.status}`,
        status: res.status,
    };
}

/** The words for a timeout. A change may have gone through all the same, and must not read as failed. */
function unanswered(init: RequestInit): Refusal {
    const reading = init.method === undefined || init.method === "GET";
    return {
        ok: false,
        error: reading
            ? "the server did not answer in time"
            : "the server did not answer in time, so it is not known whether this happened",
        timedOut: true,
    };
}

/** Makes the request and has `read` take its answer, the body included, inside the timeout. */
async function asking<T>(
    url: string,
    init: RequestInit,
    timeoutMs: number,
    read: (res: Response, signal: AbortSignal) => Promise<Answer<T>>,
): Promise<Answer<T>> {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), timeoutMs);
    try {
        const response = await fetch(url, { ...init, signal: abort.signal });
        responseWatcher?.(response);
        return await read(response, abort.signal);
    } catch (err) {
        return abort.signal.aborted ? unanswered(init) : { ok: false, error: String(err) };
    } finally {
        clearTimeout(timer);
    }
}

/** The JSON at `url`, or why not: the server's words for a refusal, else what went wrong. */
export function requestJson<T>(
    url: string,
    init: RequestInit = {},
    timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Answer<T>> {
    return asking<T>(url, init, timeoutMs, async (res, signal) => {
        const body = await jsonBody(res, signal);
        return res.ok ? { ok: true, value: body as T } : refused(res, body);
    });
}

/** The raw bytes at `url`, for what is not JSON, such as audio; a refusal reads as for {@link requestJson}. */
export function requestBytes(
    url: string,
    init: RequestInit = {},
    timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Answer<ArrayBuffer>> {
    return asking<ArrayBuffer>(url, init, timeoutMs, async (res, signal) =>
        res.ok ? { ok: true, value: await res.arrayBuffer() } : refused(res, await jsonBody(res, signal)),
    );
}

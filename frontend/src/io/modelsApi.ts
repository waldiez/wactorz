/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The stored models and their pointers, from the server (`/api/models`).
 *
 * Every request has a timeout: a reader waiting on a server that has stopped
 * answering would hold the view's "loading" forever. A refusal comes back with
 * the server's own words (`{"error": ...}`), which say why a move cannot be
 * made: already there, nothing to promote, a version that is not stored.
 */
import type { ModelAction, ModelDetail, ModelSummary, PointerMove } from "../types/models";

/** How long a request may take before it is given up on. */
export const MODELS_TIMEOUT_MS = 15_000;

/** What a request came back with: its body, or why there is none. */
export type Answer<T> = { ok: true; value: T } | { ok: false; error: string };

/** A POST of `body`, with `reason` beside it when there is one. */
function posting(body: object, reason: string): RequestInit {
    return {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reason ? { ...body, reason } : body),
    };
}

/** A DELETE, with a body only to carry `reason`: without one it is the bare unset. */
function deleting(reason: string): RequestInit {
    return reason
        ? {
              method: "DELETE",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ reason }),
          }
        : { method: "DELETE" };
}

/** Where a move is asked for, and what is sent with it. */
function route(action: ModelAction, reason: string): { path: string; init: RequestInit } {
    switch (action.kind) {
        case "shadow":
            return { path: "shadow", init: posting({ version: action.version }, reason) };
        case "promote":
            return { path: "promote", init: posting({}, reason) };
        case "rollback":
            return { path: "rollback", init: posting({}, reason) };
        case "clear-shadow":
            return { path: "shadow", init: deleting(reason) };
    }
}

/** The JSON at `url`, or why not: the server's words for a refusal, else what went wrong. */
async function requestJson<T>(url: string, init: RequestInit = {}): Promise<Answer<T>> {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), MODELS_TIMEOUT_MS);
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

/** Reads the stored models and moves their pointers, against the server at `apiBase`. */
export class ModelsApi {
    constructor(private readonly apiBase: string) {}

    /** Every stored model, with its newest version and its pointers. */
    async list(): Promise<Answer<ModelSummary[]>> {
        const got = await requestJson<{ models?: ModelSummary[] }>(`${this.apiBase}/api/models`);
        return got.ok ? { ok: true, value: got.value.models ?? [] } : got;
    }

    /** One model: every version, and its pointers. */
    detail(name: string): Promise<Answer<ModelDetail>> {
        return requestJson<ModelDetail>(`${this.apiBase}/api/models/${encodeURIComponent(name)}`);
    }

    /** The moves of `name`'s pointers, newest first. */
    async moves(name: string): Promise<Answer<PointerMove[]>> {
        const got = await requestJson<{ moves?: PointerMove[] }>(
            `${this.apiBase}/api/models/${encodeURIComponent(name)}/moves`,
        );
        return got.ok ? { ok: true, value: got.value.moves ?? [] } : got;
    }

    /** Make `action` on `name`'s pointers, saying `reason` when there is one. */
    async move(name: string, action: ModelAction, reason = ""): Promise<Answer<null>> {
        const { path, init } = route(action, reason);
        const got = await requestJson<unknown>(
            `${this.apiBase}/api/models/${encodeURIComponent(name)}/${path}`,
            init,
        );
        return got.ok ? { ok: true, value: null } : got;
    }
}

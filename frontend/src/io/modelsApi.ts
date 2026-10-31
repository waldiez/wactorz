/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The stored models and their pointers, from the server (`/api/models`). A
 * refusal says why a move cannot be made: already there, nothing to promote, a
 * version that is not stored.
 */
import type {
    ModelAction,
    ModelDetail,
    ModelSummary,
    PastTrial,
    PointerMove,
    ShadowTrial,
    TrialBucket,
} from "../types/models";
import { posting, requestJson, type Answer } from "./api";

/** How long a trial's figures may take: the server reads every pair of the trial from files. */
export const TRIAL_TIMEOUT_MS = 60_000;

/** `body`, with `reason` beside it when there is one. */
function withReason(body: object, reason: string): object {
    return reason ? { ...body, reason } : body;
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
            return { path: "shadow", init: posting(withReason({ version: action.version }, reason)) };
        case "promote":
            return { path: "promote", init: posting(withReason({}, reason)) };
        case "rollback":
            return { path: "rollback", init: posting(withReason({}, reason)) };
        case "clear-shadow":
            return { path: "shadow", init: deleting(reason) };
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

    /** What `name`'s trial under way shows so far. */
    trial(name: string): Promise<Answer<ShadowTrial>> {
        return requestJson<ShadowTrial>(
            `${this.apiBase}/api/models/${encodeURIComponent(name)}/trial`,
            {},
            TRIAL_TIMEOUT_MS,
        );
    }

    /** `name`'s trial under way over time, oldest first. */
    async trialSeries(name: string): Promise<Answer<TrialBucket[]>> {
        const got = await requestJson<{ buckets?: TrialBucket[] }>(
            `${this.apiBase}/api/models/${encodeURIComponent(name)}/trial/series`,
            {},
            TRIAL_TIMEOUT_MS,
        );
        return got.ok ? { ok: true, value: got.value.buckets ?? [] } : got;
    }

    /** How `name`'s earlier trials ended, newest first. */
    async trials(name: string): Promise<Answer<PastTrial[]>> {
        const got = await requestJson<{ trials?: PastTrial[] }>(
            `${this.apiBase}/api/models/${encodeURIComponent(name)}/trials`,
        );
        return got.ok ? { ok: true, value: got.value.trials ?? [] } : got;
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

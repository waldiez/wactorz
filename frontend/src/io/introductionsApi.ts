/**
 * SPDX-License-Identifier: Apache-2.0
 */
import { apiUrl, requestJson } from "./api";

/** Read the available agent introductions by id, or null if the request failed. */
export async function readIntroductions(): Promise<Record<string, string> | null> {
    const reply = await requestJson<unknown>(apiUrl("/api/introductions"));
    if (!reply.ok || !reply.value || typeof reply.value !== "object" || Array.isArray(reply.value)) {
        return null;
    }
    return Object.values(reply.value).every((text: unknown) => typeof text === "string")
        ? (reply.value as Record<string, string>)
        : null;
}

/** Reads introduction snapshots, retrying failed reads with a bounded delay until stopped. */
export class IntroductionReader {
    private _request = 0;
    private _retryDelay = 1_000;
    private _retry: ReturnType<typeof setTimeout> | null = null;

    constructor(private readonly received: (introductions: Record<string, string>) => void) {}

    /** Read the newest snapshot, coalescing notifications in the same microtask. */
    async refresh(): Promise<void> {
        const request = ++this._request;
        this._clearRetry();
        await Promise.resolve();
        if (request !== this._request) {
            return;
        }
        const introductions = await readIntroductions();
        if (request !== this._request) {
            return;
        }
        if (introductions !== null) {
            this._retryDelay = 1_000;
            this.received(introductions);
            return;
        }
        this._retry = setTimeout(() => {
            this._retry = null;
            void this.refresh();
        }, this._retryDelay);
        this._retryDelay = Math.min(this._retryDelay * 2, 30_000);
    }

    /** Cancel retries and ignore responses to every earlier request. */
    stop(): void {
        this._request++;
        this._retryDelay = 1_000;
        this._clearRetry();
    }

    private _clearRetry(): void {
        if (this._retry !== null) {
            clearTimeout(this._retry);
            this._retry = null;
        }
    }
}

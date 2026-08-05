/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The service worker ships to users and nothing checked it: `public` was
 * ignored by eslint, every config block matched only .ts/.tsx, and there were
 * no tests. Two defects lived there as a result — it decided what to intercept
 * from `url.pathname` alone, so any third-party GET whose path did not match
 * `NEVER_CACHE` was intercepted and its response stored in our cache; and
 * nothing ever evicted, so the cache grew until the browser dropped the whole
 * origin, losing the offline shell the worker exists to provide.
 *
 * `sw.js` is a plain script, not a module, so it is executed here against a
 * fake ServiceWorker scope and driven through the captured handlers.
 */
import { describe, it, expect, beforeEach } from "vitest";

// `?raw` rather than node:fs — the suite has no @types/node, and Vite resolves
// this at transform time so the worker's real source is what gets exercised.
import swSource from "../../public/sw.js?raw";

const ORIGIN = "https://app.test";

interface FakeCache {
    entries: Map<string, unknown>;
    put(request: { url: string }, response: unknown): Promise<void>;
    keys(): Promise<{ url: string }[]>;
    delete(request: { url: string }): Promise<boolean>;
    addAll(): Promise<void>;
}

interface Scope {
    fetchEvent(url: string, method?: string): { respondWithCalled: boolean; responded: unknown };
    cache: FakeCache;
}

/** Run sw.js against a fake scope and return a handle for driving it. */
function loadServiceWorker(networkResponse: { ok: boolean } = { ok: true }): Scope {
    const entries = new Map<string, unknown>();
    const cache: FakeCache = {
        entries,
        put: async (request, response) => {
            entries.set(request.url, response);
        },
        keys: async () => [...entries.keys()].map(url => ({ url })),
        delete: async request => entries.delete(request.url),
        addAll: async () => {},
    };
    const caches = {
        open: async () => cache,
        keys: async () => [],
        delete: async () => true,
        match: async () => undefined,
    };

    const handlers: Record<string, (e: unknown) => void> = {};
    const self = {
        addEventListener: (name: string, fn: (e: unknown) => void) => {
            handlers[name] = fn;
        },
        location: { origin: ORIGIN },
        clients: { claim: async () => {} },
        skipWaiting: () => {},
    };

    const fetchStub = async (): Promise<unknown> => ({
        ...networkResponse,
        clone: () => ({ body: "copy" }),
    });

    new Function("self", "caches", "fetch", "Response", "URL", swSource)(
        self,
        caches,
        fetchStub,
        { error: () => ({ error: true }) },
        URL,
    );

    return {
        cache,
        fetchEvent(url: string, method = "GET") {
            let respondWithCalled = false;
            let responded: unknown = null;
            handlers["fetch"]!({
                request: { url, method },
                respondWith: (r: unknown) => {
                    respondWithCalled = true;
                    responded = r;
                },
            });
            return { respondWithCalled, responded };
        },
    };
}

let sw: Scope;
beforeEach(() => {
    sw = loadServiceWorker();
});

describe("what the worker refuses to intercept", () => {
    it("leaves another origin alone", () => {
        // The bug: only `pathname` was inspected, so this was intercepted and
        // the third party's response was written into our cache.
        expect(sw.fetchEvent("https://evil.test/assets/app.js").respondWithCalled).toBe(false);
    });

    it("leaves the API alone", () => {
        expect(sw.fetchEvent(`${ORIGIN}/api/actors`).respondWithCalled).toBe(false);
    });

    it("leaves the websocket upgrade alone", () => {
        expect(sw.fetchEvent(`${ORIGIN}/ws`).respondWithCalled).toBe(false);
    });

    it("does not treat a sibling path as the websocket", () => {
        expect(sw.fetchEvent(`${ORIGIN}/wsfoo`).respondWithCalled).toBe(true);
    });

    it("leaves non-GET requests alone", () => {
        expect(sw.fetchEvent(`${ORIGIN}/thing`, "POST").respondWithCalled).toBe(false);
    });

    it("still handles its own origin", () => {
        expect(sw.fetchEvent(`${ORIGIN}/assets/app.js`).respondWithCalled).toBe(true);
    });
});

describe("the cache is bounded", () => {
    it("stops growing once the cap is reached", async () => {
        for (let i = 0; i < 200; i++) {
            sw.fetchEvent(`${ORIGIN}/assets/a-${i}.js`);
        }
        // The puts are fire-and-forget; let the microtask queue drain.
        await new Promise(r => setTimeout(r, 0));

        expect(sw.cache.entries.size).toBeGreaterThan(0);
        expect(sw.cache.entries.size).toBeLessThanOrEqual(60);
    });

    it("evicts oldest first, keeping the most recent", async () => {
        for (let i = 0; i < 200; i++) {
            sw.fetchEvent(`${ORIGIN}/assets/a-${i}.js`);
        }
        await new Promise(r => setTimeout(r, 0));

        expect(sw.cache.entries.has(`${ORIGIN}/assets/a-199.js`)).toBe(true);
        expect(sw.cache.entries.has(`${ORIGIN}/assets/a-0.js`)).toBe(false);
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What the service worker intercepts, and what it keeps.
 *
 * Two invariants: it must touch only its own origin, since a stored
 * third-party response would afterwards be served back from this one; and the
 * cache must stay bounded, or it grows until the browser evicts the origin
 * wholesale, taking the offline shell with it.
 *
 * `sw.js` is a plain script rather than a module, so it is executed here
 * against a fake ServiceWorker scope and driven through the handlers it
 * registers.
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
    activate(): Promise<void>;
    cache: FakeCache;
    deleted: string[];
}

/** Run sw.js against a fake scope and return a handle for driving it. */
function loadServiceWorker(
    networkResponse: { ok: boolean; redirected?: boolean } = { ok: true },
    scope = `${ORIGIN}/`,
    heldCaches: string[] = [],
): Scope {
    const deleted: string[] = [];
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
        keys: async () => heldCaches,
        delete: async (name: string) => {
            deleted.push(name);
            return true;
        },
        match: async () => undefined,
    };

    const handlers: Record<string, (e: unknown) => void> = {};
    const self = {
        addEventListener: (name: string, fn: (e: unknown) => void) => {
            handlers[name] = fn;
        },
        location: { origin: ORIGIN },
        registration: { scope },
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
        deleted,
        async activate() {
            let waited: Promise<unknown> = Promise.resolve();
            handlers["activate"]!({ waitUntil: (p: Promise<unknown>) => (waited = p) });
            await waited;
        },
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
        // `pathname` says nothing about the host, so the decision has to be
        // made on origin.
        expect(sw.fetchEvent("https://evil.test/assets/app.js").respondWithCalled).toBe(false);
    });

    it.each([
        "/api/actors",
        "/api/upload/abc",
        "/ws",
        // The API answers under bare names too, with the same private data.
        "/chats",
        "/config",
        "/actors",
        "/feed",
        "/upload/abc",
        "/login",
        "/docs/guide/index.html",
        "/docs/assets/app.js",
        "/wsfoo",
    ])("leaves %s to the network, so nothing of it is kept", path => {
        expect(sw.fetchEvent(`${ORIGIN}${path}`).respondWithCalled).toBe(false);
    });

    it("leaves non-GET requests alone", () => {
        expect(sw.fetchEvent(`${ORIGIN}/assets/app.js`, "POST").respondWithCalled).toBe(false);
    });
});

describe("what the worker keeps", () => {
    it.each(["/", "/index.html", "/assets/app.js", "/assets/app.css", "/favicon.svg", "/site.webmanifest"])(
        "keeps the shell's %s",
        async path => {
            expect(sw.fetchEvent(`${ORIGIN}${path}`).respondWithCalled).toBe(true);
            await new Promise(r => setTimeout(r, 0));

            expect(sw.cache.entries.has(`${ORIGIN}${path}`)).toBe(true);
        },
    );

    it("reads paths under its scope, for a dashboard served under a prefix", () => {
        const prefixed = loadServiceWorker({ ok: true }, `${ORIGIN}/wactorz/`);

        expect(prefixed.fetchEvent(`${ORIGIN}/wactorz/assets/app.js`).respondWithCalled).toBe(true);
        expect(prefixed.fetchEvent(`${ORIGIN}/wactorz/chats`).respondWithCalled).toBe(false);
        expect(prefixed.fetchEvent(`${ORIGIN}/assets/app.js`).respondWithCalled).toBe(false);
    });

    it("does not keep a page it was redirected for, such as the sign-in form", async () => {
        const redirected = loadServiceWorker({ ok: true, redirected: true });

        redirected.fetchEvent(`${ORIGIN}/`);
        await new Promise(r => setTimeout(r, 0));

        expect(redirected.cache.entries.size).toBe(0);
    });

    it("deletes what an earlier worker kept when it takes over", async () => {
        const updated = loadServiceWorker({ ok: true }, `${ORIGIN}/`, ["wactorz-v5", "wactorz-v6"]);

        await updated.activate();

        expect(updated.deleted).toEqual(["wactorz-v5"]);
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

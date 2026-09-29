/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A dashboard whose session has ended must say so, not quietly stop.
 *
 * The page keeps polling after a session expires, and every poll comes back
 * 401. Without a redirect the cards simply freeze at their last values and the
 * feed stops — a page that looks alive and is not, which is worse than one that
 * plainly asks you to sign in.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { installSessionExpiry } from "../io/sessionExpiry";
import { resetDeadSession } from "../ui/deadSession";

type FakeWindow = {
    fetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    location: { assign: ReturnType<typeof vi.fn>; pathname: string; search: string };
    // Read off the window that was handed in rather than the global one, which is
    // what the module does, so a test that sets only the global proves nothing.
    __WACTORZ_INGRESS_PATH?: string;
};

function fakeWindow(status: number, ingress = ""): FakeWindow {
    return {
        fetch: vi.fn(async () => new Response("", { status })),
        location: { assign: vi.fn(), pathname: "/", search: "" },
        __WACTORZ_INGRESS_PATH: ingress,
    };
}

let restore: (() => void) | null = null;

beforeEach(() => {
    // `exactOptionalPropertyTypes` forbids assigning undefined to an optional
    // property, so the absent case is expressed by deleting it — which is also
    // what "no ingress prefix" actually looks like.
    delete window.__WACTORZ_INGRESS_PATH;
    document.body.innerHTML = "";
    resetDeadSession();
});

afterEach(() => {
    restore?.();
    restore = null;
});

describe("installSessionExpiry", () => {
    it("sends the page to sign in when a request comes back 401", async () => {
        const w = fakeWindow(401);
        restore = installSessionExpiry(w as unknown as Window);

        await w.fetch("/api/actors");

        expect(w.location.assign).toHaveBeenCalledOnce();
        expect(w.location.assign.mock.calls[0]![0]).toContain("/login");
    });

    it("remembers where the user was, so signing in returns them there", async () => {
        const w = fakeWindow(401);
        w.location.pathname = "/";
        w.location.search = "?view=feed";
        restore = installSessionExpiry(w as unknown as Window);

        await w.fetch("/api/logs");

        expect(w.location.assign.mock.calls[0]![0]).toContain(encodeURIComponent("/?view=feed"));
    });

    it("navigates once however many polls fail together", async () => {
        // Every poll in flight fails at the same moment. Without a guard the
        // second navigation cancels the first and the address bar can end up
        // between the two.
        const w = fakeWindow(401);
        restore = installSessionExpiry(w as unknown as Window);

        await Promise.all([w.fetch("/api/actors"), w.fetch("/api/feed"), w.fetch("/api/logs")]);

        expect(w.location.assign).toHaveBeenCalledOnce();
    });

    it("leaves a successful response alone", async () => {
        const w = fakeWindow(200);
        restore = installSessionExpiry(w as unknown as Window);

        const res = await w.fetch("/api/actors");

        expect(res.status).toBe(200);
        expect(w.location.assign).not.toHaveBeenCalled();
    });

    it("does not treat a 403 as an expired session", async () => {
        // 403 is the origin check refusing the request. Sending someone to sign
        // in over that offers a fix for something signing in cannot fix.
        const w = fakeWindow(403);
        restore = installSessionExpiry(w as unknown as Window);

        await w.fetch("/api/reset");

        expect(w.location.assign).not.toHaveBeenCalled();
    });

    it("still returns the response to the caller", async () => {
        // The redirect is not instant, so whatever called fetch keeps running
        // for a moment and must not get undefined back.
        const w = fakeWindow(401);
        restore = installSessionExpiry(w as unknown as Window);

        const res = await w.fetch("/api/actors");

        expect(res.status).toBe(401);
    });

    it("restores the original fetch when released", async () => {
        const w = fakeWindow(401);
        const original = w.fetch;

        installSessionExpiry(w as unknown as Window)();

        expect(w.fetch).toBe(original);
    });

    it("honours an ingress path prefix", async () => {
        window.__WACTORZ_INGRESS_PATH = "/api/hassio_ingress/abc";
        const w = fakeWindow(401);
        restore = installSessionExpiry(w as unknown as Window);

        await w.fetch("/api/actors");

        expect(w.location.assign.mock.calls[0]![0]).toContain("/api/hassio_ingress/abc/login");
    });
});

describe("a page that can no longer reach Home Assistant", () => {
    const overlay = (): HTMLElement | null => document.querySelector(".af-dead-session");
    const INGRESS = "/api/hassio_ingress/abc";

    /** Fail `n` times, with the clock advanced past the point of no recovery. */
    async function failFor(w: FakeWindow, n: number, ms: number): Promise<void> {
        for (let i = 0; i < n; i++) {
            await w.fetch("/api/actors");
            vi.setSystemTime(Date.now() + ms / n);
        }
        await w.fetch("/api/actors");
    }

    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("says so once a run of 503s has gone on too long to be anything else", async () => {
        const w = fakeWindow(503, INGRESS);
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await failFor(w, 3, 61_000);

        expect(overlay()).not.toBeNull();
        // Says what the page can observe and what to do, not why: a revoked token
        // and a session that lapsed under a healthy add-on look the same from here.
        expect(overlay()!.textContent).toContain("no longer valid");
        expect(overlay()!.textContent).toContain("Home Assistant sidebar");
        expect(overlay()!.textContent).not.toContain("reinstall");
        expect(revoked).toHaveBeenCalledTimes(1);
    });

    it("keeps quiet through a boot, where the app answers 503 until its registry exists", async () => {
        // The UI binds before the agents start, so every restart passes through a
        // window of "registry not available". Burying a healthy install under a
        // no-way-back overlay each time would be worse than the bug being fixed.
        const w = fakeWindow(503, INGRESS);
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await failFor(w, 8, 28_000);

        expect(overlay()).toBeNull();
        expect(revoked).not.toHaveBeenCalled();
    });

    it("forgets a run of 503s as soon as anything answers", async () => {
        // A command refused while the broker is down 503s with successful polls
        // around it; only a dead token returns nothing else, ever. Without the
        // reset, two unrelated bad patches an hour apart would add up to a verdict.
        let status = 503;
        const w: FakeWindow = {
            fetch: vi.fn(async () => new Response("", { status })),
            location: { assign: vi.fn(), pathname: "/", search: "" },
            __WACTORZ_INGRESS_PATH: INGRESS,
        };
        restore = installSessionExpiry(w as unknown as Window);

        await failFor(w, 3, 40_000);
        status = 200;
        await w.fetch("/api/actors");
        status = 503;
        await failFor(w, 3, 40_000);

        // Eighty seconds of 503s in total, but neither run reached a minute.
        expect(overlay()).toBeNull();
    });

    it("does not conclude anything on a deployment with no ingress prefix", async () => {
        // Standalone is served the same bootstrap script with an empty value, so
        // the prefix is defined and falsy. Telling that install to reopen from a
        // sidebar it does not have would be nonsense.
        const w = fakeWindow(503, "");
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await failFor(w, 5, 120_000);

        expect(overlay()).toBeNull();
        expect(revoked).not.toHaveBeenCalled();
    });

    it("does not send the page to sign in, since /login is under the same dead prefix", async () => {
        const w = fakeWindow(503, INGRESS);
        restore = installSessionExpiry(w as unknown as Window);

        await failFor(w, 3, 61_000);

        expect(w.location.assign).not.toHaveBeenCalled();
    });

    it("leaves a 401 under ingress to the sign-in redirect", async () => {
        // The token is alive; the session is not. `/login` under this prefix
        // answers, so sending someone there is the fix and the overlay is not.
        const w = fakeWindow(401, INGRESS);
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await w.fetch("/api/actors");

        expect(overlay()).toBeNull();
        expect(revoked).not.toHaveBeenCalled();
        expect(w.location.assign).toHaveBeenCalled();
    });

    it("shows one overlay however many polls fail", async () => {
        const w = fakeWindow(503, INGRESS);
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await failFor(w, 3, 61_000);
        await Promise.all([w.fetch("/api/feed"), w.fetch("/api/config")]);

        expect(document.querySelectorAll(".af-dead-session")).toHaveLength(1);
        expect(revoked).toHaveBeenCalledTimes(1);
    });
});

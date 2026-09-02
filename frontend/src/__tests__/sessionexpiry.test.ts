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
    __WACTORZ_INGRESS_PATH?: string;
};

function fakeWindow(status: number, ingress = ""): FakeWindow {
    return {
        fetch: vi.fn(async () => new Response("", { status })),
        location: { assign: vi.fn(), pathname: "/", search: "" },
        ...(ingress ? { __WACTORZ_INGRESS_PATH: ingress } : {}),
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

    it("shows recovery instructions after a sustained ingress 503", async () => {
        vi.useFakeTimers();
        const w = fakeWindow(503, "/api/hassio_ingress/abc");
        const revoked = vi.fn();
        restore = installSessionExpiry(w as unknown as Window, revoked);

        await w.fetch("/api/actors");
        vi.advanceTimersByTime(30_000);
        await w.fetch("/api/actors");
        vi.advanceTimersByTime(31_000);
        await w.fetch("/api/actors");

        expect(document.querySelector(".af-dead-session")?.textContent).toContain("Home Assistant sidebar");
        expect(revoked).toHaveBeenCalledOnce();
        vi.useRealTimers();
    });
});

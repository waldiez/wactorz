/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import {
    apiUrl,
    ingressBase,
    posting,
    requestBytes,
    requestJson,
    REQUEST_TIMEOUT_MS,
    watchResponses,
} from "../io/api";
import { fetchCommandForms } from "../io/commandsApi";
import { readActorHistory, readChatLog } from "../io/chatHistoryApi";
import { readCostLimit } from "../io/costApi";
import { requestReset, RESET_TIMEOUT_MS } from "../io/resetApi";
import { installSessionExpiry } from "../io/sessionExpiry";
import { postUpload, UPLOAD_TIMEOUT_MS } from "../io/uploadsApi";
import { postOrWarn } from "../ui/dashboard/mutate";
import { toast } from "../ui/ToastManager";

/** A server that never answers: the request ends only when it is aborted. */
function silentServer(): void {
    vi.stubGlobal(
        "fetch",
        vi.fn(
            (_url: string, init?: RequestInit) =>
                new Promise((_resolve, reject) =>
                    init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                ),
        ),
    );
}

/** Whether `pending` has settled by now. */
async function settled(pending: Promise<unknown>): Promise<boolean> {
    let done = false;
    void pending.then(() => {
        done = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    return done;
}

beforeEach(() => {
    delete window.__WACTORZ_INGRESS_PATH;
    vi.clearAllMocks();
});

afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe("addresses", () => {
    it("are plain without an ingress prefix", () => {
        expect(ingressBase()).toBe("");
        expect(apiUrl("/api/cost")).toBe("/api/cost");
    });

    it("carry the ingress prefix, read when asked: the page sets it before any request", () => {
        window.__WACTORZ_INGRESS_PATH = "/hassio/ingress/abc";

        expect(apiUrl("/api/cost")).toBe("/hassio/ingress/abc/api/cost");
    });
});

describe("requestJson", () => {
    it("takes a success with no JSON in it: a change need not answer with a body", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response(null, { status: 204 })),
        );

        expect(await requestJson("/api/chat/stop", { method: "POST" })).toEqual({ ok: true, value: null });
    });

    it("says what went wrong when the request never reached the server", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => {
                throw new TypeError("Failed to fetch");
            }),
        );

        const got = await requestJson("/api/cost");

        expect(got).toEqual({ ok: false, error: "TypeError: Failed to fetch" });
    });

    it("gives up at the timeout it is handed, not the default", async () => {
        vi.useFakeTimers();
        silentServer();

        const pending = requestJson("/slow", {}, REQUEST_TIMEOUT_MS * 2);
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);
        expect(await settled(pending)).toBe(false);

        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
        expect(await pending).toMatchObject({ ok: false, timedOut: true });
    });

    it("gives up on a body that stops arriving, not only on headers that never came", async () => {
        vi.useFakeTimers();
        vi.stubGlobal(
            "fetch",
            vi.fn((_url: string, init?: RequestInit) =>
                Promise.resolve({
                    ok: true,
                    status: 200,
                    arrayBuffer: () =>
                        new Promise((_resolve, reject) =>
                            init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                        ),
                }),
            ),
        );

        const pending = requestBytes("/api/tts");
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

        expect(await pending).toMatchObject({ ok: false, timedOut: true });
    });

    it("does not take a body cut off by the timeout for a success with no body", async () => {
        vi.useFakeTimers();
        vi.stubGlobal(
            "fetch",
            vi.fn((_url: string, init?: RequestInit) =>
                Promise.resolve({
                    ok: true,
                    status: 200,
                    json: () =>
                        new Promise((_resolve, reject) =>
                            init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                        ),
                }),
            ),
        );

        const pending = requestJson("/api/feed");
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

        expect(await pending).toMatchObject({ ok: false, timedOut: true });
    });

    it("does not call a change that timed out failed: it may have gone through", async () => {
        vi.useFakeTimers();
        silentServer();

        const pending = requestJson("/api/models/x/promote", posting({}));
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

        expect(await pending).toEqual({
            ok: false,
            error: "the server did not answer in time, so it is not known whether this happened",
            timedOut: true,
        });
    });

    it("goes through the session watch, so a 401 still leads to the sign-in page", async () => {
        const assign = vi.fn();
        const target = {
            fetch: vi.fn(async () => new Response("", { status: 401 })),
            location: { assign, pathname: "/", search: "" },
        };
        const restore = installSessionExpiry(target as unknown as Window);
        vi.stubGlobal("fetch", target.fetch);

        await requestJson("/api/cost");

        expect(assign).toHaveBeenCalledWith("/login?next=%2F");
        restore();
    });
});

describe("watchResponses", () => {
    const answered = () =>
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response(null, { status: 204 })),
        );

    it("tells the watcher of every response, until it stops", async () => {
        answered();
        const heard = vi.fn();
        const stop = watchResponses(heard);

        await requestJson("/api/cost");
        stop();
        await requestJson("/api/cost");

        expect(heard).toHaveBeenCalledTimes(1);
    });

    it("leaves a later watcher in place when an earlier one stops", async () => {
        answered();
        const stopFirst = watchResponses(vi.fn());
        const later = vi.fn();
        const stopLater = watchResponses(later);

        stopFirst();
        await requestJson("/api/cost");
        stopLater();

        expect(later).toHaveBeenCalledTimes(1);
    });
});

describe("requestBytes", () => {
    it("brings back the body untouched", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response(new Uint8Array([1, 2, 3]))),
        );

        const got = await requestBytes("/api/tts");

        expect(got.ok && [...new Uint8Array(got.value)]).toEqual([1, 2, 3]);
    });

    it("keeps the status of a refusal, which decides what a caller does next", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ error: "edge-tts is not installed" }, { status: 503 })),
        );

        expect(await requestBytes("/api/tts")).toEqual({
            ok: false,
            error: "edge-tts is not installed",
            status: 503,
        });
    });
});

describe("routes that are slow when healthy", () => {
    it("a reset outlives the default timeout, then says it does not know", async () => {
        vi.useFakeTimers();
        silentServer();

        const pending = requestReset("all", null);
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);
        expect(await settled(pending)).toBe(false);

        await vi.advanceTimersByTimeAsync(RESET_TIMEOUT_MS);
        expect(await pending).toMatchObject({ ok: false, timedOut: true });
    });

    it("an upload outlives a reset's timeout", async () => {
        vi.useFakeTimers();
        silentServer();

        const pending = postUpload(new File(["x"], "a.txt"), "");
        await vi.advanceTimersByTimeAsync(RESET_TIMEOUT_MS + 1);
        expect(await settled(pending)).toBe(false);

        await vi.advanceTimersByTimeAsync(UPLOAD_TIMEOUT_MS);
        expect(await pending).toMatchObject({ ok: false, timedOut: true });
    });
});

describe("what the readers make of a body they cannot use", () => {
    beforeEach(() => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("not json")),
        );
    });

    it("a chat history reads as could-not-ask, never as no history", async () => {
        expect(await readChatLog("a1")).toBeNull();
        expect(await readActorHistory("a1")).toBeNull();
    });

    it("a spend limit reads as unknown", async () => {
        expect(await readCostLimit()).toBeNull();
    });

    it("a command list reads as empty", async () => {
        expect(await fetchCommandForms()).toEqual([]);
    });
});

describe("postOrWarn", () => {
    it("shows the server's words for a refusal", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json({ error: "no limit set" }, { status: 409 })),
        );

        expect(await postOrWarn("/api/cost/reset", { method: "POST" }, "reset spend")).toBe(false);

        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ title: "Couldn't reset spend", message: "no limit set" }),
        );
    });

    it("warns of a change that timed out instead of calling it failed", async () => {
        vi.useFakeTimers();
        silentServer();

        const pending = postOrWarn("/api/cost/reset", { method: "POST" }, "reset spend");
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

        expect(await pending).toBe(false);
        expect(toast.show).toHaveBeenCalledWith({
            type: "alert-warning",
            title: "Unconfirmed: reset spend",
            message: "the server did not answer in time, so it is not known whether this happened",
        });
    });

    it("says nothing when the change took", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response(null, { status: 204 })),
        );

        expect(await postOrWarn("/api/chat/stop", { method: "POST" }, "stop")).toBe(true);

        expect(toast.show).not.toHaveBeenCalled();
    });
});

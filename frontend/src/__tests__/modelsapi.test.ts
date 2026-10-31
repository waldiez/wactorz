/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The reader of `/api/models`: what it asks, what a refusal says, and that no
 * request waits forever.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { REQUEST_TIMEOUT_MS } from "../io/api";
import { ModelsApi } from "../io/modelsApi";

function answering(status: number, body: unknown) {
    return vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status }));
}

afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
});

describe("reading models", () => {
    it("lists them, under the ingress path", async () => {
        const fetched = answering(200, { models: [{ name: "hvac" }] });
        vi.stubGlobal("fetch", fetched);

        const got = await new ModelsApi("/ingress").list();

        expect(got).toEqual({ ok: true, value: [{ name: "hvac" }] });
        expect(fetched.mock.calls[0]![0]).toBe("/ingress/api/models");
    });

    it("reads one and its moves, the name escaped", async () => {
        const fetched = answering(200, { name: "a b", versions: [], pointers: {}, moves: [] });
        vi.stubGlobal("fetch", fetched);
        const api = new ModelsApi("");

        expect((await api.detail("a b")).ok).toBe(true);
        expect(await api.moves("a b")).toEqual({ ok: true, value: [] });
        expect(fetched.mock.calls.map(c => c[0])).toEqual(["/api/models/a%20b", "/api/models/a%20b/moves"]);
    });

    it("gives the server's words for a refusal, or the status without them", async () => {
        vi.stubGlobal("fetch", answering(404, { error: "no model 'ghost' is stored" }));
        expect(await new ModelsApi("").detail("ghost")).toEqual({
            ok: false,
            error: "no model 'ghost' is stored",
            status: 404,
        });

        vi.stubGlobal(
            "fetch",
            vi.fn(async () => new Response("not json", { status: 502 })),
        );
        expect(await new ModelsApi("").list()).toEqual({ ok: false, error: "HTTP 502", status: 502 });
    });

    it("says so when the server does not answer in time", async () => {
        vi.useFakeTimers();
        vi.stubGlobal(
            "fetch",
            vi.fn(
                (_url: string, init?: RequestInit) =>
                    new Promise((_resolve, reject) =>
                        init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
                    ),
            ),
        );

        const pending = new ModelsApi("").list();
        await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1);

        expect(await pending).toEqual({
            ok: false,
            error: "the server did not answer in time",
            timedOut: true,
        });
    });
});

describe("moving a pointer", () => {
    it("posts each move to its route, with the reason when there is one", async () => {
        const fetched = answering(200, { pointer: {} });
        vi.stubGlobal("fetch", fetched);
        const api = new ModelsApi("");

        await api.move("hvac", { kind: "shadow", version: 8 }, "regime change");
        await api.move("hvac", { kind: "promote" });
        await api.move("hvac", { kind: "rollback" }, "false alarms");

        expect(fetched.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
            ["/api/models/hvac/shadow", "POST", JSON.stringify({ version: 8, reason: "regime change" })],
            ["/api/models/hvac/promote", "POST", "{}"],
            ["/api/models/hvac/rollback", "POST", JSON.stringify({ reason: "false alarms" })],
        ]);
    });

    it("clears a shadow with a DELETE that carries the reason", async () => {
        const fetched = answering(200, { pointer: {} });
        vi.stubGlobal("fetch", fetched);

        expect(await new ModelsApi("").move("hvac", { kind: "clear-shadow" }, "noisy at night")).toEqual({
            ok: true,
            value: null,
        });

        expect(fetched.mock.calls[0]![0]).toBe("/api/models/hvac/shadow");
        expect(fetched.mock.calls[0]![1]).toEqual(
            expect.objectContaining({ method: "DELETE", body: JSON.stringify({ reason: "noisy at night" }) }),
        );
    });

    it("clears a shadow with a bare DELETE when no reason is given", async () => {
        const fetched = answering(200, { pointer: {} });
        vi.stubGlobal("fetch", fetched);

        await new ModelsApi("").move("hvac", { kind: "clear-shadow" });

        expect(fetched.mock.calls[0]![1]).toEqual({ method: "DELETE", signal: expect.any(AbortSignal) });
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Reading application-log records from `/api/logs`.
 *
 * The envelope is validated at this boundary rather than cast: a malformed
 * entry is dropped here, where there is one place to do it, instead of becoming
 * `undefined` somewhere in the middle of rendering a row.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { fetchAppLogs } from "../ui/dashboard/appLogs";

const entry = (over: Record<string, unknown> = {}) => ({
    source: "app",
    ts: 1_700_000_000,
    level: "INFO",
    origin: "wactorz.web.app",
    text: "listening",
    ...over,
});

function respond(body: unknown, ok = true): void {
    vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({ ok, json: async () => body })),
    );
}

describe("fetchAppLogs", () => {
    beforeEach(() => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it("returns the records the server sent", async () => {
        respond({ entries: [entry({ text: "one" }), entry({ text: "two" })] });

        const logs = await fetchAppLogs();

        expect(logs!.map(e => e.text)).toEqual(["one", "two"]);
        expect(logs![0]!.source).toBe("app");
    });

    it("asks for a bounded number", async () => {
        respond({ entries: [] });

        await fetchAppLogs(50);

        expect(fetch).toHaveBeenCalledWith(expect.stringContaining("limit=50"));
    });

    it("honours the ingress prefix", async () => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "/hassio/ingress/x";
        respond({ entries: [] });

        await fetchAppLogs();

        expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/hassio/ingress/x/api/logs"));
    });

    it("drops an entry it cannot draw", async () => {
        respond({ entries: [entry(), { ts: "not a number" }, null, "nonsense"] });

        const logs = await fetchAppLogs();

        expect(logs).toHaveLength(1);
    });

    it("falls back to a level the filter understands", async () => {
        respond({ entries: [entry({ level: "TRACE" })] });

        expect((await fetchAppLogs())![0]!.level).toBe("INFO");
    });

    it("names an origin even when the server omits one", async () => {
        respond({ entries: [entry({ origin: undefined })] });

        expect((await fetchAppLogs())![0]!.origin).toBe("?");
    });

    it("distinguishes a failed request from an empty log", async () => {
        // null, not []: the view can then leave what it has on screen rather
        // than reporting that nothing was ever logged.
        respond({}, false);
        expect(await fetchAppLogs()).toBeNull();

        vi.stubGlobal(
            "fetch",
            vi.fn(async () => {
                throw new Error("offline");
            }),
        );
        expect(await fetchAppLogs()).toBeNull();
    });

    it("treats a malformed body as a failure", async () => {
        respond({ entries: "not an array" });

        expect(await fetchAppLogs()).toBeNull();
    });
});

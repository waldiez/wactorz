/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { buildFusekiView } from "../ui/dashboard/fusekiView";

const DS_KEY = "wactorz-fuseki-dataset";

function mockFetch(impl: () => Partial<Response>): void {
    globalThis.fetch = vi.fn().mockImplementation(async () => impl() as Response);
}

function flush(): Promise<void> {
    return new Promise(r => setTimeout(r, 0));
}

describe("fusekiView", () => {
    beforeEach(() => {
        localStorage.clear();
        // The console auto-runs the first preset on mount; default stub returns
        // an empty SELECT result.
        mockFetch(() => ({
            ok: true,
            json: async () => ({ head: { vars: ["g"] }, results: { bindings: [] } }),
            text: async () => "",
        }));
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("always renders the console (presets + editor + dataset selector)", () => {
        const el = buildFusekiView(() => {});
        expect(el.querySelector(".af-fuseki-sidebar")).not.toBeNull();
        expect(el.querySelector(".af-fuseki-editor")).not.toBeNull();
        expect(el.querySelector(".af-fuseki-run-btn")).not.toBeNull();
        // Dataset selector replaces the old browser host/credentials form.
        expect(el.querySelector<HTMLInputElement>("#fsk-ds")?.value).toBe("wactorz");
    });

    it("auto-runs the first preset against the dataset proxy path", () => {
        localStorage.setItem(DS_KEY, "demo");
        buildFusekiView(() => {});
        expect(globalThis.fetch).toHaveBeenCalledWith(
            expect.stringContaining("/api/fuseki/demo/sparql"),
            expect.objectContaining({ method: "POST" }),
        );
    });

    it("changing the dataset persists it and re-renders", () => {
        const reRender = vi.fn();
        const el = buildFusekiView(reRender);
        const input = el.querySelector<HTMLInputElement>("#fsk-ds")!;
        input.value = "other";
        input.dispatchEvent(new Event("change"));
        expect(localStorage.getItem(DS_KEY)).toBe("other");
        expect(reRender).toHaveBeenCalled();
    });

    it("shows a server-config message when the proxy reports Fuseki not configured", async () => {
        mockFetch(() => ({
            ok: false,
            status: 503,
            text: async () => JSON.stringify({ error: "Fuseki is not configured" }),
            json: async () => ({ head: { vars: [] } }),
        }));
        const el = buildFusekiView(() => {});
        await flush();
        const results = el.querySelector(".af-fuseki-results");
        expect(results?.textContent).toContain("isn't configured on the server");
        expect(results?.innerHTML).toContain("FUSEKI_URL");
    });
});

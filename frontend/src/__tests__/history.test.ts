/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { fetchAgentsField, fetchHistory, fetchNodes } from "../ui/dashboard/history";

function answer(body: unknown, ok = true): void {
    globalThis.fetch = vi.fn(async () => ({ ok, json: async () => body })) as unknown as typeof fetch;
}

afterEach(() => {
    vi.restoreAllMocks();
    delete window.__WACTORZ_INGRESS_PATH;
});

describe("fetchHistory", () => {
    it("asks for one agent's window, the name escaped, behind the ingress path", async () => {
        window.__WACTORZ_INGRESS_PATH = "/ingress";
        answer({ samples: [{ ts: 1 }] });

        expect(await fetchHistory("agents", "a b/c", 24)).toEqual([{ ts: 1 }]);
        expect(fetch).toHaveBeenCalledWith("/ingress/api/history/agents/a%20b%2Fc?hours=24");
    });

    it("is null when the server refuses or answers with no samples", async () => {
        answer({}, false);
        expect(await fetchHistory("nodes", "rpi", 1)).toBeNull();
        answer({ error: "no database" });
        expect(await fetchHistory("nodes", "rpi", 1)).toBeNull();
        answer("not an object");
        expect(await fetchHistory("nodes", "rpi", 1)).toBeNull();
    });

    it("is null when the request fails outright", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new TypeError("offline");
        }) as unknown as typeof fetch;
        expect(await fetchHistory("nodes", "rpi", 1)).toBeNull();
    });
});

describe("fetchAgentsField", () => {
    it("maps every agent's samples by name, skipping what is not a list", async () => {
        answer({ agents: { weather: [[1, 2]], odd: "x" } });

        const found = await fetchAgentsField("messages_processed", 1);

        expect(fetch).toHaveBeenCalledWith("/api/history/agents?field=messages_processed&hours=1");
        expect([...(found ?? [])]).toEqual([["weather", [[1, 2]]]]);
    });

    it("is null without an agents map", async () => {
        answer({ field: "x" });
        expect(await fetchAgentsField("x", 1)).toBeNull();
    });
});

describe("fetchNodes", () => {
    it("lists the nodes, or is null without a list", async () => {
        answer({ nodes: [{ node: "rpi", online: true }] });
        expect(await fetchNodes()).toEqual([{ node: "rpi", online: true }]);
        answer({ nodes: "none" });
        expect(await fetchNodes()).toBeNull();
    });
});

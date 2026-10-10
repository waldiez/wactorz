/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
    CARD_HOURS,
    OverviewData,
    REFRESH_MS,
    type OverviewDataHost,
    type OverviewReaders,
} from "../ui/dashboard/overviewData";

const T0 = 1_700_000_000;

function readers(): { [K in keyof OverviewReaders]: ReturnType<typeof vi.fn> } & OverviewReaders {
    return {
        agentsField: vi.fn(
            async () =>
                new Map([
                    [
                        "weather",
                        [
                            [T0, 1],
                            [T0 + 60, 4],
                        ] as [number, number][],
                    ],
                ]),
        ),
        nodes: vi.fn(async () => [{ node: "rpi", online: true, manifest: { manifest_v: 1 } }]),
        history: vi.fn(async () => [{ ts: T0, cpu_pct: 12, mem_free_mb: 7000 }]),
    } as never;
}

function host(over: Partial<OverviewDataHost> = {}): OverviewDataHost {
    return { isOverview: () => true, nodeNames: () => ["edge"], onUpdate: vi.fn(), ...over };
}

describe("OverviewData.refresh", () => {
    it("fetches every agent's activity in one request, and each node's trend", async () => {
        const read = readers();
        const h = host();
        const data = new OverviewData(h, read);

        await data.refresh();

        expect(read.agentsField).toHaveBeenCalledWith("messages_processed", CARD_HOURS);
        expect(data.agentRates.get("weather")?.map(p => p.v)).toEqual([3]);
        expect(data.listings.get("rpi")?.manifest).toEqual({ manifest_v: 1 });
        // Both the nodes the server lists and the ones this page has heard from.
        expect(read.history.mock.calls.map(c => c[1]).sort()).toEqual(["edge", "rpi"]);
        expect(data.nodeTrends.get("rpi")?.cpu.map(p => p.v)).toEqual([12]);
        expect(data.nodeTrends.get("rpi")?.free.map(p => p.v)).toEqual([7000]);
        expect(h.onUpdate).toHaveBeenCalledOnce();
    });

    it("keeps what it had when a request fails", async () => {
        const read = readers();
        const data = new OverviewData(host({ nodeNames: () => [] }), read);
        await data.refresh();
        read.agentsField.mockResolvedValueOnce(null);
        read.nodes.mockResolvedValueOnce(null);
        read.history.mockResolvedValueOnce(null);

        await data.refresh();

        expect(data.agentRates.has("weather")).toBe(true);
        expect(data.listings.has("rpi")).toBe(true);
        expect(data.nodeTrends.has("rpi")).toBe(true);
    });
});

describe("OverviewData timer", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("fetches at once, then on each tick while the overview shows", async () => {
        const read = readers();
        let overview = true;
        const data = new OverviewData(host({ isOverview: () => overview }), read);

        data.start();
        expect(read.agentsField).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(REFRESH_MS);
        expect(read.agentsField).toHaveBeenCalledTimes(2);

        overview = false;
        await vi.advanceTimersByTimeAsync(REFRESH_MS);
        expect(read.agentsField).toHaveBeenCalledTimes(2);

        data.stop();
        overview = true;
        await vi.advanceTimersByTimeAsync(REFRESH_MS);
        expect(read.agentsField).toHaveBeenCalledTimes(2);
        data.stop();
    });

    it("waits while the page is hidden", async () => {
        const read = readers();
        const data = new OverviewData(host(), read);
        const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);

        data.start();
        await vi.advanceTimersByTimeAsync(REFRESH_MS);

        expect(read.agentsField).toHaveBeenCalledTimes(1);
        hidden.mockRestore();
        data.stop();
    });
});

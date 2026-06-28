/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MetricsController, type MetricsHost } from "../ui/dashboard/metrics";
import type { View } from "../ui/dashboard/types";

function hostBarMarkup(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML = `
      <div id="af-host-bar">
        <div class="af-host-bar-fill-cpu"></div><span class="af-host-cpu-val"></span>
        <div class="af-host-bar-fill-mem"></div><span class="af-host-mem-val"></span>
      </div>`;
    return root;
}

function makeHost(view: View, root = hostBarMarkup()) {
    const renderStats = vi.fn<() => void>();
    const renderView = vi.fn<() => void>();
    return { root, getView: () => view, renderStats, renderView } satisfies MetricsHost;
}

describe("MetricsController", () => {
    beforeEach(() => {
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => vi.restoreAllMocks());

    it("stores totals and re-renders stats only on the overview view", () => {
        const host = makeHost("overview");
        const m = new MetricsController(host);
        m.setTotalCostUsd(1.25);
        m.setTotalMessages(42);
        expect(m.totalCostUsd).toBe(1.25);
        expect(m.totalMessages).toBe(42);
        expect(host.renderStats).toHaveBeenCalledTimes(2);
    });

    it("does not re-render stats when off the overview", () => {
        const host = makeHost("chat");
        const m = new MetricsController(host);
        m.setTotalCostUsd(9);
        expect(host.renderStats).not.toHaveBeenCalled();
    });

    it("paints the host CPU/mem bars and exposes hostStats()", () => {
        const host = makeHost("overview");
        const m = new MetricsController(host);
        m.setHostStats(37.25, 2048, 8192);
        expect(host.root.querySelector<HTMLElement>(".af-host-bar-fill-cpu")!.style.width).toBe("37.3%");
        expect(host.root.querySelector(".af-host-cpu-val")!.textContent).toBe("37.3%");
        expect(host.root.querySelector<HTMLElement>(".af-host-bar-fill-mem")!.style.width).toBe("25.0%");
        expect(host.root.querySelector(".af-host-mem-val")!.textContent).toBe("2.0 / 8.0 GB");
        expect(m.hostStats()).toEqual([37.25, 2048, 8192]);
    });

    it("shows raw MB for memory when no total is known", () => {
        const host = makeHost("overview");
        const m = new MetricsController(host);
        m.setHostStats(10, 512);
        expect(host.root.querySelector(".af-host-mem-val")!.textContent).toBe("512 MB");
    });

    it("polls /api/cost on start and stores the result", async () => {
        vi.useFakeTimers();
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ limit_usd: 5 }),
        })) as unknown as typeof fetch;
        const host = makeHost("overview");
        const m = new MetricsController(host);
        m.startPolling();
        await vi.waitFor(() => expect(m.costLimitInfo).toEqual({ limit_usd: 5 }));
        expect(fetch).toHaveBeenCalledWith("/api/cost");
        m.stopPolling();
        vi.useRealTimers();
    });

    it("builds a settings view element", () => {
        const m = new MetricsController(makeHost("settings"));
        expect(m.buildSettingsView()).toBeInstanceOf(HTMLElement);
    });
});

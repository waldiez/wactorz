/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Metrics concern of the dashboard: aggregate cost / message totals, the spend
 * limit (fetch / save / reset against `/api/cost*`) and the host-resource bar
 * painting. Owns this telemetry state and routes re-renders back through the host.
 */
import { buildSettingsView, type CostLimitInfo } from "./settings";
import type { View } from "./types";

export interface MetricsHost {
    readonly root: HTMLElement;
    /** The currently active view. */
    getView(): View;
    /** Re-render the overview stat cards (caller decides if visible). */
    renderStats(): void;
    /** Full re-render of the active view (used after settings mutations). */
    renderView(): void;
}

export class MetricsController {
    private _totalCostUsd: number | null = null;
    private _totalMessages: number | null = null;
    private _hostCpu: number | null = null;
    private _hostMemUsedMb: number | null = null;
    private _hostMemTotalMb: number | null = null;
    private _costLimitInfo: CostLimitInfo | null = null;
    private _pollTimer: ReturnType<typeof setInterval> | null = null;

    constructor(private host: MetricsHost) {}

    /** Aggregate spend in USD, or null until known. */
    get totalCostUsd(): number | null {
        return this._totalCostUsd;
    }

    /** Aggregate message count, or null until known. */
    get totalMessages(): number | null {
        return this._totalMessages;
    }

    /** Latest spend-limit info from `/api/cost`, or null until fetched. */
    get costLimitInfo(): CostLimitInfo | null {
        return this._costLimitInfo;
    }

    private get _ingress(): string {
        return window.__WACTORZ_INGRESS_PATH ?? "";
    }

    /** Set the aggregate cost and re-render the stat cards if on the overview. */
    setTotalCostUsd(usd: number): void {
        this._totalCostUsd = usd;
        if (this.host.getView() === "overview") {
            this.host.renderStats();
        }
    }

    /** Set the aggregate message count and re-render the stat cards if on the overview. */
    setTotalMessages(count: number): void {
        this._totalMessages = count;
        if (this.host.getView() === "overview") {
            this.host.renderStats();
        }
    }

    /** Store host CPU/memory stats and paint them into the host bar if mounted. */
    setHostStats(cpu: number, memUsedMb: number, memTotalMb?: number): void {
        this._hostCpu = cpu;
        this._hostMemUsedMb = memUsedMb;
        if (memTotalMb !== undefined) {
            this._hostMemTotalMb = memTotalMb;
        }
        const bar = this.host.root.querySelector<HTMLElement>("#af-host-bar");
        if (!bar) {
            return;
        }
        this._paintHostCpu(bar, cpu);
        this._paintHostMem(bar, memUsedMb);
    }

    /** Host-bar CPU/memory values for the overview build. */
    hostStats(): [number | null, number | null, number | null] {
        return [this._hostCpu, this._hostMemUsedMb, this._hostMemTotalMb];
    }

    /** Begin polling `/api/cost`; immediately fetches once. */
    startPolling(): void {
        void this._fetchCostInfo();
        this._pollTimer = setInterval(() => void this._fetchCostInfo(), 30_000);
    }

    /** Stop the cost-info poll loop. */
    stopPolling(): void {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    /** Build the settings view, wiring its save-limit / reset-spend actions. */
    buildSettingsView(): HTMLElement {
        return buildSettingsView(this._costLimitInfo, {
            onSaveLimit: async (limit, period) => {
                await this._saveCostLimit(limit, period);
                if (this.host.getView() === "settings") {
                    this.host.renderView();
                }
            },
            onResetSpend: async () => {
                await this._resetCost();
                if (this.host.getView() === "settings") {
                    this.host.renderView();
                }
            },
        });
    }

    private async _fetchCostInfo(): Promise<void> {
        try {
            const res = await fetch(`${this._ingress}/api/cost`);
            if (res.ok) {
                this._costLimitInfo = await res.json();
                if (this.host.getView() === "overview") {
                    this.host.renderStats();
                } else if (this.host.getView() === "settings") {
                    this.host.renderView();
                }
            }
        } catch {
            /* ignore — server may not be ready */
        }
    }

    private async _saveCostLimit(limit_usd: number, period: string): Promise<void> {
        await fetch(`${this._ingress}/api/cost/limit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limit_usd, period }),
        });
        await this._fetchCostInfo();
    }

    private async _resetCost(): Promise<void> {
        await fetch(`${this._ingress}/api/cost/reset`, { method: "POST" });
        await this._fetchCostInfo();
    }

    private _paintHostCpu(bar: HTMLElement, cpu: number): void {
        const cpuFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-cpu");
        const cpuVal = bar.querySelector<HTMLElement>(".af-host-cpu-val");
        if (cpuFill) {
            cpuFill.style.width = `${Math.max(0, Math.min(100, cpu)).toFixed(1)}%`;
        }
        if (cpuVal) {
            cpuVal.textContent = `${cpu.toFixed(1)}%`;
        }
    }

    private _paintHostMem(bar: HTMLElement, memUsedMb: number): void {
        const memFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-mem");
        const memVal = bar.querySelector<HTMLElement>(".af-host-mem-val");
        const total = this._hostMemTotalMb;
        const pct = total && total > 0 ? (memUsedMb / total) * 100 : 0;
        if (memFill) {
            memFill.style.width = `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`;
        }
        if (memVal) {
            if (total && total > 0) {
                memVal.textContent = `${(memUsedMb / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GB`;
            } else {
                memVal.textContent = `${memUsedMb.toFixed(0)} MB`;
            }
        }
    }
}

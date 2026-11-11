/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Metrics concern of the dashboard: aggregate cost / message totals, the spend
 * limit (fetch / save / reset against `/api/cost*`) and the host-resource bar
 * painting. Owns this telemetry state and routes re-renders back through the host.
 */
import { apiUrl } from "../../io/api";
import { readCostLimit, type CostLimitInfo } from "../../io/costApi";
import type { SettingsSection } from "../../types/settings";
import { buildBudgetSection, type BudgetSection } from "../settings/budget";
import { postOrWarn } from "../shell/mutate";
import { hostBarValues } from "./cards";
import type { View } from "../shell/types";

export interface MetricsHost {
    readonly root: HTMLElement;
    /** The currently active view. */
    getView(): View;
    /** Re-render the overview stat cards (caller decides if visible). */
    renderStats(): void;
    /** The limit was saved or the spend reset: draw Settings → Budget again, if it is open. */
    budgetChanged(): void;
}

export class MetricsController {
    private _totalCostUsd: number | null = null;
    private _totalMessages: number | null = null;
    private _hostCpu: number | null = null;
    private _hostMemUsedMb: number | null = null;
    private _hostMemTotalMb: number | null = null;
    private _costLimitInfo: CostLimitInfo | null = null;
    private _pollTimer: ReturnType<typeof setInterval> | null = null;
    /** Settings → Budget while it is drawn, to show each spend read on it. */
    private _budget: BudgetSection | null = null;

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
        this._paintHostBar(bar, cpu, memUsedMb);
    }

    /** Host-bar CPU/memory values for the overview build. */
    hostStats(): [number | null, number | null, number | null] {
        return [this._hostCpu, this._hostMemUsedMb, this._hostMemTotalMb];
    }

    /** Begin polling `/api/cost`; immediately fetches once. */
    startPolling(): void {
        void this._fetchCostInfo();
        this._pollTimer = setInterval(() => {
            if (!document.hidden) {
                void this._fetchCostInfo();
            }
        }, 30_000);
    }

    /** Stop the cost-info poll loop. */
    stopPolling(): void {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    /** Settings → Budget, drawn from the limit last read, its save and reset wired to the server. */
    budgetSection(): SettingsSection {
        return {
            key: "budget",
            label: "Budget",
            line: "The language-model spend limit",
            scope: "server",
            icon: "wallet",
            build: () => this._buildBudget(),
            release: () => {
                this._budget = null;
            },
        };
    }

    private _buildBudget(): HTMLElement {
        this._budget = buildBudgetSection(this._costLimitInfo, {
            onSaveLimit: async (limit, period) => {
                await this._saveCostLimit(limit, period);
                this.host.budgetChanged();
            },
            onResetSpend: async () => {
                await this._resetCost();
                this.host.budgetChanged();
            },
        });
        return this._budget.body;
    }

    private async _fetchCostInfo(): Promise<void> {
        const info = await readCostLimit();
        if (!info) {
            return;
        }
        this._costLimitInfo = info;
        if (this.host.getView() === "overview") {
            this.host.renderStats();
        }
        // Only the spend: the fields are left as they are, someone may be typing in one.
        this._budget?.showSpend(info);
    }

    /** POST a spend change, then refetch either way — on success to pick up the
     *  new value, on failure to show what the server still holds. */
    private async _mutateCost(what: string, path: string, init: RequestInit): Promise<void> {
        await postOrWarn(apiUrl(path), init, what);
        await this._fetchCostInfo();
    }

    private async _saveCostLimit(limit_usd: number, period: string): Promise<void> {
        await this._mutateCost("save the limit", "/api/cost/limit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limit_usd, period }),
        });
    }

    private async _resetCost(): Promise<void> {
        await this._mutateCost("reset spend", "/api/cost/reset", { method: "POST" });
    }

    /** Patch the host bar's CPU/mem fill + text in place, via the same
     *  clamp/format logic `buildHostBar` uses for the initial render. */
    private _paintHostBar(bar: HTMLElement, cpu: number, memUsedMb: number): void {
        const { cpuPct, cpuText, memPct, memText } = hostBarValues(cpu, memUsedMb, this._hostMemTotalMb);
        const cpuFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-cpu");
        const cpuVal = bar.querySelector<HTMLElement>(".af-host-cpu-val");
        if (cpuFill) {
            cpuFill.style.width = `${cpuPct.toFixed(1)}%`;
        }
        if (cpuVal) {
            cpuVal.textContent = cpuText;
        }
        const memFill = bar.querySelector<HTMLElement>(".af-host-bar-fill-mem");
        const memVal = bar.querySelector<HTMLElement>(".af-host-mem-val");
        if (memFill) {
            memFill.style.width = `${memPct.toFixed(1)}%`;
        }
        if (memVal) {
            memVal.textContent = memText;
        }
    }
}

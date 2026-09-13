/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Settings view: the LLM spend-limit panel. The panel delegates save/reset to
 * the dashboard via callbacks so it can hit the backend and re-render.
 */

import { buildVoiceSection } from "./voiceSettings";

export interface CostLimitInfo {
    limit_usd?: number;
    period?: string;
    spend_usd?: number;
    /** Server-computed progress fields (present once a limit is configured). */
    pct_used?: number;
    warning?: boolean;
    limit_reached?: boolean;
}

export interface CostLimitCallbacks {
    onSaveLimit: (limitUsd: number, period: string) => Promise<void> | void;
    onResetSpend: () => Promise<void> | void;
}

function labelledField(labelText: string, control: HTMLElement): HTMLLabelElement {
    const lbl = document.createElement("label");
    lbl.className = "af-settings-field";
    const span = document.createElement("span");
    span.className = "af-settings-label";
    span.textContent = labelText;
    lbl.append(span, control);
    return lbl;
}

function buildLimitInput(currentLimit: number): HTMLInputElement {
    const input = document.createElement("input");
    input.type = "number";
    input.id = "af-cost-limit";
    input.name = "cost-limit";
    input.setAttribute("aria-label", "Spend limit (USD)");
    input.min = "0";
    input.step = "0.01";
    input.className = "af-cfg-input";
    input.placeholder = "0.00";
    input.value = currentLimit ? String(currentLimit) : "";
    return input;
}

function buildPeriodSelect(currentPeriod: string): HTMLSelectElement {
    const select = document.createElement("select");
    select.className = "af-cfg-input";
    select.id = "af-cost-period";
    select.name = "cost-period";
    select.setAttribute("aria-label", "Cost limit period");
    ["daily", "weekly", "monthly"].forEach(p => {
        const opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p.charAt(0).toUpperCase() + p.slice(1);
        if (p === currentPeriod) {
            opt.selected = true;
        }
        select.appendChild(opt);
    });
    return select;
}

function buildCostStatus(info: CostLimitInfo | null, currentLimit: number, period: string): HTMLElement {
    const status = document.createElement("p");
    status.className = "af-settings-note";
    const spend = info?.spend_usd ?? 0;
    const periodLabel = period === "daily" ? "today" : period === "weekly" ? "this week" : "this month";
    status.textContent =
        currentLimit > 0
            ? `Current spend: $${spend.toFixed(4)} / $${Number(currentLimit).toFixed(2)} ${periodLabel}`
            : `Current spend: $${spend.toFixed(4)} ${periodLabel} (no limit set)`;
    return status;
}

/** Run an async button action while disabling the button. */
function withBusy(btn: HTMLButtonElement, run: () => Promise<void> | void): void {
    btn.addEventListener("click", () => {
        btn.disabled = true;
        void Promise.resolve(run()).finally(() => {
            btn.disabled = false;
        });
    });
}

function buildCostActions(
    limitInput: HTMLInputElement,
    periodSelect: HTMLSelectElement,
    cb: CostLimitCallbacks,
): HTMLElement {
    const actions = document.createElement("div");
    actions.className = "af-settings-actions";

    const saveBtn = document.createElement("button");
    saveBtn.className = "af-mini-btn";
    saveBtn.textContent = "Save limit";
    withBusy(saveBtn, async () => {
        const v = parseFloat(limitInput.value || "0");
        if (isNaN(v) || v < 0) {
            return;
        }
        await cb.onSaveLimit(v, periodSelect.value);
    });
    actions.appendChild(saveBtn);

    const resetBtn = document.createElement("button");
    resetBtn.className = "af-mini-btn danger";
    resetBtn.textContent = "Reset spend";
    resetBtn.title =
        "Clears the period budget counter only. The lifetime " +
        "“Cost” total is separate and is not affected (use wactorz-reset --metrics for that).";
    withBusy(resetBtn, async () => {
        if (
            !window.confirm(
                "Reset the period budget counter?\n\n" +
                    "This only zeroes spend for the current period. The lifetime " +
                    "“Cost” total stays unchanged.",
            )
        ) {
            return;
        }
        await cb.onResetSpend();
    });
    actions.appendChild(resetBtn);

    return actions;
}

/** The  LLM Spend Limit panel. */
function buildCostLimitSection(info: CostLimitInfo | null, cb: CostLimitCallbacks): HTMLElement {
    const section = document.createElement("div");
    section.className = "af-settings-section";

    const h = document.createElement("h3");
    h.className = "af-settings-section-heading";
    h.textContent = "🪙 LLM Spend Limit";
    section.appendChild(h);

    const currentLimit = info?.limit_usd ?? 0;
    const currentPeriod = info?.period ?? "monthly";

    const grid = document.createElement("div");
    grid.className = "af-settings-grid";
    const limitInput = buildLimitInput(currentLimit);
    const periodSelect = buildPeriodSelect(currentPeriod);
    grid.appendChild(labelledField("Limit (USD, 0 to disable)", limitInput));
    grid.appendChild(labelledField("Period", periodSelect));
    section.appendChild(grid);

    section.appendChild(buildCostStatus(info, currentLimit, currentPeriod));
    section.appendChild(buildCostActions(limitInput, periodSelect, cb));
    return section;
}

/** Assemble the settings view. */
export function buildSettingsView(
    info: CostLimitInfo | null,
    cb: CostLimitCallbacks,
    apiBase = "",
    onVoiceChanged: () => void = () => {},
): HTMLElement {
    const el = document.createElement("div");
    el.className = "af-settings";

    const title = document.createElement("h2");
    title.className = "af-settings-title";
    title.textContent = "Settings";
    el.appendChild(title);

    el.appendChild(buildCostLimitSection(info, cb));
    // Absent entirely when this deployment neither listens nor speaks.
    const voice = buildVoiceSection(apiBase, onVoiceChanged);
    if (voice) {
        el.appendChild(voice);
    }
    // No Home Assistant fields: the HA URL comes from /api/config and the Devices
    // nav button links straight to HA — the browser never holds a token.
    return el;
}

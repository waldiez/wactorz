/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings → Budget: the language-model spend limit. The section hands save and
 * reset to its owner through callbacks, which reach the server and draw it again.
 *
 * What was spent is read again every little while; that changes only the
 * spend shown (`showSpend`), never the fields, so someone typing a new limit
 * keeps what they typed and where they were.
 */

import type { CostLimitInfo } from "../../io/costApi";
import { button, el, named, option } from "../dom";
import { confirmDialog } from "../shell/confirmDialog";
import { settingGroup, settingRow } from "./layout";

/** What the Budget section asks its owner to do. */
export interface CostLimitCallbacks {
    /** Save `limitUsd` for `period` on the server. */
    onSaveLimit: (limitUsd: number, period: string) => Promise<void> | void;
    /** Zero the current period's spend counter on the server. */
    onResetSpend: () => Promise<void> | void;
}

/** The Budget section as drawn: its content, and the way to show a spend read since. */
export interface BudgetSection {
    /** The section's content. */
    body: HTMLElement;
    /** Show what `info` says was spent, leaving the fields as they are. */
    showSpend(info: CostLimitInfo | null): void;
}

/** The periods a limit can run over, as the server names them. */
const PERIODS = ["daily", "weekly", "monthly"] as const;

/** The period `info` names, or monthly, the server's own, for one it does not: the field and the line then agree. */
function periodOf(info: CostLimitInfo | null): (typeof PERIODS)[number] {
    return PERIODS.find(p => p === info?.period) ?? "monthly";
}

/** How the spend line names the period it covers. */
function periodWord(period: string): string {
    return period === "daily" ? "today" : period === "weekly" ? "this week" : "this month";
}

function buildLimitInput(currentLimit: number): HTMLInputElement {
    const input = named(el("input", "af-cfg-input"), "cost-limit", "Spend limit (USD)", "af-cost-limit");
    input.type = "number";
    input.min = "0";
    input.step = "0.01";
    input.placeholder = "0.00";
    input.inputMode = "decimal";
    input.value = currentLimit ? String(currentLimit) : "";
    return input;
}

function buildPeriodSelect(currentPeriod: string): HTMLSelectElement {
    const select = named(el("select", "af-cfg-input"), "cost-period", "Cost limit period", "af-cost-period");
    PERIODS.forEach(p => select.appendChild(option(p, p.charAt(0).toUpperCase() + p.slice(1))));
    // Chosen once the options are in: set on an option before it joins, some engines pick another.
    select.value = currentPeriod;
    return select;
}

/** What `info` says was spent, against its limit when there is one. */
export function spendText(info: CostLimitInfo | null): string {
    const spend = info?.spend_usd ?? 0;
    const limit = info?.limit_usd ?? 0;
    const when = periodWord(periodOf(info));
    return limit > 0
        ? `$${spend.toFixed(4)} of $${Number(limit).toFixed(2)} spent ${when}`
        : `$${spend.toFixed(4)} spent ${when}, with no limit set`;
}

/** The share of the limit spent, from 0 to 1; `null` with no limit. */
export function spentShare(info: CostLimitInfo | null): number | null {
    const limit = info?.limit_usd ?? 0;
    if (limit <= 0) {
        return null;
    }
    return Math.min(1, Math.max(0, (info?.spend_usd ?? 0) / limit));
}

/** The spend line and its bar, and the way to show another reading on them. */
function buildSpend(info: CostLimitInfo | null): {
    node: HTMLElement;
    show: (info: CostLimitInfo | null) => void;
} {
    const node = el("div", "af-budget-spend");
    const line = el("p", "af-budget-spend-line");
    const bar = el("div", "af-budget-bar");
    bar.setAttribute("role", "meter");
    bar.setAttribute("aria-label", "Share of the limit spent");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    const fill = el("div", "af-budget-bar-fill");
    bar.appendChild(fill);
    node.append(line, bar);

    const show = (now: CostLimitInfo | null): void => {
        line.textContent = spendText(now);
        const share = spentShare(now);
        bar.hidden = share === null;
        const percent = Math.round((share ?? 0) * 100);
        bar.setAttribute("aria-valuenow", String(percent));
        bar.setAttribute("aria-valuetext", `${percent}%`);
        fill.style.width = `${percent}%`;
        // Amber from four fifths of the limit, red at it: the bar warns before the limit stops anything.
        bar.classList.toggle("near", percent >= 80 && percent < 100);
        bar.classList.toggle("over", percent >= 100);
    };
    show(info);
    return { node, show };
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
    const actions = el("div", "af-settings-actions");

    const saveBtn = button("af-mini-btn primary", "Save limit");
    withBusy(saveBtn, async () => {
        const v = parseFloat(limitInput.value || "0");
        if (isNaN(v) || v < 0) {
            return;
        }
        await cb.onSaveLimit(v, periodSelect.value);
    });
    actions.appendChild(saveBtn);

    const resetBtn = button("af-mini-btn danger", "Reset spend");
    resetBtn.title =
        "Clears the period budget counter only. The lifetime " +
        "“Cost” total is separate and is not affected (use wactorz-reset --metrics for that).";
    withBusy(resetBtn, async () => {
        const confirmed = await confirmDialog({
            title: "Reset the period budget counter?",
            message:
                "This only zeroes spend for the current period. The lifetime “Cost” total stays unchanged.",
            confirmLabel: "Reset spend",
        });
        if (confirmed) {
            await cb.onResetSpend();
        }
    });
    actions.appendChild(resetBtn);

    return actions;
}

/** The Budget section's content: the limit, its period, what was spent, and the actions. */
export function buildBudgetSection(info: CostLimitInfo | null, cb: CostLimitCallbacks): BudgetSection {
    const body = el("div", "af-settings-body");

    const limitInput = buildLimitInput(info?.limit_usd ?? 0);
    const periodSelect = buildPeriodSelect(periodOf(info));
    const spend = buildSpend(info);
    body.append(
        settingGroup(
            "Spend limit",
            settingRow("Limit", "In US dollars; 0 for no limit.", limitInput),
            settingRow("Period", "When the spend starts again from zero.", periodSelect),
        ),
        spend.node,
        buildCostActions(limitInput, periodSelect, cb),
    );
    return { body, showSpend: spend.show };
}

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The recipient `<select>` in the chat composer: options for every messageable
 * agent, pinned ones first. Rendering only — it shows the chat target and never
 * changes it (see `DashboardChat.setTarget`, the sole owner of that).
 */
import type { AgentInfo } from "../../types/agent";
import { canDirectMessage, MESSAGEABLE_PRIORITY } from "./agentState";

/** Pinned agents in their fixed order, then everyone else alphabetically. */
function byPriorityThenName(a: AgentInfo, b: AgentInfo): number {
    const ai = MESSAGEABLE_PRIORITY.indexOf(a.name);
    const bi = MESSAGEABLE_PRIORITY.indexOf(b.name);
    if (ai !== -1 && bi !== -1) {
        return ai - bi;
    }
    if (ai !== -1) {
        return -1;
    }
    if (bi !== -1) {
        return 1;
    }
    return a.name.localeCompare(b.name);
}

/**
 * Draw the options and show `target`.
 *
 * ⚠ Render only. This used to re-derive the chat target and fall back to the
 * first option, so drawing the dropdown decided who the next message went to —
 * and it runs on every agent-list change, including each individual removal
 * during a reset. Once the chosen agent was momentarily absent the target moved
 * to `main`, while the pane header and thread went on naming the agent the user
 * picked. The select falls back so the control is never blank; the target itself
 * is the user's, and only a user action changes it.
 */
export function renderTargetSelect(select: HTMLSelectElement, agents: AgentInfo[], target: string): void {
    select.innerHTML = "";
    agents
        .filter(canDirectMessage)
        .sort(byPriorityThenName)
        .forEach(agent => {
            const opt = document.createElement("option");
            opt.value = agent.name;
            opt.textContent = `@${agent.name}`;
            select.appendChild(opt);
        });
    const hasTarget = [...select.options].some(o => o.value === target);
    select.value = hasTarget ? target : (select.options[0]?.value ?? "");
}

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat view's skeleton: the sidebar shell, the pane shell, and the pane
 * header. Structure and appearance only — what goes *into* the agent list and
 * the thread is rendered by `chatSidebar` and `chatThread`, and every decision
 * about who the conversation is with belongs to `DashboardChat`.
 */
import type { AgentInfo } from "../../types/agent";
import { stateColor, stateLabel } from "./agentState";
import { button, el, named } from "../dom";

/** The sidebar shell: a filter box above the (separately rendered) agent list. */
export function buildChatSidebar(filter: string, onFilter: (value: string) => void): HTMLElement {
    const sidebar = el("div", "af-chat-sidebar");

    const searchWrap = el("div", "af-chat-sidebar-search");
    // Keep the default text type — `type="search"` adds browser chrome (a
    // clear button / WebKit rounding) that would change this field's look.
    const searchInput = named(el("input"), "agent-filter", "Filter agents", "af-agent-filter");
    searchInput.placeholder = "Filter agents…";
    searchInput.value = filter;
    searchInput.addEventListener("input", () => onFilter(searchInput.value.toLowerCase()));
    searchWrap.appendChild(searchInput);
    sidebar.appendChild(searchWrap);

    const agentList = el("div", "af-chat-agent-list");
    agentList.id = "af-chat-agent-list";
    sidebar.appendChild(agentList);
    return sidebar;
}

/** The pane shell: a header and an empty thread, both filled in by renders. */
export function buildChatPane(): HTMLElement {
    const pane = el("div", "af-chat-pane");

    const paneHdr = el("div", "af-chat-pane-header");
    paneHdr.id = "af-chat-pane-header";

    const thread = el("div", "af-chat-thread");
    thread.id = "af-chat-thread";

    pane.append(paneHdr, thread);
    return pane;
}

/**
 * Draw the pane header: Back, a state dot, the target's name, its state label.
 *
 * `agent` is undefined when the target is not in the list — the name is still
 * shown, because it is who the user chose and the composer will say the same.
 */
export function renderPaneHeader(
    hdr: HTMLElement,
    target: string,
    agent: AgentInfo | undefined,
    onBack: () => void,
): void {
    hdr.innerHTML = "";

    const backBtn = button("af-chat-back-btn", "‹ Back");
    backBtn.addEventListener("click", onBack);
    hdr.appendChild(backBtn);

    if (agent) {
        const dot = el("span", "af-chat-agent-dot");
        dot.style.background = stateColor(agent.state);
        hdr.appendChild(dot);
    }
    hdr.appendChild(el("span", "af-chat-pane-title", `@${target}`));
    if (agent) {
        hdr.appendChild(el("span", "af-chat-pane-state", stateLabel(agent.state)));
    }
}

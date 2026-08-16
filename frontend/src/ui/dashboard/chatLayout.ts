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

/** The sidebar shell: a filter box above the (separately rendered) agent list. */
export function buildChatSidebar(filter: string, onFilter: (value: string) => void): HTMLElement {
    const sidebar = document.createElement("div");
    sidebar.className = "af-chat-sidebar";

    const searchWrap = document.createElement("div");
    searchWrap.className = "af-chat-sidebar-search";
    const searchInput = document.createElement("input");
    // Keep the default text type — `type="search"` adds browser chrome (a
    // clear button / WebKit rounding) that would change this field's look.
    searchInput.id = "af-agent-filter";
    searchInput.name = "agent-filter";
    searchInput.placeholder = "Filter agents…";
    searchInput.setAttribute("aria-label", "Filter agents");
    searchInput.value = filter;
    searchInput.addEventListener("input", () => onFilter(searchInput.value.toLowerCase()));
    searchWrap.appendChild(searchInput);
    sidebar.appendChild(searchWrap);

    const agentList = document.createElement("div");
    agentList.className = "af-chat-agent-list";
    agentList.id = "af-chat-agent-list";
    sidebar.appendChild(agentList);
    return sidebar;
}

/** The pane shell: a header and an empty thread, both filled in by renders. */
export function buildChatPane(): HTMLElement {
    const pane = document.createElement("div");
    pane.className = "af-chat-pane";

    const paneHdr = document.createElement("div");
    paneHdr.className = "af-chat-pane-header";
    paneHdr.id = "af-chat-pane-header";

    const thread = document.createElement("div");
    thread.className = "af-chat-thread";
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

    const backBtn = document.createElement("button");
    backBtn.className = "af-chat-back-btn";
    backBtn.textContent = "‹ Back";
    backBtn.addEventListener("click", onBack);
    hdr.appendChild(backBtn);

    if (agent) {
        const dot = document.createElement("span");
        dot.className = "af-chat-agent-dot";
        dot.style.background = stateColor(agent.state);
        hdr.appendChild(dot);
    }
    const title = document.createElement("span");
    title.className = "af-chat-pane-title";
    title.textContent = `@${target}`;
    hdr.appendChild(title);
    if (agent) {
        const st = document.createElement("span");
        st.className = "af-chat-pane-state";
        st.textContent = stateLabel(agent.state);
        hdr.appendChild(st);
    }
}

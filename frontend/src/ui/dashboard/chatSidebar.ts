/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Chat sidebar agent list: diff-renders one row per agent, highlighting the
 * active target and disabling unreachable (system) agents. Selecting a row is
 * delegated to the caller via `onSelect`.
 */
import type { AgentInfo } from "../../types/agent";
import { canDirectMessage, stateColor } from "./agentState";

/** Create a sidebar row button that selects its agent on click. */
function createRow(name: string, onSelect: (name: string) => void): HTMLButtonElement {
    const row = document.createElement("button");
    row.dataset["name"] = name;
    const dot = document.createElement("span");
    dot.className = "af-chat-agent-dot";
    const nm = document.createElement("span");
    nm.className = "af-chat-agent-name";
    nm.textContent = name;
    const lock = document.createElement("span");
    lock.className = "af-chat-agent-lock";
    lock.setAttribute("aria-hidden", "true");
    row.append(dot, nm, lock);
    row.addEventListener("click", () => onSelect(name));
    return row;
}

/** Patch the mutable bits (active/disabled state, dot colour, lock) of a row. */
function patchRow(row: HTMLElement, agent: AgentInfo, chatTarget: string): void {
    const isActive = agent.name === chatTarget;
    const isDisabled = !canDirectMessage(agent);

    const cls = ["af-chat-agent-row"];
    if (isActive) {
        cls.push("active");
    }
    if (isDisabled) {
        cls.push("protected-agent");
    }
    row.className = cls.join(" ");
    (row as HTMLButtonElement).disabled = isDisabled;
    row.title = isDisabled ? `${agent.name} — infrastructure agent, not directly reachable` : agent.name;

    const color = stateColor(agent.state);
    const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
    if (dot && dot.style.background !== color) {
        dot.style.background = color;
    }
    const lock = row.querySelector<HTMLElement>(".af-chat-agent-lock");
    if (lock) {
        lock.textContent = isDisabled ? "🔒" : "";
    }
}

/** Diff-render the sidebar rows for `sorted` agents into `list`. */
export function renderChatSidebar(
    list: HTMLElement,
    sorted: AgentInfo[],
    chatTarget: string,
    onSelect: (name: string) => void,
): void {
    const existing = new Map<string, HTMLElement>();
    list.querySelectorAll<HTMLElement>(".af-chat-agent-row").forEach(r => {
        if (r.dataset["name"]) {
            existing.set(r.dataset["name"], r);
        }
    });
    const keep = new Set(sorted.map(a => a.name));
    existing.forEach((row, name) => {
        if (!keep.has(name)) {
            row.remove();
        }
    });

    sorted.forEach((agent, idx) => {
        const row = existing.get(agent.name) ?? createRow(agent.name, onSelect);
        patchRow(row, agent, chatTarget);
        const sibling = list.children[idx];
        if (sibling !== row) {
            list.insertBefore(row, sibling ?? null);
        }
    });
}

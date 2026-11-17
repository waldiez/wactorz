/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Chat sidebar agent list: diff-renders one row per agent, highlighting the
 * active target and disabling unreachable (system) agents, except one asking
 * the user something, whose thread opens so its question can be answered.
 * Rows are kept by agent id, since two agents may share a name, and named as
 * `label` names them. Selecting a row is delegated to the caller via `onSelect`.
 */
import type { AgentInfo } from "../../types/agent";
import { canDirectMessage, canOpenThread, stateColor } from "../overview/agentState";
import { avatar, moodOf, paintAvatar } from "../avatars";
import { button, el } from "../dom";

/**
 * Create a sidebar row button that selects `agent` on click. Its avatar and
 * its dot share a wrapper that takes no part in the layout unless the chat
 * shows avatars, where the dot then sits on the avatar's corner.
 */
function createRow(agent: AgentInfo, onSelect: (id: string) => void): HTMLButtonElement {
    const id = agent.id;
    const row = button();
    row.dataset["id"] = id;
    const lock = el("span", "af-chat-agent-lock");
    lock.setAttribute("aria-hidden", "true");
    const face = el("span", "af-chat-agent-face");
    face.append(avatar(agent, moodOf(agent.state), "small"), el("span", "af-chat-agent-dot"));
    row.append(face, el("span", "af-chat-agent-name"), lock);
    row.addEventListener("click", () => onSelect(id));
    return row;
}

/** Patch the mutable bits (name, active/disabled state, dot colour, lock) of a row. */
function patchRow(
    row: HTMLElement,
    agent: AgentInfo,
    name: string,
    chatTarget: string,
    asking: ReadonlySet<string>,
): void {
    const isActive = agent.id === chatTarget;
    const isDisabled = !canOpenThread(agent, asking);
    const isLocked = !canDirectMessage(agent);

    const cls = ["af-chat-agent-row"];
    if (isActive) {
        cls.push("active");
    }
    if (isDisabled) {
        cls.push("protected-agent");
    }
    row.className = cls.join(" ");
    (row as HTMLButtonElement).disabled = isDisabled;
    row.title = isDisabled
        ? `${name} — infrastructure agent, not directly reachable`
        : isLocked
          ? `${name} — asking you something; it cannot be messaged`
          : name;

    const color = stateColor(agent.state);
    const dot = row.querySelector<HTMLElement>(".af-chat-agent-dot");
    if (dot && dot.style.background !== color) {
        dot.style.background = color;
    }
    const lock = row.querySelector<HTMLElement>(".af-chat-agent-lock");
    if (lock) {
        lock.textContent = isLocked ? "🔒" : "";
    }
}

/** Show on `row` its agent's avatar as the agent is now. */
function faceRow(row: HTMLElement, agent: AgentInfo): void {
    const face = row.querySelector<HTMLElement>(".af-avatar");
    if (face) {
        paintAvatar(face, agent, moodOf(agent.state));
    }
}

/** Show `name` on `row`: an agent's label changes once another takes its name. */
function nameRow(row: HTMLElement, name: string): void {
    const shown = row.querySelector<HTMLElement>(".af-chat-agent-name");
    if (shown && shown.textContent !== name) {
        shown.textContent = name;
    }
}

/** Diff-render the sidebar rows for `sorted` agents into `list`. `chatTarget` and
 *  `asking` are agent ids; `label` is what a row calls its agent. */
export function renderChatSidebar(
    list: HTMLElement,
    sorted: AgentInfo[],
    chatTarget: string,
    onSelect: (id: string) => void,
    asking: ReadonlySet<string> = new Set(),
    label: (agent: AgentInfo) => string = agent => agent.name,
): void {
    const existing = new Map<string, HTMLElement>();
    list.querySelectorAll<HTMLElement>(".af-chat-agent-row").forEach(r => {
        if (r.dataset["id"]) {
            existing.set(r.dataset["id"], r);
        }
    });
    const keep = new Set(sorted.map(a => a.id));
    existing.forEach((row, id) => {
        if (!keep.has(id)) {
            row.remove();
        }
    });

    sorted.forEach((agent, idx) => {
        const row = existing.get(agent.id) ?? createRow(agent, onSelect);
        const name = label(agent);
        patchRow(row, agent, name, chatTarget, asking);
        nameRow(row, name);
        faceRow(row, agent);
        const sibling = list.children[idx];
        if (sibling !== row) {
            list.insertBefore(row, sibling ?? null);
        }
    });
}

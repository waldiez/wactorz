/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Whom a reset is for: the agent whose conversation is open, or every agent.
 *
 * The reset popover sits in the header, so it learns the open agent from the
 * route on the bus rather than from the chat. One agent is the default when
 * there is one: a reset meant for the agent in front of you should not have to
 * be narrowed down first, and one meant for all of them is a deliberate pick.
 */

import type { Route } from "../../router";
import { el } from "../dom";

/** The scopes that can be limited to one agent. Log files are shared by every
 *  agent, and a factory reset for one agent is deleting it. */
export const AGENT_SCOPES: ReadonlySet<string> = new Set(["chat", "metrics", "spawns", "state"]);

/** The agent whose conversation `route` opens, or null when it opens none. */
export function openAgent(route: Route | null | undefined): string | null {
    return route?.view === "chat" && route.path[0] ? route.path[0] : null;
}

/** A radio pair choosing between `agent` and every agent; one agent is checked. */
export function buildScopeSwitch(agent: string, onChange: (forAgent: boolean) => void): HTMLFieldSetElement {
    const group = el("fieldset", "af-reset-scope");
    group.style.cssText = "border:0;margin:0 0 10px;padding:0;display:flex;gap:12px;font-size:12px;";
    const legend = el("legend", "", "Clear for");
    legend.style.cssText = "font-size:10px;opacity:.6;margin-bottom:4px;padding:0;";
    group.appendChild(legend);
    for (const [value, text] of [
        ["agent", `@${agent}`],
        ["all", "All agents"],
    ] as const) {
        const label = el("label");
        label.style.cssText = "display:flex;align-items:center;gap:4px;cursor:pointer;";
        const input = el("input");
        input.type = "radio";
        input.name = "af-reset-scope";
        input.value = value;
        input.checked = value === "agent";
        input.addEventListener("change", () => onChange(value === "agent"));
        label.append(input, document.createTextNode(text));
        group.appendChild(label);
    }
    return group;
}

/** How each answer from a node reads, when it is not that the node cleared it. */
const NOT_CLEARED: Record<string, string> = {
    "no answer": "did not answer",
    offline: "is offline",
    "older release": "runs an older release; deploy it again",
};

/**
 * What to add to the toast about the nodes, or "" when every one cleared its copy.
 *
 * Named rather than counted: the person reading it is about to go and look at
 * those nodes.
 */
export function nodesSay(nodes: Record<string, string> | undefined): string {
    const missed = Object.entries(nodes ?? {}).filter(([, state]) => state !== "cleared");
    if (!missed.length) {
        return "";
    }
    const lines = missed.map(([node, state]) => `${node} ${NOT_CLEARED[state] ?? state}`);
    return ` Nodes not cleared: ${lines.join("; ")}.`;
}

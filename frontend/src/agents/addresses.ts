/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Agent addresses on the page: `name@node`, spelled as the server spells them
 * (`wactorz/core/addresses.py`).
 *
 * An agent's name is unique on the host it runs on, and main is the node
 * `main`. A bare name is an address while one agent has it; when more than one
 * does, nothing here picks one of them: the caller gets them all and says so.
 */
import { MAIN_NODE } from "./naming";

/** Separates an agent's name from its node in an address. */
export const SEPARATOR = "@";

/** Node names that mean main itself, as the server takes them. */
const MAIN_NODE_NAMES = new Set(["", "local", MAIN_NODE]);

/** What an address is made from: an agent's name and the node it runs on ("" for main). */
export interface Addressable {
    /** The agent's name. */
    name: string;
    /** The node it runs on; empty or missing for main. */
    node?: string | undefined;
}

/** The node `node` names: `main` for every name that means main. */
export function nodeOf(node: string | undefined): string {
    const trimmed = (node ?? "").trim();
    return MAIN_NODE_NAMES.has(trimmed.toLowerCase()) ? MAIN_NODE : trimmed;
}

/** `agent`'s full address, `name@node`. */
export function addressOf(agent: Addressable): string {
    return `${agent.name}${SEPARATOR}${nodeOf(agent.node)}`;
}

/** What to call `agent` among `agents`: its name while no other has it, else its address. */
export function labelAmong(agent: Addressable, agents: Iterable<Addressable>): string {
    let named = 0;
    for (const other of agents) {
        if (other.name === agent.name) {
            named += 1;
        }
    }
    return named < 2 ? agent.name : addressOf(agent);
}

/**
 * The agents of `agents` that `wanted` names, in their order.
 *
 * An agent whose whole name is `wanted` comes first, as on the server. Otherwise
 * a bare name finds every agent of that name, on whichever node, and an address
 * the one on its node. One agent found is the answer; several are a choice the
 * caller puts to the person.
 */
export function resolveAmong<T extends Addressable>(wanted: string, agents: Iterable<T>): T[] {
    const all = [...agents];
    const text = wanted.trim();
    const whole = all.filter(a => a.name === text);
    if (whole.length || !text.includes(SEPARATOR)) {
        return whole;
    }
    const at = text.indexOf(SEPARATOR);
    const name = text.slice(0, at).trim();
    const node = text.slice(at + 1).trim();
    return all.filter(a => a.name === name && (!node || nodeOf(a.node) === nodeOf(node)));
}

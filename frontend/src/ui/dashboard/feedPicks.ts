/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Narrowing the activity feed to one chat turn or one agent.
 *
 * A row names the turn it belongs to and the agent at work, and either can be
 * clicked to show only its rows; the toolbar then shows what the feed is
 * narrowed to, each with a way back. The turn is the id the server gives each
 * message a person sends, and it follows the work done to answer it.
 */
import { button } from "../dom";
import type { FeedFilters } from "./feedView";

/** An agent's name as a button that narrows the feed to that agent. */
export function agentPicker(name: string, cls: string): HTMLButtonElement {
    const pick = button(`${cls} af-feed-pick`, name);
    pick.dataset["setAgent"] = name;
    pick.title = `Show only ${name}`;
    return pick;
}

/** How many characters of a turn id a row shows; the whole id is in its title. */
const TURN_CHARS = 6;

/** A turn id as a chip that narrows the feed to that turn. */
export function turnPicker(turn: string): HTMLButtonElement {
    const pick = button("af-feed-turn af-feed-pick", turn.slice(0, TURN_CHARS));
    pick.dataset["setTurn"] = turn;
    pick.title = `Turn ${turn}: show only what was done to answer it`;
    return pick;
}

/** Record which turn and agent a row belongs to, for the filters to read. */
export function tagRow(row: HTMLElement, turn: string | undefined, agent: string | undefined): void {
    if (turn) {
        row.dataset["turn"] = turn;
    }
    if (agent) {
        row.dataset["agent"] = agent;
    }
}

/** Narrow the feed to a turn or an agent, or widen it again with "". */
export type Narrow = (patch: Partial<Pick<FeedFilters, "turn" | "agent">>) => void;

/** The turn and agent the feed is narrowed to, each a chip whose × widens it again. */
export function paintActive(active: HTMLElement, filters: FeedFilters, narrow: Narrow): void {
    const chips: HTMLElement[] = [];
    if (filters.turn) {
        chips.push(activeChip(`turn ${filters.turn}`, () => narrow({ turn: "" })));
    }
    if (filters.agent) {
        chips.push(activeChip(`agent ${filters.agent}`, () => narrow({ agent: "" })));
    }
    active.replaceChildren(...chips);
}

function activeChip(label: string, clear: () => void): HTMLButtonElement {
    const chip = button("af-mini-btn af-feed-active-chip active", `${label} ×`);
    chip.title = `Stop showing only ${label}`;
    chip.addEventListener("click", clear);
    return chip;
}

/**
 * Narrow the feed when a row's turn chip or agent name is clicked.
 *
 * Caught on the way down, before the row sees the click: a row opens in place
 * when clicked, and picking a filter from it should not also open it.
 */
export function listenForPicks(feed: HTMLElement, narrow: Narrow): void {
    feed.addEventListener(
        "click",
        e => {
            const pick = (e.target as HTMLElement | null)?.closest<HTMLElement>(".af-feed-pick");
            if (!pick) {
                return;
            }
            e.stopPropagation();
            const turn = pick.dataset["setTurn"];
            const agent = pick.dataset["setAgent"];
            if (turn) {
                narrow({ turn });
            } else if (agent) {
                narrow({ agent });
            }
        },
        true,
    );
    // Enter or Space on a pick is the button's own click; the row must not
    // also take the key as its cue to open.
    feed.addEventListener(
        "keydown",
        e => {
            const onPick = (e.target as HTMLElement | null)?.closest(".af-feed-pick");
            if (onPick && (e.key === "Enter" || e.key === " ")) {
                e.stopPropagation();
            }
        },
        true,
    );
}

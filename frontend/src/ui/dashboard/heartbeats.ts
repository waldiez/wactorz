/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * When each agent was last heard from, and the two bits of card that show it.
 *
 * A card carries a relative time ("12s ago") and a dot that goes stale after a
 * while. Both change for two different reasons — a heartbeat arriving, and time
 * passing with none — which is why this is one place rather than two: the
 * painting was written twice, once per reason, and the two copies could
 * disagree about what stale looks like.
 *
 * Owns the timestamps because it is what reads them. The overview asks for one
 * when it draws a card, which is the only other use.
 */
import { QUIET_MS, relTime, STALE_MS } from "./agentState";

/** How a card's dot says how long since its agent was heard from. */
const QUIET = "af-card-quiet";
const MISSING = "af-card-missing";

export class Heartbeats {
    private _lastSeen = new Map<string, number>();

    constructor(private root: HTMLElement) {}

    /** When each agent was last heard from — read by the overview when it draws. */
    get lastSeen(): Map<string, number> {
        return this._lastSeen;
    }

    /** Record a heartbeat and refresh that agent's card, if it is on screen. */
    record(agentId: string, timestampMs: number, options: { skip?: boolean } = {}): void {
        this._lastSeen.set(agentId, timestampMs);
        if (options.skip) {
            // An agent mid-removal: its card is animating out, and repainting it
            // would fight the animation for a row that is about to be gone.
            return;
        }
        const card = this._card(agentId);
        if (card) {
            this._paint(card, timestampMs, Date.now(), { pulse: true });
        }
    }

    /** Forget an agent, so a churned id does not leak an entry forever. */
    forget(agentId: string): void {
        this._lastSeen.delete(agentId);
    }

    /**
     * Re-render every card's age.
     *
     * Called on a timer: "12s ago" is wrong a second after it is drawn, and a
     * dot only becomes stale because nothing arrived — there is no event for
     * that, so something has to look.
     */
    refresh(): void {
        const now = Date.now();
        this._lastSeen.forEach((ms, id) => {
            const card = this._card(id);
            if (card) {
                this._paint(card, ms, now);
            }
        });
    }

    private _card(agentId: string): HTMLElement | null {
        return this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(agentId)}"]`);
    }

    private _paint(
        card: HTMLElement,
        timestampMs: number,
        now: number,
        options: { pulse?: boolean } = {},
    ): void {
        const age = card.querySelector<HTMLElement>(".af-card-hb-time");
        if (age) {
            age.textContent = relTime(timestampMs);
        }
        const dot = card.querySelector<HTMLElement>(".af-card-state-dot");
        if (!dot) {
            return;
        }
        if (!options.pulse) {
            paintFreshness(dot, card.dataset["state"] === "stopped" ? 0 : now - timestampMs);
            return;
        }
        // A heartbeat clears the warning outright rather than re-deriving it
        // from the timestamp: hearing from an agent is the fact, and a clock
        // skewed the wrong way should not leave a live agent marked missing.
        paintFreshness(dot, 0);
        dot.classList.remove("af-card-pulse");
        // Restarted rather than added: re-adding a class already present does
        // not replay the animation, so a steady heartbeat would pulse once and
        // then look dead.
        void dot.offsetWidth;
        dot.classList.add("af-card-pulse");
    }
}

/**
 * Mark a dot for how long its agent has been unheard: yellow past `QUIET_MS`,
 * red past `STALE_MS`, the state's own color before that. A stopped agent is
 * expected to be quiet and is given an age of zero by the caller.
 *
 * The card's "♥ 45s" line says the same in words, so the color is never the
 * only sign.
 */
export function paintFreshness(dot: HTMLElement, ageMs: number): void {
    const missing = ageMs > STALE_MS;
    dot.classList.toggle(MISSING, missing);
    dot.classList.toggle(QUIET, !missing && ageMs > QUIET_MS);
    dot.title = missing ? "not heard from for minutes" : ageMs > QUIET_MS ? "not heard from lately" : "";
}

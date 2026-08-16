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
import { relTime, STALE_MS } from "./agentState";

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
            dot.classList.toggle("af-card-stale", now - timestampMs > STALE_MS);
            return;
        }
        // A heartbeat clears stale outright rather than re-deriving it from the
        // timestamp: hearing from an agent is the fact, and a clock skewed the
        // wrong way should not leave a live agent greyed out.
        dot.classList.remove("af-card-pulse", "af-card-stale");
        // Restarted rather than added: re-adding a class already present does
        // not replay the animation, so a steady heartbeat would pulse once and
        // then look dead.
        void dot.offsetWidth;
        dot.classList.add("af-card-pulse");
    }
}

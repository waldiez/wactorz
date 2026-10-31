/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What happens to an agent's card that is no redraw: the animation it leaves on,
 * and the flash of a moment in its life.
 */
import type { AgentActivity, AgentChanges, AgentStore } from "../../agents/AgentStore";
import type { AgentInfo } from "../../types/agent";
import type { Heartbeats } from "./heartbeats";

/** Put `cls` on `card` for `ms`. */
function flash(card: HTMLElement, cls: string, ms: number): void {
    card.classList.add(cls);
    setTimeout(() => card.classList.remove(cls), ms);
}

/** The cards to repaint: each that changed, and each that shares a name with one
 *  that came or went, whose label gains or loses its node with it. */
export function patchedCards(changes: AgentChanges, store: Pick<AgentStore, "get" | "byName">): AgentInfo[] {
    const names = new Set<string>();
    changes.added.forEach(id => names.add(store.get(id)?.name ?? ""));
    changes.removed.forEach(agent => names.add(agent.name));
    const ids = new Set(changes.updated);
    names.forEach(name => store.byName(name).forEach(agent => ids.add(agent.id)));
    return [...ids].flatMap(id => store.get(id) ?? []);
}

/** Animates the cards under `root`, and keeps which are on their way out. */
export class CardMoments {
    /** The cards animating out, which nothing should repaint. */
    readonly removing = new Set<string>();

    constructor(
        private readonly root: HTMLElement,
        private readonly heartbeats: Heartbeats,
    ) {}

    private _card(id: string): HTMLElement | null {
        return this.root.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`);
    }

    /** Take a card off with its exit animation. */
    animateOut(id: string): void {
        const card = this._card(id);
        if (!card) {
            return;
        }
        this.removing.add(id);
        card.style.animation = "cd-exit 0.25s ease forwards";
        setTimeout(() => {
            card.remove();
            this.removing.delete(id);
        }, 250);
    }

    /** Show a moment on its agent's card. */
    show(moment: AgentActivity, visible: boolean): void {
        if (moment.kind === "heartbeat") {
            // Recorded either way; painted only when there is something to paint on.
            // A card mid-removal is animating out, and repainting it fights that.
            this.heartbeats.record(moment.id, moment.timestampMs, {
                skip: !visible || this.removing.has(moment.id),
            });
            return;
        }
        const card = visible ? this._card(moment.id) : null;
        if (!card) {
            return;
        }
        if (moment.kind === "alert") {
            const severe = moment.severity === "error" || moment.severity === "critical";
            flash(card, severe ? "af-card-alert-error" : "af-card-alert-warn", 900);
        } else {
            flash(card, "af-card-chat-flash", 600);
        }
    }
}

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The two bits of a card that say when an agent was last heard from.
 *
 * They change for two different reasons — a heartbeat arriving, and time
 * passing with none — and the painting used to be written once per reason. The
 * copies could disagree about what stale looks like, which is why this is one
 * unit now.
 */
import { describe, it, expect, beforeEach } from "vitest";

import { Heartbeats } from "../ui/dashboard/heartbeats";
import { STALE_MS } from "../ui/dashboard/agentState";

let root: HTMLElement;

function card(id: string): HTMLElement {
    const el = document.createElement("div");
    el.dataset["id"] = id;
    el.innerHTML = `<span class="af-card-hb-time"></span><span class="af-card-state-dot"></span>`;
    root.appendChild(el);
    return el;
}

const dotOf = (id: string) => root.querySelector<HTMLElement>(`[data-id="${id}"] .af-card-state-dot`)!;
const ageOf = (id: string) => root.querySelector<HTMLElement>(`[data-id="${id}"] .af-card-hb-time`)!;

beforeEach(() => {
    document.body.innerHTML = "";
    root = document.createElement("div");
    document.body.appendChild(root);
});

describe("recording a heartbeat", () => {
    it("writes the age onto the card", () => {
        card("main");
        const hb = new Heartbeats(root);

        hb.record("main", Date.now());

        expect(ageOf("main").textContent).toBeTruthy();
    });

    it("clears stale outright rather than re-deriving it", () => {
        // Hearing from an agent is the fact. A clock skewed the wrong way
        // should not leave a live agent greyed out.
        card("main");
        const hb = new Heartbeats(root);
        dotOf("main").classList.add("af-card-stale");

        hb.record("main", Date.now() - STALE_MS * 2);

        expect(dotOf("main").classList.contains("af-card-stale")).toBe(false);
    });

    it("remembers the time even when asked not to paint", () => {
        // A card mid-removal is animating out, and repainting fights that —
        // but the timestamp is still true and the overview still draws from it.
        const hb = new Heartbeats(root);

        hb.record("going", 1234, { skip: true });

        expect(hb.lastSeen.get("going")).toBe(1234);
    });

    it("does not fail when the agent has no card on screen", () => {
        const hb = new Heartbeats(root);

        expect(() => hb.record("never-drawn", Date.now())).not.toThrow();
    });
});

describe("refreshing on the timer", () => {
    it("marks a card stale once nothing has arrived for long enough", () => {
        // There is no event for "nothing happened", so something has to look.
        card("main");
        const hb = new Heartbeats(root);
        hb.lastSeen.set("main", Date.now() - STALE_MS * 2);

        hb.refresh();

        expect(dotOf("main").classList.contains("af-card-stale")).toBe(true);
    });

    it("leaves a recent card alone", () => {
        card("main");
        const hb = new Heartbeats(root);
        hb.lastSeen.set("main", Date.now());

        hb.refresh();

        expect(dotOf("main").classList.contains("af-card-stale")).toBe(false);
    });

    it("skips ids with no rendered card", () => {
        const hb = new Heartbeats(root);
        hb.lastSeen.set("ghost", Date.now());

        expect(() => hb.refresh()).not.toThrow();
    });
});

describe("forgetting", () => {
    it("drops the entry, so churned ids do not accumulate", () => {
        const hb = new Heartbeats(root);
        hb.record("gone", Date.now());

        hb.forget("gone");

        expect(hb.lastSeen.has("gone")).toBe(false);
    });
});

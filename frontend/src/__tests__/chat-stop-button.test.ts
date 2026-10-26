/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { CardDashboard } from "../ui/CardDashboard";

// The stop button (cancel in-flight generation) must be usable for the whole
// turn: it appears the moment the turn starts and is hidden once it ends, as
// the turn tracker reports them (`af-turn`). Send stays usable throughout: a
// message sent during a turn is queued, not refused.

describe("chat stop button lifecycle", () => {
    let send: HTMLButtonElement;
    let stop: HTMLButtonElement;

    beforeEach(() => {
        document.body.innerHTML = "";
        const cd = new CardDashboard() as any;
        const bar = cd._chat.buildIobar();
        document.body.appendChild(bar);
        send = bar.querySelector(".af-send-btn") as HTMLButtonElement;
        stop = bar.querySelector(".af-stop-btn") as HTMLButtonElement;
    });

    it("hides stop and enables send before any turn starts", () => {
        expect(stop.style.display).toBe("none");
        expect(send.disabled).toBe(false);
    });

    it("shows stop while a turn is under way, with send still usable", () => {
        document.dispatchEvent(new CustomEvent("af-turn", { detail: { agent: "main", queued: 0 } }));
        expect(stop.style.display).toBe("flex");
        expect(send.disabled).toBe(false);
    });

    it("hides stop once the turn ends", () => {
        document.dispatchEvent(new CustomEvent("af-turn", { detail: { agent: "main", queued: 0 } }));
        document.dispatchEvent(new CustomEvent("af-turn", { detail: { agent: null, queued: 0 } }));
        expect(stop.style.display).toBe("none");
        expect(send.disabled).toBe(false);
    });
});

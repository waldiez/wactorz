/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { CardDashboard } from "../ui/CardDashboard";

// The stop button (cancel in-flight generation) must be usable for the whole
// turn: it appears the moment the user sends and is hidden once the turn
// completes. A turn completes via "af-stream-end" (streamed reply) or
// "af-chat-message" (non-streamed reply, e.g. slash commands). While busy the
// send button is disabled so prompts can't overlap.

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

    it("shows stop and disables send the moment a message is sent", () => {
        document.dispatchEvent(new CustomEvent("af-send-message", { detail: {} }));
        expect(stop.style.display).toBe("flex");
        expect(send.disabled).toBe(true);
    });

    it("restores after a streamed reply (af-stream-end)", () => {
        document.dispatchEvent(new CustomEvent("af-send-message", { detail: {} }));
        document.dispatchEvent(new CustomEvent("af-stream-end", { detail: {} }));
        expect(stop.style.display).toBe("none");
        expect(send.disabled).toBe(false);
    });

    it("restores after a non-streamed reply (af-chat-message)", () => {
        document.dispatchEvent(new CustomEvent("af-send-message", { detail: {} }));
        document.dispatchEvent(new CustomEvent("af-chat-message", { detail: {} }));
        expect(stop.style.display).toBe("none");
        expect(send.disabled).toBe(false);
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The waiting row's wiring, as opposed to its behaviour.
 *
 * Its lifecycle is covered against the stream UI directly; what these check is
 * that the dashboard actually calls into it — a turn raises the row, a reply
 * clears it, and a command never raises it. Each of those is one call site, and
 * a missed one leaves no failing test behind, only a row that never appears or
 * never leaves.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { CardDashboard } from "../ui/CardDashboard";
import { emit } from "../events";

let cd: any;

function waitingRow(): HTMLElement | null {
    return cd.root.querySelector(".af-chat-waiting");
}

beforeEach(() => {
    document.body.innerHTML = "";
    cd = new CardDashboard() as any;
    cd.show([
        { id: "a1", name: "main", state: "running", protected: true },
        { id: "a2", name: "weather", state: "running", protected: false },
    ]);
    cd._setView("chat");
    cd._chat.setTarget("weather");
});

afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
});

describe("a turn sent from elsewhere", () => {
    it("raises the row for its target", () => {
        emit("af-send-message", { content: "what is the weather", target: "weather", attachments: [] });

        expect(waitingRow()).not.toBeNull();
    });

    it("is cleared by a reply from that agent", () => {
        emit("af-send-message", { content: "what is the weather", target: "weather", attachments: [] });

        emit("af-chat-message", {
            msg: {
                id: "m1",
                from: "weather",
                to: "user",
                content: "sunny",
                timestampMs: Date.now(),
            },
        });

        expect(waitingRow()).toBeNull();
    });

    it("is cleared when the send never left", () => {
        emit("af-send-message", { content: "what is the weather", target: "weather", attachments: [] });

        emit("af-send-failed", { content: "what is the weather", target: "weather" });

        expect(waitingRow()).toBeNull();
    });

    it("is cleared when the transport drops", () => {
        emit("af-send-message", { content: "what is the weather", target: "weather", attachments: [] });

        emit("af-connection-status", { status: "demo" });

        expect(waitingRow()).toBeNull();
    });
});

describe("a command", () => {
    it("never claims an agent is working", () => {
        emit("af-send-message", { content: "/help", target: "weather", attachments: [] });

        // Handled before any agent sees it and answered by main, so naming the
        // target would name the wrong agent.
        expect(waitingRow()).toBeNull();
    });
});

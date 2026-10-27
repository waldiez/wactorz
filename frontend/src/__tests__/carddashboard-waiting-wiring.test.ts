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

import type { CardDashboard } from "../ui/CardDashboard";
import { emit } from "../events";
import { agent, composer, goTo, openDashboard } from "./dashboard";
import { allByTestId, byRole } from "./queries";

let cd: CardDashboard;

function waitingRow(): HTMLElement | null {
    return allByTestId(document, "message-waiting")[0] ?? null;
}

beforeEach(() => {
    document.body.replaceChildren();
    localStorage.clear();
    ({ dashboard: cd } = openDashboard([
        agent("main", { id: "a1", protected: true }),
        agent("weather", { id: "a2" }),
    ]));
    goTo("chat", "weather");
});

afterEach(() => {
    cd.destroy();
    vi.useRealTimers();
    document.body.replaceChildren();
});

/** Send `words` from the composer, as the person does; the send event it puts out. */
function send(words: string): { id: string; content: string; target: string } {
    let sent: { id: string; content: string; target: string } | undefined;
    const record = (e: Event) => {
        sent = (e as CustomEvent<{ id: string; content: string; target: string }>).detail;
    };
    document.addEventListener("af-send-message", record);
    composer().value = words;
    byRole(document, "button", "Send message").click();
    document.removeEventListener("af-send-message", record);
    if (!sent) {
        throw new Error(`nothing went out for ${words}`);
    }
    return sent;
}

describe("a turn sent from the composer", () => {
    it("raises the row for its target", () => {
        send("what is the weather");

        expect(waitingRow()).not.toBeNull();
    });

    it("is cleared by a reply from that agent", () => {
        send("what is the weather");

        emit("af-chat-message", {
            msg: {
                id: "m1",
                from: "weather",
                to: "user",
                threadId: "weather",
                content: "sunny",
                timestampMs: Date.now(),
            },
        });

        expect(waitingRow()).toBeNull();
    });

    it("is cleared when the send never left", () => {
        const sent = send("what is the weather");

        emit("af-send-failed", sent);

        expect(waitingRow()).toBeNull();
    });

    it("is cleared when the socket closes", () => {
        send("what is the weather");

        emit("af-socket", { open: false });

        expect(waitingRow()).toBeNull();
    });
});

describe("a command", () => {
    it("never claims an agent is working", () => {
        send("/help");

        // Handled before any agent sees it and answered by main, so naming the
        // target would name the wrong agent.
        expect(waitingRow()).toBeNull();
    });
});

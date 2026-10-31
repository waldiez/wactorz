/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A pick among the addresses a name fits: the reply is answered once, and the
 * message that used the name goes again to the address picked.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { ChatAnswers } from "../ui/dashboard/chatAnswers";
import { ChatLog } from "../chat/chatLog";
import { emit, Listeners } from "../events";
import type { ChatMessage } from "../types/agent";

const listeners = new Listeners();
afterEach(() => listeners.off());

function message(id: string, over: Partial<ChatMessage> = {}): ChatMessage {
    return { id, from: "main", to: "user", threadId: "main-id", content: "", timestampMs: 1, ...over };
}

/** Answers over a log holding the person's `@counter five` and the reply offering two addresses. */
function answers(): { log: ChatLog; send: ReturnType<typeof vi.fn> } {
    const log = new ChatLog();
    log.add(message("m1", { from: "user", to: "main", content: "@counter five", attachments: [] }));
    log.add(
        message("r1", {
            content: "More than one agent is called 'counter'.",
            addressChoice: { addresses: ["counter@main", "counter@pi"], retryOf: "m1" },
        }),
    );
    const send = vi.fn();
    new ChatAnswers({ log, redrawThread: vi.fn(), redrawSidebar: vi.fn(), send }).wire(listeners);
    return { log, send };
}

describe("a pick among addresses", () => {
    it("answers the reply and sends the message again to the address", () => {
        const { log, send } = answers();

        emit("af-pick-address", { id: "r1", address: "counter@pi" });

        expect(log.get("r1")?.answer).toBe("counter@pi");
        expect(send).toHaveBeenCalledWith("@counter@pi five", []);
    });

    it("is taken once: a second press sends nothing more", () => {
        const { send } = answers();

        emit("af-pick-address", { id: "r1", address: "counter@pi" });
        emit("af-pick-address", { id: "r1", address: "counter@main" });

        expect(send).toHaveBeenCalledTimes(1);
    });

    it("of an address the reply did not offer does nothing", () => {
        const { log, send } = answers();

        emit("af-pick-address", { id: "r1", address: "counter@pc" });
        emit("af-pick-address", { id: "m1", address: "counter@pi" });

        expect(log.get("r1")?.answer).toBeUndefined();
        expect(send).not.toHaveBeenCalled();
    });

    it("once the message it would send was wiped, answers the reply and sends nothing", () => {
        const { log, send } = answers();
        log.reset(null);
        log.add(
            message("r1", { addressChoice: { addresses: ["counter@main", "counter@pi"], retryOf: "m1" } }),
        );

        emit("af-pick-address", { id: "r1", address: "counter@pi" });

        expect(log.get("r1")?.answer).toBe("counter@pi");
        expect(send).not.toHaveBeenCalled();
    });
});

describe("an agent's question", () => {
    function asked(): {
        log: ChatLog;
        redrawThread: ReturnType<typeof vi.fn>;
        redrawSidebar: ReturnType<typeof vi.fn>;
    } {
        const log = new ChatLog();
        log.add(
            message("q1", { from: "hvac", agentId: "hvac-id", threadId: "hvac-id", options: ["yes", "no"] }),
        );
        const redrawThread = vi.fn();
        const redrawSidebar = vi.fn();
        new ChatAnswers({ log, redrawThread, redrawSidebar, send: vi.fn() }).wire(listeners);
        return { log, redrawThread, redrawSidebar };
    }

    it("is answered by a press here or an answer from elsewhere, and redrawn", () => {
        const { log, redrawThread, redrawSidebar } = asked();

        emit("af-answered", { id: "q1", option: "no" });

        expect(log.get("q1")?.answer).toBe("no");
        expect(redrawThread).toHaveBeenCalledWith(log.get("q1"));
        expect(redrawSidebar).toHaveBeenCalled();
    });

    it("unknown here, is not redrawn for", () => {
        const { redrawThread } = asked();

        emit("af-answer-option", { id: "q9", agentId: "hvac-id", option: "no", index: 1 });

        expect(redrawThread).not.toHaveBeenCalled();
    });

    it("answered while the page was away closes when the ledger says so", () => {
        const { log, redrawThread } = asked();
        const answered = { ...log.get("q1")!, answer: "yes" };

        emit("af-questions-listed", { open: [], answered: [answered] });
        emit("af-questions-listed", { open: [], answered: [answered] });

        expect(log.get("q1")?.answer).toBe("yes");
        // Redrawn for the change, not for a ledger that changed nothing.
        expect(redrawThread).toHaveBeenCalledTimes(1);
    });
});

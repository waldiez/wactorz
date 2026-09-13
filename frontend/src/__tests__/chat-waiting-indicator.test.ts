/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The gap between sending and the first token.
 *
 * A row appears in the agent's place so a slow answer reads as work in progress
 * rather than as nothing happening. What matters is that it leaves again on
 * every path a turn can end by — including the ones that produce no text at all.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { ChatStreamUI, type StreamHost } from "../ui/dashboard/chatStreaming";
import { TURN_IDLE_TIMEOUT_MS } from "../ui/dashboard/chatIobar";

function makeHost(thread: HTMLElement, inChatView = true, openThread = "weather"): StreamHost {
    return {
        isChatView: () => inChatView,
        lastSentTarget: () => "weather",
        // What the chat controller does: a reply belongs here when it comes from
        // the agent whose thread is open.
        belongsHere: (from: string) => from === openThread,
        isOpenThread: (name: string) => name === openThread,
        thread: () => thread,
        scrollThread: () => {},
        commit: () => {},
    };
}

function waiting(thread: HTMLElement): HTMLElement | null {
    return thread.querySelector(".af-chat-waiting");
}

let thread: HTMLElement;

beforeEach(() => {
    document.body.innerHTML = "";
    thread = document.createElement("div");
    document.body.appendChild(thread);
});

describe("while an agent is working", () => {
    it("shows a row in its place", () => {
        const ui = new ChatStreamUI(makeHost(thread));

        ui.awaiting("weather");

        expect(waiting(thread)).not.toBeNull();
        expect(thread.querySelector(".af-chat-msg-from")?.textContent).toBe("weather");
    });

    it("says who is working, for a reader who cannot see the dots", () => {
        const ui = new ChatStreamUI(makeHost(thread));

        ui.awaiting("weather");

        expect(waiting(thread)?.getAttribute("aria-label")).toBe("weather is working");
        expect(waiting(thread)?.getAttribute("role")).toBe("status");
    });

    it("reports the turn as outstanding, so the thread keeps room for it", () => {
        const ui = new ChatStreamUI(makeHost(thread));

        ui.awaiting("weather");

        expect(ui.isAwaiting).toBe(true);
    });

    it("shows only one row however many turns are announced", () => {
        const ui = new ChatStreamUI(makeHost(thread));

        ui.awaiting("weather");
        ui.awaiting("weather");

        expect(thread.querySelectorAll(".af-chat-waiting").length).toBe(1);
    });

    it("renders nothing while another view is open", () => {
        const ui = new ChatStreamUI(makeHost(thread, false));

        ui.awaiting("weather");

        expect(waiting(thread)).toBeNull();
        // Still outstanding, so returning to the chat view puts it back.
        expect(ui.isAwaiting).toBe(true);
    });
});

describe("once the turn ends", () => {
    it("the first token replaces it", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.onChunk({ chunk: "it is ", from: "weather" });

        expect(waiting(thread)).toBeNull();
        expect(ui.isAwaiting).toBe(false);
        expect(thread.querySelector(".af-chat-msg-bubble")?.textContent).toBe("it is ");
    });

    it("an ending with no text at all still clears it", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        // What a stopped or failed turn looks like: the stream ends having
        // produced nothing, and the row would otherwise wait forever.
        ui.onEnd({ text: null, from: "weather" });

        expect(waiting(thread)).toBeNull();
        expect(ui.isAwaiting).toBe(false);
    });

    it("an ending attributed to nobody still clears it", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.onEnd(null);

        expect(waiting(thread)).toBeNull();
    });
});

describe("when the thread is re-rendered underneath it", () => {
    it("comes back if the turn is still outstanding", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        thread.innerHTML = "";
        ui.dropRefs();
        ui.reattachWaiting();

        expect(waiting(thread)).not.toBeNull();
    });

    it("does not come back once the turn has ended", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");
        ui.onEnd({ text: null, from: "weather" });

        thread.innerHTML = "";
        ui.reattachWaiting();

        expect(waiting(thread)).toBeNull();
    });
});

describe("whose conversation it appears in", () => {
    it("stays out of another agent's thread", () => {
        const ui = new ChatStreamUI(makeHost(thread, true, "main"));

        ui.awaiting("weather");

        // Sent to weather while main's thread is open: main is not working.
        expect(waiting(thread)).toBeNull();
        expect(ui.isAwaiting).toBe(true);
    });

    it("appears when that conversation is opened", () => {
        let open = "main";
        const ui = new ChatStreamUI({
            isChatView: () => true,
            lastSentTarget: () => "weather",
            belongsHere: (from: string) => from === open,
            isOpenThread: (name: string) => name === open,
            thread: () => thread,
            scrollThread: () => {},
            commit: () => {},
        });
        ui.awaiting("weather");
        expect(waiting(thread)).toBeNull();

        open = "weather";
        ui.reattachWaiting();

        expect(waiting(thread)).not.toBeNull();
    });
});

describe("which turn an ending belongs to", () => {
    it("a chunk from another agent leaves this one waiting", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.onChunk({ chunk: "unrelated", from: "calendar" });

        // Concurrent streams are a feature: another agent starting to answer
        // says nothing about whether this one has.
        expect(waiting(thread)).not.toBeNull();
        expect(ui.isAwaiting).toBe(true);
    });

    it("an ending from another agent leaves this one waiting", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.onEnd({ text: "done", from: "calendar" });

        expect(waiting(thread)).not.toBeNull();
        expect(ui.isAwaiting).toBe(true);
    });

    it("an ending attributed to nobody ends whatever is outstanding", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.endWait(null);

        expect(ui.isAwaiting).toBe(false);
    });

    it("a reply from a bystander agent does not end it", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        // Agent-to-agent chatter arrives as an ordinary chat frame; it is not an
        // answer to this turn. A command, which is answered by main rather than
        // by the target, never starts a wait in the first place.
        ui.endWait("main");

        expect(ui.isAwaiting).toBe(true);
    });

    it("gives up unconditionally when told nothing is coming", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        // A dropped connection or a send that never left: no reply for anyone.
        ui.endWait();

        expect(waiting(thread)).toBeNull();
        expect(ui.isAwaiting).toBe(false);
    });
});

describe("the empty state of another thread", () => {
    it("is not suppressed by a turn belonging elsewhere", () => {
        const ui = new ChatStreamUI(makeHost(thread, true, "main"));

        ui.awaiting("weather");

        // Outstanding, but not here -- main's thread must still say it is empty
        // rather than showing nothing at all.
        expect(ui.isAwaiting).toBe(true);
        expect(ui.awaitingHere()).toBe(false);
    });

    it("is suppressed for the thread the turn belongs to", () => {
        const ui = new ChatStreamUI(makeHost(thread));

        ui.awaiting("weather");

        expect(ui.awaitingHere()).toBe(true);
    });
});

describe("a turn that never goes on the wire", () => {
    it("stops waiting when the send fails", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        // What DashboardChat does on af-send-failed: nothing left, so nothing
        // is coming back.
        ui.endWait();

        expect(waiting(thread)).toBeNull();
    });

    it("stops waiting when the transport drops", () => {
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        ui.endWait();

        expect(ui.isAwaiting).toBe(false);
    });
});

describe("when nothing ever answers", () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it("gives up rather than waiting for the life of the page", () => {
        vi.useFakeTimers();
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);

        // A backend that dies mid-turn sends none of the endings, and the row
        // would otherwise stay up until the page is reloaded.
        expect(waiting(thread)).toBeNull();
        expect(ui.isAwaiting).toBe(false);
    });

    it("keeps waiting right up to the limit", () => {
        vi.useFakeTimers();
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");

        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS - 1);

        expect(ui.isAwaiting).toBe(true);
    });

    it("does not fire after the turn already ended", () => {
        vi.useFakeTimers();
        const ui = new ChatStreamUI(makeHost(thread));
        ui.awaiting("weather");
        ui.onChunk({ chunk: "sunny", from: "weather" });

        // A later turn must not be cut short by the previous turn's timer.
        ui.awaiting("weather");
        vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS - 1);

        expect(ui.isAwaiting).toBe(true);
    });
});

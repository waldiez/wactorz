/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The streaming UI keeps buffers for every agent but a row for only the visible
 * thread, so each entry point has to cope with the two being out of step: the
 * user on another view, a stream belonging to a different agent, a thread that
 * has just been re-rendered away.
 *
 * Buffering must continue regardless — leaving the chat view mid-answer and
 * coming back has to show the whole reply, not the tail.
 */
import { describe, it, expect, vi } from "vitest";

import { ChatStreamUI, type StreamHost } from "../ui/dashboard/chatStreaming";

interface Host extends StreamHost {
    thread(): HTMLElement | null;
}

/** A host whose answers to each question can be set per test. */
function makeHost(over: Partial<Host> = {}): Host {
    return {
        isChatView: () => true,
        lastSentTarget: () => "main",
        belongsHere: () => true,
        thread: () => document.createElement("div"),
        scrollThread: vi.fn(),
        commit: vi.fn(),
        ...over,
    };
}

describe("onChunk when the chat view is not open", () => {
    it("buffers without building a row", () => {
        const scrollThread = vi.fn();
        const ui = new ChatStreamUI(makeHost({ isChatView: () => false, scrollThread }));

        ui.onChunk({ chunk: "half an answer", from: "weather" });

        // Buffered, so returning to the view shows the whole reply…
        expect(ui.streams.text("weather")).toBe("half an answer");
        // …but nothing was rendered or scrolled into a thread nobody is looking at.
        expect(scrollThread).not.toHaveBeenCalled();
    });
});

describe("onChunk when there is no thread to render into", () => {
    it("still buffers", () => {
        // The chat view is open but its element is gone — mid re-render.
        const ui = new ChatStreamUI(makeHost({ thread: () => null }));

        ui.onChunk({ chunk: "text", from: "weather" });

        expect(ui.streams.text("weather")).toBe("text");
    });
});

describe("streamHereText", () => {
    it("is null when nothing is streaming", () => {
        expect(new ChatStreamUI(makeHost()).streamHereText()).toBeNull();
    });

    it("is null when the active stream has produced no text yet", () => {
        const ui = new ChatStreamUI(makeHost());
        ui.onChunk({ chunk: "", from: "weather" });

        expect(ui.streamHereText()).toBeNull();
    });

    it("is null when the active stream belongs to another thread", () => {
        // Someone else's agent is answering; its text must not drive this
        // thread's empty-state or get reattached into it.
        const ui = new ChatStreamUI(makeHost({ belongsHere: () => false }));
        ui.onChunk({ chunk: "not for you", from: "other" });

        expect(ui.streamHereText()).toBeNull();
    });

    it("returns the text when the active stream is this thread's", () => {
        const ui = new ChatStreamUI(makeHost());
        ui.onChunk({ chunk: "for you", from: "weather" });

        expect(ui.streamHereText()).toBe("for you");
    });
});

describe("reattachRow", () => {
    it("does nothing when no stream is live", () => {
        const thread = document.createElement("div");

        new ChatStreamUI(makeHost()).reattachRow(thread);

        // A stray empty bubble after a thread re-render would sit there with
        // nothing ever filling it.
        expect(thread.children.length).toBe(0);
    });

    it("rebuilds the live row with the text so far", () => {
        const ui = new ChatStreamUI(makeHost());
        ui.onChunk({ chunk: "partial", from: "weather" });
        const thread = document.createElement("div");

        ui.reattachRow(thread);

        expect(thread.textContent).toContain("partial");
    });
});

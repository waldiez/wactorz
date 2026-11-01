/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn(), close: vi.fn() } }));

import { tombstoneWiped } from "../agents/deletionGuard";
import { socketMessage } from "../agents/mapping";
import { chatIngress } from "../ui/chatIngress";
import { QuestionToasts } from "../ui/questionToast";
import { toast } from "../ui/ToastManager";
import type { ChatMessage } from "../types/agent";

const said = (over: Partial<ChatMessage> = {}) =>
    ({ id: "m1", from: "weather", to: "user", content: "sunny", timestampMs: 1, ...over }) as ChatMessage;
const asked = (over: Partial<ChatMessage> = {}) => said({ id: "q1", options: ["Yes", "No"], ...over });

describe("QuestionToasts", () => {
    beforeEach(() => vi.clearAllMocks());

    it("raises a question once, and says what it is", () => {
        const toasts = new QuestionToasts(() => false);

        expect(toasts.arrived(asked())).toBe(true);
        expect(toasts.arrived(asked())).toBe(true);

        expect(toast.show).toHaveBeenCalledTimes(1);
    });

    it("takes a message that asks nothing for what it is", () => {
        expect(new QuestionToasts(() => false).arrived(said())).toBe(false);
        expect(toast.show).not.toHaveBeenCalled();
    });

    it("counts a question seen in the Inbox as shown", () => {
        let inbox = true;
        const toasts = new QuestionToasts(() => inbox);

        toasts.arrived(asked());
        inbox = false;
        toasts.arrived(asked());

        expect(toast.show).not.toHaveBeenCalled();
    });

    it("keeps what it showed through a failed read of the ledger", () => {
        const toasts = new QuestionToasts(() => false);
        toasts.listed({ open: [asked()], answered: [], listed: { questions: [], askedAt: 0 } });

        toasts.listed({ open: [], answered: [] });
        toasts.listed({ open: [asked()], answered: [], listed: { questions: [], askedAt: 0 } });

        expect(toast.show).toHaveBeenCalledTimes(1);
    });
});

describe("chatIngress", () => {
    const speaker = { receiveAgentMessage: vi.fn() };
    const store = { onChat: vi.fn() };
    const ingest = () => chatIngress({ speaker, store, questions: new QuestionToasts(() => false) });

    beforeEach(() => vi.clearAllMocks());

    it("takes a message everywhere it goes", () => {
        const events: string[] = [];
        const hear = (e: Event) => events.push(e.type);
        document.addEventListener("af-chat-message", hear);
        document.addEventListener("af-feed-push", hear);

        ingest()(said());

        document.removeEventListener("af-chat-message", hear);
        document.removeEventListener("af-feed-push", hear);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "chat", title: "weather" }));
        expect(speaker.receiveAgentMessage).toHaveBeenCalled();
        expect(store.onChat).toHaveBeenCalledWith("weather");
        expect(events).toEqual(["af-chat-message", "af-feed-push"]);
    });

    it("raises a question's own toast, not a plain one", () => {
        ingest()(asked());

        expect(toast.show).toHaveBeenCalledTimes(1);
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ key: "q1", sticky: true }));
    });

    it("shows no notice for what the person said", () => {
        ingest()(said({ from: "user", to: "weather" }));

        expect(toast.show).not.toHaveBeenCalled();
        expect(store.onChat).toHaveBeenCalledWith("user");
    });

    it("cuts a long message short in its notice", () => {
        ingest()(said({ content: "x".repeat(200) }));

        expect(vi.mocked(toast.show).mock.calls[0]![0].message).toHaveLength(120);
    });
});

describe("tombstoneWiped", () => {
    it("marks what the wipe took, not what survived", () => {
        const marked: string[] = [];

        tombstoneWiped([{ id: "a" }, { id: "b" }, { id: "c" }], ["b"], id => marked.push(id));

        expect(marked).toEqual(["a", "c"]);
    });
});

describe("socketMessage", () => {
    it("keeps what the frame said, and leaves out what it did not", () => {
        const msg = socketMessage("hi", "main", 5, "user", "", "", "", "", "t1", null);

        expect(msg).toMatchObject({ from: "main", to: "user", content: "hi", threadId: "t1" });
        expect(msg).not.toHaveProperty("source");
        expect(msg).not.toHaveProperty("addressChoice");
    });

    it("gives every message an id of its own, even in one millisecond", () => {
        const frame = ["hi", "main", 5, "user", "", "", "", "", "", null] as const;

        expect(socketMessage(...frame).id).not.toBe(socketMessage(...frame).id);
    });

    it("carries the frame's extras", () => {
        const msg = socketMessage("hi", "main", 5, "user", "cli", "voice", "Kitchen", "gpt", "t", null);

        expect(msg).toMatchObject({ source: "cli", surface: "voice", surfaceLabel: "Kitchen", brain: "gpt" });
    });
});

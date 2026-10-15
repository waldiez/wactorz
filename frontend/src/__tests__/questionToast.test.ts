/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { questionToast } from "../ui/questionToast";
import type { ChatMessage } from "../types/agent";

function message(over: Partial<ChatMessage> = {}): ChatMessage {
    return {
        id: "q1",
        from: "installer",
        to: "user",
        content: "'probe' needs a package. Install?",
        timestampMs: 0,
        agentId: "installer-id",
        options: ["Install", "Refuse"],
        ...over,
    };
}

describe("questionToast", () => {
    it("is nothing for a message without options", () => {
        const { options: _omitted, ...plain } = message();
        expect(questionToast(plain)).toBeNull();
        expect(questionToast(message({ options: [] }))).toBeNull();
    });

    it("is nothing for a question already answered", () => {
        expect(questionToast(message({ answer: "Install" }))).toBeNull();
    });

    it("stays up, keyed by the question, with a button per option", () => {
        const toast = questionToast(message())!;

        expect(toast.sticky).toBe(true);
        expect(toast.key).toBe("q1");
        expect(toast.title).toBe("installer");
        expect(toast.message).toContain("Install?");
        expect(toast.actions!.map(a => a.label)).toEqual(["Install", "Refuse"]);
    });

    it("a button sends the pick as the thread's card does", () => {
        const seen = vi.fn();
        document.addEventListener("af-answer-option", seen);
        try {
            questionToast(message())!.actions![1]!.onClick();
        } finally {
            document.removeEventListener("af-answer-option", seen);
        }

        const detail = (seen.mock.calls[0]![0] as CustomEvent).detail;
        expect(detail).toEqual({ id: "q1", agentId: "installer-id", option: "Refuse", index: 1 });
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { QuestionNotices } from "../ui/questionToast";

describe("QuestionNotices", () => {
    it("says a question is new once", () => {
        const notices = new QuestionNotices();

        expect(notices.firstTime("q1")).toBe(true);
        expect(notices.firstTime("q1")).toBe(false);
        expect(notices.firstTime("q2")).toBe(true);
    });

    it("keeps what is still open and forgets the rest", () => {
        const notices = new QuestionNotices();
        notices.firstTime("q1");
        notices.firstTime("q2");

        notices.keepOnly([{ id: "q2" }]);

        expect(notices.firstTime("q2")).toBe(false);
        expect(notices.firstTime("q1")).toBe(true);
    });
});

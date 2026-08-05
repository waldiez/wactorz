/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * An inline image must open the lightbox however it arrived.
 *
 * The markdown renderer emits inert images by design, so the click behaviour is
 * attached by the dashboard layer. Two render paths reach it — a message
 * replayed from history, and a stream finishing live — and both must wire the
 * image, or a snapshot is clickable only after a reload. These drive both.
 */
import { describe, it, expect, beforeEach } from "vitest";

import { ChatStreamUI, type StreamHost } from "../ui/dashboard/chatStreaming";
import { buildChatMessageEl } from "../ui/dashboard/chatThread";
import type { ChatMessage } from "../types/agent";

const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==";
const SNAPSHOT = `here you go\n\n![snapshot](${PNG})`;

function inlineImage(root: ParentNode): HTMLImageElement | null {
    return root.querySelector("img.af-chat-md-img");
}

/** A host whose thread is a real element, so rows actually render. */
function makeHost(thread: HTMLElement): StreamHost {
    return {
        isChatView: () => true,
        lastSentTarget: () => "camera",
        belongsHere: () => true,
        thread: () => thread,
        scrollThread: () => {},
        commit: () => {},
    };
}

beforeEach(() => {
    document.body.innerHTML = "";
});

describe("inline images from a finished stream", () => {
    it("are clickable without a reload", () => {
        const thread = document.createElement("div");
        document.body.appendChild(thread);
        const ui = new ChatStreamUI(makeHost(thread));

        ui.onChunk({ chunk: SNAPSHOT, from: "camera" });
        ui.onEnd({ text: SNAPSHOT, from: "camera" });

        const img = inlineImage(thread);
        expect(img).not.toBeNull();
        // role=button is what makeZoomable adds; without it the image is inert.
        expect(img!.getAttribute("role")).toBe("button");

        img!.click();
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });

    it("are reachable from the keyboard", () => {
        const thread = document.createElement("div");
        document.body.appendChild(thread);
        const ui = new ChatStreamUI(makeHost(thread));

        ui.onChunk({ chunk: SNAPSHOT, from: "camera" });
        ui.onEnd({ text: SNAPSHOT, from: "camera" });

        const img = inlineImage(thread)!;
        expect(img.tabIndex).toBe(0);
        img.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });
});

describe("inline images replayed from history", () => {
    it("are clickable too", () => {
        const msg: ChatMessage = {
            id: "1",
            from: "camera",
            to: "user",
            content: SNAPSHOT,
            timestampMs: 1_700_000_000_000,
        };

        const row = buildChatMessageEl(msg);
        document.body.appendChild(row);

        const img = inlineImage(row)!;
        expect(img.getAttribute("role")).toBe("button");
        img.click();
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { buildChatMessageEl, buildChatEmptyState } from "../ui/dashboard/chatThread";
import type { ChatMessage, Attachment } from "../types/agent";

function msg(over: Partial<ChatMessage>): ChatMessage {
    return { id: "1", from: "main", to: "user", content: "hi", timestampMs: 1_700_000_000_000, ...over };
}

function att(over: Partial<Attachment>): Attachment {
    return { id: "a", name: "f", mime: "application/pdf", size: 1024, ...over };
}

describe("buildChatEmptyState", () => {
    it("uses the orchestrator copy for main-actor", () => {
        expect(buildChatEmptyState("main-actor").textContent).toContain("orchestrator");
    });

    it("names the target agent otherwise", () => {
        expect(buildChatEmptyState("io").innerHTML).toContain("@io");
    });
});

describe("buildChatMessageEl", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("renders a user message as plain text with no time row", () => {
        const el = buildChatMessageEl(msg({ from: "user", content: "*not* markdown" }));
        expect(el.className).toContain("af-chat-msg-user");
        expect(el.querySelector(".af-chat-msg-from")!.textContent).toContain("you ·");
        // plain text — asterisks are preserved literally
        expect(el.querySelector(".af-chat-msg-bubble")!.textContent).toBe("*not* markdown");
        expect(el.querySelector(".af-chat-msg-time")).toBeNull();
    });

    it("renders an agent message as markdown with a time row", () => {
        const el = buildChatMessageEl(msg({ from: "main", content: "**bold**" }));
        expect(el.className).toContain("af-chat-msg-agent");
        expect(el.querySelector(".af-chat-msg-from")!.textContent).toBe("main");
        expect(el.querySelector(".af-chat-msg-bubble strong")).not.toBeNull(); // markdown rendered
        expect(el.querySelector(".af-chat-msg-time")).not.toBeNull();
    });

    it("renders an image attachment as a thumbnail that opens the lightbox", () => {
        const el = buildChatMessageEl(
            msg({ attachments: [att({ mime: "image/png", url: "http://x/p.png", name: "p" })] }),
        );
        const thumb = el.querySelector<HTMLImageElement>(".af-chat-attach-thumb")!;
        expect(thumb).not.toBeNull();
        thumb.click();
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });

    it("exposes the image thumbnail as a keyboard-operable button", () => {
        const el = buildChatMessageEl(
            msg({ attachments: [att({ mime: "image/png", url: "http://x/p.png", name: "p" })] }),
        );
        const thumb = el.querySelector<HTMLImageElement>(".af-chat-attach-thumb")!;
        expect(thumb.getAttribute("role")).toBe("button");
        expect(thumb.tabIndex).toBe(0);
        expect(thumb.getAttribute("aria-label")).toContain("p");
        thumb.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });

    it("renders a non-image attachment with a url as a download link", () => {
        const el = buildChatMessageEl(msg({ attachments: [att({ url: "http://x/f.pdf", name: "f.pdf" })] }));
        const link = el.querySelector<HTMLAnchorElement>("a.af-chat-attach-file")!;
        expect(link.getAttribute("href")).toBe("http://x/f.pdf");
        expect(link.target).toBe("_blank");
    });

    it("renders a url-less attachment as a non-clickable span", () => {
        const el = buildChatMessageEl(msg({ attachments: [att({})] })); // no url key
        expect(el.querySelector("span.af-chat-attach-file")).not.toBeNull();
        expect(el.querySelector("a.af-chat-attach-file")).toBeNull();
    });

    it("drops a hostile-scheme attachment url (no javascript: href)", () => {
        const el = buildChatMessageEl(msg({ attachments: [att({ url: "javascript:alert(1)", name: "x" })] }));
        expect(el.querySelector("a.af-chat-attach-file")).toBeNull(); // not linkable
        expect(el.querySelector("span.af-chat-attach-file")).not.toBeNull();
    });

    it("keeps a same-origin (relative) attachment url", () => {
        const el = buildChatMessageEl(msg({ attachments: [att({ url: "/api/upload/f.pdf", name: "f" })] }));
        const link = el.querySelector<HTMLAnchorElement>("a.af-chat-attach-file")!;
        expect(link.getAttribute("href")).toBe("/api/upload/f.pdf");
    });
});

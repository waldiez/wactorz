/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import { buildChatMessageEl, buildChatEmptyState } from "../ui/dashboard/chatThread";
import type { ChatMessage, Attachment } from "../types/agent";

function msg(over: Partial<ChatMessage>): ChatMessage {
    return {
        id: "1",
        from: "main",
        to: "user",
        threadId: "main",
        content: "hi",
        timestampMs: 1_700_000_000_000,
        ...over,
    };
}

function att(over: Partial<Attachment>): Attachment {
    return { id: "a", name: "f", mime: "application/pdf", size: 1024, ...over };
}

describe("buildChatEmptyState", () => {
    it("uses the orchestrator copy for main", () => {
        expect(buildChatEmptyState("main").textContent).toContain("orchestrator");
    });

    it("names the target agent otherwise", () => {
        expect(buildChatEmptyState("io").innerHTML).toContain("@io");
    });

    it("escapes a hostile agent name (no markup injection)", () => {
        const el = buildChatEmptyState(`<img src=x onerror="window.__pwned=1">`);
        expect(el.querySelector("img")).toBeNull(); // not parsed as markup
        expect(el.textContent).toContain("No messages with"); // rendered as text
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

    it("labels an interface-mediated voice exchange without exposing its brain", () => {
        const user = buildChatMessageEl(
            msg({
                from: "user",
                to: "reachy-mini",
                threadId: "reachy-mini",
                source: "voice",
                surface: "reachy-mini",
                surfaceLabel: "Reachy",
                brain: "main",
            }),
        );
        const assistant = buildChatMessageEl(
            msg({
                from: "reachy-mini",
                source: "voice",
                surface: "reachy-mini",
                surfaceLabel: "Reachy",
                brain: "main",
            }),
        );

        expect(user.querySelector(".af-chat-msg-from")!.textContent).toContain("you · via Reachy");
        expect(assistant.querySelector(".af-chat-msg-from")!.textContent).toBe("Reachy");
        expect(assistant.textContent).not.toContain("main");
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

    it("opens the lightbox from the keyboard with Space as well as Enter", () => {
        const el = buildChatMessageEl(
            msg({ attachments: [att({ mime: "image/png", url: "http://x/p.png", name: "p" })] }),
        );
        const thumb = el.querySelector<HTMLImageElement>(".af-chat-attach-thumb")!;

        thumb.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));

        expect(document.querySelector(".af-lightbox")).not.toBeNull();
    });

    it("ignores other keys on the thumbnail", () => {
        const el = buildChatMessageEl(
            msg({ attachments: [att({ mime: "image/png", url: "http://x/p.png", name: "p" })] }),
        );
        const thumb = el.querySelector<HTMLImageElement>(".af-chat-attach-thumb")!;

        thumb.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));

        // Typing must not open a viewer over the thread.
        expect(document.querySelector(".af-lightbox")).toBeNull();
    });

    it("renders an attachment with no url as an unlinked chip", () => {
        // `att` has no url by default — omitted, not undefined (exactOptionalPropertyTypes).
        const el = buildChatMessageEl(msg({ attachments: [att({ name: "f.pdf" })] }));

        expect(el.querySelector("a.af-chat-attach-file")).toBeNull();
        expect(el.querySelector("span.af-chat-attach-file")!.textContent).toContain("f.pdf");
    });

    it("falls back to a chip when an image's url is refused", () => {
        const el = buildChatMessageEl(
            msg({ attachments: [att({ mime: "image/png", url: "javascript:alert(1)", name: "x.png" })] }),
        );

        // No thumbnail, and no link either — the scheme is refused for both.
        expect(el.querySelector(".af-chat-attach-thumb")).toBeNull();
        expect(el.querySelector("a.af-chat-attach-file")).toBeNull();
    });

    it("renders an empty message without a bubble body", () => {
        const el = buildChatMessageEl(msg({ content: "", from: "main" }));

        expect(el.querySelector(".af-chat-msg-bubble")!.childNodes.length).toBe(0);
    });

    it("never links a non-image attachment through a data: url", () => {
        // `data:text/html` in an href is a navigation context: following it runs
        // script in this origin. The image path below still accepts data:,
        // because rendering is not navigating.
        const el = buildChatMessageEl(
            msg({
                attachments: [
                    att({ mime: "text/html", url: "data:text/html,<script>x=1</script>", name: "n.html" }),
                ],
            }),
        );
        expect(el.querySelector("a.af-chat-attach-file")).toBeNull();
        // Still shown, just not clickable — the name is information.
        expect(el.querySelector("span.af-chat-attach-file")!.textContent).toContain("n.html");
    });

    it("still renders a data: image as a thumbnail", () => {
        const el = buildChatMessageEl(
            msg({
                attachments: [
                    att({ mime: "image/png", url: "data:image/png;base64,iVBORw0KGgo=", name: "s" }),
                ],
            }),
        );
        expect(el.querySelector(".af-chat-attach-thumb")).not.toBeNull();
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

describe("buildChatMessageEl — a question with options", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    const question = (over: Partial<ChatMessage> = {}): ChatMessage =>
        msg({
            id: "q1",
            from: "hvac-watch",
            agentId: "agent-1",
            content: "Did something change at 14:02?",
            options: ["maintenance", "real problem", "not sure"],
            ...over,
        });

    it("renders one button per option, in order, as text", () => {
        const el = buildChatMessageEl(question({ options: ["<b>x</b>", "y"] }));
        const buttons = el.querySelectorAll<HTMLButtonElement>("button.af-chat-option");
        expect([...buttons].map(b => b.textContent)).toEqual(["<b>x</b>", "y"]);
        expect(el.querySelector("button.af-chat-option b")).toBeNull(); // not parsed as markup
        buttons.forEach(b => expect(b.disabled).toBe(false));
    });

    it("a press names the message, the agent, the option and its place", () => {
        const picks: unknown[] = [];
        const seen = (e: Event): void => {
            picks.push((e as CustomEvent).detail);
        };
        document.addEventListener("af-answer-option", seen);
        try {
            const el = buildChatMessageEl(question());
            el.querySelectorAll<HTMLButtonElement>("button.af-chat-option")[1]!.click();
        } finally {
            document.removeEventListener("af-answer-option", seen);
        }
        expect(picks).toEqual([{ id: "q1", agentId: "agent-1", option: "real problem", index: 1 }]);
    });

    it("an answered question keeps its buttons dimmed, with the pick marked", () => {
        const el = buildChatMessageEl(question({ answer: "not sure" }));
        const buttons = [...el.querySelectorAll<HTMLButtonElement>("button.af-chat-option")];
        buttons.forEach(b => expect(b.disabled).toBe(true));
        const picked = el.querySelector<HTMLButtonElement>(".af-chat-option-picked")!;
        expect(picked.textContent).toBe("not sure");
        expect(picked.getAttribute("aria-pressed")).toBe("true");
    });

    it("shows an answer the buttons do not carry, as text", () => {
        const el = buildChatMessageEl(question({ answer: "something else" }));
        expect(el.querySelector(".af-chat-option-note")!.textContent).toBe("✓ something else");
    });

    it("says where else the question went", () => {
        const el = buildChatMessageEl(question({ channel: "telegram" }));
        expect(el.querySelector(".af-chat-msg-time")!.textContent).toContain("also on telegram");
    });

    it("a plain message has no buttons, and a user message never does", () => {
        expect(buildChatMessageEl(msg({})).querySelector(".af-chat-options")).toBeNull();
        expect(
            buildChatMessageEl(msg({ from: "user", options: ["a", "b"] })).querySelector(".af-chat-options"),
        ).toBeNull();
    });
});

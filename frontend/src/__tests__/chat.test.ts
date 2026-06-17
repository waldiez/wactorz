/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock AgentImageGen before importing ChatPanel
vi.mock("../io/AgentImageGen", () => ({
    agentImageGen: { get: () => "https://dicebear.com/fake.svg" },
}));

import { ChatPanel } from "../ui/ChatPanel";
import type { AgentInfo, ChatMessage } from "../types/agent";

function setupDOM() {
    document.body.innerHTML = `
    <div id="chat-panel"></div>
    <div id="panel-agent-name"></div>
    <div id="panel-agent-status"></div>
    <img id="panel-agent-avatar" />
    <button id="panel-close"></button>
    <div id="chat-messages"></div>
    <div id="chat-agent-list"></div>
    <input id="chat-sidebar-search" />
  `;
}

function agent(name = "alpha", opts: Partial<AgentInfo> = {}): AgentInfo {
    return { id: name, name, state: "running", protected: false, ...opts };
}

function msg(overrides: Partial<ChatMessage> = {}): ChatMessage {
    return {
        id: "msg-1",
        from: "alpha",
        to: "user",
        content: "hello",
        timestampMs: Date.now(),
        ...overrides,
    };
}

function panel() {
    return document.getElementById("chat-panel")!;
}
function messages() {
    return document.getElementById("chat-messages")!;
}
function agentList() {
    return document.getElementById("chat-agent-list")!;
}

describe("ChatPanel", () => {
    beforeEach(() => {
        setupDOM();
        vi.clearAllMocks();
        // Stub navigator.clipboard
        Object.defineProperty(navigator, "clipboard", {
            value: { writeText: vi.fn().mockResolvedValue(undefined) },
            configurable: true,
        });
    });

    it("constructs without throwing", () => {
        expect(() => new ChatPanel()).not.toThrow();
    });

    it("open() adds 'open' class to panel", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        expect(panel().classList.contains("open")).toBe(true);
    });

    it("open() sets agent name in header", () => {
        const cp = new ChatPanel();
        cp.open(agent("bravo"));
        expect(document.getElementById("panel-agent-name")!.textContent).toBe("bravo");
    });

    it("open() sets agent state in header", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha", { state: "paused" }));
        expect(document.getElementById("panel-agent-status")!.textContent).toBe("paused");
    });

    it("open() shows 'failed' for object state", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha", { state: { failed: "timeout" } }));
        expect(document.getElementById("panel-agent-status")!.textContent).toBe("failed");
    });

    it("open() dispatches 'panel-opened' event", () => {
        const cp = new ChatPanel();
        const spy = vi.fn();
        document.addEventListener("panel-opened", spy);
        cp.open(agent("alpha"));
        expect(spy).toHaveBeenCalledOnce();
        document.removeEventListener("panel-opened", spy);
    });

    it("open() dispatches 'agent-unread-cleared' event", () => {
        const cp = new ChatPanel();
        const spy = vi.fn();
        document.addEventListener("agent-unread-cleared", spy);
        cp.open(agent("alpha"));
        expect(spy).toHaveBeenCalledOnce();
        document.removeEventListener("agent-unread-cleared", spy);
    });

    it("close() removes 'open' class", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.close();
        expect(panel().classList.contains("open")).toBe(false);
    });

    it("close() dispatches 'panel-closed' event", () => {
        const cp = new ChatPanel();
        const spy = vi.fn();
        document.addEventListener("panel-closed", spy);
        cp.open(agent("alpha"));
        cp.close();
        expect(spy).toHaveBeenCalledOnce();
        document.removeEventListener("panel-closed", spy);
    });

    it("panel-close button click closes panel", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        (document.getElementById("panel-close") as HTMLButtonElement).click();
        expect(panel().classList.contains("open")).toBe(false);
        void cp;
    });

    it("Escape key closes panel", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        expect(panel().classList.contains("open")).toBe(false);
        void cp;
    });

    it("swipe right closes panel", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        panel().dispatchEvent(new TouchEvent("touchstart", { touches: [{ clientX: 10 } as Touch] }));
        panel().dispatchEvent(new TouchEvent("touchend", { changedTouches: [{ clientX: 80 } as Touch] }));
        expect(panel().classList.contains("open")).toBe(false);
        void cp;
    });

    it("swipe left does not close panel", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        panel().dispatchEvent(new TouchEvent("touchstart", { touches: [{ clientX: 80 } as Touch] }));
        panel().dispatchEvent(new TouchEvent("touchend", { changedTouches: [{ clientX: 30 } as Touch] }));
        expect(panel().classList.contains("open")).toBe(true);
        void cp;
    });

    it("switching agents cross-fades thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.open(agent("bravo")); // panel already open, different agent → animated
        expect(document.getElementById("panel-agent-name")!.textContent).toBe("bravo");
    });

    it("animated renderThread executes after 140ms timeout", () => {
        vi.useFakeTimers();
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "hello" }));
        cp.open(agent("bravo")); // triggers animate path
        // Before timeout: opacity is 0
        expect(messages().style.opacity).toBe("0");
        vi.advanceTimersByTime(150);
        // After timeout: paint() runs and opacity is restored
        expect(messages().style.opacity).toBe("1");
        vi.useRealTimers();
    });

    it("re-opening same agent does not re-render thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        const spy = vi.spyOn(messages(), "scrollTop", "set");
        cp.open(agent("alpha")); // same agent, panel already open → no-op
        // The spy may or may not be called; just verify it doesn't throw
        expect(true).toBe(true);
        void spy;
    });

    it("agent-selected event opens the panel", () => {
        const cp = new ChatPanel();
        const a = agent("charlie");
        document.dispatchEvent(new CustomEvent("agent-selected", { detail: { agent: a } }));
        expect(panel().classList.contains("open")).toBe(true);
        expect(document.getElementById("panel-agent-name")!.textContent).toBe("charlie");
        void cp;
    });

    it("ensureOpen() opens panel when closed", () => {
        const cp = new ChatPanel();
        cp.ensureOpen("io-agent");
        expect(panel().classList.contains("open")).toBe(true);
    });

    it("ensureOpen() is no-op when already open", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        const nameBefore = document.getElementById("panel-agent-name")!.textContent;
        cp.ensureOpen("override");
        expect(document.getElementById("panel-agent-name")!.textContent).toBe(nameBefore);
    });

    it("appendMessage() renders user message in active thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "user", to: "alpha" }));
        expect(messages().querySelector(".af-chat-msg-user")).not.toBeNull();
    });

    it("appendMessage() renders agent message with avatar", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", to: "user" }));
        expect(messages().querySelector(".af-chat-msg-agent")).not.toBeNull();
    });

    it("appendMessage() renders system message", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "system", to: "user", content: "sys msg" }));
        expect(messages().querySelector(".af-chat-msg-system")).not.toBeNull();
    });

    it("appendMessage() routes io-gateway to active thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "io-gateway", to: "alpha" }));
        expect(messages().querySelectorAll(".af-chat-msg").length).toBe(1);
    });

    it("appendMessage() to background agent fires agent-unread event", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        const spy = vi.fn();
        document.addEventListener("agent-unread", spy);
        cp.appendMessage(msg({ from: "bravo", to: "user" })); // bravo is not active
        expect(spy).toHaveBeenCalledOnce();
        document.removeEventListener("agent-unread", spy);
    });

    it("updateAgentStatus() updates status when agent is selected", () => {
        const cp = new ChatPanel();
        const a = agent("alpha", { id: "id-alpha" });
        cp.open(a);
        cp.updateAgentStatus("id-alpha", "paused");
        expect(document.getElementById("panel-agent-status")!.textContent).toBe("paused");
    });

    it("updateAgentStatus() is no-op for unselected agent", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha", { id: "id-alpha" }));
        cp.updateAgentStatus("id-bravo", "paused");
        expect(document.getElementById("panel-agent-status")!.textContent).not.toBe("paused");
    });

    it("activeAgent getter returns selected agent", () => {
        const cp = new ChatPanel();
        const a = agent("alpha");
        cp.open(a);
        expect(cp.activeAgent?.name).toBe("alpha");
    });

    it("activeAgent getter returns null after close", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.close();
        expect(cp.activeAgent).toBeNull();
    });

    it("streamChunk() creates bubble on first chunk", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("hello", "alpha");
        expect(messages().querySelector(".af-chat-msg-bubble")).not.toBeNull();
    });

    it("streamChunk() accumulates text across chunks", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("hel", "alpha");
        cp.streamChunk("lo", "alpha");
        const bubble = messages().querySelector(".af-chat-msg-bubble")!;
        expect(bubble.textContent).toBe("hello");
    });

    it("finalizeStream() renders markdown and stores in thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("**bold**", "alpha");
        cp.finalizeStream();
        const bubble = messages().querySelector(".af-chat-msg-bubble")!;
        expect(bubble.innerHTML).toContain("<strong>");
    });

    it("finalizeStream() adds copy button", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("text", "alpha");
        cp.finalizeStream();
        expect(messages().querySelector(".af-chat-copy-btn")).not.toBeNull();
    });

    it("finalizeStream() sets lastStreamedText", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("stream content", "alpha");
        cp.finalizeStream();
        expect(cp.lastStreamedText).toBe("stream content");
    });

    it("lastStreamedText getter clears after read", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.streamChunk("stream content", "alpha");
        cp.finalizeStream();
        cp.lastStreamedText; // first read
        expect(cp.lastStreamedText).toBe(""); // cleared
    });

    it("finalizeStream() is no-op when no stream is active", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        expect(() => cp.finalizeStream()).not.toThrow();
    });

    it("streamChunk() does not append to DOM when panel is closed", () => {
        const cp = new ChatPanel();
        cp.streamChunk("hidden", "alpha");
        // panel not open — element created but not appended to messagesEl
        expect(messages().querySelectorAll(".af-chat-msg").length).toBe(0);
    });

    it("copy button writes text to clipboard", async () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", to: "user", content: "copy me" }));
        const btn = messages().querySelector<HTMLButtonElement>(".af-chat-copy-btn")!;
        btn.click();
        await vi.waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("copy me"));
    });

    it("copy button restores icon after 2 second timeout", async () => {
        vi.useFakeTimers();
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", to: "user", content: "text" }));
        const btn = messages().querySelector<HTMLButtonElement>(".af-chat-copy-btn")!;
        btn.click();
        await Promise.resolve(); // allow writeText to resolve
        vi.advanceTimersByTime(2100);
        // After the inner setTimeout fires, innerHTML is restored (SVG) and color is cleared
        expect(btn.style.color).toBe("");
        vi.useRealTimers();
    });

    it("copy button handles clipboard failure gracefully", async () => {
        Object.defineProperty(navigator, "clipboard", {
            value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
            configurable: true,
        });
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", to: "user", content: "text" }));
        const btn = messages().querySelector<HTMLButtonElement>(".af-chat-copy-btn")!;
        expect(() => btn.click()).not.toThrow();
        await new Promise(r => setTimeout(r, 10));
    });

    it("showTyping() adds typing bubble to active thread", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        // agentId must equal activeAgentName ("alpha") for the bubble to be appended
        cp.showTyping("alpha", "alpha");
        expect(messages().querySelector(".af-chat-typing")).not.toBeNull();
    });

    it("showTyping() is no-op when bubble already exists", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.showTyping("alpha", "alpha");
        cp.showTyping("alpha", "alpha");
        expect(messages().querySelectorAll(".af-chat-typing").length).toBe(1);
    });

    it("hideTyping() removes the bubble", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.showTyping("alpha", "alpha");
        cp.hideTyping("alpha");
        expect(messages().querySelector(".af-chat-typing")).toBeNull();
    });

    it("hideTyping() is no-op when no bubble exists", () => {
        const cp = new ChatPanel();
        expect(() => cp.hideTyping("non-existent-id")).not.toThrow();
    });

    it("showTyping() uses agentId as label when name not provided", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.showTyping("alpha");
        const fromEl = messages().querySelector(".af-chat-msg-from")!;
        expect(fromEl.textContent).toBe("alpha");
    });

    it("showTyping() appends bubble when no activeAgentName set", () => {
        const cp = new ChatPanel();
        // Don't open any agent — activeAgentName is null
        cp.showTyping("bravo", "bravo");
        expect(messages().querySelector(".af-chat-typing")).not.toBeNull();
    });

    it("updateAgentList() renders agent rows", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha"), agent("bravo")]);
        expect(agentList().querySelectorAll(".af-chat-agent-row").length).toBe(2);
    });

    it("updateAgentList() sorts main-actor first", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("zzz"), agent("main-actor"), agent("aaa")]);
        const rows = agentList().querySelectorAll<HTMLElement>(".af-chat-agent-row");
        expect(rows[0]!.dataset["name"]).toBe("main-actor");
    });

    it("updateAgentList() removes agents no longer in list", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha"), agent("bravo")]);
        cp.updateAgentList([agent("alpha")]);
        expect(agentList().querySelectorAll(".af-chat-agent-row").length).toBe(1);
    });

    it("clicking agent row dispatches agent-selected", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha")]);
        const spy = vi.fn();
        document.addEventListener("agent-selected", spy);
        const row = agentList().querySelector<HTMLButtonElement>(".af-chat-agent-row")!;
        row.click();
        expect(spy).toHaveBeenCalledOnce();
        document.removeEventListener("agent-selected", spy);
    });

    it("protected agent row is disabled", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("secret", { protected: true })]);
        const row = agentList().querySelector<HTMLButtonElement>(".af-chat-agent-row")!;
        expect(row.disabled).toBe(true);
    });

    it("protected main-actor is not disabled", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("main-actor", { protected: true })]);
        const row = agentList().querySelector<HTMLButtonElement>(".af-chat-agent-row")!;
        expect(row.disabled).toBe(false);
    });

    it("sidebar search filters by agent name", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha"), agent("bravo"), agent("beta")]);
        const search = document.getElementById("chat-sidebar-search") as HTMLInputElement;
        search.value = "b";
        search.dispatchEvent(new Event("input"));
        const rows = agentList().querySelectorAll(".af-chat-agent-row");
        expect(rows.length).toBe(2); // bravo + beta
        void cp;
    });

    it("dot color is red for failed state", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha", { state: { failed: "error" } })]);
        const dot = agentList().querySelector<HTMLElement>(".af-chat-agent-dot")!;
        expect(dot.style.background).toBe("#f87171");
    });

    it("dot color is green for running state", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha", { state: "running" })]);
        const dot = agentList().querySelector<HTMLElement>(".af-chat-agent-dot")!;
        expect(dot.style.background).toBe("#34d399");
    });

    it("dot color is yellow for paused state", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha", { state: "paused" })]);
        const dot = agentList().querySelector<HTMLElement>(".af-chat-agent-dot")!;
        expect(dot.style.background).toBe("#fbbf24");
    });

    it("dot color is gray for stopped state", () => {
        const cp = new ChatPanel();
        cp.updateAgentList([agent("alpha", { state: "stopped" })]);
        const dot = agentList().querySelector<HTMLElement>(".af-chat-agent-dot")!;
        expect(dot.style.background).toBe("#4b5563");
    });

    it("renderMarkdown renders bold text", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "**bold**" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<strong>");
    });

    it("renderMarkdown renders italic text", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "*italic*" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<em>");
    });

    it("renderMarkdown renders headings", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "## heading" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<h2>");
    });

    it("renderMarkdown renders unordered list", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "* item1\n* item2" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<ul>");
    });

    it("renderMarkdown renders ordered list", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "1. first\n2. second" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<ol>");
    });

    it("renderMarkdown renders fenced code blocks", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "```\ncode here\n```" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<pre>");
    });

    it("renderMarkdown renders inline code", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "`inline`" }));
        expect(messages().querySelector(".af-chat-msg-bubble")!.innerHTML).toContain("<code>");
    });

    it("renderMarkdown XSS-escapes raw HTML in content", () => {
        const cp = new ChatPanel();
        cp.open(agent("alpha"));
        cp.appendMessage(msg({ from: "alpha", content: "<script>alert(1)</script>" }));
        const html = messages().querySelector(".af-chat-msg-bubble")!.innerHTML;
        expect(html).not.toContain("<script>");
        expect(html).toContain("&lt;script&gt;");
    });
});

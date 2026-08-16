/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ChatInput, type ChatInputHost } from "../ui/dashboard/chatInput";

interface Harness {
    ci: ChatInput;
    input: HTMLTextAreaElement;
    select: HTMLSelectElement;
    ghost: HTMLElement;
    panel: HTMLElement;
    host: ChatInputHost & { setTarget: ReturnType<typeof vi.fn>; send: ReturnType<typeof vi.fn> };
}

function setup(agents = ["main", "io-agent"]): Harness {
    document.body.innerHTML = "";
    const wrap = document.createElement("div");
    const ghost = document.createElement("div");
    ghost.className = "af-input-ghost";
    const input = document.createElement("textarea");
    wrap.append(ghost, input); // input.previousElementSibling === ghost (recordSent relies on this)
    document.body.appendChild(wrap);

    const select = document.createElement("select");
    agents.forEach(n => {
        const o = document.createElement("option");
        o.value = n;
        o.text = n;
        select.appendChild(o);
    });

    const panel = document.createElement("div");
    document.body.appendChild(panel);

    const host = {
        agentNames: vi.fn(() => agents),
        setTarget: vi.fn(),
        send: vi.fn(),
    };
    return { ci: new ChatInput(host), input, select, ghost, panel, host };
}

const key = (k: string, init: KeyboardEventInit = {}) => new KeyboardEvent("keydown", { key: k, ...init });

describe("ChatInput @mentions", () => {
    let h: Harness;
    beforeEach(() => {
        h = setup();
    });

    it("opens the mention panel with a chip per agent on '@'", () => {
        h.input.value = "@";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.panel.classList.contains("open")).toBe(true);
        expect(h.panel.querySelectorAll("button").length).toBe(2);
        expect(h.host.agentNames).toHaveBeenCalled();
    });

    it("filters mention matches by prefix", () => {
        h.input.value = "@i";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.panel.querySelectorAll("button").length).toBe(1);
        expect(h.panel.querySelector("button")!.textContent).toBe("io-agent");
    });

    it("closes the panel when no match", () => {
        h.input.value = "@zzz";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.panel.classList.contains("open")).toBe(false);
    });

    it("Tab accepts the first mention and sets the target", () => {
        h.input.value = "hi @i";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        h.ci.onKeydown(key("Tab"), h.input, h.select, h.ghost, h.panel);
        expect(h.host.setTarget).toHaveBeenCalledWith("io-agent");
        expect(h.input.value).toBe("hi"); // trailing @query stripped
        expect(h.panel.classList.contains("open")).toBe(false);
    });

    it("ArrowRight/ArrowLeft move the active mention before accepting", () => {
        h.input.value = "@";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        h.ci.onKeydown(key("ArrowRight"), h.input, h.select, h.ghost, h.panel); // idx 0 -> 1
        h.ci.onKeydown(key("Enter"), h.input, h.select, h.ghost, h.panel);
        expect(h.host.setTarget).toHaveBeenCalledWith("io-agent");
    });

    it("Escape closes the mention panel", () => {
        h.input.value = "@";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        h.ci.onKeydown(key("Escape"), h.input, h.select, h.ghost, h.panel);
        expect(h.panel.classList.contains("open")).toBe(false);
    });

    it("clicking a chip accepts that mention", () => {
        h.input.value = "@";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        const chip = [...h.panel.querySelectorAll("button")].find(b => b.textContent === "main")!;
        chip.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        expect(h.host.setTarget).toHaveBeenCalledWith("main");
    });

    it("a mention with no matching <select> option is a no-op, not a fake target switch", () => {
        // A suggested name the <select> can't target (e.g. a non-messageable
        // agent). Accepting it must NOT setTarget, must NOT strip the @query,
        // and must dismiss the panel — never leaving a misleading placeholder.
        (h.host.agentNames as ReturnType<typeof vi.fn>).mockReturnValue(["main", "io-agent", "ghost-agent"]);
        h.input.value = "@ghost";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        const chip = [...h.panel.querySelectorAll("button")].find(b => b.textContent === "ghost-agent")!;
        expect(chip).toBeTruthy();
        chip.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        expect(h.host.setTarget).not.toHaveBeenCalled();
        expect(h.input.value).toBe("@ghost");
        expect(h.panel.classList.contains("open")).toBe(false);
    });
});

describe("ChatInput send", () => {
    let h: Harness;
    beforeEach(() => {
        h = setup();
    });

    it("Enter sends the message", () => {
        h.input.value = "hello";
        h.ci.onKeydown(key("Enter"), h.input, h.select, h.ghost, h.panel);
        expect(h.host.send).toHaveBeenCalledWith(h.input);
    });

    it("Shift+Enter does not send", () => {
        h.input.value = "hello";
        h.ci.onKeydown(key("Enter", { shiftKey: true }), h.input, h.select, h.ghost, h.panel);
        expect(h.host.send).not.toHaveBeenCalled();
    });
});

describe("ChatInput history & ghost suggestion", () => {
    let h: Harness;
    beforeEach(() => {
        h = setup();
    });

    it("ArrowUp/ArrowDown step through sent history", () => {
        h.ci.recordSent("first", h.input);
        h.ci.recordSent("second", h.input);
        h.input.value = "";

        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("second");
        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("first");
        h.ci.onKeydown(key("ArrowDown"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("second");
        h.ci.onKeydown(key("ArrowDown"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe(""); // back to the saved draft
    });

    it("shows a ghost suggestion from history and Tab accepts it", () => {
        h.ci.recordSent("hello world", h.input);
        h.input.value = "hel";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.input.classList.contains("has-suggestion")).toBe(true);
        expect(h.ghost.textContent).toContain("lo world");

        h.ci.onKeydown(key("Tab"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("hello world");
        expect(h.input.classList.contains("has-suggestion")).toBe(false);
    });

    it("ArrowRight at end-of-input accepts the suggestion", () => {
        h.ci.recordSent("status report", h.input);
        h.input.value = "stat";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        h.input.setSelectionRange(h.input.value.length, h.input.value.length);
        h.ci.onKeydown(key("ArrowRight"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("status report");
    });

    it("recordSent clears the ghost/suggestion state", () => {
        h.ci.recordSent("hello world", h.input);
        h.input.value = "hel";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.input.classList.contains("has-suggestion")).toBe(true);

        h.ci.recordSent("hello world", h.input);
        expect(h.input.classList.contains("has-suggestion")).toBe(false);
        expect(h.ghost.textContent).toBe("");
    });

    it("Escape clears an active ghost suggestion", () => {
        h.ci.recordSent("deploy now", h.input);
        h.input.value = "dep";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.input.classList.contains("has-suggestion")).toBe(true);
        h.ci.onKeydown(key("Escape"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.classList.contains("has-suggestion")).toBe(false);
    });
    it("fires input on history recall so the textarea auto-grows to fit", () => {
        const h = setup();
        const grew = vi.fn();
        h.input.addEventListener("input", grew);
        h.ci.recordSent("line 1\nline 2\nline 3", h.input);
        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("line 1\nline 2\nline 3");
        expect(grew).toHaveBeenCalled();
    });
});

describe("ChatInput history edges", () => {
    let h: Harness;
    beforeEach(() => {
        h = setup();
    });

    it("does nothing on ArrowUp with an empty history", () => {
        h.input.value = "typed";

        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);

        // Recalling from nothing must leave what the user was typing alone.
        expect(h.input.value).toBe("typed");
    });

    it("ignores ArrowDown before any recall has started", () => {
        h.ci.recordSent("earlier", h.input);
        h.input.value = "draft";

        h.ci.onKeydown(key("ArrowDown"), h.input, h.select, h.ghost, h.panel);

        expect(h.input.value).toBe("draft");
    });

    it("restores the draft when walking back down past the newest entry", () => {
        h.ci.recordSent("earlier", h.input);
        h.input.value = "half-typed";

        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);
        expect(h.input.value).toBe("earlier");

        h.ci.onKeydown(key("ArrowDown"), h.input, h.select, h.ghost, h.panel);

        // The half-typed message is not lost by browsing history past it.
        expect(h.input.value).toBe("half-typed");
    });

    it("caps the history so a long session cannot grow it without bound", () => {
        for (let i = 0; i < 60; i++) {
            h.ci.recordSent(`msg-${i}`, h.input);
        }

        // `history` is private, so the cap is observed through recall: walking
        // up 50 times reaches the oldest kept entry, and further presses stay
        // there rather than reaching msg-9 and below.
        h.input.value = "";
        for (let i = 0; i < 60; i++) {
            h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);
        }

        expect(h.input.value).toBe("msg-10"); // 50 kept: msg-59 … msg-10
    });

    it("resets the history cursor when the user types anything else", () => {
        h.ci.recordSent("earlier", h.input);
        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);

        h.ci.onKeydown(key("a"), h.input, h.select, h.ghost, h.panel);
        h.input.value = "";
        h.ci.onKeydown(key("ArrowUp"), h.input, h.select, h.ghost, h.panel);

        // Editing after a recall starts browsing again from the top rather
        // than continuing from where the last walk left off.
        expect(h.input.value).toBe("earlier");
    });

    it("clears the ghost when the input is emptied", () => {
        h.ci.recordSent("status report", h.input);
        h.input.value = "sta";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);
        expect(h.ghost.textContent).not.toBe("");

        h.input.value = "   ";
        h.ci.onChange(h.input, h.select, h.ghost, h.panel);

        expect(h.ghost.textContent).toBe("");
    });
});

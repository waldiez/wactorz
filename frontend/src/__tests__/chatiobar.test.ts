/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { Listeners } from "../events";
import { buildIobar, type IobarDeps } from "../ui/chat/chatIobar";
import type { ChatInput } from "../ui/chat/chatInput";
import { CommandCatalog, CommandCompletion } from "../ui/chat/commandCompletion";
import { byRole } from "./queries";
import { emit } from "../events";

function makeDeps(over: Partial<IobarDeps> = {}): IobarDeps {
    return {
        listeners: new Listeners(),
        chatInput: {
            onChange: vi.fn(),
            onKeydown: vi.fn(),
            closePanel: vi.fn(),
            commands: new CommandCompletion(new CommandCatalog(async () => [])),
        } as unknown as ChatInput,
        target: () => "main",
        setTarget: vi.fn(),
        populateSelect: vi.fn((sel: HTMLSelectElement) => {
            const o = document.createElement("option");
            o.value = "main";
            o.text = "main";
            sel.appendChild(o);
        }),
        send: vi.fn(),
        stop: vi.fn(),
        ...over,
    };
}

function mount(deps: IobarDeps): HTMLElement {
    const bar = buildIobar(deps);
    document.body.appendChild(bar);
    return bar;
}

describe("buildIobar", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
    });

    it("builds the select, textarea, send and (hidden) stop buttons", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        expect(bar.querySelector("#af-target-select")).not.toBeNull();
        expect(bar.querySelector("#af-iobar-input")).not.toBeNull();
        expect(bar.querySelector(".af-send-btn")).not.toBeNull();
        const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;
        expect(stop.style.display).toBe("none");
        expect(deps.populateSelect).toHaveBeenCalled();
    });

    it("places the command list beside the box and ties the box to it", () => {
        const bar = mount(makeDeps());
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        const list = byRole(bar, "listbox", "Commands");

        expect(list.parentElement).toBe(input.parentElement);
        expect(input.getAttribute("aria-controls")).toBe(list.id);
        expect(bar.querySelector(".af-input-hint")!.textContent).toContain("/ commands");
    });

    it("the icon-only send and stop buttons expose an accessible name", () => {
        const bar = mount(makeDeps());
        expect(bar.querySelector(".af-send-btn")!.getAttribute("aria-label")).toBe("Send message");
        expect(bar.querySelector(".af-stop-btn")!.getAttribute("aria-label")).toBe("Stop generating");
    });

    it("send button closes the mention panel and calls send()", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        bar.querySelector<HTMLButtonElement>(".af-send-btn")!.click();
        expect(deps.chatInput.closePanel).toHaveBeenCalled();
        expect(deps.send).toHaveBeenCalled();
    });

    it("stop button calls stop()", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        bar.querySelector<HTMLButtonElement>(".af-stop-btn")!.click();
        expect(deps.stop).toHaveBeenCalled();
    });

    it("changing the target select updates target and placeholder", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        const select = bar.querySelector<HTMLSelectElement>("#af-target-select")!;
        select.value = "main";
        select.dispatchEvent(new Event("change"));
        expect(deps.setTarget).toHaveBeenCalledWith("main");
        expect(bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!.placeholder).toBe("Message @main…");
    });

    it("routes textarea input and keydown to the ChatInput controller", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        const input = bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!;
        input.dispatchEvent(new Event("input"));
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
        expect(deps.chatInput.onChange).toHaveBeenCalled();
        expect(deps.chatInput.onKeydown).toHaveBeenCalled();
    });

    it("shows Stop while a turn is under way, and keeps Send usable to queue the next", () => {
        // Who the turn waits on and when it ends is the turn tracker's, tested there.
        const bar = mount(makeDeps());
        const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;
        const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;
        expect(stop.style.display).toBe("none");

        emit("af-turn", { agent: "main", queued: 0 });
        expect(stop.style.display).toBe("flex");
        expect(send.disabled).toBe(false);

        emit("af-turn", { agent: null, queued: 1 });
        expect(stop.style.display).toBe("none");
    });

    it("closes the mention panel shortly after the textarea blurs", () => {
        vi.useFakeTimers();
        const deps = makeDeps();
        const bar = mount(deps);
        bar.querySelector<HTMLTextAreaElement>("#af-iobar-input")!.dispatchEvent(new Event("blur"));
        vi.advanceTimersByTime(150);
        expect(deps.chatInput.closePanel).toHaveBeenCalled();
        vi.useRealTimers();
    });

    it("stops following the turn once its listeners are let go", () => {
        const listeners = new Listeners();
        const bar = buildIobar(makeDeps({ listeners }));
        const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;

        listeners.off();
        emit("af-turn", { agent: "main", queued: 0 });

        expect(stop.style.display).toBe("none");
    });
});

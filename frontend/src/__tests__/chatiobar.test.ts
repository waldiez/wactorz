/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../io/SpeechToText";

function makeDeps(over: Partial<IobarDeps> = {}): IobarDeps {
    return {
        chatInput: { onChange: vi.fn(), onKeydown: vi.fn(), closePanel: vi.fn() } as unknown as ChatInput,
        stt: {} as SpeechToText,
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

    it("toggles send/stop across a generation turn via document events", () => {
        const deps = makeDeps();
        const bar = mount(deps);
        const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;
        const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;

        document.dispatchEvent(new CustomEvent("af-send-message"));
        expect(send.disabled).toBe(true);
        expect(stop.style.display).toBe("flex");

        document.dispatchEvent(new CustomEvent("af-stream-end"));
        expect(send.disabled).toBe(false);
        expect(stop.style.display).toBe("none");

        // non-streamed replies end the turn too
        document.dispatchEvent(new CustomEvent("af-send-message"));
        expect(send.disabled).toBe(true);
        document.dispatchEvent(new CustomEvent("af-chat-message"));
        expect(send.disabled).toBe(false);
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
});

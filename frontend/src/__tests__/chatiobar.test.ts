/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { buildIobar, TURN_IDLE_TIMEOUT_MS, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import type { SpeechToText } from "../io/SpeechToText";
import { emit } from "../events";
import type { ChatMessage } from "../types/agent";

/** Start a turn aimed at `target`, as DashboardChat does on send. */
function sendTo(target: string): void {
    emit("af-send-message", { content: "hi", target, attachments: [] });
}

/** A chat frame from `from`; `to` defaults to "user" as a real reply would. */
function chatFrom(from: string, to = "user"): void {
    const msg: ChatMessage = { id: "m", from, to, content: "c", timestampMs: 0 };
    emit("af-chat-message", { msg });
}

function streamEndFrom(from: string): void {
    emit("af-stream-end", { text: "done", from });
}

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

        sendTo("main");
        expect(send.disabled).toBe(true);
        expect(stop.style.display).toBe("flex");

        streamEndFrom("main");
        expect(send.disabled).toBe(false);
        expect(stop.style.display).toBe("none");

        // non-streamed replies end the turn too
        sendTo("main");
        expect(send.disabled).toBe(true);
        chatFrom("main");
        expect(send.disabled).toBe(false);
    });

    describe("the turn belongs to one target", () => {
        it("ignores chat traffic from an agent that is not the current target", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;
            const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;

            sendTo("main");
            // agent-to-agent chatter, nothing to do with the user's turn
            chatFrom("worker-1", "planner");

            expect(send.disabled).toBe(true);
            expect(stop.style.display).toBe("flex");
        });

        it("ignores a stream ending for a different agent", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("main");
            streamEndFrom("worker-1");

            expect(send.disabled).toBe(true);
        });

        it("ends the turn on a reply from the target, which is addressed to the user", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("catalog");
            // the match key is the sender, not `to` — a real reply is always to: "user"
            chatFrom("catalog", "user");

            expect(send.disabled).toBe(false);
        });

        it("tracks the target of the newest turn, not the first", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("main");
            sendTo("catalog");
            chatFrom("main");
            expect(send.disabled).toBe(true);

            chatFrom("catalog");
            expect(send.disabled).toBe(false);
        });
    });

    describe("recovery while the backend is unreachable", () => {
        it("idles when the transport stops being live mid-turn", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;
            const stop = bar.querySelector<HTMLElement>(".af-stop-btn")!;

            sendTo("main");
            emit("af-connection-status", { status: "demo" });

            expect(send.disabled).toBe(false);
            expect(stop.style.display).toBe("none");
        });

        it("stays busy while the transport is still live", () => {
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("main");
            emit("af-connection-status", { status: "live" });

            expect(send.disabled).toBe(true);
        });

        it("idles after a silent stretch with no terminating event", () => {
            vi.useFakeTimers();
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("main");
            vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);

            expect(send.disabled).toBe(false);
            vi.useRealTimers();
        });

        it("does not cut off a slow but living stream", () => {
            vi.useFakeTimers();
            const bar = mount(makeDeps());
            const send = bar.querySelector<HTMLButtonElement>(".af-send-btn")!;

            sendTo("main");
            // tokens keep arriving, just slowly — each one must restart the clock
            for (let i = 0; i < 5; i++) {
                vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS - 1_000);
                emit("af-stream-chunk", { chunk: "tok", from: "main" });
            }
            expect(send.disabled).toBe(true);

            vi.advanceTimersByTime(TURN_IDLE_TIMEOUT_MS + 1);
            expect(send.disabled).toBe(false);
            vi.useRealTimers();
        });
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

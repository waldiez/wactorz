/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildIobar, type IobarDeps } from "../ui/dashboard/chatIobar";
import type { ChatInput } from "../ui/dashboard/chatInput";
import { CommandCatalog, CommandCompletion } from "../ui/dashboard/commandCompletion";
import { buildRoomMicBtn } from "../ui/dashboard/roomMic";
import type { SpeechToText } from "../ext/stt";
import { safeStorage } from "../safeStorage";
import { toast } from "../ui/ToastManager";
import { blockingFindings } from "./axe";
import { allByRole, byRole } from "./queries";

function chatBox(): HTMLElement {
    const deps: IobarDeps = {
        chatInput: {
            onChange: vi.fn(),
            onKeydown: vi.fn(),
            closePanel: vi.fn(),
            commands: new CommandCompletion(new CommandCatalog(async () => [])),
        } as unknown as ChatInput,
        stt: { recording: false } as unknown as SpeechToText,
        target: () => "main",
        setTarget: vi.fn(),
        populateSelect: vi.fn(),
        send: vi.fn(),
        stop: vi.fn(),
    };
    const bar = buildIobar(deps);
    document.body.appendChild(bar);
    return bar;
}

/** A server whose listen route answers with `body`, once `release` is called. */
function aRoom(body: unknown, ok = true): { sent: unknown[]; release: () => void } {
    const sent: unknown[] = [];
    let release = () => {};
    const answered = new Promise<void>(resolve => {
        release = resolve;
    });
    vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init: RequestInit) => {
            sent.push([url, JSON.parse(init.body as string)]);
            await answered;
            return { ok, json: async () => body };
        }),
    );
    return { sent, release: () => release() };
}

describe("the room's microphone in the chat box", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        safeStorage.remove("wactorz-stt-mode");
    });

    it("is offered on host, in place of this browser's", () => {
        safeStorage.set("wactorz-stt-mode", "host");

        const bar = chatBox();

        expect(byRole(bar, "button", "Ask the room to listen")).toBeDefined();
        expect(allByRole(bar, "button", "Voice input")).toEqual([]);
    });

    it("is not offered on the other branches", () => {
        for (const mode of ["off", "browser", "server"]) {
            document.body.innerHTML = "";
            safeStorage.set("wactorz-stt-mode", mode);

            expect(allByRole(chatBox(), "button", "Ask the room to listen")).toEqual([]);
        }
    });

    it("asks the room to listen and act, and waits lit until it has", async () => {
        const room = aRoom({ heard: true, text: "what time is it" });
        const btn = buildRoomMicBtn();

        btn.click();

        // The server decides when the speaker has finished; a second press
        // meanwhile would only queue a second turn behind the first.
        await vi.waitFor(() => expect(room.sent).toEqual([["/api/stt/listen", { act: true }]]));
        expect(btn.disabled).toBe(true);
        expect(btn.getAttribute("aria-label")).toBe("Listening in the room…");

        room.release();

        await vi.waitFor(() => expect(btn.disabled).toBe(false));
        expect(btn.getAttribute("aria-label")).toBe("Ask the room to listen");
        // Heard: the turn reaches the chat the usual way, so nothing more is said here.
        expect(toast.show).not.toHaveBeenCalled();
    });

    it.each([
        [{ heard: false }, true, "Nothing heard"],
        [{ heard: false, speaking: true }, true, "Still speaking"],
        [{ error: "no microphone on this machine" }, false, "Could not listen"],
    ])("says why when there is no turn: %j", async (body, ok, title) => {
        const room = aRoom(body, ok);
        const btn = buildRoomMicBtn();

        btn.click();
        room.release();

        await vi.waitFor(() => expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title })));
    });

    it("passes axe", async () => {
        safeStorage.set("wactorz-stt-mode", "host");

        expect(await blockingFindings(chatBox())).toEqual([]);
    });
});

/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildVoiceSection, listeningSays, speakingSays } from "../ui/dashboard/voiceSettings";
import { safeStorage } from "../safeStorage";
import { toast } from "../ui/ToastManager";
import { tts } from "../ext/tts";

function configured(stt: string, tts: string, extra: Record<string, string> = {}): void {
    safeStorage.set("wactorz-stt-mode", stt);
    safeStorage.set("wactorz-tts-mode", tts);
    for (const [key, value] of Object.entries(extra)) {
        safeStorage.set(key, value);
    }
}

describe("what the settings view says about voice", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    afterEach(() => {
        for (const key of [
            "wactorz-wake-mode",
            "wactorz-wake-ready",
            "wactorz-wake-phrases",
            "wactorz-stt-mode",
            "wactorz-tts-mode",
            "wactorz-stt-available",
            "wactorz-stt-live",
            "wactorz-tts-available",
        ]) {
            safeStorage.remove(key);
        }
    });

    it("says nothing is listening when nothing is", () => {
        configured("off", "off");
        expect(listeningSays()).toContain("Nothing is listening");
    });

    it("distinguishes words as you speak from words when you stop", () => {
        configured("server", "off", { "wactorz-stt-available": "1", "wactorz-stt-live": "1" });
        expect(listeningSays()).toContain("as they are spoken");

        configured("server", "off", { "wactorz-stt-available": "1", "wactorz-stt-live": "0" });
        expect(listeningSays()).toContain("when you stop");
    });

    it("says so when a microphone is configured with nothing to hear it", () => {
        configured("server", "off", { "wactorz-stt-available": "0" });

        // The button is absent in that case, and this is the only place that
        // accounts for why.
        expect(listeningSays()).toContain("no recogniser");
    });

    it("says where the speaking happens", () => {
        configured("off", "browser");
        expect(speakingSays()).toContain("sent nowhere");

        configured("off", "host");
        expect(speakingSays()).toContain("its own speakers");

        configured("off", "server", { "wactorz-tts-available": "1" });
        expect(speakingSays()).toContain("played here");
    });

    it("distinguishes a browser that can recognise from one that cannot", () => {
        configured("browser", "off");
        // The constructor alone is not enough -- Chrome defines it over plain
        // HTTP and then refuses at start() -- so both halves must be reachable.
        Reflect.set(window, "SpeechRecognition", class {});
        Reflect.set(window, "isSecureContext", true);
        expect(listeningSays()).toContain("goes to its vendor");

        Reflect.set(window, "isSecureContext", false);
        expect(listeningSays()).toContain("cannot");

        Reflect.deleteProperty(window, "SpeechRecognition");
        expect(listeningSays()).toContain("cannot");
    });

    it("shows nothing at all when this deployment neither listens nor speaks", () => {
        configured("off", "off");

        // A section saying twice that nothing happens is worse than no section.
        expect(buildVoiceSection("")).toBeNull();
    });

    const buttonLabels = (section: HTMLElement | null): string[] =>
        [...(section?.querySelectorAll("button") ?? [])].map(b => b.textContent ?? "");

    it("offers the listen control only to the branch that has no other", () => {
        configured("host", "off");
        expect(buttonLabels(buildVoiceSection(""))).toContain("Test microphone");

        configured("server", "off", { "wactorz-stt-available": "1" });
        expect(buttonLabels(buildVoiceSection(""))).not.toContain("Test microphone");
    });

    const clickButton = (section: HTMLElement, label: string): void => {
        [...section.querySelectorAll("button")].find(b => b.textContent === label)!.click();
    };

    const answering = (body: unknown, ok = true): void => {
        vi.stubGlobal(
            "fetch",
            vi.fn(() => Promise.resolve({ ok, json: () => Promise.resolve(body) })),
        );
    };

    it("still says something when a refusal comes with no reason", async () => {
        // Every one of these reads a reason off the body, and a server that
        // refuses without giving one must not put "undefined" in a toast.
        configured("host", "off");
        answering({}, false);

        const section = buildVoiceSection("")!;
        clickButton(section, "Test microphone");
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ message: "" }));

        vi.clearAllMocks();
        clickButton(section, "Reset to configured");
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ message: "" }));

        vi.clearAllMocks();
        const select = section.querySelector("select")!;
        select.value = "off";
        select.dispatchEvent(new Event("change"));
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ message: "" }));
    });

    it("reports a turn it heard nothing recognisable in", async () => {
        configured("host", "off");
        answering({ heard: true });

        clickButton(buildVoiceSection("")!, "Test microphone");
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith({ type: "system", title: "Microphone works", message: "" });
    });

    it("asks to hear without acting, so a hardware check costs nothing", () => {
        // Routing what it heard would put the answer through a model and leave a
        // row in the chat log: a check that spends a turn is not a check.
        configured("host", "off");
        const sent: unknown[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn((_url: string, init: RequestInit) => {
                sent.push(JSON.parse(init.body as string));
                return Promise.resolve({ ok: true, json: () => Promise.resolve({ heard: false }) });
            }),
        );

        clickButton(buildVoiceSection("")!, "Test microphone");

        expect(sent[0]).toEqual({ act: false });
    });

    const withVoices = (names: string[]): void => {
        vi.spyOn(tts, "voices", "get").mockReturnValue(names.map(name => ({ name }) as never));
    };

    it("sets the voice the deployment speaks in, not just this browser's", () => {
        // The popover's picker is per-browser local storage, which means nothing
        // on `host`: nobody is at a browser when the machine speaks into a room.
        configured("off", "server", { "wactorz-tts-available": "1" });
        withVoices(["en-GB-Alba", "en-US-Amy"]);
        const sent: unknown[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn((url: string, init: RequestInit) => {
                sent.push([url, JSON.parse(init.body as string)]);
                return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
            }),
        );

        const select = [...buildVoiceSection("")!.querySelectorAll("select")].find(
            s => s.getAttribute("aria-label") === "Voice",
        )!;
        select.value = "en-US-Amy";
        select.dispatchEvent(new Event("change"));

        expect(sent[0]).toEqual(["/api/voice", { voice: "en-US-Amy" }]);
    });

    it("shows the voice that is actually in force", () => {
        // The choice outlives a restart, so a picker stuck on "as configured"
        // tells someone their setting did not take when it did.
        configured("off", "server", { "wactorz-tts-available": "1", "wactorz-tts-voice": "en-US-Amy" });
        withVoices(["en-GB-Alba", "en-US-Amy"]);

        const select = [...buildVoiceSection("")!.querySelectorAll("select")].find(
            s => s.getAttribute("aria-label") === "Voice",
        )!;

        expect(select.value).toBe("en-US-Amy");
    });

    it("goes back to the voice in force when a change is refused", async () => {
        // Blank would claim the deployment fell back to its configured voice,
        // which a refused change has not done.
        configured("off", "server", { "wactorz-tts-available": "1", "wactorz-tts-voice": "en-US-Amy" });
        withVoices(["en-GB-Alba", "en-US-Amy"]);
        vi.stubGlobal(
            "fetch",
            vi.fn(() => Promise.reject(new Error("no answer"))),
        );

        const select = [...buildVoiceSection("")!.querySelectorAll("select")].find(
            s => s.getAttribute("aria-label") === "Voice",
        )!;
        select.value = "en-GB-Alba";
        select.dispatchEvent(new Event("change"));
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(select.value).toBe("en-US-Amy");
    });

    it("says the service chose, when this deployment cannot enumerate voices", () => {
        // A named synthesiser answers with an empty list, which is an answer and
        // not a list still loading.
        configured("off", "server", { "wactorz-tts-available": "1" });
        withVoices([]);

        const select = [...buildVoiceSection("")!.querySelectorAll("select")].find(
            s => s.getAttribute("aria-label") === "Voice",
        )!;

        expect(select.disabled).toBe(true);
        expect(select.textContent).toContain("chosen by the service");
    });

    const wakeSelect = (section: HTMLElement | null): HTMLSelectElement | undefined =>
        [...(section?.querySelectorAll("select") ?? [])].find(
            s => s.getAttribute("aria-label") === "Wake word",
        );

    it("offers the wake word only where there is a microphone to own", () => {
        // Every other branch records in a browser or not at all, so a phrase has
        // no device to interrupt and the switch would turn on nothing.
        configured("host", "off", { "wactorz-wake-ready": "1" });
        expect(wakeSelect(buildVoiceSection(""))).toBeDefined();

        configured("server", "off", { "wactorz-stt-available": "1", "wactorz-wake-ready": "1" });
        expect(wakeSelect(buildVoiceSection(""))).toBeUndefined();
    });

    it("sets the wake word deployment-wide", () => {
        configured("host", "off", { "wactorz-wake-ready": "1" });
        const sent: unknown[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn((url: string, init: RequestInit) => {
                sent.push([url, JSON.parse(init.body as string)]);
                return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
            }),
        );

        const select = wakeSelect(buildVoiceSection(""))!;
        select.value = "on";
        select.dispatchEvent(new Event("change"));

        expect(sent[0]).toEqual(["/api/voice", { waking: "on" }]);
    });

    it("shows whether it is already on", () => {
        configured("host", "off", { "wactorz-wake-ready": "1", "wactorz-wake-mode": "on" });

        expect(wakeSelect(buildVoiceSection(""))!.value).toBe("on");
    });

    it("says what to say to wake it", () => {
        // A switch that turns on listening for a phrase, without saying which
        // phrase, leaves someone guessing at their own configuration.
        configured("host", "off", {
            "wactorz-wake-ready": "1",
            "wactorz-wake-phrases": "hey waldiez",
        });

        expect(buildVoiceSection("")!.textContent).toContain("hey waldiez");
    });

    it("says so when there is nothing to wake with", () => {
        // The model is weights fetched at deploy time, not shipped with the code,
        // so a deployment can be set to wake and have nothing to wake with.
        configured("host", "off", { "wactorz-wake-ready": "0" });

        const select = wakeSelect(buildVoiceSection(""))!;

        expect(select.disabled).toBe(true);
        expect(buildVoiceSection("")!.textContent).toContain("no wake-word model");
    });

    it("always offers the way back, because the choices are kept", () => {
        // The note says the choices survive a restart, so there has to be a
        // control that undoes them -- on every branch, not just the one that
        // happens to have another button.
        configured("server", "off", { "wactorz-stt-available": "1" });
        expect(buttonLabels(buildVoiceSection(""))).toContain("Reset to configured");
    });

    it("says the choices are kept rather than good for one run", () => {
        configured("server", "server", { "wactorz-stt-available": "1" });

        expect(buildVoiceSection("")!.textContent).toContain("kept until reset");
    });

    it("hands every branch back when reset is asked for", async () => {
        configured("host", "server", { "wactorz-stt-available": "1" });
        const sent: unknown[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn((_url: string, init: RequestInit) => {
                sent.push(JSON.parse(init.body as string));
                return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
            }),
        );

        const section = buildVoiceSection("")!;
        const reset = [...section.querySelectorAll("button")].find(
            b => b.textContent === "Reset to configured",
        )!;
        reset.click();
        await vi.waitFor(() => expect(sent).toHaveLength(1));

        expect(sent[0]).toEqual({ reset: true });
    });

    it("passes on the server's reason for refusing a reset", async () => {
        configured("server", "off", { "wactorz-stt-available": "1" });
        vi.stubGlobal(
            "fetch",
            vi.fn(() =>
                Promise.resolve({
                    ok: false,
                    json: () => Promise.resolve({ error: "nowhere to remember it" }),
                }),
            ),
        );

        const section = buildVoiceSection("")!;
        [...section.querySelectorAll("button")].find(b => b.textContent === "Reset to configured")!.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ title: "Could not reset", message: "nowhere to remember it" }),
        );
    });

    it("says so when the reset does not take", async () => {
        configured("server", "off", { "wactorz-stt-available": "1" });
        vi.stubGlobal(
            "fetch",
            vi.fn(() => Promise.reject(new Error("no answer"))),
        );

        const section = buildVoiceSection("")!;
        const reset = [...section.querySelectorAll("button")].find(
            b => b.textContent === "Reset to configured",
        )!;
        reset.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Could not reset" }));
    });

    it("says what can be changed here and what cannot", () => {
        configured("server", "server", { "wactorz-stt-available": "1" });

        const section = buildVoiceSection("")!;
        // Two things it must be clear about: the branches are changeable here,
        // and where the services live is not.
        // Listening, speaking, and the voice the deployment speaks in.
        expect(section.querySelectorAll("select")).toHaveLength(3);
        expect(section.textContent).toContain("Where the services live is set on the machine");
    });

    it("changes a branch on the server when one is picked", async () => {
        configured("server", "server", { "wactorz-stt-available": "1" });
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ listening: "off", speaking: "server", voice: "" }),
        }) as unknown as typeof fetch;
        const redrawn = vi.fn();
        const select = buildVoiceSection("/ha", redrawn)!.querySelector("select")!;

        select.value = "off";
        select.dispatchEvent(new Event("change"));
        await vi.waitFor(() => expect(redrawn).toHaveBeenCalled());

        const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]!;
        // Under Home Assistant the dashboard is served beneath a prefix, and a
        // bare path reaches the top of that host instead.
        expect(url).toBe("/ha/api/voice");
        expect(JSON.parse(init.body)).toEqual({ listening: "off" });
    });

    it("says so when the server does not answer a change at all", async () => {
        configured("server", "server", { "wactorz-stt-available": "1" });
        globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch;
        const select = buildVoiceSection("")!.querySelector("select")!;

        select.value = "off";
        select.dispatchEvent(new Event("change"));
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(select.value).toBe("server");
        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ message: "The server did not answer." }),
        );
    });

    it("puts the select back when the server will not have it", async () => {
        configured("server", "server", { "wactorz-stt-available": "1" });
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: false,
            json: async () => ({ error: "speaking cannot be 'telepathy'" }),
        }) as unknown as typeof fetch;
        const select = buildVoiceSection("")!.querySelector("select")!;

        select.value = "off";
        select.dispatchEvent(new Event("change"));
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        // Otherwise the page shows a branch the deployment is not on.
        expect(select.value).toBe("server");
    });

    it("reports what the room said", async () => {
        configured("host", "off");
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ text: "turn on the lights", heard: true }),
        }) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ message: "turn on the lights" }));
    });

    it("says to wait rather than reporting silence while the machine is talking", async () => {
        configured("host", "host");
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ text: "", heard: false, speaking: true }),
        }) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        // It declines while the room is being spoken to, so that it does not
        // hear the reply and answer it. That is not "nobody spoke".
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Still speaking" }));
    });

    it("says when nobody spoke", async () => {
        configured("host", "off");
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ text: "", heard: false }),
        }) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Nothing heard" }));
    });

    it("passes on why the server refused", async () => {
        configured("host", "off");
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: false,
            json: async () => ({ error: "no microphone on this machine" }),
        }) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(
            expect.objectContaining({ message: "no microphone on this machine" }),
        );
    });

    it("says so when the server does not answer at all", async () => {
        configured("host", "off");
        globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(toast.show).toHaveBeenCalled());

        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: "alert-error" }));
    });

    it("puts the button back after a turn", async () => {
        configured("host", "off");
        globalThis.fetch = vi.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ text: "", heard: false }),
        }) as unknown as typeof fetch;
        const btn = buildVoiceSection("")!.querySelector("button")!;

        btn.click();
        await vi.waitFor(() => expect(btn.disabled).toBe(false));

        expect(btn.textContent).toBe("Test microphone");
    });
});

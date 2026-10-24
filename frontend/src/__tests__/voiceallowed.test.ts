/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";

vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildVoiceSection } from "../ui/dashboard/voiceSettings";
import { ALLOWED_KEYS } from "../ui/dashboard/voiceAllowed";
import { seedServerConfig } from "../config/serverConfig";
import { safeStorage } from "../safeStorage";
import { blockingFindings } from "./axe";
import { byRole } from "./queries";

const UP_TO_SERVER = ["off", "browser", "server"];
const EVERY_BRANCH = ["off", "browser", "server", "host"];

/** Seed the page from an /api/config that says what is in force and what may be chosen. */
async function served(
    listening: string,
    speaking: string,
    allowed?: Record<"listening" | "speaking" | "waking", string[]>,
): Promise<void> {
    const cfg: Record<string, unknown> = {
        stt: { mode: listening },
        tts: { mode: speaking, available: true },
        wake: { mode: "off", ready: true, phrases: [] },
    };
    if (allowed) {
        cfg["voice"] = { allowed };
    }
    vi.stubGlobal(
        "fetch",
        vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve(cfg) })),
    );
    await seedServerConfig();
}

function options(select: HTMLElement): Record<string, boolean> {
    return Object.fromEntries([...(select as HTMLSelectElement).options].map(o => [o.value, !o.disabled]));
}

describe("the voice branches this deployment lets Settings choose", () => {
    afterEach(() => {
        vi.unstubAllGlobals();
        document.body.innerHTML = "";
        for (const key of [
            ...Object.values(ALLOWED_KEYS),
            "wactorz-stt-mode",
            "wactorz-tts-mode",
            "wactorz-wake-mode",
            "wactorz-wake-ready",
        ]) {
            safeStorage.remove(key);
            safeStorage.remove(`${key}__server`);
        }
    });

    it("disables the branches past the ceiling, and says which variable sets it", async () => {
        await served("server", "server", {
            listening: UP_TO_SERVER,
            speaking: EVERY_BRANCH,
            waking: ["off"],
        });

        const section = buildVoiceSection("")!;
        document.body.appendChild(section);

        // Shown, not hidden: the page says what exists and what is kept out of reach.
        expect(options(byRole(section, "combobox", "Listening"))).toEqual({
            off: true,
            browser: true,
            server: true,
            host: false,
        });
        expect(section.textContent).toContain('allows up to "server"; WACTORZ_STT');
        // Speaking is not limited here, so nothing is said about it.
        expect(Object.values(options(byRole(section, "combobox", "Speaking")))).not.toContain(false);
        expect(section.textContent).not.toContain("WACTORZ_TTS");
    });

    it("cannot turn the wake word on where the deployment leaves it off", async () => {
        await served("host", "off", { listening: EVERY_BRANCH, speaking: ["off"], waking: ["off"] });

        const section = buildVoiceSection("")!;

        expect(options(byRole(section, "combobox", "Wake word"))).toEqual({ off: true, on: false });
        expect(section.textContent).toContain("WACTORZ_WAKE");
    });

    it("keeps the section where both are off but may be turned back on", async () => {
        // Turned down to nothing from this page, the way back must still be here.
        await served("off", "off", { listening: UP_TO_SERVER, speaking: EVERY_BRANCH, waking: ["off"] });

        expect(buildVoiceSection("")).not.toBeNull();
    });

    it("shows no section where the deployment allows nothing", async () => {
        await served("off", "off", { listening: ["off"], speaking: ["off"], waking: ["off"] });

        expect(buildVoiceSection("")).toBeNull();
    });

    it("offers every branch when the server does not say, without keeping an old limit", async () => {
        await served("server", "server", { listening: ["off"], speaking: ["off"], waking: ["off"] });
        await served("server", "server");

        const section = buildVoiceSection("")!;

        expect(Object.values(options(byRole(section, "combobox", "Listening")))).not.toContain(false);
        expect(section.textContent).not.toContain("allows up to");
    });

    it("passes axe with branches out of reach", async () => {
        await served("host", "server", { listening: UP_TO_SERVER, speaking: UP_TO_SERVER, waking: ["off"] });
        const section = buildVoiceSection("")!;
        document.body.appendChild(section);

        expect(await blockingFindings(section)).toEqual([]);
    });
});
